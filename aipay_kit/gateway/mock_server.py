"""⚠️ MOCK 支付宝 HTTP 网关 —— 在本机模拟 gateway.do、收银台和智能体钱包。不是支付宝，没有资金。

- POST /mock/alipay/gateway.do：校验商户 RSA2 签名 → 调 MockGateway → 返回带「支付宝」签名的响应（与官方格式一致）
- GET  /mock/alipay/gateway.do?method=alipay.trade.page.pay&...：page.pay 跳转 → 建单并 302 到收银台
- GET  /mock/alipay/cashier/{out_trade_no}：MOCK 收银台页面（显著标注 MOCK）
- POST /mock/alipay/cashier/{out_trade_no}/pay：模拟买家确认 → 投递异步通知
- GET  /mock/alipay/a402/{amt_pre_pay_id}、POST .../pay：模拟 @alipay/agent-payment 钱包展示账单与付款
"""

from __future__ import annotations

import html
import json

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from aipay_kit import signing
from aipay_kit.gateway.base import GatewayError, PagePayRequest, PrecreateRequest
from aipay_kit.gateway.mock import MockGateway

BANNER = "MOCK 网关 · 非真实支付 · 不产生任何资金流动"


def build_router(gw: MockGateway, merchant_public_keys: dict[str, str]) -> APIRouter:
    """merchant_public_keys: app_id -> 商户应用公钥（PEM 或裸 base64）。"""
    r = APIRouter(prefix="/mock/alipay")
    pubs = {k: signing.load_public(v) for k, v in merchant_public_keys.items()}

    def signed(method: str, payload: dict) -> Response:
        key = method.replace(".", "_") + "_response"
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        sig = signing.rsa2_sign(raw, gw._alipay_key)
        body = '{"' + key + '":' + raw + ',"sign":"' + sig + '"}'
        return Response(body, media_type="application/json;charset=utf-8")

    def err(method: str, sub_code: str, msg: str, code: str = "40004") -> Response:
        return signed(method, {"code": code, "msg": "Business Failed", "sub_code": sub_code, "sub_msg": msg})

    def check_sig(p: dict[str, str]) -> str | None:
        pub = pubs.get(p.get("app_id", ""))
        if pub is None:
            return "isv.invalid-app-id"
        if p.get("sign_type") != "RSA2" or not signing.rsa2_verify(signing.sign_content(p), p.get("sign", ""), pub):
            return "isv.invalid-signature"
        return None

    @r.get("/gateway.do")
    async def page_pay_get(request: Request) -> Response:
        p = dict(request.query_params)
        bad = check_sig(p)
        if bad:
            return HTMLResponse(f"<h1>{BANNER}</h1><p>{bad}</p>", status_code=400)
        if p.get("method") != "alipay.trade.page.pay":
            return HTMLResponse("unsupported", status_code=400)
        biz = json.loads(p["biz_content"])
        try:
            url = gw.page_pay(PagePayRequest(biz["out_trade_no"], biz["total_amount"], biz["subject"],
                                             p.get("notify_url", ""), p.get("return_url", ""),
                                             biz.get("product_code", "FAST_INSTANT_TRADE_PAY"), biz.get("time_expire", "")))
        except GatewayError as e:
            return HTMLResponse(f"<h1>{BANNER}</h1><p>{html.escape(e.sub_code)} {html.escape(e.msg)}</p>", status_code=400)
        return RedirectResponse(url, status_code=302)

    @r.post("/gateway.do")
    async def gateway(request: Request) -> Response:
        p = {k: str(v) for k, v in (await request.form()).items()}
        method = p.get("method", "")
        bad = check_sig(p)
        if bad:
            return err(method, bad, "MOCK 签名校验失败", code="40002")
        biz = json.loads(p.get("biz_content") or "{}")
        try:
            if method == "alipay.trade.query":
                s = gw.trade_query(biz["out_trade_no"])
                return signed(method, {"code": "10000", "msg": "Success", "out_trade_no": s.out_trade_no,
                                       "trade_no": s.trade_no, "trade_status": s.trade_status,
                                       "total_amount": s.total_amount, "refund_amount": s.refund_amount, "_mock": True})
            if method == "alipay.trade.refund":
                x = gw.refund(biz["out_trade_no"], biz["refund_amount"], biz.get("out_request_no", ""))
                return signed(method, {"code": "10000", "msg": "Success", "out_trade_no": x.out_trade_no,
                                       "refund_fee": x.refund_amount, "fund_change": "Y" if x.fund_change else "N",
                                       "_mock": True})
            if method == "alipay.trade.fastpay.refund.query":
                return signed(method, {"code": "10000", "msg": "Success",
                                       **gw.refund_query(biz["out_trade_no"], biz["out_request_no"]), "_mock": True})
            if method == "alipay.aipay.agent.payment.precreate":
                pid = gw.precreate(PrecreateRequest(biz["out_trade_no"], biz["service_id"], biz["goods_name"],
                                                    biz["amount"], biz["pay_before"], biz.get("currency", "CNY"),
                                                    biz.get("memo", ""), biz.get("resource_id", "")))
                return signed(method, {"code": "10000", "msg": "Success", "amt_pre_pay_id": pid, "_mock": True})
            if method == "alipay.aipay.agent.payment.verify":
                v = gw.verify_proof(biz.get("payment_proof", ""), biz.get("trade_no", ""), biz.get("client_session", ""))
                return signed(method, {"code": "10000", "msg": "Success", "active": v.active, "amount": v.amount,
                                       "out_trade_no": v.out_trade_no, "trade_no": v.trade_no,
                                       "resource_id": v.resource_id, "_mock": True})
            if method == "alipay.aipay.agent.fulfillment.confirm":
                if not gw.fulfillment_confirm(biz.get("trade_no", "")):
                    return err(method, "TRADE_NOT_PAID", "交易不存在或未支付")
                return signed(method, {"code": "10000", "msg": "Success", "_mock": True})
        except GatewayError as e:
            return err(method, e.sub_code, e.msg, e.code)
        except (KeyError, json.JSONDecodeError) as e:
            return err(method, "isv.missing-parameter", str(e), code="40001")
        return err(method, "isv.invalid-method", method, code="40001")

    @r.get("/cashier/{out_trade_no}", response_class=HTMLResponse)
    async def cashier(out_trade_no: str) -> HTMLResponse:
        try:
            s = gw.trade_query(out_trade_no)
        except GatewayError:
            return HTMLResponse("not found", status_code=404)
        o = html.escape(out_trade_no)
        amt = html.escape(s.total_amount)
        subject = html.escape(gw._orders[out_trade_no].subject) if out_trade_no in gw._orders else ""
        st = html.escape(s.trade_status)
        return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" /><title>MOCK 收银台</title>
<link rel="stylesheet" href="/static/elidot/tokens.css" /><link rel="stylesheet" href="/static/elidot/components.css" />
<link rel="stylesheet" href="/static/vendor/phosphor/style.css" /><link rel="stylesheet" href="/static/app.css" /></head>
<body style="background:#F6F6F1;color:#131311;font-family:system-ui,sans-serif;margin:0">
<div class="mock-banner" role="note" data-mock="1" style="background:#F7401D;color:#131311;font-weight:700;text-align:center;padding:8px 12px">{BANNER} · 这不是支付宝官方收银台</div>
<main id="main" class="container" style="max-width:520px;margin:48px auto;padding:0 20px">
<p class="eyebrow">MOCK 收银台 · 模拟支付宝电脑网站支付</p>
<div class="card" style="background:#fff;border:1px solid #DEDDD5;border-radius:12px;padding:24px">
<p class="small muted" style="margin:0">{subject}</p>
<p class="stat" style="font-size:40px;margin:8px 0 16px" data-amount="{amt}">¥{amt}</p>
<div class="order-line"><span>商户订单号</span><span class="mono small">{o}</span></div>
<div class="order-line"><span>状态</span><span data-status="{st}">{st}</span></div>
<form method="post" action="/mock/alipay/cashier/{o}/pay" style="margin-top:24px">
<button class="btn btn-primary btn-lg btn-block" type="submit" id="mock-pay" style="width:100%;padding:12px">模拟买家确认付款</button></form>
<p class="trust-line small muted">点击后，MOCK 网关会用支付宝公钥对应的模拟私钥签名一条异步通知，投递到商户的 notify_url。</p>
</div></main></body></html>""")

    @r.post("/cashier/{out_trade_no}/pay")
    async def cashier_pay(out_trade_no: str) -> Response:
        try:
            p = gw.buyer_pay(out_trade_no)
        except GatewayError as e:
            return JSONResponse({"_mock": True, "error": e.sub_code}, status_code=400)
        o = gw._orders[out_trade_no]
        if o.return_url:
            return RedirectResponse(f"{o.return_url}?out_trade_no={out_trade_no}&_mock=1", status_code=303)
        return JSONResponse({"_mock": True, "trade_no": p["trade_no"], "trade_status": p["trade_status"]})

    @r.get("/a402/{amt_pre_pay_id}")
    async def wallet_bill(amt_pre_pay_id: str) -> Response:
        try:
            return JSONResponse({**gw.describe_prepay(amt_pre_pay_id), "banner": BANNER})
        except GatewayError as e:
            return JSONResponse({"_mock": True, "error": e.sub_code}, status_code=404)

    @r.post("/a402/{amt_pre_pay_id}/pay")
    async def wallet_pay(amt_pre_pay_id: str) -> Response:
        try:
            return JSONResponse({**gw.agent_wallet_pay(amt_pre_pay_id), "_mock": True})
        except GatewayError as e:
            return JSONResponse({"_mock": True, "error": e.sub_code}, status_code=400)

    return r


def build_app(gw: MockGateway, merchant_public_keys: dict[str, str]) -> FastAPI:
    app = FastAPI(title="MOCK Alipay gateway (not Alipay)")
    app.include_router(build_router(gw, merchant_public_keys))
    return app
