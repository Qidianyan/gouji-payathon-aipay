"""Replaceable payment gateway contract. Implementations:
- OpenApiGateway / AlipaySdkGateway: Alipay Open Platform & AI-Pay RSA2 client supporting
  sandbox (openapi-sandbox.dl.alipaydev.com) and production (openapi.alipay.com).
- MockGateway: deterministic local contract gateway (explicitly marked is_mock=True) for offline CI/tests.
Business code depends only on PaymentGateway Protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class PagePayRequest:  # alipay.trade.page.pay (Vibe Pay PC Web)
    out_trade_no: str
    total_amount: str  # yuan, 2 decimals, [0.01, 100000000]
    subject: str
    notify_url: str
    return_url: str = ""
    product_code: str = "FAST_INSTANT_TRADE_PAY"
    time_expire: str = ""  # "yyyy-MM-dd HH:mm:ss" Asia/Shanghai


@dataclass(frozen=True)
class WapPayRequest:  # alipay.trade.wap.pay (Vibe Pay Mobile H5)
    out_trade_no: str
    total_amount: str
    subject: str
    notify_url: str
    return_url: str = ""
    quit_url: str = ""
    product_code: str = "QUICK_WAP_WAY"
    time_expire: str = ""


@dataclass(frozen=True)
class TradeStatus:  # alipay.trade.query
    out_trade_no: str
    trade_no: str
    trade_status: str  # WAIT_BUYER_PAY | TRADE_SUCCESS | TRADE_FINISHED | TRADE_CLOSED
    total_amount: str
    refund_amount: str = "0.00"


@dataclass(frozen=True)
class RefundResult:  # alipay.trade.refund
    out_trade_no: str
    out_request_no: str
    refund_amount: str
    fund_change: bool


@dataclass(frozen=True)
class RefundQueryResult:  # alipay.trade.fastpay.refund.query
    out_trade_no: str
    out_request_no: str
    refund_amount: str
    refund_status: str  # REFUND_SUCCESS or ""


@dataclass(frozen=True)
class PrecreateRequest:  # alipay.aipay.agent.payment.precreate (Machine Pay / A402)
    out_trade_no: str
    service_id: str
    goods_name: str
    amount: str  # yuan, <= 50
    pay_before: str
    currency: str = "CNY"
    memo: str = ""
    resource_id: str = ""  # 资源防串标识，与 Payment-Needed.resource_id 及 verify 响应一致


@dataclass(frozen=True)
class ProofVerifyResult:  # alipay.aipay.agent.payment.verify
    active: bool
    amount: str
    out_trade_no: str
    trade_no: str
    resource_id: str
    raw: dict = field(default_factory=dict)


class GatewayError(Exception):
    """开放平台业务错误（code/sub_code 与官方风格一致，如 ACQ.TRADE_NOT_EXIST）。"""

    def __init__(self, sub_code: str, msg: str = "", code: str = "40004") -> None:
        super().__init__(f"{sub_code}: {msg}")
        self.code = code
        self.sub_code = sub_code
        self.msg = msg


class PaymentGateway(Protocol):
    is_mock: bool

    def page_pay(self, req: PagePayRequest) -> str: ...  # returns cashier URL / form
    def trade_query(self, out_trade_no: str) -> TradeStatus: ...
    def refund(self, out_trade_no: str, refund_amount: str, out_request_no: str) -> RefundResult: ...
    def precreate(self, req: PrecreateRequest) -> str: ...  # returns amt_pre_pay_id
    def verify_proof(self, payment_proof: str, trade_no: str, client_session: str) -> ProofVerifyResult: ...
    def fulfillment_confirm(self, trade_no: str) -> bool: ...  # alipay.aipay.agent.fulfillment.confirm
    def verify_notify(self, params: dict[str, str]) -> bool: ...
