# T2t 测试：审计日志完整测试 tests/test_audit.py

先读 specs/T2_audit.md。不要修改 aipay_kit/audit.py；实现与 SPEC 不符时让测试失败并在回复中说明，不要放宽断言。

## 新建文件
- `tests/test_audit.py`

## 必须覆盖
1. 连续 append 5 条：seq 1..5，首条 prev_hash==GENESIS，链接正确，hash 可用 record_hash 重算。
2. export_jsonl → verify_jsonl ok=True，count=5。
3. 篡改：改某行 data → hash_mismatch 且 first_bad_seq 正确；删除中间一行 → seq_gap 或 prev_mismatch；交换两行 → 失败；改 prev_hash → 失败；非 JSON 行 → parse_error。
4. ref 过滤：records(ref) 与 export_jsonl(ref) 只含该 ref。
5. export_csv：表头精确、行数=记录数+1、data 列可被 json.loads。
6. 中文内容 data 不被转义（ensure_ascii=False）。
7. 文件型数据库（tmp_path）关闭后重新打开继续 append，seq 接续、链仍然 ok。
8. 10 个线程各 append 20 条 → 200 条、seq 无重复、链 ok。
9. 对象上不存在 update/delete 方法。

## 验收
- `.venv/bin/python -m pytest -q tests/test_audit.py` 通过；测试数 ≥ 15。
