# T9a 规则降级选品器 `agentpay/rule_planner.py`（LLM 不可用时使用）

先读 `specs/_common.md`。只新建 `agentpay/rule_planner.py` 和 `tests/test_rule_planner_smoke.py`。

## 接口（必须完全一致）
```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Pick:
    sku: str        # 选中的 sku；没有合适的 → ""
    score: float    # 匹配分，>=0
    reason: str     # 一句中文理由，例如 "关键词命中：可见度、报告"

def tokenize(text: str) -> list[str]:
    """中文按 2-gram（相邻两个汉字）切分，英文/数字按连续 [a-z0-9]+（转小写）切分；去重保序。"""

def pick(task: str, items: list[dict], budget: str | None = None) -> Pick:
    """items 每项含 sku/title/desc/price(字符串元)/category。
    score = task 的 token 与 (title + desc + sku) 的 token 交集大小。
    budget 非 None 时，price > budget 的项不参与。
    最高分 > 0 的那项胜出；同分取价格低者，再同取 sku 字典序小者。
    全部为 0 或 items 为空 → Pick("", 0.0, "没有匹配的商品")。"""
```
- 金额比较用 `decimal.Decimal`。
- 纯函数，不访问网络、不读文件。

## 冒烟测试（tests/test_rule_planner_smoke.py，3–5 条）
用下面目录：
- geo-report "AI 可见度体检报告（单品牌）" 0.50 report
- catalog-audit "商品目录 ACP/UCP 结账字段合规检查" 2.00 report
- feed-export "多渠道商品 feed 导出（100 SKU）" 5.00 data

断言：
- "帮我查一下品牌在 AI 里的可见度" → geo-report
- "导出商品 feed" → feed-export
- "导出商品 feed"，budget="1.00" → 不选 feed-export
- "天气怎么样" → sku == ""
