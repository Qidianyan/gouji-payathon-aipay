"""T9a 规则降级选品器：LLM 不可用时的确定性回退。纯函数，不访问网络、不读文件。"""

import re
from dataclasses import dataclass
from decimal import Decimal

# 英文/数字按连续 [a-z0-9]+ 切分；中文按相邻汉字串切（再拆 2-gram）
_TOKEN_RE = re.compile(r"[a-z0-9]+|[一-鿿]+")


@dataclass(frozen=True)
class Pick:
    sku: str        # 选中的 sku；没有合适的 → ""
    score: float    # 匹配分，>=0
    reason: str     # 一句中文理由，例如 "关键词命中：可见度、报告"


def tokenize(text: str) -> list[str]:
    """中文按 2-gram（相邻两个汉字）切分，英文/数字按连续 [a-z0-9]+（转小写）切分；去重保序。"""
    tokens: list[str] = []
    seen: set[str] = set()

    def emit(tok: str) -> None:
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)

    for run in _TOKEN_RE.findall(text.lower()):
        if "一" <= run[0] <= "鿿":
            for i in range(len(run) - 1):  # 汉字串相邻两两成 gram
                emit(run[i:i + 2])
        else:
            emit(run)
    return tokens


def pick(task: str, items: list[dict], budget: str | None = None) -> Pick:
    """score = task 的 token 与 (title + desc + sku) 的 token 交集大小。
    budget 非 None 时，price > budget 的项不参与。
    最高分 > 0 的那项胜出；同分取价格低者，再同取 sku 字典序小者。
    全部为 0 或 items 为空 → Pick("", 0.0, "没有匹配的商品")。
    """
    task_tokens = tokenize(task)
    budget_d = Decimal(budget) if budget is not None else None

    best: Pick | None = None
    best_key: tuple[int, Decimal, str] | None = None
    for item in items:
        price = Decimal(str(item["price"]))
        if budget_d is not None and price > budget_d:
            continue  # 超预算不参与
        item_tokens = set(tokenize(f'{item["title"]} {item["desc"]} {item["sku"]}'))
        matched = [t for t in task_tokens if t in item_tokens]
        if not matched:
            continue
        key = (-len(matched), price, item["sku"])
        if best_key is None or key < best_key:
            best_key = key
            best = Pick(sku=item["sku"], score=float(len(matched)),
                        reason=f"关键词命中：{'、'.join(matched[:4])}" + ("…" if len(matched) > 4 else ""))
    if best is None:
        return Pick("", 0.0, "没有匹配的商品")
    return best
