# T6 实现：幂等存储 aipay_kit/idempotency.py

## 目标
通用"一次性写入"存储，用于：异步通知去重（同一 notify 多次投递只处理一次）、同一 trade_no 只履约一次、同一 out_trade_no 只预下单一次。sqlite3（路径或 ":memory:"），threading.Lock + BEGIN IMMEDIATE。

## 新建文件
- `aipay_kit/idempotency.py`
- `tests/test_idempotency_smoke.py`（3–5 条）

## 接口（必须一致）
```python
class IdemConflict(Exception):
    """同一 (scope, key) 已用不同 fingerprint 写入过。属性 scope, key。"""

def fingerprint(obj) -> str:
    # sha256 hex of json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

class IdempotencyStore:
    def __init__(self, path: str = ":memory:") -> None
    def put_once(self, scope: str, key: str, fp: str, result: dict) -> tuple[dict, bool]
        # 不存在 → 写入 (scope,key,fp,json(result))，返回 (result, True)
        # 已存在且 fp 相同 → 返回 (已存的 result, False)，不覆盖
        # 已存在且 fp 不同 → raise IdemConflict
        # scope/key 为空字符串 → ValueError
    def get(self, scope: str, key: str) -> dict | None
    def seen(self, scope: str, key: str) -> bool
    def count(self, scope: str) -> int
    def close(self) -> None
```
- result 以 JSON 存储（ensure_ascii=False, default=str），读出时 json.loads。
- 不提供删除/覆盖方法。

## 验收
`.venv/bin/python -m pytest -q` 全部通过。`tests/test_idempotency.py` 若存在是 Bot 写的验收测试，不许修改，必须通过。
