# T4t 测试：tests/test_proof.py
先读 specs/T4_proof.md；不要改实现；不符就让测试失败并在回复中说明。
覆盖：往返编解码（含中文 goods_name）；无填充/有填充/标准 base64 都能解；官方文档示例结构的 Payment-Proof；缺字段、空字符串、trade_no 非数字/过短/过长、超长 header、非 JSON、JSON 数组 → ProofError；Payment-Needed 缺必填/未知字段/非法 amount（"1.234"、"-1"、"abc"）/currency 非 CNY → ProofError；Payment-Validation 往返。测试数 ≥ 20。
