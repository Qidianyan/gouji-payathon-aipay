# T1t 测试：授权边界引擎完整测试 tests/test_mandate.py

## 目标
为 `aipay_kit/mandate.py`（接口见 specs/T1_mandate.md，**先读它**）写完整测试。不要修改 `aipay_kit/mandate.py`；如果发现实现与 SPEC 不符，测试按 SPEC 写，并在最终回复里列出不符之处（让测试失败即可，不要改实现、不要放宽断言）。

## 新建文件
- `tests/test_mandate.py`

## 必须覆盖
1. 13 个规则码各至少一个命中用例 + 一个刚好不命中的边界用例（如 amount == max_amount_per_tx 允许；used_amount + amount == total 允许；now == expires_at 拒绝；now == not_before 允许）。
2. 多个 DENY 同时命中时 codes 的顺序与 SPEC 表一致。
3. DENY 与 CONFIRM 同时命中 → decision=DENY，codes 里两类都在且 DENY 在前。
4. 只有 CONFIRM → NEEDS_CONFIRMATION。
5. single_use=True 且 used_count=1 → MANDATE_USES_EXHAUSTED；single_use=True 且 max_uses=5 时上限按 1。
6. 小数位 3 位（Decimal("0.001")）、0、负数 → MANDATE_INVALID_AMOUNT。
7. intent_score：空关键词 1.0；大小写不敏感；部分命中比例；关键词前后空白被去掉。
8. remaining_amount 不为负、remaining_uses 为 None / 数值两种情况。
9. NaN / Infinity 金额 → 只命中 MANDATE_INVALID_AMOUNT（及其它与金额无关的码），不抛异常；空白关键词被忽略。
10. 用 `pytest.mark.parametrize` 组织；用一个 `make_mandate(**overrides)` 工厂。

## 验收
- `.venv/bin/python -m pytest -q tests/test_mandate.py` 全部通过（若实现有 bug 导致失败，在回复中写清楚是哪条）。
- 测试数 ≥ 30。
