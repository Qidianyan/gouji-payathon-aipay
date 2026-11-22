"""Agent Pay end-to-end on MOCK: full chain + 9 interception scenarios (bot-written)."""

import json

import pytest
from fastapi.testclient import TestClient

from aipay_kit import signing
from aipay_kit.audit import verify_jsonl
from aipay_kit.audit import canonical_json
from aipay_kit.gateway.mock import MockGateway
from aipay_kit.gateway.mock_server import build_app as build_mock_app
from aipay_kit.gateway.mock_server import build_router
from aipay_kit.gateway.openapi import OpenApiGateway
from aipay_kit import proof as px
from agentpay import identity
from agentpay.agent_client import BuyerAgent
from agentpay.app import create_app


class Clock:
    def __init__(self):
        self.t = 1_800_000_000

    def __call__(self):
        return self.t


USER = identity.new_key()


def mandate_doc(mid="m1", **o):
    d = {"type": "admit.mandate.v1", "mandate_id": mid, "user_ref": "u_wang", "merchant_ids": ["facetry-demo"],
         "currency": "CNY", "max_amount_per_tx": "5.00", "max_amount_total": "6.00", "expires_at": 1_800_000_000 + 3600,
         "categories": ["report", "data"], "max_uses": 3, "agent_keyid": "kid-1",
         "intent_keywords": [], "user_public_key": identity.pub_b64(USER)}
    d.update(o)
    return identity.sign_obj(USER, d)


@pytest.fixture(params=["inprocess", "openapi-http"])
def world(request):
    clock = Clock()
    gw = MockGateway(clock=clock)
    if request.param == "inprocess":
        app = create_app(gw, clock=clock, mock_router=build_router(gw, {}))
        http = TestClient(app)
        wallet = lambda pid: http.post(f"/mock/alipay/a402/{pid}/pay").json()
    else:  # 走 OpenAPI 客户端 → MOCK HTTP gateway.do（与真沙箱同一条代码路径）
        mk = signing.generate_keypair()
        mock_http = TestClient(build_mock_app(gw, {gw.app_id: signing.public_pem(mk.public_key()).decode()}), base_url="http://mock")
        cli = OpenApiGateway(gw.app_id, signing.private_pem(mk).decode(), signing.public_pem(gw.alipay_public_key).decode(),
                             "http://mock/mock/alipay/gateway.do", client=mock_http, clock=clock, is_mock=True)
        app = create_app(cli, clock=clock)
        http = TestClient(app)
        wallet = lambda pid: mock_http.post(f"/mock/alipay/a402/{pid}/pay").json()
    agent_key = identity.new_key()
    assert http.post("/agents", json={"keyid": "kid-1", "public_key": identity.pub_b64(agent_key), "owner": "u_wang"}).status_code == 201
    assert http.post("/mandates", json=mandate_doc()).status_code == 201
    agent = BuyerAgent(http, "kid-1", agent_key, "m1", wallet, clock=clock)
    return {"http": http, "agent": agent, "gw": gw, "clock": clock, "app": app, "agent_key": agent_key, "wallet": wallet}


def test_full_chain(world):
    a, http = world["agent"], world["http"]
    disc = a.discover()
    assert disc["payment"]["gateway_mode"] == "mock"
    assert any(i["sku"] == "geo-report" for i in a.search("可见度"))
    r = a.buy("geo-report")
    assert r.ok and r.body["already_fulfilled"] is False and r.body["content"]["sku"] == "geo-report"
    assert http.get(f"/orders/{r.out_trade_no}").json()["status"] == "FULFILLED"
    assert world["gw"].fulfillment_log == [r.trade_no]
    rc = http.get(f"/receipts/{r.trade_no}").json()
    assert rc["mandate"]["mandate_id"] == "m1" and rc["fulfillment_confirmed"] and rc["_mock"] is True
    events = [x["event"] for x in rc["audit"]]
    assert events == ["order.precreated", "payment.verified", "fulfilled", "fulfillment.confirmed"]
    sig = rc.pop("merchant_signature")
    pub = signing.load_public(http.get("/merchant/public_key").text)
    assert signing.rsa2_verify(canonical_json(rc), sig, pub)
    exp = http.get("/audit/export").text
    assert verify_jsonl(exp).ok
    assert http.get("/audit/export", params={"format": "csv"}).text.startswith("seq,ts,actor")


def test_payment_needed_header_is_signed_bill(world):
    a = world["agent"]
    r = a._get("/paid/resource/geo-report")
    assert r.status_code == 402 and r.headers["amtPaymentLink"].startswith("amtpp_v1_")
    bill = px.parse_payment_needed(r.headers["Payment-Needed"])
    pub = signing.load_public(world["http"].get("/merchant/public_key").text)
    assert signing.rsa2_verify(signing.sign_content({k: v for k, v in bill.items() if k != "seller_signature"}),
                               bill["seller_signature"], pub)


def blocked_codes(world):
    out = []
    for line in world["http"].get("/audit/export").text.splitlines():
        r = json.loads(line)
        if r["event"] == "blocked":
            out += [r["data"]["code"]] if "code" in r["data"] else r["data"]["codes"]
    return out


# 1 超额
def test_s1_over_amount(world):
    r = world["agent"].buy("giftcard-50")
    assert r.status == 403 and "MANDATE_AMOUNT_PER_TX" in r.body["codes"]


# 2 错商户
def test_s2_wrong_merchant(world):
    http = world["http"]
    http.post("/mandates", json=mandate_doc("m2", merchant_ids=["other-shop"]))
    world["agent"].mandate_id = "m2"
    r = world["agent"].buy("geo-report")
    assert r.status == 403 and r.code == "MANDATE_MERCHANT_MISMATCH"


# 3 过期
def test_s3_expired(world):
    world["clock"].t += 3600
    r = world["agent"].buy("geo-report")
    assert r.status == 403 and r.code == "MANDATE_EXPIRED"


# 4 次数/额度用尽（累计）
def test_s4_uses_and_total(world):
    a = world["agent"]
    assert a.buy("feed-export").ok          # 5.00
    r = a.buy("catalog-audit")              # +2.00 > 6.00
    assert r.status == 403 and r.code == "MANDATE_AMOUNT_TOTAL"
    assert a.buy("geo-report").ok           # 5.50
    r = a.buy("geo-report")                 # 第 3 次 → 6.00 ok
    assert r.ok
    r = a.buy("geo-report")
    assert r.status == 403 and "MANDATE_USES_EXHAUSTED" in r.body["codes"]


# 5 类目越界
def test_s5_category(world):
    http = world["http"]
    http.post("/mandates", json=mandate_doc("m5", max_amount_per_tx="50.00", max_amount_total="100.00"))
    world["agent"].mandate_id = "m5"
    r = world["agent"].buy("giftcard-50")
    assert r.status == 403 and r.body["codes"] == ["MANDATE_CATEGORY"]


# 6 意图偏移 → 二次确认 → 用户批准后通过
def test_s6_intent_drift_confirm(world):
    http, a = world["http"], world["agent"]
    http.post("/mandates", json=mandate_doc("m6", intent_keywords=["可见度"]))
    a.mandate_id = "m6"
    assert a.buy("geo-report").ok  # 命中关键词：不需确认
    r = a.buy("catalog-audit")
    assert r.status == 409 and r.body["codes"] == ["CONFIRM_INTENT_DRIFT"]
    cid = r.body["confirmation_id"]
    assert http.post(f"/confirmations/{cid}", json={"signature": "AAAA"}).status_code == 401
    assert a.buy("catalog-audit", confirmation_id=cid).status == 409  # 未批准
    sig = identity.sign(USER, f"confirm:{cid}".encode())
    assert http.post(f"/confirmations/{cid}", json={"signature": sig}).status_code == 200
    assert a.buy("catalog-audit", confirmation_id=cid).ok
    assert a.buy("catalog-audit", confirmation_id=cid).status == 409  # 一次性


# 7 重复支付 / 重复下单
def test_s7_duplicate(world):
    a, http, gw = world["agent"], world["http"], world["gw"]
    r1 = a._get("/paid/resource/geo-report"); r2 = a._get("/paid/resource/geo-report")
    assert r1.headers["amtPaymentLink"] == r2.headers["amtPaymentLink"]  # 复用待支付单
    p = world["wallet"](r1.headers["amtPaymentLink"])
    hdr = px.build_payment_proof(px.PaymentProof(p["payment_proof"], p["trade_no"], p["client_session"]))
    d1 = a._get("/paid/resource/geo-report", proof=hdr).json()
    d2 = a._get("/paid/resource/geo-report", proof=hdr).json()
    assert d1["already_fulfilled"] is False and d2["already_fulfilled"] is True
    assert d1["content"] == d2["content"]
    assert gw.fulfillment_log == [p["trade_no"]]          # 只回执一次
    m = world["app"].state.svc.usage("m1")
    assert m.used_count == 1                              # 只计一次


# 8 伪造/挪用支付凭证（误付拦截）
def test_s8_forged_or_misused_proof(world):
    a = world["agent"]
    r = a._get("/paid/resource/geo-report")
    p = world["wallet"](r.headers["amtPaymentLink"])
    forged = px.build_payment_proof(px.PaymentProof("0" * 64, p["trade_no"], p["client_session"]))
    assert a._get("/paid/resource/geo-report", proof=forged).json()["code"] == "INVALID_PAYMENT_PROOF"
    real = px.build_payment_proof(px.PaymentProof(p["payment_proof"], p["trade_no"], p["client_session"]))
    x = a._get("/paid/resource/feed-export", proof=real)    # 用 0.50 的凭证拿 5.00 的资源
    assert x.status_code == 402 and x.json()["code"] == "PROOF_RESOURCE_MISMATCH"
    assert a._get("/paid/resource/geo-report", proof=real).status_code == 200
    bad = a._get("/paid/resource/geo-report", proof="not-base64!!")
    assert bad.status_code == 400


# 9 伪造 / 吊销 agent
def test_s9_forged_agent(world):
    http, a = world["http"], world["agent"]
    evil = BuyerAgent(http, "kid-1", identity.new_key(), "m1", world["wallet"], clock=world["clock"])
    assert evil.buy("geo-report").code == "AGENT_SIGNATURE_INVALID"
    ghost = BuyerAgent(http, "kid-ghost", identity.new_key(), "m1", world["wallet"], clock=world["clock"])
    assert ghost.buy("geo-report").code == "AGENT_SIGNATURE_INVALID"
    other = identity.new_key()
    http.post("/agents", json={"keyid": "kid-2", "public_key": identity.pub_b64(other)})
    r = BuyerAgent(http, "kid-2", other, "m1", world["wallet"], clock=world["clock"]).buy("geo-report")
    assert r.status == 403 and r.code == "MANDATE_AGENT_MISMATCH"   # 授权绑定的不是这个 agent
    http.post("/agents/kid-1/revoke")
    assert a.buy("geo-report").code == "AGENT_KEY_REVOKED"


def test_stale_signature(world):
    a = world["agent"]
    a.clock = lambda: world["clock"].t - 1000
    assert a.buy("geo-report").code == "AGENT_SIGNATURE_STALE"


def test_mandate_signature_and_tofu(world):
    http = world["http"]
    d = mandate_doc("m9"); d["max_amount_total"] = "999.00"   # 签名后篡改
    assert http.post("/mandates", json=d).json()["code"] == "MANDATE_SIGNATURE_INVALID"
    mallory = identity.new_key()
    d = identity.sign_obj(mallory, {**{k: v for k, v in mandate_doc("m10").items() if k != "signature"},
                                    "user_public_key": identity.pub_b64(mallory)})
    assert http.post("/mandates", json=d).json()["code"] == "USER_KEY_MISMATCH"


def test_mandate_revoke(world):
    http, a = world["http"], world["agent"]
    assert http.post("/mandates/m1/revoke", json={"signature": "AAAA"}).status_code == 401
    http.post("/mandates/m1/revoke", json={"signature": identity.sign(USER, b"revoke:m1")})
    assert a.buy("geo-report").code == "MANDATE_REVOKED"


def test_pending_orders_reserve_budget(world):
    """并发下多张待支付单也不能突破累计额度（预占）。"""
    a = world["agent"]
    assert a._get("/paid/resource/feed-export").status_code == 402      # 预占 5.00
    r = a._get("/paid/resource/catalog-audit")                          # 5 + 2 > 6
    assert r.status_code == 403 and r.json()["code"] == "MANDATE_AMOUNT_TOTAL"
    world["clock"].t += 16 * 60                                          # 待支付单过期释放
    # 但授权单也只剩 44 分钟，仍有效
    assert a._get("/paid/resource/catalog-audit").status_code == 402


def test_blocked_events_audited(world):
    a = world["agent"]
    a.buy("giftcard-50")
    world["clock"].t += 3600
    a.buy("geo-report")
    codes = blocked_codes(world)
    assert "MANDATE_AMOUNT_PER_TX" in codes and "MANDATE_EXPIRED" in codes
