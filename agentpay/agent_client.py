"""买方 Agent（脚本版，无 LLM）：发现 → 选品 → 签名请求 → 处理 402 → 钱包付款 → 带凭证重试 → 取凭证包。

wallet(amt_pre_pay_id) -> {"payment_proof","trade_no","client_session"}：
MOCK 下调用 /mock/alipay/a402/{id}/pay；真实环境由 @alipay/agent-payment（alipay-bot）在用户确认后完成。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from aipay_kit import proof as px
from agentpay import identity


@dataclass
class BuyResult:
    ok: bool
    status: int
    code: str = ""
    body: dict = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    out_trade_no: str = ""
    trade_no: str = ""


_RE_REQ = re.compile(r"^GET (\S+)( \+Payment-Proof)? -> (\d+)$")
_RE_BILL = re.compile(r"^402 bill (\S+) ¥(\S+) via (\S+)$")
_RE_PAY = re.compile(r"^wallet paid trade_no=(\S+)$")


def humanize(step: str) -> tuple[str, str, str]:
    """把 BuyerAgent 的技术步骤转成 (kind, 中文标题, 技术细节)，给时间线展示。"""
    m = _RE_REQ.match(step)
    if m:
        path, proof, code = m.group(1), bool(m.group(2)), int(m.group(3))
        sku = path.rsplit("/", 1)[-1]
        if proof:
            return ("request", f"带 Payment-Proof 重新请求 → {code}" + (" 交付" if code == 200 else " 被拒"), step)
        if code == 402:
            return ("request", f"签名请求 {sku} → 402 需要付款", step)
        if code == 409:
            return ("request", f"签名请求 {sku} → 409 暂停，需用户确认", step)
        return ("request", f"签名请求 {sku} → {code} 被商户拒绝", step)
    m = _RE_BILL.match(step)
    if m:
        return ("bill", f"402 账单 ¥{m.group(2)}（订单 {m.group(1)}）", f"amtPaymentLink {m.group(3)}")
    m = _RE_PAY.match(step)
    if m:
        return ("pay", "支付宝钱包付款成功（MOCK）", f"trade_no {m.group(1)}")
    return ("info", step, "")


class BuyerAgent:
    def __init__(self, http, keyid: str, key, mandate_id: str, wallet: Callable[[str], dict],
                 clock: Callable[[], float] = time.time) -> None:
        self.http, self.keyid, self.key, self.mandate_id = http, keyid, key, mandate_id
        self.wallet, self.clock = wallet, clock

    def discover(self) -> dict:
        return self.http.get("/.well-known/agent-service.json").json()

    def search(self, q: str = "") -> list[dict]:
        return self.http.get("/catalog", params={"q": q}).json()["items"]

    def _get(self, path: str, proof: str = "", confirmation_id: str = ""):
        created = int(self.clock())
        base = identity.signature_base("GET", path, self.mandate_id, proof, self.keyid, created)
        h = {"X-Mandate-Id": self.mandate_id, "Signature-Keyid": self.keyid, "Signature-Created": str(created),
             "Signature": identity.sign(self.key, base)}
        if proof:
            h["Payment-Proof"] = proof
        if confirmation_id:
            h["X-Confirmation-Id"] = confirmation_id
        return self.http.get(path, headers=h)

    def buy(self, sku: str, confirmation_id: str = "") -> BuyResult:
        path = f"/paid/resource/{sku}"
        r = self._get(path, confirmation_id=confirmation_id)
        steps = [f"GET {path} -> {r.status_code}"]
        if r.status_code != 402:
            b = r.json()
            return BuyResult(False, r.status_code, b.get("code", ""), b, steps)
        bill = px.parse_payment_needed(r.headers["Payment-Needed"])
        pid = r.headers["amtPaymentLink"]
        steps.append(f"402 bill {bill['out_trade_no']} ¥{bill['amount']} via {pid[:16]}…")
        p = self.wallet(pid)
        steps.append(f"wallet paid trade_no={p['trade_no']}")
        hdr = px.build_payment_proof(px.PaymentProof(p["payment_proof"], p["trade_no"], p["client_session"]))
        r2 = self._get(path, proof=hdr)
        steps.append(f"GET {path} +Payment-Proof -> {r2.status_code}")
        b2 = r2.json()
        return BuyResult(r2.status_code == 200, r2.status_code, b2.get("code", ""), b2, steps,
                         bill["out_trade_no"], p["trade_no"])
