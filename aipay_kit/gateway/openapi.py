"""支付宝开放平台 / AI 付标准 OpenAPI 与 SDK 对接客户端（支持官方沙箱、正式生产网关及合同测试网关）。

协议规范（对齐支付宝开放平台 AlipaySignature.rsaCheckV1 / A2M 智能收产品对接规范）：
- 公共参数：app_id, method, format=JSON, charset=utf-8, sign_type=RSA2, timestamp, version=1.0, biz_content(+notify_url/return_url)
- 请求签名：除 sign 外全部非空参数按字典序拼接，RSA2（SHA256withRSA，应用 PKCS8 私钥）。
- 响应验签：提取 `<method 点换下划线>_response` 原始 JSON 子串（禁止重序列化），用支付宝公钥做 RSA2 验签。
- 网页收款：支持 pageExecute GET 跳转 URL 与标准自动提交 POST 表单（alipay.trade.page.pay / alipay.trade.wap.pay）。
- 异步通知：严格执行 rsaCheckV1 验签（排除 sign 与 sign_type）并核对 app_id（及可选 seller_id）。
- A402 智能收：完整支持 precreate、payment.verify（透传 client_session）、fulfillment.confirm 及商家账单 RSA2 加签。
"""

from __future__ import annotations

import html
import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

from aipay_kit import signing
from aipay_kit.gateway.base import (
    GatewayError,
    PagePayRequest,
    PrecreateRequest,
    ProofVerifyResult,
    RefundQueryResult,
    RefundResult,
    TradeStatus,
    WapPayRequest,
)

SANDBOX_URL = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
PROD_URL = "https://openapi.alipay.com/gateway.do"
CN = timezone(timedelta(hours=8))

A2M_SIGN_KEYS = (
    "amount",
    "currency",
    "goods_name",
    "out_trade_no",
    "pay_before",
    "resource_id",
    "seller_id",
    "service_id",
)


def extract_response_text(body: str, key: str) -> str | None:
    """取出 body 里 "key": 后面那个 JSON 对象的原始文本（验签必须用原文，不能重新序列化）。"""
    marker = f'"{key}"'
    i = body.find(marker)
    if i < 0:
        return None
    j = body.find("{", i + len(marker))
    if j < 0:
        return None
    depth, in_str, esc = 0, False, False
    for k in range(j, len(body)):
        ch = body[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return body[j : k + 1]
    return None


class OpenApiGateway:
    is_mock = False

    def __init__(
        self,
        app_id: str,
        app_private_key: str,
        alipay_public_key: str,
        gateway_url: str = SANDBOX_URL,
        *,
        seller_id: str = "",
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
        is_mock: bool = False,
    ) -> None:
        self.app_id = app_id
        self.seller_id = seller_id
        self._key = signing.load_private(app_private_key)
        self.alipay_public_key = signing.load_public(alipay_public_key)
        self.gateway_url = gateway_url
        self._client = client or httpx.Client(timeout=15)
        self._clock = clock
        self.is_mock = is_mock

    # ---------------------------------------------------------------- core
    def _common(self, method: str, biz: dict, extra: dict[str, str] | None = None) -> dict[str, str]:
        p = {
            "app_id": self.app_id,
            "method": method,
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": datetime.fromtimestamp(self._clock(), CN).strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
        }
        p.update(extra or {})
        p["sign"] = signing.sign_params(p, self._key)
        return p

    def call(self, method: str, biz: dict) -> dict:
        params = self._common(method, biz)
        r = self._client.post(self.gateway_url, data=params,
                              headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
        body = r.text
        key = method.replace(".", "_") + "_response"
        raw = extract_response_text(body, key)
        if raw is None:
            raise GatewayError("GATEWAY_BAD_RESPONSE", body[:200], code="50000")
        data = json.loads(raw)
        sign = json.loads(body).get("sign")
        if data.get("code") == "10000":
            if not sign or not signing.rsa2_verify(raw, sign, self.alipay_public_key):
                raise GatewayError("RESPONSE_SIGN_INVALID", method, code="50000")
            return data
        # 错误响应也可能带签名；验签失败的错误照样按错误抛
        raise GatewayError(data.get("sub_code") or data.get("code", "UNKNOWN"), data.get("sub_msg") or data.get("msg", ""),
                           code=str(data.get("code", "")))

    # ---------------------------------------------------------------- 网页与 H5 收款
    def _page_params(self, method: str, biz: dict, notify_url: str, return_url: str = "") -> dict[str, str]:
        extra = {"notify_url": notify_url}
        if return_url:
            extra["return_url"] = return_url
        return self._common(method, biz, extra)

    def page_pay(self, req: PagePayRequest) -> str:
        biz = {"out_trade_no": req.out_trade_no, "total_amount": req.total_amount, "subject": req.subject,
               "product_code": req.product_code}
        if req.time_expire:
            biz["time_expire"] = req.time_expire
        p = self._page_params("alipay.trade.page.pay", biz, req.notify_url, req.return_url)
        return f"{self.gateway_url}?{urlencode(p)}"

    def page_pay_form(self, req: PagePayRequest) -> str:
        """生成与官方 SDK pageExecute('POST') 等价的自动提交 HTML 表单（适配千艺网页收款嵌入页）。"""
        biz = {"out_trade_no": req.out_trade_no, "total_amount": req.total_amount, "subject": req.subject,
               "product_code": req.product_code}
        if req.time_expire:
            biz["time_expire"] = req.time_expire
        p = self._page_params("alipay.trade.page.pay", biz, req.notify_url, req.return_url)
        inputs = "".join(
            f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}" />'
            for k, v in p.items()
        )
        action = html.escape(f"{self.gateway_url}?charset=utf-8")
        return (
            f'<form id="alipay_submit" name="alipay_submit" action="{action}" method="POST">'
            f"{inputs}"
            '<input type="submit" value="立即前往支付宝付款" style="display:none" />'
            "</form><script>document.forms['alipay_submit'].submit();</script>"
        )

    def wap_pay(self, req: WapPayRequest) -> str:
        """手机网站 H5 收款（alipay.trade.wap.pay，product_code=QUICK_WAP_WAY）。"""
        biz = {"out_trade_no": req.out_trade_no, "total_amount": req.total_amount, "subject": req.subject,
               "product_code": req.product_code}
        if req.quit_url:
            biz["quit_url"] = req.quit_url
        if req.time_expire:
            biz["time_expire"] = req.time_expire
        p = self._page_params("alipay.trade.wap.pay", biz, req.notify_url, req.return_url)
        return f"{self.gateway_url}?{urlencode(p)}"

    def trade_query(self, out_trade_no: str) -> TradeStatus:
        d = self.call("alipay.trade.query", {"out_trade_no": out_trade_no})
        return TradeStatus(d.get("out_trade_no", out_trade_no), d.get("trade_no", ""), d.get("trade_status", ""),
                           d.get("total_amount", ""), d.get("refund_amount", "0.00") or "0.00")

    def refund(self, out_trade_no: str, refund_amount: str, out_request_no: str) -> RefundResult:
        d = self.call("alipay.trade.refund", {"out_trade_no": out_trade_no, "refund_amount": refund_amount,
                                              "out_request_no": out_request_no})
        return RefundResult(out_trade_no, out_request_no, d.get("refund_fee", refund_amount), d.get("fund_change") == "Y")

    def refund_query(self, out_trade_no: str, out_request_no: str) -> RefundQueryResult:
        d = self.call("alipay.trade.fastpay.refund.query",
                      {"out_trade_no": out_trade_no, "out_request_no": out_request_no})
        return RefundQueryResult(
            out_trade_no=d.get("out_trade_no", out_trade_no),
            out_request_no=d.get("out_request_no", out_request_no),
            refund_amount=d.get("refund_amount", "0.00") or "0.00",
            refund_status=d.get("refund_status", ""),
        )

    def verify_notify(self, params: dict[str, str]) -> bool:
        if params.get("app_id") != self.app_id:
            return False
        if self.seller_id and params.get("seller_id") and params.get("seller_id") != self.seller_id:
            return False
        return signing.verify_notify(params, self.alipay_public_key)

    # ---------------------------------------------------------------- A402 / A2M 智能收
    def sign_a2m_bill(self, fields: dict[str, str]) -> str:
        """按官方 A2MPaymentDemoController.generateSellerSignature 规则生成商家 RSA2 签名。"""
        subset = {k: str(fields[k]) for k in A2M_SIGN_KEYS if fields.get(k)}
        return signing.sign_params(subset, self._key)

    def precreate(self, req: PrecreateRequest) -> str:
        biz = {"out_trade_no": req.out_trade_no, "service_id": req.service_id, "goods_name": req.goods_name,
               "amount": req.amount, "pay_before": req.pay_before, "currency": req.currency}
        if req.memo:
            biz["memo"] = req.memo
        if req.resource_id:
            biz["resource_id"] = req.resource_id
        d = self.call("alipay.aipay.agent.payment.precreate", biz)
        return d["amt_pre_pay_id"]

    def verify_proof(self, payment_proof: str, trade_no: str, client_session: str) -> ProofVerifyResult:
        try:
            d = self.call("alipay.aipay.agent.payment.verify",
                          {"payment_proof": payment_proof, "trade_no": trade_no, "client_session": client_session})
        except GatewayError as e:
            return ProofVerifyResult(False, "", "", trade_no, "", {"error": e.sub_code})
        active = d.get("active") in (True, "true")
        return ProofVerifyResult(active, d.get("amount", ""), d.get("out_trade_no", ""), d.get("trade_no", trade_no),
                                 d.get("resource_id", ""), d)

    def fulfillment_confirm(self, trade_no: str) -> bool:
        try:
            self.call("alipay.aipay.agent.fulfillment.confirm", {"trade_no": trade_no})
            return True
        except GatewayError:
            return False


AlipaySdkGateway = OpenApiGateway
