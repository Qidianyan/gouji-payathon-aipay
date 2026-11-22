# T3 实现：次数包账本 aipay_kit/credits.py（Vibe Pay 勾稽）

## 目标
用 sqlite3（路径或 ":memory:"）记账：购买入账（按支付订单幂等）、消耗、按未用次数退款冲正。线程安全（threading.Lock + BEGIN IMMEDIATE）。

## 新建文件
- `aipay_kit/credits.py`
- `tests/test_credits_smoke.py`（3–5 条）

## 接口（必须一致）
```python
class CreditsError(Exception):
    code: str   # 构造：CreditsError(code, message)

@dataclass(frozen=True)
class Grant:
    grant_id: str          # = out_trade_no
    account: str
    credits: int           # 购买次数
    used: int              # 已从本包消耗
    refunded: int          # 已退回次数
    amount_cents: int      # 实付金额（分）
    expires_at: int        # unix 秒

class CreditsLedger:
    def __init__(self, path: str = ":memory:") -> None
    def grant(self, account: str, out_trade_no: str, credits: int, amount_cents: int, expires_at: int) -> tuple[Grant, bool]
        # 返回 (grant, created)。同一 out_trade_no 再调用：参数完全一致 → (原 grant, False)；不一致 → CreditsError("GRANT_CONFLICT")
        # credits <= 0 或 amount_cents < 0 → CreditsError("INVALID")
    def balance(self, account: str, now: int) -> int    # 未过期包的 credits-used-refunded 之和
    def consume(self, account: str, n: int, now: int, request_id: str) -> int
        # 从最早过期的有效包开始扣（FIFO by expires_at, then grant_id）；不足 → CreditsError("INSUFFICIENT")，不部分扣
        # request_id 幂等：同一 request_id 重复调用不重复扣，返回当前余额
        # n <= 0 → CreditsError("INVALID")
    def refundable(self, out_trade_no: str, now: int) -> tuple[int, int]
        # (可退次数, 可退金额分)。可退次数 = credits-used-refunded（过期包为 0）；金额 = amount_cents * 可退次数 // credits
    def refund(self, out_trade_no: str, now: int, out_request_no: str) -> tuple[int, int]
        # 退掉全部可退次数，返回 (次数, 金额分)。out_request_no 幂等：重复调用返回第一次结果不重复退。无可退 → CreditsError("NOTHING_TO_REFUND")；包不存在 → CreditsError("NOT_FOUND")
    def get(self, out_trade_no: str) -> Grant | None
    def close(self) -> None
```

## 验收
- `.venv/bin/python -m pytest -q` 全部通过。
