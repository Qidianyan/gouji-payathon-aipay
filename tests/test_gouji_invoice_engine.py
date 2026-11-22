"""勾稽（Gouji）真实中国电子发票解析、多模态视觉识别、三单勾稽与红冲差额引擎完整测试套件。"""

from __future__ import annotations

import base64
import csv
import io
import json
from decimal import Decimal
from urllib.parse import urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from aipay_kit.gateway.mock import MockGateway
from aipay_kit.gateway.mock_server import build_router
from vibepay.app import create_app
from vibepay.invoice_engine import (
    InvoiceParseError,
    REAL_INVOICE_SAMPLES,
    VisionInvoiceClient,
    build_invoice_pdf_bytes,
    build_invoice_png_bytes,
    compute_red_ink_adjustment,
    extract_pdf_text,
    parse_any_invoice,
    parse_fapiao_qr,
    parse_invoice_text,
    reconcile_and_bookkeep,
    validate_tax_id,
)


@pytest.mark.parametrize("sample_id", list(REAL_INVOICE_SAMPLES.keys()))
def test_real_pdf_roundtrip_all_samples(sample_id: str) -> None:
    spec = REAL_INVOICE_SAMPLES[sample_id]
    pdf_bytes = build_invoice_pdf_bytes(spec)
    assert pdf_bytes.startswith(b"%PDF-1.4") and len(pdf_bytes) > 800
    extracted = extract_pdf_text(pdf_bytes)
    assert spec["invoice_number"] in extracted
    assert spec["seller_name"] in extracted

    inv, _ = parse_any_invoice(file_bytes=pdf_bytes, filename=f"{sample_id}.pdf", use_vision=False)
    assert inv.extraction_source == "pdf_text"
    assert inv.invoice_number == spec["invoice_number"]
    assert inv.invoice_code == spec["invoice_code"]
    assert inv.issue_date == spec["issue_date"]
    assert inv.buyer_tax_id == spec["buyer_tax_id"]
    assert f"{inv.net_amount:.2f}" == spec["net_amount"]
    assert f"{inv.tax_amount:.2f}" == spec["tax_amount"]
    assert f"{inv.total_amount:.2f}" == spec["total_amount"]


def test_fapiao_qr_parsing_and_tax_id_validation() -> None:
    qr = parse_fapiao_qr("01,10,031002300111,83920174,1886.79,20261112,83920174650192837465,7F2A")
    assert qr["invoice_code"] == "031002300111"
    assert qr["invoice_number"] == "83920174"
    assert qr["net_amount"] == "1886.79"
    assert qr["issue_date"] == "2026-11-12"

    qr_digital = parse_fapiao_qr("01,32,,26312000000198273645,1886.79,20261112,83920174650192837465,7F2A")
    assert qr_digital["invoice_code"] == ""
    assert qr_digital["invoice_number"] == "26312000000198273645"
    assert "电子发票" in qr_digital["invoice_type"]

    with pytest.raises(InvoiceParseError):
        parse_fapiao_qr("99,bad,qr")

    assert validate_tax_id("91310115MA1K49283X") is True
    assert validate_tax_id("91330106673959654P") is True
    assert validate_tax_id("INVALID") is False


def test_three_way_reconciliation_matched_and_overbilled_red_ink() -> None:
    # 1) 正常三单完全相符
    inv1, s1 = parse_any_invoice(sample_id="cloud-vat-normal", use_vision=False)
    r1 = reconcile_and_bookkeep(inv1, taxpayer_type="general", contract=s1["contract"], bank_flow=s1["bank_flow"])
    assert r1["reconciliation"]["status"] == "MATCHED"
    assert r1["reconciliation"]["contract_diff"] == "0.00"
    assert r1["reconciliation"]["payment_diff"] == "0.00"
    assert r1["red_ink_adjustment"]["required"] is False
    assert r1["voucher"]["balanced"] is True
    assert r1["voucher"]["debit_total"] == "2000.00" == r1["voucher"]["credit_total"]
    assert "_demo" not in json.dumps(r1)

    # 2) 专票 ¥1,130.00 超出合同 ¥1,000.00 → 自动计算 ¥130.00 红冲（不含税 -115.04，税额 -14.96）
    inv2, s2 = parse_any_invoice(sample_id="office-spec-overbill", use_vision=False)
    r2 = reconcile_and_bookkeep(inv2, taxpayer_type="general", contract=s2["contract"], bank_flow=s2["bank_flow"])
    assert r2["reconciliation"]["status"] == "NEEDS_RED_INK"
    red = r2["red_ink_adjustment"]
    assert red["required"] is True and red["trigger"] == "OVERBILLED_VS_CONTRACT"
    assert red["red_ink_total"] == "-130.00"
    assert red["red_ink_net"] == "-115.04"
    assert red["red_ink_tax"] == "-14.96"
    assert Decimal(red["red_ink_net"]) + Decimal(red["red_ink_tax"]) == Decimal(red["red_ink_total"])
    # 流水只付了 1000.00，未付差额 130.00 挂应付账款，借贷平衡 1130.00
    assert r2["voucher"]["balanced"] is True
    assert r2["voucher"]["debit_total"] == "1130.00" == r2["voucher"]["credit_total"]
    rows = list(csv.reader(io.StringIO(r2["kingdee_csv"])))
    assert rows[0][0] == "凭证日期" and len(rows) >= 6


def test_general_vs_small_taxpayer_and_prepaid_variance() -> None:
    inv, s = parse_any_invoice(sample_id="rail-ticket-deduct", use_vision=False)
    gen = reconcile_and_bookkeep(inv, taxpayer_type="general", contract=s["contract"], bank_flow=s["bank_flow"])
    sml = reconcile_and_bookkeep(inv, taxpayer_type="small", contract=s["contract"], bank_flow=s["bank_flow"])
    assert len(gen["voucher"]["entries"]) == 3  # 差旅费 66.97 + 进项税 6.03 / 银行存款 73.00
    assert len(sml["voucher"]["entries"]) == 2  # 小规模全额 73.00 入差旅费

    # 流水超付（预付账款挂账）仍借贷平衡
    over = reconcile_and_bookkeep(
        inv,
        taxpayer_type="general",
        contract=s["contract"],
        bank_flow={"flow_id": "F1", "payee_name": inv.seller_name, "paid_amount": "100.00"},
    )
    assert over["voucher"]["balanced"] is True
    assert over["voucher"]["debit_total"] == "100.00" == over["voucher"]["credit_total"]


def test_explicit_red_ink_and_negative_invoice() -> None:
    inv_neg, s_neg = parse_any_invoice(sample_id="red-ink-credit-note", use_vision=False)
    r_neg = reconcile_and_bookkeep(inv_neg, contract=s_neg["contract"], bank_flow=s_neg["bank_flow"])
    assert inv_neg.is_red_ink is True
    assert r_neg["red_ink_adjustment"]["required"] is True
    assert r_neg["red_ink_adjustment"]["red_ink_total"] == "-636.00"
    assert r_neg["voucher"]["balanced"] is True

    inv_pos = parse_invoice_text(
        "票据类型：增值税电子普通发票\n发票号码：26312000000999888777\n开票日期：2026-11-18\n"
        "购买方名称：上海奇点砚科技有限公司\n购买方纳税人识别号：91310115MA1K49283X\n"
        "销售方名称：阿里云计算有限公司\n货物或应税劳务、服务名称：*信息技术服务*云存储服务\n"
        "税率：6%\n价税合计（小写）：¥1060.00"
    )
    red = compute_red_ink_adjustment(inv_pos, explicit_red_ink_amount=Decimal("212.00"))
    assert red["required"] is True
    assert red["red_ink_total"] == "-212.00" and red["red_ink_net"] == "-200.00" and red["red_ink_tax"] == "-12.00"


def test_vision_invoice_client_resizes_and_retries_without_leaking_key(tmp_path, monkeypatch) -> None:
    calls: list[httpx.Request] = []
    attempts = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"error": "upstream_busy"})
        reply = {
            "invoice_type": "增值税电子普通发票",
            "invoice_code": "031002300111",
            "invoice_number": "88990011",
            "issue_date": "2026-11-17",
            "buyer_name": "上海奇点砚科技有限公司",
            "buyer_tax_id": "91310115MA1K49283X",
            "seller_name": "上海数云网络科技有限公司",
            "seller_tax_id": "91310115MA1K123456",
            "item_name": "*软件服务*AI算力服务费",
            "net_amount": "500.00",
            "tax_rate": "6%",
            "tax_amount": "30.00",
            "total_amount": "530.00",
            "check_code": "123456",
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"}}]})

    # 构造超大图（2200x1400）验证自动压缩至长边 ≤1024px
    big = Image.new("RGB", (2200, 1400), (255, 255, 255))
    buf = io.BytesIO()
    big.save(buf, format="JPEG")

    env_file = tmp_path / "cliproxy.env"
    env_file.write_text("CLIPROXY_BASE_URL=http://100.105.95.126:8317/v1\nCLIPROXY_API_KEY=sk-never-print\nAI_MODEL_DEFAULT=gemini-4-carbon-high\n")
    monkeypatch.delenv("CLIPROXY_BASE_URL", raising=False)
    monkeypatch.delenv("CLIPROXY_API_KEY", raising=False)

    vc = VisionInvoiceClient.from_default_env(env_file, transport=httpx.MockTransport(handler))
    assert vc is not None and vc.model == "gemini-4-carbon-high"
    assert "sk-never-print" not in repr(vc)

    doc = vc.extract_image(buf.getvalue())
    assert attempts["n"] == 2  # 首次 503 自动重试成功
    assert doc.extraction_source == "vision:gemini-4-carbon-high"
    assert doc.invoice_number == "88990011" and f"{doc.total_amount:.2f}" == "530.00"

    sent_payload = json.loads(calls[-1].content)
    data_url = sent_payload["messages"][1]["content"][1]["image_url"]["url"]
    raw_png = base64.b64decode(data_url.split(",", 1)[1])
    with Image.open(io.BytesIO(raw_png)) as resized:
        assert max(resized.size) == 1024


def test_vibepay_starter_pack_upload_and_parse_failure_no_deduct() -> None:
    holder: dict = {}
    gw = MockGateway(notifier=lambda url, p: holder["h"].post(urlparse(url).path, data=p).text)
    vc_calls = {"n": 0}

    def vision_handler(req: httpx.Request) -> httpx.Response:
        vc_calls["n"] += 1
        spec = REAL_INVOICE_SAMPLES["cloud-vat-normal"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(spec, ensure_ascii=False)}}]})

    vc = VisionInvoiceClient("http://127.0.0.1:8317/v1", "k", "gemini-4-carbon-high", transport=httpx.MockTransport(vision_handler))
    app = create_app(gw, mock_router=build_router(gw, {}), vision_client=vc)
    http = TestClient(app)
    holder["h"] = http

    # 1) 购买 ¥0.99 破冰体验包（5 次）
    packs = http.get("/billing/packs").json()
    assert packs["starter_pack"]["id"] == "p5" and packs["starter_pack"]["price"] == "0.99"
    co = http.post("/billing/checkout", json={"account": "firm-1", "pack_id": "p5"}).json()
    assert co["amount"] == "0.99"
    http.post(f"/mock/alipay/cashier/{co['out_trade_no']}/pay")
    assert http.get("/billing/balance", params={"account": "firm-1"}).json()["balance"] == 5

    # 2) 下载真实样张 PDF 并通过 multipart 上传识别（扣 1 次 → 剩 4 次）
    pdf_resp = http.get("/gouji/samples/office-spec-overbill/pdf")
    assert pdf_resp.status_code == 200 and pdf_resp.content.startswith(b"%PDF")
    up = http.post(
        "/gouji/recognize/upload",
        data={"account": "firm-1", "request_id": "up-1", "taxpayer_type": "general"},
        files={"file": ("invoice.pdf", pdf_resp.content, "application/pdf")},
    )
    assert up.status_code == 200
    body = up.json()
    assert body["balance"] == 4
    assert body["invoice"]["invoice_number"] == "48291037"
    assert body["red_ink_adjustment"]["red_ink_total"] == "-130.00"
    assert "_demo" not in up.text

    # 3) 上传真 PNG 图像触发多模态视觉识别（扣 1 次 → 剩 3 次）
    png_bytes = build_invoice_png_bytes(REAL_INVOICE_SAMPLES["cloud-vat-normal"], embed_qr_metadata=False)
    up_img = http.post(
        "/gouji/recognize",
        json={"account": "firm-1", "request_id": "img-1", "file_b64": base64.b64encode(png_bytes).decode(), "filename": "fapiao.png"},
    )
    assert up_img.status_code == 200 and up_img.json()["balance"] == 3 and vc_calls["n"] == 1

    # 4) 损坏文件解析失败严格不扣次数（仍剩 3 次！）
    bad = http.post(
        "/gouji/recognize",
        json={"account": "firm-1", "request_id": "bad-1", "file_b64": base64.b64encode(b"%PDF-broken").decode(), "filename": "bad.pdf"},
    )
    assert bad.status_code == 422
    assert http.get("/billing/balance", params={"account": "firm-1"}).json()["balance"] == 3

    # 5) 剩余 3 次按比例原路退款：99 * 3 // 5 = 59 分（¥0.59）
    rf = http.post(f"/billing/orders/{co['out_trade_no']}/refund", json={"account": "firm-1"}).json()
    assert rf["refunded_credits"] == 3 and rf["refund_amount"] == "0.59" and rf["balance"] == 0
