"""Vibe Pay（勾稽次数包）end-to-end on MOCK (bot-written)."""

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from aipay_kit import signing
from aipay_kit.audit import verify_jsonl
from aipay_kit.gateway.mock import MockGateway
from aipay_kit.gateway.mock_server import build_app as build_mock_app
from aipay_kit.gateway.mock_server import build_router
from aipay_kit.gateway.openapi import OpenApiGateway
from vibepay.app import create_app, load_pricing

P100 = next(x for x in load_pricing()["packs"] if x["id"] == "p100")
P100_REFUND_90 = f"{int(round(float(P100['price']) * 100)) * 90 // P100['credits'] / 100:.2f}"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000

    def __call__(self):
        return self.t


@pytest.fixture(params=["inprocess", "openapi-http"])
def world(request):
    clock = Clock()
    holder = {}
    gw = MockGateway(clock=clock, notifier=lambda url, p: holder["http"].post(urlparse(url).path, data=p).text)
    if request.param == "inprocess":
        app = create_app(gw, clock=clock, mock_router=build_router(gw, {}))
        http = TestClient(app)
        pay = lambda out, url: http.post(f"/mock/alipay/cashier/{out}/pay", follow_redirects=False)
    else:
        mk = signing.generate_keypair()
        mock_http = TestClient(build_mock_app(gw, {gw.app_id: signing.public_pem(mk.public_key()).decode()}), base_url="http://mock")
        cli = OpenApiGateway(gw.app_id, signing.private_pem(mk).decode(), signing.public_pem(gw.alipay_public_key).decode(),
                             "http://mock/mock/alipay/gateway.do", client=mock_http, clock=clock, is_mock=True)
        app = create_app(cli, clock=clock)
        http = TestClient(app)

        def pay(out, url):
            u = urlparse(url)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            assert mock_http.get(u.path, params=q, follow_redirects=False).status_code == 302
            return mock_http.post(f"/mock/alipay/cashier/{out}/pay", follow_redirects=False)
    holder["http"] = http
    return {"http": http, "gw": gw, "pay": pay, "clock": clock, "app": app}


def buy(w, pack="p100", acct="ws1"):
    r = w["http"].post("/billing/checkout", json={"account": acct, "pack_id": pack}).json()
    return r


def test_pricing_config_valid():
    p = load_pricing()
    assert {x["id"] for x in p["packs"]} == {"p100", "p500", "p2000"}
    unit = [float(x["price"]) / x["credits"] for x in sorted(p["packs"], key=lambda x: x["credits"])]
    assert unit == sorted(unit, reverse=True) and unit[0] <= float(p["per_call_price"])  # 大包更便宜


def test_pricing_rejects_402_over_50(tmp_path):
    import json
    bad = load_pricing()
    bad["per_call_price"] = "50.01"
    f = tmp_path / "p.json"
    f.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        load_pricing(f)


def test_full_chain_buy_unlock_consume_refund(world):
    http = world["http"]
    assert http.post("/gouji/recognize", json={"account": "ws1", "request_id": "r0"}).status_code == 402
    c = buy(world)
    assert c["_mock"] is True and c["amount"] == P100["price"]
    assert http.get(f"/billing/orders/{c['out_trade_no']}").json()["status"] == "PENDING"
    world["pay"](c["out_trade_no"], c["pay_url"])
    o = http.get(f"/billing/orders/{c['out_trade_no']}").json()
    assert o["status"] == "PAID" and len(o["trade_no"]) == 32
    assert http.get("/billing/balance", params={"account": "ws1"}).json()["balance"] == 100
    for i in range(10):
        r = http.post("/gouji/recognize", json={"account": "ws1", "request_id": f"r{i+1}", "doc_name": "增值税发票"})
        assert r.status_code == 200
    assert http.post("/gouji/recognize", json={"account": "ws1", "request_id": "r1"}).json()["balance"] == 90  # 幂等
    rf = http.post(f"/billing/orders/{c['out_trade_no']}/refund", json={"account": "ws1"}).json()
    assert rf["refunded_credits"] == 90 and rf["refund_amount"] == P100_REFUND_90 and rf["balance"] == 0
    s = world["gw"].trade_query(c["out_trade_no"])
    assert s.refund_amount == P100_REFUND_90 and s.trade_status == "TRADE_SUCCESS"
    again = http.post(f"/billing/orders/{c['out_trade_no']}/refund", json={"account": "ws1"})
    assert again.status_code == 409
    assert verify_jsonl(http.get("/audit/export").text).ok


def test_trial_once(world):
    http = world["http"]
    assert http.post("/billing/trial", json={"account": "ws9"}).json() == {"account": "ws9", "granted": True, "balance": 20}
    assert http.post("/billing/trial", json={"account": "ws9"}).json()["granted"] is False


def test_duplicate_notify_grants_once():
    clock = Clock(); holder = {}
    gw = MockGateway(clock=clock, faults={"duplicate_notify"},
                     notifier=lambda url, p: holder["h"].post(urlparse(url).path, data=p).text)
    http = TestClient(create_app(gw, clock=clock, mock_router=build_router(gw, {}))); holder["h"] = http
    c = http.post("/billing/checkout", json={"account": "ws1", "pack_id": "p100"}).json()
    http.post(f"/mock/alipay/cashier/{c['out_trade_no']}/pay")
    assert http.get("/billing/balance", params={"account": "ws1"}).json()["balance"] == 100
    events = [l for l in http.get("/audit/export").text.splitlines() if '"notify.duplicate"' in l]
    assert len(events) == 1


@pytest.mark.parametrize("fault,why", [("tamper_amount", "SIGNATURE_INVALID"), ("bad_signature", "SIGNATURE_INVALID")])
def test_forged_notify_rejected(fault, why):
    clock = Clock(); holder = {}
    gw = MockGateway(clock=clock, faults={fault}, notifier=lambda url, p: holder["h"].post(urlparse(url).path, data=p).text)
    http = TestClient(create_app(gw, clock=clock, mock_router=build_router(gw, {}))); holder["h"] = http
    c = http.post("/billing/checkout", json={"account": "ws1", "pack_id": "p100"}).json()
    http.post(f"/mock/alipay/cashier/{c['out_trade_no']}/pay")
    assert http.get("/billing/balance", params={"account": "ws1"}).json()["balance"] == 0
    assert why in http.get("/audit/export").text
    assert not gw._orders[c["out_trade_no"]].notify_acked


def test_forged_notify_direct_post(world):
    http = world["http"]
    c = buy(world)
    fake = {"out_trade_no": c["out_trade_no"], "trade_status": "TRADE_SUCCESS", "total_amount": P100["price"],
            "app_id": world["gw"].app_id, "sign_type": "RSA2", "sign": "AAAA"}
    assert http.post("/alipay/notify", data=fake).text == "failure"
    assert http.get("/billing/balance", params={"account": "ws1"}).json()["balance"] == 0


def test_dropped_notify_recovered_by_query():
    clock = Clock(); holder = {}
    gw = MockGateway(clock=clock, faults={"drop_notify"}, notifier=lambda url, p: holder["h"].post(urlparse(url).path, data=p).text)
    http = TestClient(create_app(gw, clock=clock, mock_router=build_router(gw, {}))); holder["h"] = http
    c = http.post("/billing/checkout", json={"account": "ws1", "pack_id": "p500"}).json()
    http.post(f"/mock/alipay/cashier/{c['out_trade_no']}/pay")
    assert http.get(f"/billing/orders/{c['out_trade_no']}").json()["status"] == "PAID"   # 主动查询补单
    assert http.get("/billing/balance", params={"account": "ws1"}).json()["balance"] == 500
    gw.faults.clear(); gw.redeliver_pending()                                              # 迟到通知不重复入账
    assert http.get("/billing/balance", params={"account": "ws1"}).json()["balance"] == 500


def test_return_url_does_not_credit(world):
    http = world["http"]
    c = buy(world)
    assert "确认中" in http.get("/billing/return", params={"out_trade_no": c["out_trade_no"]}).text
    assert http.get("/billing/balance", params={"account": "ws1"}).json()["balance"] == 0


def test_unpaid_expired_order_closed(world):
    http = world["http"]
    c = buy(world)
    world["clock"].t += 31 * 60
    assert http.get(f"/billing/orders/{c['out_trade_no']}").json()["status"] == "CLOSED"


def test_refund_rules(world):
    http = world["http"]
    c = buy(world)
    assert http.post(f"/billing/orders/{c['out_trade_no']}/refund", json={"account": "ws1"}).status_code == 409  # 未付
    world["pay"](c["out_trade_no"], c["pay_url"])
    http.get(f"/billing/orders/{c['out_trade_no']}")
    assert http.post(f"/billing/orders/{c['out_trade_no']}/refund", json={"account": "evil"}).status_code == 404
    assert http.post("/billing/consume", json={"account": "ws1", "request_id": "x", "n": 100}).json()["balance"] == 0
    assert http.post(f"/billing/orders/{c['out_trade_no']}/refund", json={"account": "ws1"}).json()["code"] == "NOTHING_TO_REFUND"


def test_bad_checkout(world):
    assert world["http"].post("/billing/checkout", json={"account": "ws1", "pack_id": "nope"}).status_code == 400


def test_packs_endpoint(world):
    p = world["http"].get("/billing/packs").json()
    assert p["gateway_mode"] == "mock" and p["trial_credits"] == 20


def test_orders_list_and_return_redirect():
    from aipay_kit.gateway.mock import MockGateway as G
    gw = G()
    app = create_app(gw, mock_router=build_router(gw, {}), return_page="/order.html")
    gw.notifier = lambda url, p: http.post(urlparse(url).path, data=p).text
    http = TestClient(app)
    c = http.post("/billing/checkout", json={"account": "a1", "pack_id": "p100"}).json()
    assert http.get("/billing/orders", params={"account": "a1"}).json()["orders"][0]["out_trade_no"] == c["out_trade_no"]
    assert http.get("/billing/orders", params={"account": "zz"}).json()["orders"] == []
    r = http.get("/billing/return", params={"out_trade_no": c["out_trade_no"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/order.html?order={c['out_trade_no']}"
    r = http.get("/billing/return", params={"out_trade_no": "<script>x</script>"})
    assert "<script>" not in r.text


def test_trial_again_after_time_passes_is_not_500(world):
    """回归：时钟前进后再领试用，到期时间不同 → 账本 GRANT_CONFLICT；接口应答“已领过”而不是 500。"""
    http, clock = world["http"], world["clock"]
    assert http.post("/billing/trial", json={"account": "ws10"}).json()["granted"] is True
    clock.t += 3600
    r = http.post("/billing/trial", json={"account": "ws10"})
    assert r.status_code == 200 and r.json() == {"account": "ws10", "granted": False, "balance": 20}
