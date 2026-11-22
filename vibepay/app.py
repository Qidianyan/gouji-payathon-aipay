"""作品 B：Vibe Pay —— 勾稽按次 / 次数包收费（支付宝网页支付）。

链路：额度不足 402 → 选包下单（alipay.trade.page.pay）→ 收银台付款 → 异步通知验签 + 订单/金额/状态核对
→ 幂等入账次数 → 解锁 AI 记账（每次扣 1）→ 未用次数退款（alipay.trade.refund）冲正。
只认异步通知或主动查询（GET /billing/orders/{id} 会对 PENDING 单主动 query 补单），return_url 只展示「确认中」。
"""

from __future__ import annotations

import base64
import html
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from aipay_kit.audit import AuditLog
from aipay_kit.credits import CreditsError, CreditsLedger
from aipay_kit.gateway.base import GatewayError, PagePayRequest
from aipay_kit.idempotency import IdempotencyStore
from vibepay.invoice_engine import (
    InvoiceParseError,
    REAL_INVOICE_SAMPLES,
    VisionInvoiceClient,
    build_invoice_pdf_bytes,
    build_invoice_png_bytes,
    lookup_ledger_counterparts,
    parse_any_invoice,
    reconcile_and_bookkeep,
)

CN = timezone(timedelta(hours=8))
PRICING_PATH = Path(__file__).with_name("pricing.json")
SUCCESS = ("TRADE_SUCCESS", "TRADE_FINISHED")


def _check_pack(pk: dict) -> None:
    d = Decimal(pk["price"])
    if not (d > 0 and d == d.quantize(Decimal("0.01")) and int(pk["credits"]) > 0):
        raise ValueError(f"bad pack {pk}")


def load_pricing(path: Path = PRICING_PATH) -> dict:
    p = json.loads(path.read_text(encoding="utf-8"))
    for pk in p["packs"]:
        _check_pack(pk)
    if p.get("starter_pack"):
        _check_pack(p["starter_pack"])
    per_call = Decimal(p["per_call_price"])
    cap = Decimal(p.get("max_402_single_payment", "50.00"))
    if not (0 < per_call <= cap <= Decimal("50.00")):
        raise ValueError("402 单笔须 ≤50 元")
    return p


def cents(yuan: str) -> int:
    return int((Decimal(yuan) * 100).to_integral_value())


def yuan(c: int) -> str:
    return f"{Decimal(c) / 100:.2f}"


@dataclass
class Order:
    out_trade_no: str
    account: str
    pack_id: str
    credits: int
    amount: str
    status: str = "PENDING"  # PENDING | PAID | REFUNDED | CLOSED
    trade_no: str = ""
    expire_at: int = 0


def create_app(gateway, *, clock: Callable[[], float] = time.time, base_url: str = "http://127.0.0.1:8703",
               pricing: dict | None = None, mock_router=None, ledger_path: str = ":memory:",
               audit_path: str = ":memory:", idem_path: str = ":memory:", return_page: str = "",
               vision_client: VisionInvoiceClient | None = None) -> FastAPI:
    gw = gateway
    pricing = pricing or load_pricing()
    packs = {p["id"]: p for p in pricing["packs"]}
    if pricing.get("starter_pack"):
        packs[pricing["starter_pack"]["id"]] = pricing["starter_pack"]
    ledger = CreditsLedger(ledger_path)
    audit = AuditLog(audit_path)
    idem = IdempotencyStore(idem_path)
    orders: dict[str, Order] = {}
    recognized_cache: dict[str, dict] = {}
    lock = threading.RLock()
    app = FastAPI(title="勾稽 Gouji · Vibe Pay 次数包与真票三单勾稽")
    app.state.ledger, app.state.orders, app.state.audit = ledger, orders, audit
    if mock_router is not None:
        app.include_router(mock_router)

    def _gw_mode() -> str:
        if getattr(gw, "is_mock", False):
            return "mock"
        url = str(getattr(gw, "gateway_url", ""))
        return "prod" if ("openapi.alipay.com" in url and "sandbox" not in url) else "sandbox"

    def now() -> int:
        return int(clock())

    def expires() -> int:
        return now() + int(pricing["validity_days"]) * 86400

    def credit(o: Order, trade_no: str, source: str) -> None:
        """幂等入账（通知与主动查询共用）。调用方持锁。"""
        g, created = ledger.grant(o.account, o.out_trade_no, o.credits, cents(o.amount), expires())
        o.status, o.trade_no = "PAID", trade_no
        if created:
            audit.append("gateway", "credits.granted", o.out_trade_no,
                         {"credits": o.credits, "amount": o.amount, "trade_no": trade_no, "via": source})

    @app.get("/billing/packs")
    def get_packs() -> dict:
        out = {"currency": "CNY", "per_call_price": pricing["per_call_price"], "trial_credits": pricing["trial_credits"],
               "packs": pricing["packs"], "gateway_mode": _gw_mode()}
        if pricing.get("starter_pack"):
            out["starter_pack"] = pricing["starter_pack"]
        return out

    @app.post("/billing/trial")
    async def trial(body: dict) -> Response:
        acct = body.get("account", "")
        if not acct:
            return JSONResponse({"code": "INVALID"}, status_code=400)
        with lock:
            try:
                _, created = ledger.grant(acct, f"TRIAL-{acct}", int(pricing["trial_credits"]), 0, expires())
            except CreditsError as e:
                # 再次领取时到期时间已变，账本判为参数不同；试用每个工作区只发一次，视为“已领过”
                if e.code != "GRANT_CONFLICT":
                    raise
                created = False
        if created:
            audit.append(f"user:{acct}", "trial.granted", f"TRIAL-{acct}", {"credits": pricing["trial_credits"]})
        return JSONResponse({"account": acct, "granted": created, "balance": ledger.balance(acct, now())})

    @app.post("/billing/checkout")
    async def checkout(body: dict) -> Response:
        acct, pack = body.get("account", ""), packs.get(body.get("pack_id", ""))
        if not acct or pack is None:
            return JSONResponse({"code": "INVALID", "message": "account / pack_id"}, status_code=400)
        out = f"GJ{datetime.fromtimestamp(now(), CN):%Y%m%d%H%M%S}{secrets.token_hex(4)}"
        expire = datetime.fromtimestamp(now() + 30 * 60, CN).strftime("%Y-%m-%d %H:%M:%S")
        try:
            url = gw.page_pay(PagePayRequest(out, pack["price"], f"勾稽{pack['name']}", f"{base_url}/alipay/notify",
                                             f"{base_url}/billing/return", time_expire=expire))
        except GatewayError as e:
            return JSONResponse({"code": e.sub_code, "message": e.msg}, status_code=502)
        with lock:
            orders[out] = Order(out, acct, pack["id"], int(pack["credits"]), pack["price"], expire_at=now() + 30 * 60)
        audit.append(f"user:{acct}", "order.created", out, {"pack": pack["id"], "amount": pack["price"]})
        return JSONResponse({"out_trade_no": out, "pay_url": url, "amount": pack["price"],
                             "_mock": bool(getattr(gw, "is_mock", False))})

    @app.post("/alipay/notify")
    async def notify(request: Request) -> PlainTextResponse:
        p = {k: str(v) for k, v in (await request.form()).items()}
        out = p.get("out_trade_no", "")

        def fail(why: str) -> PlainTextResponse:
            audit.append("gateway", "notify.rejected", out, {"why": why, "notify_id": p.get("notify_id", "")})
            return PlainTextResponse("failure")

        if not gw.verify_notify(p):
            return fail("SIGNATURE_INVALID")
        with lock:
            o = orders.get(out)
            if o is None:
                return fail("UNKNOWN_ORDER")
            if p.get("total_amount") != o.amount:
                return fail("AMOUNT_MISMATCH")
            status = p.get("trade_status", "")
            _, first = idem.put_once("notify", f"{out}:{status}", p.get("trade_no", ""), {"status": status})
            if not first:
                audit.append("gateway", "notify.duplicate", out, {"trade_status": status})
                return PlainTextResponse("success")
            audit.append("gateway", "notify.verified", out, {"trade_status": status, "trade_no": p.get("trade_no", "")})
            if status in SUCCESS:
                credit(o, p.get("trade_no", ""), "notify")
        return PlainTextResponse("success")

    @app.get("/billing/return", response_class=HTMLResponse)
    def ret(out_trade_no: str = "") -> Response:  # 同步跳转只展示，不改状态
        if return_page and out_trade_no in orders:
            return RedirectResponse(f"{return_page}?order={out_trade_no}", status_code=303)
        return HTMLResponse(f"<meta charset=utf-8><p>支付结果确认中…（订单 {html.escape(out_trade_no)}）。以服务器收到的通知为准。</p>")

    @app.get("/billing/orders")
    def list_orders(account: str) -> dict:
        with lock:
            items = [{"out_trade_no": o.out_trade_no, "pack_id": o.pack_id, "credits": o.credits, "amount": o.amount,
                      "status": o.status, "trade_no": o.trade_no} for o in orders.values() if o.account == account]
        return {"account": account, "orders": items[::-1]}

    @app.get("/billing/orders/{out_trade_no}")
    def order(out_trade_no: str) -> Response:
        with lock:
            o = orders.get(out_trade_no)
            if o is None:
                return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
            if o.status == "PENDING":  # 主动查询补单（通知丢失时）
                try:
                    s = gw.trade_query(out_trade_no)
                    if s.trade_status in SUCCESS and s.total_amount == o.amount:
                        credit(o, s.trade_no, "query")
                    elif s.trade_status == "TRADE_CLOSED" and not s.trade_no:
                        o.status = "CLOSED"
                except GatewayError as e:
                    # 买家没打开收银台时支付宝侧可能还没有这笔交易；本地超时即关单
                    if e.sub_code == "ACQ.TRADE_NOT_EXIST" and now() >= o.expire_at:
                        o.status = "CLOSED"
            return JSONResponse({"out_trade_no": o.out_trade_no, "status": o.status, "credits": o.credits,
                                 "amount": o.amount, "trade_no": o.trade_no})

    @app.get("/billing/balance")
    def balance(account: str) -> dict:
        return {"account": account, "balance": ledger.balance(account, now())}

    @app.post("/billing/consume")
    async def consume(body: dict) -> Response:
        acct, rid, n = body.get("account", ""), body.get("request_id", ""), int(body.get("n", 1))
        with lock:
            try:
                left = ledger.consume(acct, n, now(), rid)
            except CreditsError as e:
                if e.code == "INSUFFICIENT":
                    return JSONResponse({"code": "QUOTA_EXHAUSTED", "message": "次数不足，请购买次数包",
                                         "buy": "/billing/packs"}, status_code=402)
                return JSONResponse({"code": e.code}, status_code=400)
        audit.append(f"user:{acct}", "credits.consumed", rid, {"n": n, "left": left})
        return JSONResponse({"account": acct, "balance": left})

    @app.get("/gouji/samples")
    def list_samples() -> dict:
        items = [
            {
                "sample_id": s["sample_id"],
                "label": s["label"],
                "invoice_type": s["invoice_type"],
                "invoice_number": s["invoice_number"],
                "total_amount": s["total_amount"],
                "tax_rate": s["tax_rate"],
                "buyer_tax_id": s["buyer_tax_id"],
                "qr_code": s["qr_code"],
                "contract": s["contract"],
                "bank_flow": s["bank_flow"],
                "pdf_url": f"/gouji/samples/{s['sample_id']}/pdf",
                "png_url": f"/gouji/samples/{s['sample_id']}/png",
            }
            for s in REAL_INVOICE_SAMPLES.values()
        ]
        return {"samples": items}

    @app.get("/gouji/samples/{sample_id}/pdf")
    def download_sample_pdf(sample_id: str) -> Response:
        spec = REAL_INVOICE_SAMPLES.get(sample_id)
        if spec is None:
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
        pdf_bytes = build_invoice_pdf_bytes(spec)
        return Response(
            pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{sample_id}.pdf"'},
        )

    @app.get("/gouji/samples/{sample_id}/png")
    def download_sample_png(sample_id: str) -> Response:
        spec = REAL_INVOICE_SAMPLES.get(sample_id)
        if spec is None:
            return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
        png_bytes = build_invoice_png_bytes(spec, embed_qr_metadata=True)
        return Response(
            png_bytes,
            media_type="image/png",
            headers={"Content-Disposition": f'attachment; filename="{sample_id}.png"'},
        )

    def _execute_recognize(
        *,
        acct: str,
        rid: str,
        sample_id: str = "",
        doc_name: str = "",
        invoice_text: str = "",
        qr_code: str = "",
        file_bytes: bytes | None = None,
        filename: str = "",
        taxpayer_type: str = "general",
        buyer_tax_id: str = "",
        contract: dict | None = None,
        bank_flow: dict | None = None,
        red_ink_amount: str | None = None,
        use_vision: bool = True,
    ) -> Response:
        if not acct or not rid:
            return JSONResponse({"code": "INVALID", "message": "account / request_id required"}, status_code=400)
        with lock:
            if rid in recognized_cache:
                cached = dict(recognized_cache[rid])
                cached["balance"] = ledger.balance(acct, now())
                cached["idempotent_replay"] = True
                return JSONResponse(cached)
            if ledger.balance(acct, now()) < 1:
                return JSONResponse(
                    {"code": "QUOTA_EXHAUSTED", "message": "次数不足，请购买次数包", "buy": "/billing/packs"},
                    status_code=402,
                )

        # 先解析再扣费：严格兑现“识别失败不扣次”承诺
        try:
            inv_doc, matched_sample = parse_any_invoice(
                sample_id=sample_id,
                doc_name=doc_name,
                invoice_text=invoice_text,
                qr_code=qr_code,
                file_bytes=file_bytes,
                filename=filename,
                use_vision=use_vision,
                vision_client=vision_client,
            )
            auto_contract, auto_flow = lookup_ledger_counterparts(inv_doc)
            eff_contract = contract if contract is not None else (matched_sample.get("contract") if matched_sample else auto_contract)
            eff_flow = bank_flow if bank_flow is not None else (matched_sample.get("bank_flow") if matched_sample else auto_flow)
            result = reconcile_and_bookkeep(
                inv_doc,
                taxpayer_type=taxpayer_type,
                expected_buyer_tax_id=buyer_tax_id or (eff_contract.get("buyer_tax_id", "") if eff_contract else ""),
                contract=eff_contract,
                bank_flow=eff_flow,
                red_ink_amount=red_ink_amount,
            )
        except InvoiceParseError as exc:
            audit.append(f"user:{acct}", "invoice.parse_failed", rid, {"reason": str(exc)[:120]})
            return JSONResponse({"code": "INVOICE_PARSE_FAILED", "message": str(exc)}, status_code=422)

        with lock:
            try:
                left = ledger.consume(acct, 1, now(), rid)
            except CreditsError as e:
                if e.code == "INSUFFICIENT":
                    return JSONResponse(
                        {"code": "QUOTA_EXHAUSTED", "message": "次数不足，请购买次数包", "buy": "/billing/packs"},
                        status_code=402,
                    )
                return JSONResponse({"code": e.code}, status_code=400)
            payload = {**result, "balance": left, "idempotent_replay": False}
            recognized_cache[rid] = payload

        audit.append(
            f"user:{acct}",
            "invoice.recognized",
            rid,
            {
                "invoice_number": inv_doc.invoice_number,
                "total_amount": f"{inv_doc.total_amount:.2f}",
                "source": inv_doc.extraction_source,
                "reconciliation_status": result["reconciliation"]["status"],
                "red_ink_required": result["red_ink_adjustment"]["required"],
                "left": left,
            },
        )
        return JSONResponse(payload)

    @app.post("/gouji/recognize")
    async def recognize(body: dict) -> Response:
        """真实中国电子发票解析 + 三单勾稽 + 红冲差额计算（扣 1 次额度，同 request_id 幂等，失败不扣次）。"""
        file_bytes: bytes | None = None
        if body.get("file_b64"):
            raw_b64 = str(body["file_b64"])
            if "," in raw_b64 and raw_b64.startswith("data:"):
                raw_b64 = raw_b64.split(",", 1)[1]
            try:
                file_bytes = base64.b64decode(raw_b64, validate=True)
            except ValueError:
                return JSONResponse({"code": "INVALID_BASE64", "message": "file_b64 不是合法的 Base64"}, status_code=422)
        return _execute_recognize(
            acct=str(body.get("account", "")),
            rid=str(body.get("request_id", "")),
            sample_id=str(body.get("sample_id", "")),
            doc_name=str(body.get("doc_name", "")),
            invoice_text=str(body.get("invoice_text", "")),
            qr_code=str(body.get("qr_code", "")),
            file_bytes=file_bytes,
            filename=str(body.get("filename", "")),
            taxpayer_type=str(body.get("taxpayer_type", "general")),
            buyer_tax_id=str(body.get("buyer_tax_id", "")),
            contract=body.get("contract"),
            bank_flow=body.get("bank_flow"),
            red_ink_amount=str(body["red_ink_amount"]) if body.get("red_ink_amount") not in (None, "") else None,
            use_vision=bool(body.get("use_vision", True)),
        )

    @app.post("/gouji/recognize/upload")
    async def recognize_upload(
        account: str = Form(...),
        request_id: str = Form(...),
        file: UploadFile = File(...),
        taxpayer_type: str = Form("general"),
        qr_code: str = Form(""),
        contract_json: str = Form(""),
        bank_flow_json: str = Form(""),
        red_ink_amount: str = Form(""),
        use_vision: bool = Form(True),
    ) -> Response:
        raw = await file.read()
        contract_obj = json.loads(contract_json) if contract_json.strip() else None
        flow_obj = json.loads(bank_flow_json) if bank_flow_json.strip() else None
        return _execute_recognize(
            acct=account,
            rid=request_id,
            file_bytes=raw,
            filename=file.filename or "",
            taxpayer_type=taxpayer_type,
            qr_code=qr_code,
            contract=contract_obj,
            bank_flow=flow_obj,
            red_ink_amount=red_ink_amount or None,
            use_vision=use_vision,
        )

    @app.post("/billing/orders/{out_trade_no}/refund")
    async def refund(out_trade_no: str, body: dict) -> Response:
        with lock:
            o = orders.get(out_trade_no)
            if o is None or o.account != body.get("account"):
                return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
            if o.status != "PAID":
                return JSONResponse({"code": "NOT_REFUNDABLE", "status": o.status}, status_code=409)
            n, amt = ledger.refundable(out_trade_no, now())
            if n == 0 or amt == 0:
                return JSONResponse({"code": "NOTHING_TO_REFUND"}, status_code=409)
            req_no = f"RF{out_trade_no}"
            try:
                gw.refund(out_trade_no, yuan(amt), req_no)
            except GatewayError as e:
                audit.append("gateway", "refund.failed", out_trade_no, {"sub_code": e.sub_code})
                return JSONResponse({"code": e.sub_code}, status_code=502)
            got = ledger.refund(out_trade_no, now(), req_no)
            o.status = "REFUNDED"
        audit.append(f"user:{o.account}", "refunded", out_trade_no, {"credits": got[0], "amount": yuan(got[1])})
        return JSONResponse({"out_trade_no": out_trade_no, "refunded_credits": got[0], "refund_amount": yuan(got[1]),
                             "balance": ledger.balance(o.account, now())})

    @app.get("/audit/export")
    def audit_export(format: str = "jsonl") -> Response:
        if format == "csv":
            return Response(audit.export_csv(), media_type="text/csv; charset=utf-8")
        return Response(audit.export_jsonl(), media_type="application/x-ndjson; charset=utf-8")

    return app
