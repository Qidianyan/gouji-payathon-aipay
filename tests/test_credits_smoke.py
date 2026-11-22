"""冒烟测试：aipay_kit.credits 次数包账本的基本可用性（完整矩阵见 tests/test_credits.py）。"""

import threading

import pytest

from aipay_kit.credits import CreditsError, CreditsLedger

NOW = 1_700_000_000
Y = NOW + 365 * 86400


def test_grant_and_balance_smoke():
    lg = CreditsLedger()
    try:
        g, created = lg.grant("ws1", "o1", 100, 9900, Y)
        assert created and g.grant_id == "o1" and g.credits == 100
        assert lg.balance("ws1", NOW) == 100
        # 同单号同参再入账：幂等，不新增
        g2, created2 = lg.grant("ws1", "o1", 100, 9900, Y)
        assert not created2 and g2 == g
    finally:
        lg.close()


def test_consume_and_refund_smoke():
    lg = CreditsLedger()
    try:
        lg.grant("ws1", "a", 10, 1000, Y)
        assert lg.consume("ws1", 4, NOW, "r1") == 6
        # request_id 幂等：不重复扣
        assert lg.consume("ws1", 4, NOW, "r1") == 6
        assert lg.get("a").used == 4
        assert lg.refundable("a", NOW) == (6, 600)
        assert lg.refund("a", NOW, "rf1") == (6, 600)
        assert lg.get("a").refunded == 6 and lg.balance("ws1", NOW) == 0
    finally:
        lg.close()


def test_insufficient_and_expiry_smoke():
    lg = CreditsLedger()
    try:
        lg.grant("ws1", "a", 3, 300, NOW - 1)  # 已过期
        assert lg.balance("ws1", NOW) == 0
        with pytest.raises(CreditsError) as e:
            lg.consume("ws1", 1, NOW, "r1")
        assert e.value.code == "INSUFFICIENT"
        assert lg.refundable("a", NOW) == (0, 0)  # 过期包不可退
        with pytest.raises(CreditsError) as e2:
            lg.refund("a", NOW, "rf1")
        assert e2.value.code == "NOTHING_TO_REFUND"
    finally:
        lg.close()


def test_concurrent_consume_smoke(tmp_path):
    lg = CreditsLedger(str(tmp_path / "c.db"))
    lg.grant("ws1", "a", 20, 200, Y)
    ok = []

    def work(k):
        try:
            lg.consume("ws1", 1, NOW, f"r{k}")
            ok.append(1)
        except CreditsError:
            pass

    ts = [threading.Thread(target=work, args=(k,)) for k in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 20 and lg.balance("ws1", NOW) == 0
    lg.close()


def test_file_reopen_smoke(tmp_path):
    p = str(tmp_path / "c.db")
    a = CreditsLedger(p)
    a.grant("ws1", "a", 10, 100, Y)
    a.consume("ws1", 3, NOW, "r1")
    a.close()
    b = CreditsLedger(p)
    try:
        assert b.balance("ws1", NOW) == 7
        assert b.get("a").used == 3
    finally:
        b.close()
