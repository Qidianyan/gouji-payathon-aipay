"""演示商品目录（Facetry 风格的数字商品）。价格单位元（字符串，两位小数）；402 单笔 ≤ 50 元。"""

CATALOG = {
    "geo-report": {"sku": "geo-report", "title": "AI 可见度体检报告（单品牌）", "price": "0.50", "category": "report",
                   "desc": "检查品牌在主流 AI 助手回答中的可见度与引用来源"},
    "catalog-audit": {"sku": "catalog-audit", "title": "商品目录 ACP/UCP 结账字段合规检查", "price": "2.00",
                      "category": "report", "desc": "检查商品 feed 是否满足 agentic checkout 必填字段"},
    "feed-export": {"sku": "feed-export", "title": "多渠道商品 feed 导出（100 SKU）", "price": "5.00", "category": "data",
                    "desc": "导出 ChatGPT / Perplexity / Google 渠道 feed"},
    "giftcard-50": {"sku": "giftcard-50", "title": "通用礼品卡 50 元", "price": "50.00", "category": "giftcard",
                    "desc": "演示：类目越界与超额拦截"},
}
