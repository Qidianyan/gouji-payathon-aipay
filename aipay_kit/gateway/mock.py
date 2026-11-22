"""⚠️ MOCK 支付宝网关 —— 本地模拟，不是支付宝，不产生任何资金流动。

模拟范围（字段名/规则取自官方文档，见 materials/架构与开发计划.md）：
- 网页收款：alipay.trade.page.pay 建单 → 模拟收银台买家确认 → 异步通知（RSA2 签名，排除 sign/sign_type）
  → alipay.trade.query → alipay.trade.refund（按 out_request_no 幂等，全额退款后 TRADE_CLOSED）。
- 按量付费（402）：alipay.aipay.agent.payment.precreate（单笔 ≤ 50 元）→ 模拟智能体钱包付款得到
  payment_proof / trade_no / client_session → alipay.aipay.agent.payment.verify → fulfillment.confirm。
- 故障注入：drop_notify / duplicate_notify / tamper_amount / bad_signature。
所有对外返回都带 mock 标记；UI 必须显示「MOCK 网关 · 非真实支付」。
"""

from __future__ import annotations

import base64
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from aipay_kit import signing
from aipay_kit.gateway.base import (
    GatewayError,
    PagePayRequest,
    PrecreateRequest,
    ProofVerifyResult,
    RefundResult,
    TradeStatus,
)

CN = timezone(timedelta(hours=8))
MOCK_SELLER_ID = "2088000000000001"  # 2088 开头的假商户号
MOCK_BUYER_ID = "2088000000009999"
AMOUNT_RE = re.compile(r"^(0|[1-9]\d*)(\.\d{1,2})?$")
FAULTS = frozenset({"drop_notify", "duplicate_notify", "tamper_amount", "bad_signature"})

Notifier = Callable[[str, dict[str, str]], str]


def _money(s: str, *, lo: str, hi: str, what: str) -> Decimal:
    if not isinstance(s, str) or not AMOUNT_RE.match(s):
        raise GatewayError("ACQ.INVALID_PARAMETER", f"{what} 格式错误: {s!r}")
    try:
        d = Decimal(s)
    except InvalidOperation as e:  # pragma: no cover - regex guards
        raise GatewayError("ACQ.INVALID_PARAMETER", what) from e
    if d < Decimal(lo) or d > Decimal(hi):
        raise GatewayError("ACQ.INVALID_PARAMETER", f"{what} 超出范围 [{lo}, {hi}]: {s}")
    return d


def _fmt(d: Decimal) -> str:
    return f"{d.quantize(Decimal('0.01'))}"


@dataclass
class _Order:
    out_trade_no: str
    total_amount: Decimal
    subject: str
    notify_url: str
    return_url: str
    time_expire: int  # unix 秒
    created: int
    status: str = "WAIT_BUYER_PAY"
    trade_no: str = ""
    paid_at: int = 0
    refunded: Decimal = Decimal("0")
    refunds: dict[str, Decimal] = field(default_factory=dict)  # out_request_no -> amount
    notify_attempts: int = 0
    notify_acked: bool = False
    last_notify: dict[str, str] = field(default_factory=dict)


@dataclass
class _PrePay:
    amt_pre_pay_id: str
    req: PrecreateRequest
    amount: Decimal
    pay_before: int
    status: str = "WAIT_PAY"  # WAIT_PAY | PAID | CLOSED
    trade_no: str = ""
    payment_proof: str = ""
    client_session: str = ""
    fulfilled: int = 0


class MockGateway:
    """In-process MOCK。线程安全（一把锁）。时间可注入（clock 返回 unix 秒）。"""

    is_mock = True

    def __init__(
        self,
        app_id: str = "9021000000000000",
        *,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.time,
        cashier_base: str = "http://127.0.0.1:8701/mock/alipay",
        faults: set[str] | None = None,
    ) -> None:
        self.app_id = app_id
        self.notifier = notifier
        self.clock = clock
        self.cashier_base = cashier_base.rstrip("/")
        self.faults: set[str] = set(faults or ())
        if not self.faults <= FAULTS:
            raise ValueError(f"unknown faults: {self.faults - FAULTS}")
        self._alipay_key = signing.generate_keypair()  # 「支付宝」私钥：只在 MOCK 内部
        self.alipay_public_key = self._alipay_key.public_key()
        self._orders: dict[str, _Order] = {}
        self._prepays: dict[str, _PrePay] = {}
        self._prepay_by_out: dict[str, str] = {}
        self._by_trade_no: dict[str, str] = {}  # trade_no -> out_trade_no / amt id
        self._lock = threading.RLock()
        self.fulfillment_log: list[str] = []

    # ------------------------------------------------------------------ helpers
    def _now(self) -> int:
        return int(self.clock())

    def _new_trade_no(self) -> str:
        day = datetime.fromtimestamp(self._now(), CN).strftime("%Y%m%d")
        while True:
            t = day + "".join(secrets.choice("0123456789") for _ in range(24))  # 32 位数字
            if t not in self._by_trade_no:
                return t

    @staticmethod
    def _ts(unix: int) -> str:
        return datetime.fromtimestamp(unix, CN).strftime("%Y-%m-%d %H:%M:%S")

    def _parse_expire(self, s: str, default_s: int) -> int:
        if not s:
            return self._now() + default_s
        try:
            return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
        except ValueError as e:
            raise GatewayError("ACQ.INVALID_PARAMETER", f"time_expire 格式错误: {s!r}") from e

    def _expire_if_needed(self, o: _Order) -> None:
        if o.status == "WAIT_BUYER_PAY" and self._now() >= o.time_expire:
            o.status = "TRADE_CLOSED"

    # ------------------------------------------------------------ 网页收款
    def page_pay(self, req: PagePayRequest) -> str:
        if req.product_code != "FAST_INSTANT_TRADE_PAY":
            raise GatewayError("ACQ.INVALID_PARAMETER", "product_code 只支持 FAST_INSTANT_TRADE_PAY")
        if not req.out_trade_no or len(req.out_trade_no) > 64:
            raise GatewayError("ACQ.INVALID_PARAMETER", "out_trade_no 必填且 ≤ 64 位")
        if not req.subject or any(c in req.subject for c in "/=&"):
            raise GatewayError("ACQ.INVALID_PARAMETER", "subject 必填且不能含 / = &")
        amount = _money(req.total_amount, lo="0.01", hi="100000000", what="total_amount")
        with self._lock:
            o = self._orders.get(req.out_trade_no)
            if o is not None:
                same = (o.total_amount, o.subject, o.notify_url) == (amount, req.subject, req.notify_url)
                if not same:
                    raise GatewayError("ACQ.CONTEXT_INCONSISTENT", "同一 out_trade_no 参数不一致")
                self._expire_if_needed(o)
                if o.status != "WAIT_BUYER_PAY":
                    raise GatewayError("ACQ.TRADE_HAS_SUCCESS" if o.trade_no else "ACQ.TRADE_HAS_CLOSE", o.status)
            else:
                now = self._now()
                o = _Order(req.out_trade_no, amount, req.subject, req.notify_url, req.return_url,
                           self._parse_expire(req.time_expire, 2 * 3600), now)
                self._orders[req.out_trade_no] = o
        return f"{self.cashier_base}/cashier/{req.out_trade_no}"

    def buyer_pay(self, out_trade_no: str, buyer_id: str = MOCK_BUYER_ID) -> dict[str, str]:
        """模拟买家在（沙箱）支付宝 APP 里确认付款。返回投递出去的通知参数。"""
        with self._lock:
            o = self._orders.get(out_trade_no)
            if o is None:
                raise GatewayError("ACQ.TRADE_NOT_EXIST", out_trade_no)
            self._expire_if_needed(o)
            if o.status == "TRADE_CLOSED":
                raise GatewayError("ACQ.TRADE_HAS_CLOSE", out_trade_no)
            if o.status == "WAIT_BUYER_PAY":
                o.status = "TRADE_SUCCESS"
                o.trade_no = self._new_trade_no()
                o.paid_at = self._now()
                self._by_trade_no[o.trade_no] = out_trade_no
            params = self._notify_params(o, buyer_id)
            o.last_notify = params
        self._deliver(o, params)
        return params

    def _notify_params(self, o: _Order, buyer_id: str) -> dict[str, str]:
        amt = _fmt(o.total_amount)
        p = {
            "notify_time": self._ts(self._now()),
            "notify_type": "trade_status_sync",
            "notify_id": secrets.token_hex(16),
            "app_id": self.app_id,
            "charset": "utf-8",
            "version": "1.0",
            "sign_type": "RSA2",
            "trade_no": o.trade_no,
            "out_trade_no": o.out_trade_no,
            "buyer_id": buyer_id,
            "seller_id": MOCK_SELLER_ID,
            "trade_status": o.status,
            "total_amount": amt,
            "receipt_amount": amt,
            "buyer_pay_amount": amt,
            "subject": o.subject,
            "gmt_create": self._ts(o.created),
            "gmt_payment": self._ts(o.paid_at),
        }
        if o.refunded > 0:
            p["refund_fee"] = _fmt(o.refunded)
            p["gmt_refund"] = self._ts(self._now())
        p["sign"] = signing.rsa2_sign(signing.sign_content(p, exclude=("sign", "sign_type")), self._alipay_key)
        if "tamper_amount" in self.faults:
            p["total_amount"] = _fmt(o.total_amount * 100)  # 签名后篡改：商户验签必须失败
        if "bad_signature" in self.faults:
            other = signing.generate_keypair()
            p["sign"] = signing.rsa2_sign(signing.sign_content(p, exclude=("sign", "sign_type")), other)
        return p

    def _deliver(self, o: _Order, params: dict[str, str]) -> None:
        if self.notifier is None or not o.notify_url or "drop_notify" in self.faults:
            return
        times = 2 if "duplicate_notify" in self.faults else 1
        for _ in range(times):
            o.notify_attempts += 1
            try:
                body = self.notifier(o.notify_url, dict(params))
            except Exception:  # 商户服务不可达：保持未确认，等待重投
                body = ""
            if body.strip() == "success":
                o.notify_acked = True

    def redeliver_pending(self) -> int:
        """模拟支付宝的通知重试：对已支付但商户未回 success 的订单重投一次。"""
        with self._lock:
            pending = [o for o in self._orders.values() if o.trade_no and not o.notify_acked]
        n = 0
        for o in pending:
            params = self._notify_params(o, MOCK_BUYER_ID)
            self._deliver(o, params)
            n += 1
        return n

    def trade_query(self, out_trade_no: str) -> TradeStatus:
        with self._lock:
            o = self._orders.get(out_trade_no)
            if o is None:
                raise GatewayError("ACQ.TRADE_NOT_EXIST", out_trade_no)
            self._expire_if_needed(o)
            return TradeStatus(o.out_trade_no, o.trade_no, o.status, _fmt(o.total_amount), _fmt(o.refunded))

    def refund(self, out_trade_no: str, refund_amount: str, out_request_no: str) -> RefundResult:
        amt = _money(refund_amount, lo="0.01", hi="100000000", what="refund_amount")
        if not out_request_no:
            raise GatewayError("ACQ.INVALID_PARAMETER", "out_request_no 必填")
        with self._lock:
            o = self._orders.get(out_trade_no)
            if o is None:
                raise GatewayError("ACQ.TRADE_NOT_EXIST", out_trade_no)
            if out_request_no in o.refunds:  # 幂等：同一退款请求号不重复退
                if o.refunds[out_request_no] != amt:
                    raise GatewayError("ACQ.REFUND_AMT_NOT_EQUAL_TOTAL", "同一 out_request_no 金额不一致")
                return RefundResult(out_trade_no, out_request_no, _fmt(amt), False)
            if o.status not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
                raise GatewayError("ACQ.TRADE_STATUS_ERROR", o.status)
            if o.refunded + amt > o.total_amount:
                raise GatewayError("ACQ.REFUND_AMT_NOT_EQUAL_TOTAL", "退款总额超过实付金额")
            o.refunds[out_request_no] = amt
            o.refunded += amt
            if o.refunded == o.total_amount:
                o.status = "TRADE_CLOSED"
            return RefundResult(out_trade_no, out_request_no, _fmt(amt), True)

    def refund_query(self, out_trade_no: str, out_request_no: str) -> dict[str, str]:
        """alipay.trade.fastpay.refund.query 的最小模拟。"""
        with self._lock:
            o = self._orders.get(out_trade_no)
            if o is None or out_request_no not in o.refunds:
                return {"out_trade_no": out_trade_no, "out_request_no": out_request_no, "refund_status": ""}
            return {"out_trade_no": out_trade_no, "out_request_no": out_request_no,
                    "refund_amount": _fmt(o.refunds[out_request_no]), "refund_status": "REFUND_SUCCESS"}

    def verify_notify(self, params: dict[str, str]) -> bool:
        return params.get("app_id") == self.app_id and signing.verify_notify(params, self.alipay_public_key)

    # ------------------------------------------------------------ 402 按量付费
    def precreate(self, req: PrecreateRequest) -> str:
        amount = _money(req.amount, lo="0.01", hi="50", what="amount")
        if req.currency != "CNY":
            raise GatewayError("ACQ.INVALID_PARAMETER", "currency 仅支持 CNY")
        for name in ("out_trade_no", "service_id", "goods_name", "pay_before"):
            if not getattr(req, name):
                raise GatewayError("ACQ.INVALID_PARAMETER", f"{name} 必填")
        pay_before = self._parse_pay_before(req.pay_before)
        with self._lock:
            existing = self._prepay_by_out.get(req.out_trade_no)
            if existing:
                pp = self._prepays[existing]
                if (pp.req.amount, pp.req.service_id, pp.req.resource_id) != (req.amount, req.service_id, req.resource_id):
                    raise GatewayError("ACQ.CONTEXT_INCONSISTENT", "同一 out_trade_no 参数不一致")
                return existing
            pid = "amtpp_v1_" + secrets.token_hex(16)
            self._prepays[pid] = _PrePay(pid, req, amount, pay_before)
            self._prepay_by_out[req.out_trade_no] = pid
            return pid

    def _parse_pay_before(self, s: str) -> int:
        for fmt in ("%Y-%m-%d %H:%M:%S",):
            try:
                return int(datetime.strptime(s, fmt).replace(tzinfo=CN).timestamp())
            except ValueError:
                pass
        try:
            d = datetime.fromisoformat(s)
        except ValueError as e:
            raise GatewayError("ACQ.INVALID_PARAMETER", f"pay_before 格式错误: {s!r}") from e
        if d.tzinfo is None:
            d = d.replace(tzinfo=CN)
        return int(d.timestamp())

    def describe_prepay(self, amt_pre_pay_id: str) -> dict[str, str]:
        """MOCK 钱包展示账单用（真实环境由支付宝 CLI 展示）。"""
        with self._lock:
            pp = self._prepays.get(amt_pre_pay_id)
            if pp is None:
                raise GatewayError("ACQ.TRADE_NOT_EXIST", amt_pre_pay_id)
            return {"amt_pre_pay_id": pp.amt_pre_pay_id, "out_trade_no": pp.req.out_trade_no,
                    "goods_name": pp.req.goods_name, "amount": _fmt(pp.amount), "currency": "CNY",
                    "seller_id": MOCK_SELLER_ID, "status": pp.status, "_mock": "true"}

    def agent_wallet_pay(self, amt_pre_pay_id: str, agent_token: str = "mock-agent") -> dict[str, str]:
        """模拟 @alipay/agent-payment（alipay-bot）在用户确认后付款，返回 Payment-Proof 三字段。"""
        with self._lock:
            pp = self._prepays.get(amt_pre_pay_id)
            if pp is None:
                raise GatewayError("ACQ.TRADE_NOT_EXIST", amt_pre_pay_id)
            if pp.status == "PAID":
                return {"payment_proof": pp.payment_proof, "trade_no": pp.trade_no, "client_session": pp.client_session}
            if pp.status == "CLOSED" or self._now() >= pp.pay_before:
                pp.status = "CLOSED"
                raise GatewayError("ACQ.TRADE_HAS_CLOSE", "已过支付截止时间")
            pp.status = "PAID"
            pp.trade_no = self._new_trade_no()
            pp.payment_proof = secrets.token_hex(32)
            pp.client_session = base64.b64encode(f"{agent_token}:{secrets.token_hex(8)}".encode()).decode()
            self._by_trade_no[pp.trade_no] = amt_pre_pay_id
            return {"payment_proof": pp.payment_proof, "trade_no": pp.trade_no, "client_session": pp.client_session}

    def _prepay_by_trade(self, trade_no: str) -> _PrePay | None:
        pid = self._by_trade_no.get(trade_no)
        return self._prepays.get(pid) if pid and pid in self._prepays else None

    def verify_proof(self, payment_proof: str, trade_no: str, client_session: str) -> ProofVerifyResult:
        with self._lock:
            pp = self._prepay_by_trade(trade_no)
            if pp is None:
                return ProofVerifyResult(False, "", "", trade_no, "", {"_mock": True, "reason": "TRADE_NOT_EXIST"})
            ok = (pp.status == "PAID" and secrets.compare_digest(pp.payment_proof, payment_proof)
                  and secrets.compare_digest(pp.client_session, client_session))
            return ProofVerifyResult(ok, _fmt(pp.amount), pp.req.out_trade_no, trade_no, pp.req.resource_id,
                                     {"_mock": True})

    def fulfillment_confirm(self, trade_no: str) -> bool:
        with self._lock:
            pp = self._prepay_by_trade(trade_no)
            if pp is None or pp.status != "PAID":
                return False
            pp.fulfilled += 1
            self.fulfillment_log.append(trade_no)
            return True
