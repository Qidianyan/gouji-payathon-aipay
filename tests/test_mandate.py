"""Full tests for aipay_kit.mandate (SPEC: specs/T1_mandate.md). Written by bot after dsh T1t timed out."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

from aipay_kit.mandate import Decision, Mandate, PayRequest, Usage, evaluate, intent_score

NOW = 1_800_000_000


def make_mandate(**o) -> Mandate:
    base = dict(
        mandate_id="m1", user_ref="u1", merchant_ids=("facetry",), currency="CNY",
        max_amount_per_tx=D("10.00"), max_amount_total=D("30.00"),
        expires_at=NOW + 3600, not_before=NOW - 3600,
    )
    base.update(o)
    return Mandate(**base)


def make_req(**o) -> PayRequest:
    base = dict(merchant_id="facetry", amount=D("5.00"), currency="CNY", category="report",
                agent_keyid="kid-1", item_title="AI 可见度体检报告", now=NOW)
    base.update(o)
    return PayRequest(**base)


U0 = Usage(0, D("0"))


def test_allow_baseline():
    v = evaluate(make_mandate(), U0, make_req())
    assert v.decision is Decision.ALLOW and v.codes == ()
    assert v.remaining_amount == D("30.00") and v.remaining_uses is None


# (mandate overrides, usage, request overrides, expected code)
HIT = [
    ({"revoked": True}, U0, {}, "MANDATE_REVOKED"),
    ({}, U0, {"now": NOW - 3601}, "MANDATE_NOT_YET_VALID"),
    ({}, U0, {"now": NOW + 3600}, "MANDATE_EXPIRED"),
    ({"agent_keyid": "kid-1"}, U0, {"agent_keyid": "kid-evil"}, "MANDATE_AGENT_MISMATCH"),
    ({}, U0, {"merchant_id": "other"}, "MANDATE_MERCHANT_MISMATCH"),
    ({}, U0, {"currency": "USD"}, "MANDATE_CURRENCY"),
    ({"currency": "USD"}, U0, {"currency": "USD"}, "MANDATE_CURRENCY"),
    ({}, U0, {"amount": D("0")}, "MANDATE_INVALID_AMOUNT"),
    ({"categories": ("report",)}, U0, {"category": "giftcard"}, "MANDATE_CATEGORY"),
    ({}, U0, {"amount": D("10.01")}, "MANDATE_AMOUNT_PER_TX"),
    ({}, Usage(2, D("25.01")), {}, "MANDATE_AMOUNT_TOTAL"),
    ({"max_uses": 2}, Usage(2, D("1")), {}, "MANDATE_USES_EXHAUSTED"),
    ({"confirm_above": D("4.99")}, U0, {}, "CONFIRM_AMOUNT"),
    ({"intent_keywords": ("机票",)}, U0, {}, "CONFIRM_INTENT_DRIFT"),
]


@pytest.mark.parametrize("mo,usage,ro,code", HIT, ids=[h[3] + str(i) for i, h in enumerate(HIT)])
def test_each_rule_hits(mo, usage, ro, code):
    v = evaluate(make_mandate(**mo), usage, make_req(**ro))
    assert code in v.codes
    expected = Decision.NEEDS_CONFIRMATION if code.startswith("CONFIRM") else Decision.DENY
    assert v.decision is expected


# boundaries that must NOT fire
MISS = [
    ({}, U0, {"now": NOW - 3600}),                       # now == not_before
    ({}, U0, {"now": NOW + 3599}),                       # just before expiry
    ({"agent_keyid": None}, U0, {"agent_keyid": "any"}),
    ({"merchant_ids": ("a", "facetry")}, U0, {}),
    ({}, U0, {"amount": D("0.01")}),
    ({}, U0, {"amount": D("5.10")}),
    ({"categories": ()}, U0, {"category": "anything"}),
    ({}, U0, {"amount": D("10.00")}),                    # == per_tx
    ({}, Usage(2, D("20.00")), {"amount": D("10.00")}),  # used + amount == total
    ({"max_uses": 2}, Usage(1, D("1")), {}),
    ({"confirm_above": D("5.00")}, U0, {}),              # amount == confirm_above
    ({"intent_keywords": ("体检", "报告")}, U0, {}),
    ({"intent_keywords": ("体检", "机票")}, U0, {}),      # score 0.5 is not < 0.5
]


@pytest.mark.parametrize("mo,usage,ro", MISS)
def test_boundaries_allow(mo, usage, ro):
    v = evaluate(make_mandate(**mo), usage, make_req(**ro))
    assert v.decision is Decision.ALLOW, v.codes


def test_multiple_denies_in_table_order():
    m = make_mandate(revoked=True, agent_keyid="kid-1", categories=("report",))
    r = make_req(now=NOW + 99999, agent_keyid="x", merchant_id="y", currency="USD", category="z", amount=D("50"))
    v = evaluate(m, Usage(0, D("0")), r)
    assert v.codes == (
        "MANDATE_REVOKED", "MANDATE_EXPIRED", "MANDATE_AGENT_MISMATCH", "MANDATE_MERCHANT_MISMATCH",
        "MANDATE_CURRENCY", "MANDATE_CATEGORY", "MANDATE_AMOUNT_PER_TX", "MANDATE_AMOUNT_TOTAL",
    )


def test_deny_and_confirm_together_is_deny_with_deny_first():
    m = make_mandate(confirm_above=D("1"), intent_keywords=("机票",))
    v = evaluate(m, U0, make_req(merchant_id="other"))
    assert v.decision is Decision.DENY
    assert v.codes == ("MANDATE_MERCHANT_MISMATCH", "CONFIRM_AMOUNT", "CONFIRM_INTENT_DRIFT")


def test_only_confirms():
    v = evaluate(make_mandate(confirm_above=D("1"), intent_keywords=("机票",)), U0, make_req())
    assert v.decision is Decision.NEEDS_CONFIRMATION
    assert v.codes == ("CONFIRM_AMOUNT", "CONFIRM_INTENT_DRIFT")


def test_single_use():
    v = evaluate(make_mandate(single_use=True), Usage(1, D("5")), make_req())
    assert v.codes == ("MANDATE_USES_EXHAUSTED",) and v.remaining_uses == 0
    v = evaluate(make_mandate(single_use=True, max_uses=5), Usage(1, D("5")), make_req())
    assert "MANDATE_USES_EXHAUSTED" in v.codes
    v = evaluate(make_mandate(single_use=True), U0, make_req())
    assert v.decision is Decision.ALLOW and v.remaining_uses == 1


@pytest.mark.parametrize("amt", ["0.001", "0", "-1", "-0.01", "NaN", "Infinity", "-Infinity", "sNaN"])
def test_invalid_amounts(amt):
    v = evaluate(make_mandate(confirm_above=D("1")), U0, make_req(amount=D(amt)))
    assert v.decision is Decision.DENY
    assert "MANDATE_INVALID_AMOUNT" in v.codes
    if not D(amt).is_finite():
        assert v.codes == ("MANDATE_INVALID_AMOUNT",)


def test_intent_score():
    assert intent_score((), "x") == 1.0
    assert intent_score(("  ", "\t"), "x") == 1.0
    assert intent_score(("REPORT",), "ai report") == 1.0
    assert intent_score(("  报告 ",), "体检报告") == 1.0
    assert intent_score(("a", "b", "c", "d"), "xa-b") == 0.5
    assert intent_score(("机票",), "报告") == 0.0


def test_remaining_values():
    v = evaluate(make_mandate(max_uses=3), Usage(5, D("99")), make_req())
    assert v.remaining_amount == D("0") and v.remaining_uses == 0
    v = evaluate(make_mandate(), Usage(1, D("12.50")), make_req())
    assert v.remaining_amount == D("17.50") and v.remaining_uses is None


def test_frozen_dataclasses():
    m = make_mandate()
    with pytest.raises(Exception):
        m.revoked = True  # type: ignore[misc]
    assert replace(m, revoked=True).revoked
