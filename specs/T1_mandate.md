# T1 实现：授权边界引擎 aipay_kit/mandate.py

## 目标
实现一个**纯函数**的授权（mandate）校验引擎：给定用户授权单、使用记录和一笔待支付请求，判断是 ALLOW / DENY / NEEDS_CONFIRMATION，并给出错误码。不做签名校验（签名由别处负责），不访问数据库。

## 新建文件
- `aipay_kit/mandate.py`
- `tests/test_mandate_smoke.py`（3–5 条冒烟测试即可，完整测试由另一个任务写）

## 接口（必须一字不差）
```python
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"

@dataclass(frozen=True)
class Mandate:
    mandate_id: str
    user_ref: str
    merchant_ids: tuple[str, ...]        # 允许的商户
    currency: str                         # 只支持 "CNY"
    max_amount_per_tx: Decimal            # 单笔上限（元）
    max_amount_total: Decimal             # 累计上限（元）
    expires_at: int                       # unix 秒
    not_before: int = 0                   # unix 秒
    categories: tuple[str, ...] = ()      # 空 = 不限类目
    max_uses: int | None = None           # None = 不限次数
    single_use: bool = False              # True 等价 max_uses=1
    agent_keyid: str | None = None        # 绑定 agent；None = 不限
    confirm_above: Decimal | None = None  # 单笔金额 > 此值需二次确认
    intent_keywords: tuple[str, ...] = () # 用户意图关键词（小写比较）；空 = 不做意图检查
    revoked: bool = False

@dataclass(frozen=True)
class Usage:
    used_count: int          # 已成功支付的笔数
    used_amount: Decimal     # 已成功支付的累计金额

@dataclass(frozen=True)
class PayRequest:
    merchant_id: str
    amount: Decimal
    currency: str
    category: str
    agent_keyid: str
    item_title: str          # 商品标题，用于意图检查
    now: int                 # unix 秒

@dataclass(frozen=True)
class Verdict:
    decision: Decision
    codes: tuple[str, ...]   # 所有命中的规则码（DENY 码在前，按下表顺序）
    remaining_amount: Decimal
    remaining_uses: int | None

def evaluate(m: Mandate, usage: Usage, req: PayRequest) -> Verdict: ...
def intent_score(keywords: tuple[str, ...], title: str) -> float: ...
```

## 规则（按此顺序检查，全部检查完再汇总，codes 按下表顺序列出命中的码）
| 码 | 条件 | 类型 |
|---|---|---|
| MANDATE_REVOKED | m.revoked | DENY |
| MANDATE_NOT_YET_VALID | req.now < m.not_before | DENY |
| MANDATE_EXPIRED | req.now >= m.expires_at | DENY |
| MANDATE_AGENT_MISMATCH | m.agent_keyid 不为 None 且 != req.agent_keyid | DENY |
| MANDATE_MERCHANT_MISMATCH | req.merchant_id 不在 m.merchant_ids | DENY |
| MANDATE_CURRENCY | req.currency != m.currency 或 m.currency != "CNY" | DENY |
| MANDATE_INVALID_AMOUNT | req.amount 非有限数（NaN/Infinity），或 <= 0，或小数位 > 2；非有限数时后面的金额规则（PER_TX/TOTAL/CONFIRM_AMOUNT）不再判定、不抛异常 | DENY |
| MANDATE_CATEGORY | m.categories 非空且 req.category 不在其中 | DENY |
| MANDATE_AMOUNT_PER_TX | req.amount > m.max_amount_per_tx | DENY |
| MANDATE_AMOUNT_TOTAL | usage.used_amount + req.amount > m.max_amount_total | DENY |
| MANDATE_USES_EXHAUSTED | 有效次数上限（single_use→1，否则 max_uses）不为 None 且 usage.used_count >= 上限 | DENY |
| CONFIRM_AMOUNT | m.confirm_above 不为 None 且 req.amount > m.confirm_above | CONFIRM |
| CONFIRM_INTENT_DRIFT | m.intent_keywords 非空且 intent_score(...) < 0.5 | CONFIRM |

- 有任一 DENY 码 → decision=DENY；否则有 CONFIRM 码 → NEEDS_CONFIRMATION；否则 ALLOW（codes 为空元组）。
- `intent_score`：关键词小写去空白后（**去空白后为空的关键词直接忽略**；全部被忽略视同空），统计有多少个关键词作为子串出现在 `title.lower()` 中，返回 命中数/关键词数；keywords 为空返回 1.0。
- `remaining_amount = max(Decimal("0"), m.max_amount_total - usage.used_amount)`（不扣本次）。
- `remaining_uses`：上限为 None 时为 None，否则 `max(0, 上限 - usage.used_count)`。
- 金额一律用 Decimal，禁止 float。

## 验收
- `.venv/bin/python -m pytest -q` 全部通过（含已有 tests/test_signing.py 的 5 条）。
- `python -c "from aipay_kit.mandate import evaluate, Mandate, Usage, PayRequest, Decision, Verdict, intent_score"` 成功。
