"""T9 LLM 买方 Agent（bot-written）。LLM 用桥接 mock：脚本化回复 / httpx.MockTransport 模拟本机代理。"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpay import identity
from agentpay.agent_client import BuyerAgent
from agentpay.app import create_app
from agentpay.llm_agent import LLMBuyerAgent, LLMUnavailable, OpenAICompatLLM, default_llm, plan
from aipay_kit.gateway.mock import MockGateway
from aipay_kit.gateway.mock_server import build_router


class Clock:
    t = 1_800_000_000

    def __call__(self):
        return self.t


class ScriptLLM:
    """桥接 mock：按顺序返回脚本；元素为 Exception 时抛出。"""
    name = "mock-bridge"

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages):
        self.calls.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


USER = identity.new_key()


def mandate(mid, **o):
    d = {"type": "admit.mandate.v1", "mandate_id": mid, "user_ref": "u_wang", "merchant_ids": ["facetry-demo"],
         "currency": "CNY", "max_amount_per_tx": "5.00", "max_amount_total": "6.00", "expires_at": Clock.t + 3600,
         "categories": ["report", "data"], "max_uses": 3, "agent_keyid": "kid-1", "intent_keywords": [],
         "user_public_key": identity.pub_b64(USER)}
    d.update(o)
    return identity.sign_obj(USER, d)


@pytest.fixture
def env():
    clock = Clock()
    gw = MockGateway(clock=clock)
    http = TestClient(create_app(gw, clock=clock, mock_router=build_router(gw, {})))
    k = identity.new_key()
    http.post("/agents", json={"keyid": "kid-1", "public_key": identity.pub_b64(k), "owner": "u_wang"})
    http.post("/mandates", json=mandate("m1"))
    http.post("/mandates", json=mandate("m6", intent_keywords=["可见度"]))
    wallet = lambda pid: http.post(f"/mock/alipay/a402/{pid}/pay").json()
    buyer = BuyerAgent(http, "kid-1", k, "m1", wallet, clock=clock)
    return {"http": http, "buyer": buyer, "gw": gw}


def J(sku, reason="符合任务"):
    return json.dumps({"sku": sku, "reason": reason}, ensure_ascii=False)


def test_llm_happy_path_delivers_with_receipt(env):
    llm = ScriptLLM("好的：" + J("geo-report"))
    r = LLMBuyerAgent(env["buyer"], llm).run("帮我查一下我们品牌在 AI 助手里的可见度")
    assert r.state == "delivered" and r.sku == "geo-report"
    assert r.plan.planner == "llm:mock-bridge"
    kinds = [s.kind for s in r.steps]
    assert kinds[:2] == ["discover", "plan"] and "bill" in kinds and "pay" in kinds and kinds[-2:] == ["deliver", "receipt"]
    assert r.receipt["_mock"] is True and r.receipt["fulfillment_confirmed"]
    assert len(env["gw"].fulfillment_log) == 1
    # 目录真的发给了 LLM，且没有带任何 key
    sent = json.dumps(llm.calls[0], ensure_ascii=False)
    assert "geo-report" in sent and "Bearer" not in sent


def test_llm_overreach_blocked_by_mandate(env):
    r = LLMBuyerAgent(env["buyer"], ScriptLLM(J("giftcard-50"))).run("买一张礼品卡")
    assert r.state == "blocked" and r.code in {"MANDATE_AMOUNT_PER_TX", "MANDATE_CATEGORY"}
    assert r.steps[-1].kind == "blocked"
    assert env["gw"].fulfillment_log == []


def test_intent_drift_needs_confirmation_then_resume(env):
    env["buyer"].mandate_id = "m6"
    agent = LLMBuyerAgent(env["buyer"], ScriptLLM(J("catalog-audit")))
    r = agent.run("检查商品目录结账字段")
    assert r.state == "needs_confirmation" and r.confirmation_id
    assert r.steps[-1].data["codes"] == ["CONFIRM_INTENT_DRIFT"]
    # 未确认时 resume 仍被挡
    assert agent.resume(r.task, r.sku, r.confirmation_id).state == "needs_confirmation"
    sig = identity.sign(USER, f"confirm:{r.confirmation_id}".encode())
    assert env["http"].post(f"/confirmations/{r.confirmation_id}", json={"signature": sig}).status_code == 200
    r2 = agent.resume(r.task, r.sku, r.confirmation_id)
    assert r2.state == "delivered" and r2.steps[0].kind == "confirm"


@pytest.mark.parametrize("reply", [LLMUnavailable("ConnectError"), "我不知道", J("hacked-sku"), "{bad json"])
def test_fallback_to_rules(env, reply):
    r = LLMBuyerAgent(env["buyer"], ScriptLLM(reply)).run("导出商品 feed")
    assert r.plan.planner == "rule" and r.plan.fallback_reason
    assert r.sku == "feed-export" and r.state == "delivered"


def test_no_llm_configured_uses_rules(env, tmp_path):
    assert default_llm(tmp_path / "missing.env") is None
    r = LLMBuyerAgent(env["buyer"], None).run("品牌 AI 可见度报告")
    assert r.plan.planner == "rule" and r.state == "delivered" and r.sku == "geo-report"


def test_no_match(env):
    r = LLMBuyerAgent(env["buyer"], ScriptLLM(J(""))).run("今天天气怎么样")
    assert r.state == "no_match" and env["gw"].fulfillment_log == []


def test_budget_from_task_passed_to_planner(env):
    r = LLMBuyerAgent(env["buyer"], None).run("导出商品 feed，预算 1 元")
    assert r.steps[1].data["budget"] == "1"
    assert r.sku != "feed-export"


# ---- OpenAI 兼容客户端（桥接 mock 本机代理） ----

def _transport(status=200, content="{\"sku\":\"geo-report\",\"reason\":\"x\"}", seen=None):
    def handler(req: httpx.Request):
        if seen is not None:
            seen.append(req)
        if status != 200:
            return httpx.Response(status, json={"error": "x"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 10}})
    return httpx.MockTransport(handler)


def test_openai_client_posts_to_local_proxy():
    seen = []
    c = OpenAICompatLLM("http://127.0.0.1:18767/v1", "sk-test-not-real", "deepseek-v4.1-flash", transport=_transport(seen=seen))
    assert "geo-report" in c.chat([{"role": "user", "content": "hi"}])
    assert str(seen[0].url) == "http://127.0.0.1:18767/v1/chat/completions"
    assert json.loads(seen[0].content)["model"] == "deepseek-v4.1-flash"
    assert "sk-test-not-real" not in repr(c)


def test_openai_client_refuses_public_url():
    with pytest.raises(ValueError):
        OpenAICompatLLM("https://api.example.com/v1", "k", "m")


def test_openai_client_error_becomes_unavailable_and_falls_back():
    c = OpenAICompatLLM("http://127.0.0.1:18767/v1", "k", "m", transport=_transport(status=429))
    with pytest.raises(LLMUnavailable):
        c.chat([])
    items = [{"sku": "geo-report", "title": "AI 可见度体检报告", "price": "0.50", "category": "report", "desc": "可见度"}]
    p = plan("品牌可见度", items, c)
    assert p.planner == "rule" and p.sku == "geo-report" and "HTTPStatusError" in p.fallback_reason


def test_from_env_reads_file_without_leaking(tmp_path):
    f = tmp_path / "llm.env"
    f.write_text("# c\nOPENAI_BASE_URL=http://127.0.0.1:18767/v1\nOPENAI_API_KEY=sk-secret-xyz\nLLM_MODEL=deepseek-v4.1-flash\n")
    c = OpenAICompatLLM.from_env(f)
    assert c.name == "deepseek-v4.1-flash" and "sk-secret-xyz" not in repr(c)
    f.write_text("OPENAI_BASE_URL=https://api.gavinchen.top/v1\nOPENAI_API_KEY=x\n")
    assert default_llm(f) is None   # 公网地址拒绝 → 规则降级


def test_humanize_steps():
    from agentpay.agent_client import humanize
    assert humanize("GET /paid/resource/geo-report -> 402")[1] == "签名请求 geo-report → 402 需要付款"
    assert humanize("GET /paid/resource/geo-report +Payment-Proof -> 200")[1].endswith("200 交付")
    assert humanize("GET /paid/resource/x -> 403")[1].endswith("被商户拒绝")
    assert humanize("GET /paid/resource/x -> 409")[1].endswith("需用户确认")
    k, t, d = humanize("402 bill AP1 ¥0.50 via amtpp_v1_abc…")
    assert (k, t) == ("bill", "402 账单 ¥0.50（订单 AP1）") and "amtpp_v1_abc" in d
    assert humanize("wallet paid trade_no=123")[0] == "pay"
    assert humanize("other") == ("info", "other", "")
