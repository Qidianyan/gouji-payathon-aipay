"""演示服务 API 测试（bot-written）：前端用到的每个接口和页面都在这里先跑通。"""

from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from demo.agentpay_demo import SCENARIOS, create_demo_app
from demo.gouji_demo import create_gouji_demo_app


@pytest.fixture
def ap():
    return TestClient(create_demo_app(llm_factory=lambda: None))


def test_agentpay_state_and_pages(ap):
    s = ap.get("/api/state").json()
    assert s["mock"] and s["planner"] == "rule" and s["mandate"]["mandate_id"] == "m-demo"
    assert "signature" not in s["mandate"] and len(s["scenarios"]) == 9
    for path in ["/", "/receipt.html", "/terms", "/privacy", "/refund", "/static/app.css", "/static/agentpay.js",
                 "/static/vendor/phosphor/style.css", "/static/elidot/tokens.css", "/.well-known/agent-service.json"]:
        assert ap.get(path).status_code == 200, path
    assert ap.get("/nope.html").status_code == 404
    assert "MOCK" in ap.get("/").text


def test_agentpay_run_delivers_and_receipt_verifies(ap):
    r = ap.post("/api/run", json={"task": "品牌在 AI 里的可见度报告", "use_llm": True}).json()
    assert r["state"] == "delivered" and r["sku"] == "geo-report"
    tn = r["receipt"]["trade_no"]
    rc = ap.get(f"/api/receipts/{tn}").json()
    assert rc["signature_valid"] is True and rc["receipt"]["_mock"] is True
    assert ap.get("/api/receipts/nope").status_code == 404
    assert ap.get("/api/state").json()["mandate"]["used_count"] == 1


def test_agentpay_drift_confirm_and_reject(ap):
    r = ap.post("/api/run", json={"task": "检查商品目录结账字段"}).json()
    assert r["state"] == "needs_confirmation"
    ok = ap.post("/api/confirm", json={"confirmation_id": r["confirmation_id"], "approve": True}).json()
    assert ok["state"] == "delivered" and ok["sku"] == "catalog-audit"
    assert ap.post("/api/confirm", json={"confirmation_id": r["confirmation_id"]}).status_code == 404  # 一次性
    r2 = ap.post("/api/run", json={"task": "检查商品目录结账字段"}).json()
    no = ap.post("/api/confirm", json={"confirmation_id": r2["confirmation_id"], "approve": False}).json()
    assert no["state"] == "rejected"


def test_agentpay_overreach_blocked(ap):
    r = ap.post("/api/run", json={"task": "买一张 50 元礼品卡"}).json()
    assert r["state"] in {"blocked", "no_match"}
    r = ap.post("/api/run", json={"task": "礼品卡"}).json()
    assert r["state"] == "blocked"


@pytest.mark.parametrize("n", range(1, 10))
def test_each_scenario_blocks(ap, n):
    r = ap.post(f"/api/scenarios/{n}").json()
    assert r["blocked"] is True, r
    expect = next(s[3] for s in SCENARIOS if s[0] == n)
    assert r["code"] == expect


def test_all_scenarios_twice_and_audit_chain(ap):
    for _ in range(2):
        for n in range(1, 10):
            assert ap.post(f"/api/scenarios/{n}").json()["blocked"]
    v = ap.get("/api/audit/verify").json()
    assert v["ok"] and v["count"] > 20
    e = ap.get("/api/audit/export", params={"format": "csv"})
    assert e.headers["content-disposition"].endswith('agentpay-audit.csv"') and e.text.startswith("seq,")
    assert ap.post("/api/scenarios/10").status_code == 404


def test_scenario6_confirm_flow(ap):
    r = ap.post("/api/scenarios/6").json()
    ok = ap.post("/api/confirm", json={"confirmation_id": r["confirmation_id"]}).json()
    assert ok["state"] == "delivered"


def test_reset(ap):
    ap.post("/api/run", json={"task": "可见度报告"})
    assert ap.get("/api/state").json()["mandate"]["used_count"] == 1
    ap.post("/api/reset")
    assert ap.get("/api/state").json()["mandate"]["used_count"] == 0


# ---------------- 勾稽

def test_gouji_full_browser_like_flow():
    c = TestClient(create_gouji_demo_app("http://testserver"))
    for path in ["/", "/checkout.html", "/order.html", "/app.html", "/terms", "/privacy", "/refund"]:
        assert c.get(path).status_code == 200, path
    assert c.post("/gouji/recognize", json={"account": "demo-ws", "request_id": "x"}).status_code == 402
    co = c.post("/billing/checkout", json={"account": "demo-ws", "pack_id": "p100"}).json()
    u = urlparse(co["pay_url"])
    r = c.get(f"{u.path}?{u.query}", follow_redirects=False)        # 网关 → 收银台
    assert r.status_code == 302
    cash = c.get(r.headers["location"])
    assert "MOCK" in cash.text and "这不是支付宝官方收银台" in cash.text
    r = c.post(f"/mock/alipay/cashier/{co['out_trade_no']}/pay", follow_redirects=False)
    assert r.status_code == 303
    r = c.get(urlparse(r.headers["location"]).path + "?" + urlparse(r.headers["location"]).query, follow_redirects=False)
    assert r.headers["location"] == f"/order.html?order={co['out_trade_no']}"
    assert c.get(f"/billing/orders/{co['out_trade_no']}").json()["status"] == "PAID"
    assert c.post("/gouji/recognize", json={"account": "demo-ws", "request_id": "r1"}).json()["balance"] == 99
    rf = c.post(f"/billing/orders/{co['out_trade_no']}/refund", json={"account": "demo-ws"}).json()
    assert rf["refunded_credits"] == 99
    assert c.get("/billing/orders", params={"account": "demo-ws"}).json()["orders"][0]["status"] == "REFUNDED"
