"""Contract tests for the MOCK gateway (bot-written)."""

from decimal import Decimal

import pytest

from aipay_kit import signing
from aipay_kit.gateway.base import GatewayError, PagePayRequest, PrecreateRequest
from aipay_kit.gateway.mock import MockGateway


class Clock:
    def __init__(self, t=1_800_000_000):
        self.t = t

    def __call__(self):
        return self.t


class Merchant:
    """最小商户：验签 + 核对金额，回 success/failure。"""

    def __init__(self):
        self.gw = None
        self.orders = {}
        self.received = []

    def __call__(self, url, params):
        self.received.append(params)
        if not self.gw.verify_notify(params):
            return "failure"
        o = self.orders.get(params["out_trade_no"])
        if o is None or o != params["total_amount"]:
            return "failure"
        return "success"


@pytest.fixture
def env():
    m = Merchant(); c = Clock()
    gw = MockGateway(notifier=m, clock=c)
    m.gw = gw
    return gw, m, c


def req(no="o1", amt="9.90", **kw):
    return PagePayRequest(out_trade_no=no, total_amount=amt, subject="勾稽 100 次包", notify_url="http://m/notify", **kw)


def test_is_mock(env):
    assert env[0].is_mock is True


def test_page_pay_flow_notify_query(env):
    gw, m, _ = env
    m.orders["o1"] = "9.90"
    url = gw.page_pay(req())
    assert "/cashier/o1" in url
    assert gw.trade_query("o1").trade_status == "WAIT_BUYER_PAY"
    p = gw.buyer_pay("o1")
    assert len(p["trade_no"]) == 32 and p["trade_no"].isdigit()
    assert p["trade_status"] == "TRADE_SUCCESS" and p["sign_type"] == "RSA2"
    assert signing.verify_notify(p, gw.alipay_public_key)
    q = gw.trade_query("o1")
    assert q.trade_status == "TRADE_SUCCESS" and q.trade_no == p["trade_no"] and q.total_amount == "9.90"
    assert len(m.received) == 1


def test_notify_signature_excludes_sign_type(env):
    gw, m, _ = env
    m.orders["o1"] = "9.90"
    gw.page_pay(req()); p = gw.buyer_pay("o1")
    content = signing.sign_content(p, exclude=("sign", "sign_type"))
    assert "sign_type" not in content and signing.rsa2_verify(content, p["sign"], gw.alipay_public_key)


@pytest.mark.parametrize("amt", ["0", "0.001", "-1", "1e3", "abc", "100000000.01"])
def test_page_pay_bad_amount(env, amt):
    with pytest.raises(GatewayError) as e:
        env[0].page_pay(req(amt=amt))
    assert e.value.sub_code == "ACQ.INVALID_PARAMETER"


def test_page_pay_subject_rules(env):
    with pytest.raises(GatewayError):
        env[0].page_pay(PagePayRequest("o1", "1.00", "a/b", "http://m"))


def test_page_pay_idempotent_and_conflict(env):
    gw = env[0]
    assert gw.page_pay(req()) == gw.page_pay(req())
    with pytest.raises(GatewayError) as e:
        gw.page_pay(req(amt="1.00"))
    assert e.value.sub_code == "ACQ.CONTEXT_INCONSISTENT"


def test_expire_closes(env):
    gw, _, c = env
    gw.page_pay(req())
    c.t += 2 * 3600
    assert gw.trade_query("o1").trade_status == "TRADE_CLOSED"
    with pytest.raises(GatewayError) as e:
        gw.buyer_pay("o1")
    assert e.value.sub_code == "ACQ.TRADE_HAS_CLOSE"


def test_query_unknown(env):
    with pytest.raises(GatewayError) as e:
        env[0].trade_query("nope")
    assert e.value.sub_code == "ACQ.TRADE_NOT_EXIST"


def test_refund_partial_full_idempotent(env):
    gw, m, _ = env
    m.orders["o1"] = "9.90"
    gw.page_pay(req()); gw.buyer_pay("o1")
    r1 = gw.refund("o1", "3.30", "rf1")
    assert r1.fund_change and r1.refund_amount == "3.30"
    assert gw.refund("o1", "3.30", "rf1").fund_change is False
    with pytest.raises(GatewayError):
        gw.refund("o1", "9.00", "rf2")
    gw.refund("o1", "6.60", "rf3")
    q = gw.trade_query("o1")
    assert q.trade_status == "TRADE_CLOSED" and q.refund_amount == "9.90"
    assert gw.refund_query("o1", "rf3")["refund_status"] == "REFUND_SUCCESS"


def test_refund_unpaid_rejected(env):
    gw = env[0]
    gw.page_pay(req())
    with pytest.raises(GatewayError) as e:
        gw.refund("o1", "1.00", "rf1")
    assert e.value.sub_code == "ACQ.TRADE_STATUS_ERROR"


def test_fault_tamper_amount_fails_verify():
    m = Merchant(); gw = MockGateway(notifier=m, faults={"tamper_amount"}); m.gw = gw
    m.orders["o1"] = "9.90"
    gw.page_pay(req()); p = gw.buyer_pay("o1")
    assert not gw.verify_notify(p)


def test_fault_bad_signature():
    m = Merchant(); gw = MockGateway(notifier=m, faults={"bad_signature"}); m.gw = gw
    m.orders["o1"] = "9.90"
    gw.page_pay(req()); p = gw.buyer_pay("o1")
    assert not gw.verify_notify(p)


def test_fault_duplicate_and_drop_then_redeliver():
    m = Merchant(); gw = MockGateway(notifier=m, faults={"duplicate_notify"}); m.gw = gw
    m.orders["o1"] = "9.90"
    gw.page_pay(req()); gw.buyer_pay("o1")
    assert len(m.received) == 2
    m2 = Merchant(); gw2 = MockGateway(notifier=m2, faults={"drop_notify"}); m2.gw = gw2
    m2.orders["o1"] = "9.90"
    gw2.page_pay(req()); gw2.buyer_pay("o1")
    assert m2.received == []
    gw2.faults.clear()
    assert gw2.redeliver_pending() == 1 and len(m2.received) == 1
    assert gw2.redeliver_pending() == 0


def test_verify_notify_wrong_app_id(env):
    gw, m, _ = env
    m.orders["o1"] = "9.90"
    gw.page_pay(req()); p = gw.buyer_pay("o1")
    assert not gw.verify_notify({**p, "app_id": "other"})


def test_unknown_fault():
    with pytest.raises(ValueError):
        MockGateway(faults={"nope"})


# ---------------------------------------------------------------- 402
def pre(no="a1", amt="0.50", **kw):
    base = dict(out_trade_no=no, service_id="svc", goods_name="体检报告", amount=amt,
                pay_before="2027-02-01 12:00:00", resource_id="/paid/resource/r1")
    base.update(kw)
    return PrecreateRequest(**base)


def test_402_flow(env):
    gw = env[0]
    pid = gw.precreate(pre())
    assert pid.startswith("amtpp_v1_") and gw.precreate(pre()) == pid
    assert gw.describe_prepay(pid)["amount"] == "0.50"
    proof = gw.agent_wallet_pay(pid)
    assert gw.agent_wallet_pay(pid) == proof
    v = gw.verify_proof(proof["payment_proof"], proof["trade_no"], proof["client_session"])
    assert v.active and v.amount == "0.50" and v.out_trade_no == "a1" and v.resource_id == "/paid/resource/r1"
    assert gw.fulfillment_confirm(proof["trade_no"])


def test_402_wrong_proof_or_session(env):
    gw = env[0]
    proof = gw.agent_wallet_pay(gw.precreate(pre()))
    assert not gw.verify_proof("0" * 64, proof["trade_no"], proof["client_session"]).active
    assert not gw.verify_proof(proof["payment_proof"], proof["trade_no"], "x").active
    assert not gw.verify_proof(proof["payment_proof"], "1" * 32, proof["client_session"]).active


def test_402_unpaid_not_active_and_no_fulfil(env):
    gw = env[0]
    gw.precreate(pre())
    assert not gw.fulfillment_confirm("1" * 32)


@pytest.mark.parametrize("amt", ["50.01", "0", "100"])
def test_402_amount_limit(env, amt):
    with pytest.raises(GatewayError):
        env[0].precreate(pre(amt=amt))


def test_402_ok_at_50(env):
    assert env[0].precreate(pre(amt="50"))


def test_402_pay_before(env):
    gw, _, c = env
    pid = gw.precreate(pre(pay_before="2027-02-01T12:00:00+08:00"))
    c.t = 1_900_000_000
    with pytest.raises(GatewayError) as e:
        gw.agent_wallet_pay(pid)
    assert e.value.sub_code == "ACQ.TRADE_HAS_CLOSE"


def test_402_conflict(env):
    gw = env[0]
    gw.precreate(pre())
    with pytest.raises(GatewayError):
        gw.precreate(pre(amt="1.00"))


def test_402_currency(env):
    with pytest.raises(GatewayError):
        env[0].precreate(pre(currency="USD"))
