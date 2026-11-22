"""LLM 买方 Agent（T9 升维版）：自然语言任务 → 发现服务 → 高速稳定 LLM 选品（严格超时 + 自动退避重试）→ 授权内 402 付款 → 交付 → APASS 四层回执。

- 默认优先读取本机/内网高速代理配置（~/.secrets/cliproxy.env，默认高速稳定模型 gemini-3-flash / gemini-4-carbon-high），
  同时兼容标准 OpenAI 环境变量文件；默认仅允许回环地址、RFC1918 私网与 Tailscale CGNAT 内网（100.64.0.0/10），拒绝未授权公网泄露；key 绝不打印或入日志。
- 单次调用严格超时（默认 8.0s，彻底消除旧版 53s 阻塞缺陷）+ 瞬时超时/5xx/429 指数退避重试（默认最多重试 2 次）。
- LLM 不可用 / 输出不合法 / 选了目录外的 sku → 确定性规则引擎无损兜底（agentpay.rule_planner）。
- 越权由商户侧 Admit 规则拦截（403）；意图偏移返回 409 NEEDS_CONFIRMATION，
  Agent 停下并把 confirmation_id 交给用户，用户签名批准后调用 resume() 继续。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from agentpay import rule_planner
from agentpay.agent_client import BuyerAgent, humanize

CLIPROXY_ENV = Path.home() / ".secrets" / "cliproxy.env"
DEFAULT_ENV = Path("/workspace/competitions/shared/llm_api.env")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_trusted_llm_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host in LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or (ip in TAILSCALE_CGNAT)
    except ValueError:
        return False


class LLMUnavailable(RuntimeError):
    pass


class LLM(Protocol):
    name: str

    def chat(self, messages: list[dict]) -> str: ...


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class OpenAICompatLLM:
    """高速稳定 OpenAI 兼容 /chat/completions 客户端（严格超时 + 自动重试 + 内网安全门禁）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout: float = 8.0,
        max_retries: int = 2,
        backoff_base: float = 0.2,
        allow_public: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not allow_public and not _is_trusted_llm_host(base_url):
            raise ValueError("LLM 默认仅允许本机/内网代理地址（127.0.0.1 / 私网 / Tailscale CGNAT）")
        self.name = model
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._model = model
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def __repr__(self) -> str:  # 防止 key 出现在日志
        return f"OpenAICompatLLM(model={self._model!r}, url={self._url!r})"

    @classmethod
    def from_env(cls, path: Path = DEFAULT_ENV, **kw) -> "OpenAICompatLLM":
        if not path.exists():
            raise LLMUnavailable(f"缺少 {path.name}")
        e = read_env(path)
        base_url = e.get("OPENAI_BASE_URL") or e.get("CLIPROXY_BASE_URL") or e.get("AI_BASE_URL")
        api_key = e.get("OPENAI_API_KEY") or e.get("CLIPROXY_API_KEY") or e.get("AI_API_KEY")
        if not base_url:
            raise LLMUnavailable(f"{path.name} 缺少 OPENAI_BASE_URL / CLIPROXY_BASE_URL")
        if not api_key:
            raise LLMUnavailable(f"{path.name} 缺少 OPENAI_API_KEY / CLIPROXY_API_KEY")
        # 若为 cliproxy.env，买方选品默认采用亚 2 秒响应的 gemini-3-flash（可通过 LLM_MODEL / CLIPROXY_BUYER_MODEL 覆盖）
        default_model = "gemini-3-flash" if "CLIPROXY_BASE_URL" in e else "deepseek-v4.1-flash"
        model = e.get("LLM_MODEL") or e.get("CLIPROXY_BUYER_MODEL") or default_model
        allow_pub = e.get("OPENAI_ALLOW_PUBLIC", "").lower() == "true"
        return cls(base_url, api_key, model, allow_public=allow_pub, **kw)

    def chat(self, messages: list[dict]) -> str:
        body = {"model": self._model, "messages": messages, "temperature": 0, "max_tokens": 300, "stream": False}
        headers = {"Authorization": f"Bearer {self._key}"}
        last_ex: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                r = self._client.post(self._url, json=body, headers=headers)
                if r.status_code in (429, 502, 503, 504) and attempt < self._max_retries:
                    if self._backoff_base > 0:
                        time.sleep(self._backoff_base * (2 ** attempt))
                    continue
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"] or ""
            except (httpx.TimeoutException, httpx.TransportError) as ex:
                last_ex = ex
                if attempt < self._max_retries:
                    if self._backoff_base > 0:
                        time.sleep(self._backoff_base * (2 ** attempt))
                    continue
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as ex:
                last_ex = ex
                break
        raise LLMUnavailable(type(last_ex).__name__ if last_ex else "LLMError") from None


SYSTEM = ("你是用户授权的采购 Agent。只能从给定商品目录里选 1 个最符合任务的商品，不得编造 sku。"
          "如果都不合适，sku 填空字符串。只输出 JSON：{\"sku\":\"...\",\"reason\":\"一句话\"}。")


def _extract_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no json")
    return json.loads(m.group(0))


@dataclass
class Plan:
    sku: str
    reason: str
    planner: str          # "llm:<model>" 或 "rule"
    fallback_reason: str = ""


def plan(task: str, items: list[dict], llm: LLM | None, budget: str | None = None) -> Plan:
    if llm is not None:
        catalog = [{k: i[k] for k in ("sku", "title", "price", "category", "desc")} for i in items]
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"任务：{task}\n预算上限：{budget or '未指定'} 元\n目录：{json.dumps(catalog, ensure_ascii=False)}"}]
        try:
            d = _extract_json(llm.chat(msgs))
            sku = str(d.get("sku", ""))
            if sku == "" or sku in {i["sku"] for i in items}:
                return Plan(sku, str(d.get("reason", ""))[:200], f"llm:{llm.name}")
            why = f"LLM 选了目录外的 sku：{sku[:40]}"
        except LLMUnavailable as ex:
            why = f"LLM 不可用（{ex}）"
        except (ValueError, TypeError, AttributeError):
            why = "LLM 输出不是合法 JSON"
    else:
        why = "未配置 LLM"
    p = rule_planner.pick(task, items, budget)
    return Plan(p.sku, p.reason, "rule", why)


@dataclass
class Step:
    kind: str     # discover | plan | request | bill | pay | deliver | receipt | blocked | confirm | info
    title: str
    data: dict = field(default_factory=dict)


@dataclass
class RunResult:
    state: str    # delivered | blocked | needs_confirmation | no_match
    task: str
    plan: Plan | None
    steps: list[Step]
    code: str = ""
    confirmation_id: str = ""
    receipt: dict | None = None
    content: dict | None = None
    sku: str = ""

    def to_dict(self) -> dict:
        return {"state": self.state, "task": self.task, "code": self.code, "sku": self.sku,
                "confirmation_id": self.confirmation_id, "receipt": self.receipt, "content": self.content,
                "plan": None if self.plan is None else self.plan.__dict__,
                "steps": [s.__dict__ for s in self.steps]}


def _budget(task: str) -> str | None:
    m = re.search(r"(\d+(?:\.\d{1,2})?)\s*(?:元|块|rmb|cny)", task, re.I)
    if not m:
        return None
    try:
        return str(Decimal(m.group(1)))
    except InvalidOperation:
        return None


class LLMBuyerAgent:
    def __init__(self, buyer: BuyerAgent, llm: LLM | None) -> None:
        self.buyer, self.llm = buyer, llm

    def run(self, task: str) -> RunResult:
        steps: list[Step] = []
        disc = self.buyer.discover()
        steps.append(Step("discover", f"发现服务：{disc.get('name', '')}（读取 /.well-known/agent-service.json）",
                          {"gateway_mode": disc.get("payment", {}).get("gateway_mode")}))
        items = self.buyer.search("")
        budget = _budget(task)
        p = plan(task, items, self.llm, budget)
        label = "规则降级" if p.planner == "rule" else p.planner.replace("llm:", "LLM ")
        title = next((i["title"] for i in items if i["sku"] == p.sku), "没有合适的商品")
        steps.append(Step("plan", f"选品（{label}）：{title}",
                          {"reason": p.reason, "fallback_reason": p.fallback_reason, "budget": budget}))
        if not p.sku:
            return RunResult("no_match", task, p, steps)
        return self._buy(task, p, p.sku, steps)

    def resume(self, task: str, sku: str, confirmation_id: str) -> RunResult:
        steps = [Step("confirm", "用户已签名确认，继续购买", {"confirmation_id": confirmation_id})]
        return self._buy(task, None, sku, steps, confirmation_id)

    def _buy(self, task: str, p: Plan | None, sku: str, steps: list[Step], cid: str = "") -> RunResult:
        r = self.buyer.buy(sku, confirmation_id=cid)
        for s in r.steps:
            kind, title, detail = humanize(s)
            steps.append(Step(kind, title, {"detail": detail}))
        if r.status == 409:
            cid2 = r.body.get("confirmation_id", "")
            steps.append(Step("confirm", "意图偏移：需要用户二次确认", {"codes": r.body.get("codes", []), "confirmation_id": cid2}))
            return RunResult("needs_confirmation", task, p, steps, "NEEDS_CONFIRMATION", cid2, sku=sku)
        if not r.ok:
            steps.append(Step("blocked", f"被拦截：{r.code}", {"codes": r.body.get("codes", [r.code]), "message": r.body.get("message", "")}))
            return RunResult("blocked", task, p, steps, r.code, sku=sku)
        steps.append(Step("deliver", f"已交付：{sku}", {"already_fulfilled": r.body.get("already_fulfilled")}))
        rc = self.buyer.http.get(f"/receipts/{r.trade_no}").json()
        steps.append(Step("receipt", "取回签名回执", {"trade_no": r.trade_no}))
        return RunResult("delivered", task, p, steps, receipt=rc, content=r.body.get("content"), sku=sku)


def default_llm(env_path: Path | None = None, **kw) -> LLM | None:
    """加载默认买方选品 LLM：显式指定 env_path 时严格读该文件；未指定时依次探测 AIPAY_LLM_ENV → ~/.secrets/cliproxy.env → DEFAULT_ENV。"""
    if env_path is not None:
        try:
            return OpenAICompatLLM.from_env(env_path, **kw)
        except (LLMUnavailable, ValueError):
            return None

    candidates: list[Path] = []
    if os.environ.get("AIPAY_LLM_ENV"):
        candidates.append(Path(os.environ["AIPAY_LLM_ENV"]))
    candidates.extend([CLIPROXY_ENV, DEFAULT_ENV])
    for cand in candidates:
        if cand.is_file():
            try:
                return OpenAICompatLLM.from_env(cand, **kw)
            except (LLMUnavailable, ValueError):
                continue
    return None


Factory = Callable[[], LLM | None]
