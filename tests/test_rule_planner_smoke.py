"""冒烟测试：agentpay.rule_planner 规则降级选品器（LLM 不可用时的确定性回退）。"""

from agentpay.rule_planner import Pick, pick, tokenize

CATALOG = [
    {"sku": "geo-report", "title": "AI 可见度体检报告（单品牌）", "price": "0.50", "category": "report",
     "desc": "检查品牌在主流 AI 助手回答中的可见度与引用来源"},
    {"sku": "catalog-audit", "title": "商品目录 ACP/UCP 结账字段合规检查", "price": "2.00", "category": "report",
     "desc": "检查商品 feed 是否满足 agentic checkout 必填字段"},
    {"sku": "feed-export", "title": "多渠道商品 feed 导出（100 SKU）", "price": "5.00", "category": "data",
     "desc": "导出 ChatGPT / Perplexity / Google 渠道 feed"},
]


def test_visibility_task_picks_geo_report():
    assert tokenize("AI 可见度") == ["ai", "可见", "见度"]
    assert tokenize("feed Feed") == ["feed"]  # 转小写 + 去重保序
    res = pick("帮我查一下品牌在 AI 里的可见度", CATALOG)
    assert res.sku == "geo-report"
    assert res.score > 0
    assert res.reason.startswith("关键词命中：")


def test_feed_export_task_picks_feed_export():
    res = pick("导出商品 feed", CATALOG)
    assert res.sku == "feed-export"
    assert res.score > 0


def test_budget_excludes_feed_export():
    res = pick("导出商品 feed", CATALOG, budget="1.00")
    assert res.sku != "feed-export"  # 5.00 > 1.00，被预算排除
    assert res == Pick("", 0.0, "没有匹配的商品")


def test_unrelated_task_picks_nothing():
    res = pick("天气怎么样", CATALOG)
    assert res.sku == ""
    assert res.score == 0.0
    assert res.reason == "没有匹配的商品"


def test_tie_break_by_lower_price():
    res = pick("商品", CATALOG)  # catalog-audit 与 feed-export 同分，取低价者
    assert res.sku == "catalog-audit"
