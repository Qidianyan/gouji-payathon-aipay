"""Pure mandate (authorization) evaluation engine.

Given a mandate, its accumulated usage and one pending pay request, decide
ALLOW / DENY / NEEDS_CONFIRMATION plus every rule code that fired.

Deliberately pure: no signature verification (that lives in aipay_kit.signing),
no database, no network. All money math uses Decimal only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

ZERO = Decimal("0")


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"


@dataclass(frozen=True)
class Mandate:
    mandate_id: str
    user_ref: str
    merchant_ids: tuple[str, ...]        # 允许的商户
    currency: str                         # 只支持 "CNY"
    max_amount_per_tx: Decimal            # 单笔上限（元）
    max_amount_total: Decimal             # 累计上限（元）
    expires_at: int                       # unix 秒
    not_before: int = 0                   # unix 秒
    categories: tuple[str, ...] = ()      # 空 = 不限类目
    max_uses: int | None = None           # None = 不限次数
    single_use: bool = False              # True 等价 max_uses=1
    agent_keyid: str | None = None        # 绑定 agent；None = 不限
    confirm_above: Decimal | None = None  # 单笔金额 > 此值需二次确认
    intent_keywords: tuple[str, ...] = () # 用户意图关键词（小写比较）；空 = 不做意图检查
    revoked: bool = False


@dataclass(frozen=True)
class Usage:
    used_count: int          # 已成功支付的笔数
    used_amount: Decimal     # 已成功支付的累计金额


@dataclass(frozen=True)
class PayRequest:
    merchant_id: str
    amount: Decimal
    currency: str
    category: str
    agent_keyid: str
    item_title: str          # 商品标题，用于意图检查
    now: int                 # unix 秒


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    codes: tuple[str, ...]   # 所有命中的规则码（DENY 码在前，按规则表顺序）
    remaining_amount: Decimal
    remaining_uses: int | None


def intent_score(keywords: tuple[str, ...], title: str) -> float:
    """命中关键词数 / 关键词总数；keywords 为空视为不做意图检查，返回 1.0。"""
    kws = [k.strip().lower() for k in keywords if k.strip()]  # 空白关键词忽略（否则恒命中）
    if not kws:
        return 1.0
    haystack = title.lower()
    hits = sum(1 for k in kws if k in haystack)
    return hits / len(kws)


def _effective_max_uses(m: Mandate) -> int | None:
    """single_use=True 等价于 max_uses=1。"""
    if m.single_use:
        return 1
    return m.max_uses


def _decimal_places(amount: Decimal) -> int:
    """Decimal 存储的小数位数（1.000 记为 3 位）。"""
    exp = amount.as_tuple().exponent
    if not isinstance(exp, int):  # NaN / sNaN / Infinity
        return 99
    return -exp if exp < 0 else 0


def evaluate(m: Mandate, usage: Usage, req: PayRequest) -> Verdict:
    """按规则表顺序逐条检查，汇总命中的码并给出最终决策。"""
    denies: list[str] = []
    confirms: list[str] = []

    # ---- DENY 规则（按规则表顺序） ----
    if m.revoked:
        denies.append("MANDATE_REVOKED")
    if req.now < m.not_before:
        denies.append("MANDATE_NOT_YET_VALID")
    if req.now >= m.expires_at:
        denies.append("MANDATE_EXPIRED")
    if m.agent_keyid is not None and m.agent_keyid != req.agent_keyid:
        denies.append("MANDATE_AGENT_MISMATCH")
    if req.merchant_id not in m.merchant_ids:
        denies.append("MANDATE_MERCHANT_MISMATCH")
    if req.currency != m.currency or m.currency != "CNY":
        denies.append("MANDATE_CURRENCY")
    if not req.amount.is_finite() or req.amount <= 0 or _decimal_places(req.amount) > 2:
        denies.append("MANDATE_INVALID_AMOUNT")
    if m.categories and req.category not in m.categories:
        denies.append("MANDATE_CATEGORY")
    finite = req.amount.is_finite()
    if finite and req.amount > m.max_amount_per_tx:
        denies.append("MANDATE_AMOUNT_PER_TX")
    if finite and usage.used_amount + req.amount > m.max_amount_total:
        denies.append("MANDATE_AMOUNT_TOTAL")
    max_uses = _effective_max_uses(m)
    if max_uses is not None and usage.used_count >= max_uses:
        denies.append("MANDATE_USES_EXHAUSTED")

    # ---- CONFIRM 规则 ----
    if finite and m.confirm_above is not None and req.amount > m.confirm_above:
        confirms.append("CONFIRM_AMOUNT")
    if m.intent_keywords and intent_score(m.intent_keywords, req.item_title) < 0.5:
        confirms.append("CONFIRM_INTENT_DRIFT")

    if denies:
        decision = Decision.DENY
    elif confirms:
        decision = Decision.NEEDS_CONFIRMATION
    else:
        decision = Decision.ALLOW

    remaining_uses = None
    if max_uses is not None:
        remaining_uses = max(0, max_uses - usage.used_count)

    return Verdict(
        decision=decision,
        codes=tuple(denies) + tuple(confirms),  # DENY 码在前，同类型按规则表顺序
        remaining_amount=max(ZERO, m.max_amount_total - usage.used_amount),  # 不扣本次
        remaining_uses=remaining_uses,
    )
