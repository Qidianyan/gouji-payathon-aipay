"""Acceptance tests for aipay_kit.credits (SPEC specs/T3_credits.md). Written by bot."""

import threading

import pytest

from aipay_kit.credits import CreditsError, CreditsLedger

NOW = 1_800_000_000
Y = NOW + 365 * 86400


@pytest.fixture
def lg():
    x = CreditsLedger()
    yield x
    x.close()


def code(exc):
    return exc.value.code


def test_grant_idempotent(lg):
    g, c = lg.grant("ws1", "o1", 100, 9900, Y)
    assert c and g.credits == 100 and g.used == 0 and g.refunded == 0
    g2, c2 = lg.grant("ws1", "o1", 100, 9900, Y)
    assert not c2 and g2 == g
    assert lg.balance("ws1", NOW) == 100


@pytest.mark.parametrize("args", [("ws1", "o1", 101, 9900, Y), ("ws2", "o1", 100, 9900, Y), ("ws1", "o1", 100, 1, Y)])
def test_grant_conflict(lg, args):
    lg.grant("ws1", "o1", 100, 9900, Y)
    with pytest.raises(CreditsError) as e:
        lg.grant(*args)
    assert code(e) == "GRANT_CONFLICT"


@pytest.mark.parametrize("credits,amt", [(0, 100), (-1, 100), (10, -1)])
def test_grant_invalid(lg, credits, amt):
    with pytest.raises(CreditsError) as e:
        lg.grant("ws1", "o1", credits, amt, Y)
    assert code(e) == "INVALID"


def test_balance_multi_and_expiry(lg):
    lg.grant("ws1", "a", 10, 100, NOW + 10)
    lg.grant("ws1", "b", 20, 200, Y)
    lg.grant("ws2", "c", 5, 50, Y)
    assert lg.balance("ws1", NOW) == 30
    assert lg.balance("ws1", NOW + 10) == 20
    assert lg.balance("ws2", NOW) == 5


def test_consume_fifo(lg):
    lg.grant("ws1", "late", 10, 100, Y)
    lg.grant("ws1", "early", 5, 50, NOW + 100)
    assert lg.consume("ws1", 7, NOW, "r1") == 8
    assert lg.get("early").used == 5 and lg.get("late").used == 2


def test_consume_insufficient_no_partial(lg):
    lg.grant("ws1", "a", 3, 30, Y)
    with pytest.raises(CreditsError) as e:
        lg.consume("ws1", 4, NOW, "r1")
    assert code(e) == "INSUFFICIENT"
    assert lg.get("a").used == 0 and lg.balance("ws1", NOW) == 3


def test_consume_request_idempotent(lg):
    lg.grant("ws1", "a", 3, 30, Y)
    assert lg.consume("ws1", 1, NOW, "r1") == 2
    assert lg.consume("ws1", 1, NOW, "r1") == 2
    assert lg.get("a").used == 1


@pytest.mark.parametrize("n", [0, -2])
def test_consume_invalid(lg, n):
    lg.grant("ws1", "a", 3, 30, Y)
    with pytest.raises(CreditsError) as e:
        lg.consume("ws1", n, NOW, "r")
    assert code(e) == "INVALID"


def test_consume_skips_expired(lg):
    lg.grant("ws1", "old", 5, 50, NOW - 1)
    with pytest.raises(CreditsError):
        lg.consume("ws1", 1, NOW, "r1")


def test_refundable_and_refund(lg):
    lg.grant("ws1", "a", 3, 1000, Y)
    lg.consume("ws1", 1, NOW, "r1")
    assert lg.refundable("a", NOW) == (2, 666)
    assert lg.refund("a", NOW, "rf1") == (2, 666)
    assert lg.refund("a", NOW, "rf1") == (2, 666)
    assert lg.get("a").refunded == 2 and lg.balance("ws1", NOW) == 0
    with pytest.raises(CreditsError) as e:
        lg.refund("a", NOW, "rf2")
    assert code(e) == "NOTHING_TO_REFUND"


def test_refund_not_found(lg):
    with pytest.raises(CreditsError) as e:
        lg.refund("nope", NOW, "rf1")
    assert code(e) == "NOT_FOUND"


def test_expired_not_refundable(lg):
    lg.grant("ws1", "a", 3, 300, NOW - 1)
    assert lg.refundable("a", NOW) == (0, 0)
    with pytest.raises(CreditsError) as e:
        lg.refund("a", NOW, "rf1")
    assert code(e) == "NOTHING_TO_REFUND"


def test_get_none(lg):
    assert lg.get("x") is None


def test_reopen(tmp_path):
    p = str(tmp_path / "c.db")
    a = CreditsLedger(p); a.grant("ws1", "a", 10, 100, Y); a.consume("ws1", 3, NOW, "r1"); a.close()
    b = CreditsLedger(p)
    assert b.balance("ws1", NOW) == 7 and b.consume("ws1", 3, NOW, "r1") == 7
    b.close()


def test_concurrent_no_oversell(tmp_path):
    lg = CreditsLedger(str(tmp_path / "t.db"))
    lg.grant("ws1", "a", 50, 500, Y)
    ok, fail = [], []

    def work(k):
        for j in range(10):
            try:
                lg.consume("ws1", 1, NOW, f"r{k}-{j}"); ok.append(1)
            except CreditsError:
                fail.append(1)

    ts = [threading.Thread(target=work, args=(k,)) for k in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(ok) == 50 and len(fail) == 50 and lg.balance("ws1", NOW) == 0
    lg.close()


def test_refunded_credits_cannot_be_consumed(lg):
    lg.grant("ws1", "a", 3, 300, Y)
    lg.refund("a", NOW, "rf1")
    with pytest.raises(CreditsError) as e:
        lg.consume("ws1", 1, NOW, "r1")
    assert code(e) == "INSUFFICIENT"
    assert lg.balance("ws1", NOW) == 0 and lg.get("a").used == 0
