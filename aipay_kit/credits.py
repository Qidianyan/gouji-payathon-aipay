"""次数包账本（T3）：购买入账 / 消耗 / 按未用次数退款冲正。

存储为 stdlib sqlite3（文件路径或 ":memory:"）。所有写操作在
threading.Lock 内以 BEGIN IMMEDIATE 事务串行化，保证并发不超卖；
读操作同样持锁，避免与写并发交错。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass


class CreditsError(Exception):
    """账本业务错误；code 为错误码（INVALID / GRANT_CONFLICT / INSUFFICIENT /
    NOTHING_TO_REFUND / NOT_FOUND）。"""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
    """次数包账本。grant 按支付订单幂等；consume 按最早过期优先（FIFO）扣减；
    refund 退掉全部未用次数并按比例退金额，二者均按请求号幂等。"""

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS grants ("
        "grant_id TEXT PRIMARY KEY,"
        "account TEXT NOT NULL,"
        "credits INTEGER NOT NULL,"
        "used INTEGER NOT NULL DEFAULT 0,"
        "refunded INTEGER NOT NULL DEFAULT 0,"
        "amount_cents INTEGER NOT NULL,"
        "expires_at INTEGER NOT NULL);"
        "CREATE TABLE IF NOT EXISTS consume_requests ("
        "request_id TEXT PRIMARY KEY,"
        "account TEXT NOT NULL,"
        "n INTEGER NOT NULL,"
        "now INTEGER NOT NULL);"
        "CREATE TABLE IF NOT EXISTS refund_requests ("
        "out_request_no TEXT PRIMARY KEY,"
        "out_trade_no TEXT NOT NULL,"
        "credits INTEGER NOT NULL,"
        "amount_cents INTEGER NOT NULL)"
    )

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        # check_same_thread=False：并发访问统一由 self._lock 串行化
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._conn.executescript(self._SCHEMA)

    def grant(
        self, account: str, out_trade_no: str, credits: int, amount_cents: int, expires_at: int
    ) -> tuple[Grant, bool]:
        """入账一个次数包。同一 out_trade_no：参数一致 → (原 grant, False)；
        参数不一致 → CreditsError("GRANT_CONFLICT")；参数非法 → INVALID。"""
        if credits <= 0 or amount_cents < 0:
            raise CreditsError("INVALID", "credits must be > 0 and amount_cents >= 0")
        with self._lock:
            conn = self._require_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT account, credits, used, refunded, amount_cents, expires_at"
                    " FROM grants WHERE grant_id = ?",
                    (out_trade_no,),
                ).fetchone()
                if row is not None:
                    if (row[0], row[1], row[4], row[5]) != (account, credits, amount_cents, expires_at):
                        raise CreditsError(
                            "GRANT_CONFLICT", f"out_trade_no {out_trade_no!r} exists with different params"
                        )
                    conn.execute("COMMIT")
                    return self._row_to_grant((out_trade_no, *row)), False
                conn.execute(
                    "INSERT INTO grants (grant_id, account, credits, used, refunded, amount_cents, expires_at)"
                    " VALUES (?, ?, ?, 0, 0, ?, ?)",
                    (out_trade_no, account, credits, amount_cents, expires_at),
                )
                conn.execute("COMMIT")
                return Grant(out_trade_no, account, credits, 0, 0, amount_cents, expires_at), True
            except BaseException:
                self._rollback(conn)
                raise

    def balance(self, account: str, now: int) -> int:
        """未过期包（expires_at > now）的 credits-used-refunded 之和。"""
        with self._lock:
            row = self._require_open().execute(
                "SELECT COALESCE(SUM(credits - used - refunded), 0) FROM grants"
                " WHERE account = ? AND expires_at > ?",
                (account, now),
            ).fetchone()
        return int(row[0])

    def consume(self, account: str, n: int, now: int, request_id: str) -> int:
        """扣减 n 次：从最早过期的有效包开始扣（FIFO by expires_at, then grant_id）；
        不足 → INSUFFICIENT 且不部分扣。request_id 幂等：重复调用不重复扣，
        返回当前余额。成功返回扣后余额。"""
        if n <= 0:
            raise CreditsError("INVALID", "n must be > 0")
        with self._lock:
            conn = self._require_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute(
                    "SELECT 1 FROM consume_requests WHERE request_id = ?", (request_id,)
                ).fetchone() is not None:
                    bal = self._balance_in_txn(conn, account, now)
                    conn.execute("COMMIT")
                    return bal
                rows = conn.execute(
                    "SELECT grant_id, credits, used, refunded FROM grants"
                    " WHERE account = ? AND expires_at > ? AND credits - used - refunded > 0"
                    " ORDER BY expires_at ASC, grant_id ASC",
                    (account, now),
                ).fetchall()
                plan: list[tuple[str, int]] = []
                left = n
                for grant_id, credits, used, refunded in rows:
                    avail = int(credits) - int(used) - int(refunded)  # 已退次数不可再用
                    take = min(avail, left)
                    plan.append((grant_id, take))
                    left -= take
                    if left == 0:
                        break
                if left > 0:
                    raise CreditsError("INSUFFICIENT", f"account {account!r} lacks {n} credits")
                for grant_id, take in plan:
                    conn.execute(
                        "UPDATE grants SET used = used + ? WHERE grant_id = ?", (take, grant_id)
                    )
                conn.execute(
                    "INSERT INTO consume_requests (request_id, account, n, now) VALUES (?, ?, ?, ?)",
                    (request_id, account, n, now),
                )
                bal = self._balance_in_txn(conn, account, now)
                conn.execute("COMMIT")
                return bal
            except BaseException:
                self._rollback(conn)
                raise

    def refundable(self, out_trade_no: str, now: int) -> tuple[int, int]:
        """(可退次数, 可退金额分)。可退次数 = credits-used-refunded（过期包为 0）；
        金额 = amount_cents * 可退次数 // credits。包不存在 → (0, 0)。"""
        with self._lock:
            row = self._require_open().execute(
                "SELECT credits, used, refunded, amount_cents, expires_at"
                " FROM grants WHERE grant_id = ?",
                (out_trade_no,),
            ).fetchone()
        if row is None:
            return (0, 0)
        credits, used, refunded, amount_cents, expires_at = (int(v) for v in row)
        remain = credits - used - refunded
        if expires_at <= now or remain <= 0:
            return (0, 0)
        return (remain, amount_cents * remain // credits)

    def refund(self, out_trade_no: str, now: int, out_request_no: str) -> tuple[int, int]:
        """退掉全部可退次数，返回 (次数, 金额分)。out_request_no 幂等：重复调用
        返回第一次结果。包不存在 → NOT_FOUND；无可退 → NOTHING_TO_REFUND。"""
        with self._lock:
            conn = self._require_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                prior = conn.execute(
                    "SELECT credits, amount_cents FROM refund_requests WHERE out_request_no = ?",
                    (out_request_no,),
                ).fetchone()
                if prior is not None:
                    conn.execute("COMMIT")
                    return (int(prior[0]), int(prior[1]))
                row = conn.execute(
                    "SELECT credits, used, refunded, amount_cents, expires_at"
                    " FROM grants WHERE grant_id = ?",
                    (out_trade_no,),
                ).fetchone()
                if row is None:
                    raise CreditsError("NOT_FOUND", f"grant {out_trade_no!r} not found")
                credits, used, refunded, amount_cents, expires_at = (int(v) for v in row)
                remain = credits - used - refunded
                if expires_at <= now or remain <= 0:
                    raise CreditsError(
                        "NOTHING_TO_REFUND", f"grant {out_trade_no!r} has nothing refundable"
                    )
                amount = amount_cents * remain // credits
                conn.execute(
                    "UPDATE grants SET refunded = refunded + ? WHERE grant_id = ?",
                    (remain, out_trade_no),
                )
                conn.execute(
                    "INSERT INTO refund_requests (out_request_no, out_trade_no, credits, amount_cents)"
                    " VALUES (?, ?, ?, ?)",
                    (out_request_no, out_trade_no, remain, amount),
                )
                conn.execute("COMMIT")
                return (remain, amount)
            except BaseException:
                self._rollback(conn)
                raise

    def get(self, out_trade_no: str) -> Grant | None:
        """按 out_trade_no 取当前 Grant；不存在 → None。"""
        with self._lock:
            row = self._require_open().execute(
                "SELECT account, credits, used, refunded, amount_cents, expires_at"
                " FROM grants WHERE grant_id = ?",
                (out_trade_no,),
            ).fetchone()
        return None if row is None else self._row_to_grant((out_trade_no, *row))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- 内部工具 ----

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("CreditsLedger is closed")
        return self._conn

    def _balance_in_txn(self, conn: sqlite3.Connection, account: str, now: int) -> int:
        """在已开启的事务内计算余额（调用方须持锁）。"""
        row = conn.execute(
            "SELECT COALESCE(SUM(credits - used - refunded), 0) FROM grants"
            " WHERE account = ? AND expires_at > ?",
            (account, now),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _row_to_grant(row: tuple) -> Grant:
        grant_id, account, credits, used, refunded, amount_cents, expires_at = row
        return Grant(grant_id, account, int(credits), int(used), int(refunded), int(amount_cents), int(expires_at))

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
