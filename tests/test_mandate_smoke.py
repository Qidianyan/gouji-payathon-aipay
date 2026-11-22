"""冒烟测试：完整规则矩阵由另一个任务覆盖。"""

from decimal import Decimal

from aipay_kit.mandate import (
    Decision,
    Mandate,
    PayRequest,
    Usage,
    evaluate,
    intent_score,
)


def _mandate(**kw: object) -> Mandate:
    base: dict[str, object] = dict(
        mandate_id="m1",
        user_ref="u1",
        merchant_ids=("merchant_a",),
        currency="CNY",
        max_amount_per_tx=Decimal("100.00"),
        max_amount_total=Decimal("300.00"),
        expires_at=2000,
        not_before=1000,
    )
    base.update(kw)
    return Mandate(**base)  # type: ignore[arg-type]


def _req(**kw: object) -> PayRequest:
    base: dict[str, object] = dict(
        merchant_id="merchant_a",
        amount=Decimal("50.00"),
        currency="CNY",
        category="food",
        agent_keyid="agent_1",
        item_title="有机咖啡豆",
        now=1500,
    )
    base.update(kw)
    return PayRequest(**base)  # type: ignore[arg-type]


def test_allow_and_remaining():
    v = evaluate(_mandate(max_uses=3), Usage(1, Decimal("30.00")), _req())
    assert v.decision == Decision.ALLOW
    assert v.codes == ()
    assert v.remaining_amount == Decimal("270.00")  # 不扣本次
    assert v.remaining_uses == 2


def test_multiple_denies_in_rule_order():
    m = _mandate(revoked=True, agent_keyid="agent_2", categories=("book",))
    v = evaluate(m, Usage(0, Decimal("0")), _req(amount=Decimal("10.001")))
    assert v.decision == Decision.DENY
    assert v.codes == (
        "MANDATE_REVOKED",
        "MANDATE_AGENT_MISMATCH",
        "MANDATE_INVALID_AMOUNT",
        "MANDATE_CATEGORY",
    )


def test_confirm_codes_and_decision():
    m = _mandate(confirm_above=Decimal("20.00"), intent_keywords=("coffee", "organic"))
    v = evaluate(m, Usage(0, Decimal("0")), _req(item_title="意式浓缩咖啡机"))
    assert v.decision == Decision.NEEDS_CONFIRMATION
    assert v.codes == ("CONFIRM_AMOUNT", "CONFIRM_INTENT_DRIFT")


def test_intent_score_and_single_use():
    assert intent_score((" Coffee ", "BEANS"), "Hot Coffee Beans") == 1.0
    assert intent_score(("coffee", "beans"), "Hot Coffee Beans") == 1.0
    assert intent_score(("coffee", "tea", "milk", "sugar"), "意式浓缩") == 0.0
    assert intent_score((), "任意标题") == 1.0

    v = evaluate(_mandate(single_use=True), Usage(1, Decimal("10.00")), _req())
    assert "MANDATE_USES_EXHAUSTED" in v.codes
    assert v.remaining_uses == 0
