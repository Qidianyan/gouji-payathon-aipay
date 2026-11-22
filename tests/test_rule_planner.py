"""T9a 验收（bot-written）。"""
from agentpay.catalog import CATALOG
from agentpay.rule_planner import Pick, pick, tokenize

ITEMS = list(CATALOG.values())


def test_tokenize_bigrams_and_ascii():
    assert tokenize("AI 可见度") == ["ai", "可见", "见度"]
    assert tokenize("Feed feed 导出") == ["feed", "导出"]
    assert tokenize("") == [] and tokenize("的") == []


def test_no_cross_field_bigram():
    items = [{"sku": "a", "title": "报告", "desc": "检查", "price": "1.00", "category": "x"}]
    assert pick("告检", items).sku == ""   # title/desc 边界不应拼出新词


def test_picks_on_real_catalog():
    assert pick("品牌在 AI 助手里的可见度", ITEMS).sku == "geo-report"
    assert pick("商品目录结账字段合规", ITEMS).sku == "catalog-audit"
    assert pick("礼品卡", ITEMS).sku == "giftcard-50"


def test_budget_and_tiebreak():
    items = [{"sku": "b", "title": "报告", "desc": "", "price": "2.00", "category": "r"},
             {"sku": "a", "title": "报告", "desc": "", "price": "2.00", "category": "r"},
             {"sku": "c", "title": "报告", "desc": "", "price": "1.00", "category": "r"}]
    assert pick("报告", items).sku == "c"
    assert pick("报告", items[:2]).sku == "a"
    assert pick("报告", items, budget="0.50") == Pick("", 0.0, "没有匹配的商品")
    p = pick("报告", items)
    assert p.score == 1.0 and "报告" in p.reason
