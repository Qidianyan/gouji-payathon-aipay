# T3t 测试：tests/test_credits.py
先读 specs/T3_credits.md；不要改实现；不符就让测试失败并在回复中说明。
覆盖：grant 幂等（同参 created=False；异参 GRANT_CONFLICT）；INVALID 参数；balance 跨多包与过期；consume FIFO 顺序（看各包 used）、不足时不部分扣、request_id 幂等；refundable 金额整除；refund 幂等、二次不同 out_request_no → NOTHING_TO_REFUND；NOT_FOUND；过期包不可退；文件库重开数据仍在；10 线程并发 consume 总量不超卖。测试数 ≥ 20。
