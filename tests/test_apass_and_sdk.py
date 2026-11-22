"""APASS 四层可信授权存证结构、A2M 分层 402 头、支付宝生产/沙箱 SDK 适配与高速重试 LLM 测试套件。"""

from __future__ import annotations

import copy
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpay import identity
from agentpay.agent_client import BuyerAgent
from agentpay.app import create_app
from agentpay.llm_agent import OpenAICompatLLM, default_llm
from aipay_kit import apass, proof as px, signing
from aipay_kit.gateway.base import PagePayRequest, WapPayRequest
from aipay_kit.gateway.factory import load_gateway
from aipay_kit.gateway.mock import MockGateway
from aipay_kit.gateway.mock_server import build_app, build_router
from aipay_kit.gateway.openapi import AlipaySdkGateway, OpenApiGateway, PROD_URL
from demo.agentpay_demo import create_demo_app


def _make_fulfilled_receipt():
    gw = MockGateway()
    app = create_app(gw, mock_router=build_router(gw, {}))
    http = TestClient(app)
    user_k = identity.new_key()
    agent_k = identity.new_key()
    http.post("/agents", json={"keyid": "agent-kya-1", "public_key": identity.pub_b64(agent_k), "owner": "u_kya"})
    mdoc = identity.sign_obj(
        user_k,
        {
            "type": "admit.mandate.v1",
            "mandate_id": "m-kya-1",
            "user_ref": "u_kya",
            "merchant_ids": ["facetry-demo"],
            "currency": "CNY",
            "max_amount_per_tx": "5.00",
            "max_amount_total": "10.00",
            "expires_at": 2_000_000_000,
            "categories": ["report", "data"],
            "max_uses": 5,
            "agent_keyid": "agent-kya-1",
            "intent_keywords": ["可见度"],
            "user_public_key": identity.pub_b64(user_k),
        },
    )
    assert http.post("/mandates", json=mdoc).status_code == 201
    buyer = BuyerAgent(http, "agent-kya-1", agent_k, "m-kya-1", lambda pid: http.post(f"/mock/alipay/a402/{pid}/pay").json())
    res = buyer.buy("geo-report")
    assert res.ok
    rc = http.get(f"/receipts/{res.trade_no}").json()
    pub_pem = http.get("/merchant/public_key").text
    return http, buyer, rc, pub_pem


def test_apass_four_layer_chain_verifies_and_detects_any_tampering() -> None:
    _, _, rc, pub_pem = _make_fulfilled_receipt()
    att = rc["apass_attestation"]
    assert att["schema"] == "apass.kya.v1" and len(att["layers"]) == 4

    ok_res = apass.verify_apass_attestation(att, pub_pem)
    assert ok_res.ok is True and ok_res.verified_layers == 4 and ok_res.root_hash == att["layers"][3]["hash"]

    # 篡改 Layer 1 人类预算上限
    t1 = copy.deepcopy(att)
    t1["layers"][0]["payload"]["budget_cap"]["max_amount_total"] = "99999.00"
    r1 = apass.verify_apass_attestation(t1, pub_pem)
    assert not r1.ok and r1.failed_layer == 1 and r1.reason == "LAYER_HASH_TAMPERED"

    # 篡改 Layer 1 用户 Ed25519 签名
    t1_sig = copy.deepcopy(att)
    t1_sig["mandate_doc"]["max_amount_per_tx"] = "99.00"
    r1_sig = apass.verify_apass_attestation(t1_sig, pub_pem)
    assert not r1_sig.ok and r1_sig.failed_layer == 1

    # 篡改 Layer 2 商户商品锁定金额
    t2 = copy.deepcopy(att)
    t2["layers"][1]["payload"]["locked_amount"] = "0.01"
    r2 = apass.verify_apass_attestation(t2, pub_pem)
    assert not r2.ok and r2.failed_layer == 2

    # 篡改 Layer 3 支付凭据 active 标志
    t3 = copy.deepcopy(att)
    t3["layers"][2]["payload"]["active"] = False
    r3 = apass.verify_apass_attestation(t3, pub_pem)
    assert not r3.ok and r3.failed_layer == 3

    # 篡改 Layer 4 履约交付摘要或根签名
    t4 = copy.deepcopy(att)
    t4["merchant_root_signature"] = "AAAA"
    r4 = apass.verify_apass_attestation(t4, pub_pem)
    assert not r4.ok and r4.failed_layer == 4 and r4.reason == "ROOT_SIGNATURE_INVALID"


def test_a2m_layered_payment_needed_and_alipay_bot_endpoint() -> None:
    http, buyer, _, pub_pem = _make_fulfilled_receipt()
    r402 = buyer._get("/a2m/resource/geo-report")
    assert r402.status_code == 402
    raw_hdr = r402.headers["Payment-Needed"]
    decoded_raw = px.b64url_decode_json(raw_hdr)
    # 确认是官方 A2MPaymentDemoController / alipay-bot 分层结构
    assert set(decoded_raw.keys()) == {"protocol", "method"}
    assert "seller_signature" in decoded_raw["protocol"]
    assert "goods_name" in decoded_raw["method"]

    parsed = px.parse_a2m_payment_needed(raw_hdr)
    assert px.parse_any_payment_needed(raw_hdr) == parsed
    pub = signing.load_public(pub_pem)
    assert signing.rsa2_verify(
        signing.sign_content({k: v for k, v in parsed.items() if k != "seller_signature"}),
        parsed["seller_signature"],
        pub,
    )

    with pytest.raises(px.ProofError):
        px.parse_a2m_payment_needed(px.b64url_encode_json({"protocol": {"evil": "1"}, "method": {}}))


def test_openapi_sdk_form_wap_refund_query_and_prod_gate(tmp_path, monkeypatch) -> None:
    keys = signing.generate_keypair()
    gw = MockGateway("2026000199998888", cashier_base="http://mock/mock/alipay")
    tc = TestClient(build_app(gw, {"2026000199998888": signing.public_pem(keys.public_key()).decode()}), base_url="http://mock")
    sdk = AlipaySdkGateway(
        "2026000199998888",
        signing.private_pem(keys).decode(),
        signing.public_pem(gw.alipay_public_key).decode(),
        "http://mock/mock/alipay/gateway.do",
        seller_id="2088000000000001",
        client=tc,
    )
    form_html = sdk.page_pay_form(PagePayRequest("ord-form-1", "0.99", "勾稽5次极速体验包", "http://m/notify", "http://m/return"))
    assert '<form id="alipay_submit"' in form_html and "alipay.trade.page.pay" in form_html

    wap_url = sdk.wap_pay(WapPayRequest("ord-wap-1", "0.99", "勾稽H5收款", "http://m/notify", quit_url="http://m/"))
    assert "method=alipay.trade.wap.pay" in wap_url and "QUICK_WAP_WAY" in wap_url

    # 下单付款并核验 refund_query 与 seller_id 通知校验
    gw.page_pay(PagePayRequest("ord-rq-1", "25.00", "勾稽100次", "http://m/notify"))
    notify_params = gw.buyer_pay("ord-rq-1")
    assert sdk.verify_notify(notify_params) is True
    assert sdk.verify_notify({**notify_params, "seller_id": "2088999999999999"}) is False

    sdk.refund("ord-rq-1", "10.00", "rf-rq-1")
    rq = sdk.refund_query("ord-rq-1", "rf-rq-1")
    assert rq.refund_status == "REFUND_SUCCESS" and rq.refund_amount == "10.00"

    a2m_sig = sdk.sign_a2m_bill({
        "amount": "0.50", "currency": "CNY", "goods_name": "体检报告", "out_trade_no": "AP1",
        "pay_before": "2026-12-01T12:00:00+08:00", "resource_id": "/paid/resource/geo-report",
        "seller_id": "2088000000000001", "service_id": "svc_01",
    })
    assert len(a2m_sig) > 64

    # 生产网关注入位：显式开启 AIPAY_ALLOW_PROD=true 时允许切换至 prod 模式
    monkeypatch.delenv("AIPAY_GATEWAY", raising=False)
    prod_env = tmp_path / "aipay_prod.env"
    prod_env.write_text(
        "\n".join([
            f"AIPAY_GATEWAY_URL={PROD_URL}",
            "AIPAY_ALLOW_PROD=true",
            "AIPAY_APP_ID=2026000199998888",
            "AIPAY_SELLER_ID=2088000000000001",
            "AIPAY_SERVICE_ID=svc_prod_01",
            "AIPAY_PRIVATE_PKCS_KEY=" + "".join(signing.private_pem(keys).decode().splitlines()[1:-1]),
            "AIPAY_ALIPAY_PUBLIC_KEY=" + "".join(signing.public_pem(keys.public_key()).decode().splitlines()[1:-1]),
        ])
    )
    prod_env.chmod(0o600)
    choice = load_gateway(prod_env)
    assert choice.mode == "prod" and choice.service_id == "svc_prod_01"
    assert choice.gateway.gateway_url == PROD_URL


def test_fast_llm_client_tailscale_and_timeout_retry(tmp_path) -> None:
    state = {"calls": 0}

    def flaky_handler(req: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            raise httpx.ReadTimeout("slow_upstream", request=req)
        if state["calls"] == 2:
            return httpx.Response(502, json={"error": "bad_gateway"})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"sku":"geo-report","reason":"最佳匹配"}'}}]})

    env_file = tmp_path / "cliproxy.env"
    env_file.write_text(
        "CLIPROXY_BASE_URL=http://100.105.95.126:8317/v1\n"
        "CLIPROXY_API_KEY=sk-top-secret\n"
        "CLIPROXY_BUYER_MODEL=gemini-3-flash\n"
    )
    llm = OpenAICompatLLM.from_env(env_file, backoff_base=0.0, max_retries=2, transport=httpx.MockTransport(flaky_handler))
    assert llm.name == "gemini-3-flash" and "sk-top-secret" not in repr(llm)
    out = llm.chat([{"role": "user", "content": "查品牌可见度"}])
    assert state["calls"] == 3 and "geo-report" in out

    # 验证 demo_app /api/receipts 透出 APASS 四层存证校验结果
    ap_client = TestClient(create_demo_app(llm_factory=lambda: llm))
    run_res = ap_client.post("/api/run", json={"task": "品牌可见度体检", "use_llm": True}).json()
    assert run_res["state"] == "delivered" and run_res["plan"]["planner"] == "llm:gemini-3-flash"
    rc_res = ap_client.get(f"/api/receipts/{run_res['receipt']['trade_no']}").json()
    assert rc_res["signature_valid"] is True and rc_res["apass_valid"] is True and rc_res["apass_verified_layers"] == 4
