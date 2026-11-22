"""通用幂等存储（T6）：同一 (scope, key) 只允许写入一次。

用于：异步通知去重（同一 notify 多次投递只处理一次）、同一 trade_no 只履约
一次、同一 out_trade_no 只预下单一次。存储为 stdlib sqlite3（文件路径或
":memory:"），所有读写在 threading.Lock 内以 BEGIN IMMEDIATE 事务串行化。

不提供删除 / 覆盖接口；同一 (scope, key) 以不同 fingerprint 再次写入视为冲突
（IdemConflict）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from typing import Any


class IdemConflict(Exception):
    """同一 (scope, key) 已用不同 fingerprint 写入过。属性 scope, key。"""

    def __init__(self, scope: str, key: str) -> None:
        super().__init__(f"idempotency conflict: scope={scope!r} key={key!r} already stored with a different fingerprint")
        self.scope = scope
        self.key = key


def fingerprint(obj: Any) -> str:
    """obj 的确定性 sha256 hex：canonical JSON（键排序、无空格、中文不转义、
    不可序列化对象走 default=str）。"""
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """一次性写入存储。put_once 按 (scope, key) 幂等：fp 一致返回已存结果，
    fp 不一致抛 IdemConflict。"""

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS idem_records ("
        "scope TEXT NOT NULL,"
        "key TEXT NOT NULL,"
        "fp TEXT NOT NULL,"
        "result TEXT NOT NULL DEFAULT '{}',"
        "PRIMARY KEY (scope, key))"
    )

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        # check_same_thread=False：并发访问统一由 self._lock 串行化
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._conn.executescript(self._SCHEMA)

    def put_once(self, scope: str, key: str, fp: str, result: dict) -> tuple[dict, bool]:
        """一次性写入 (scope, key, fp, json(result))。

        - 不存在 → 写入并返回 (result, True)
        - 已存在且 fp 相同 → 返回 (已存的 result, False)，不覆盖
        - 已存在且 fp 不同 → raise IdemConflict
        - scope / key / fp 非字符串或为空 → ValueError（Bot 验收时加严：None 也拒绝）
        """
        if not (isinstance(scope, str) and isinstance(key, str) and scope and key and isinstance(fp, str) and fp):
            raise ValueError("scope and key must be non-empty strings")
        payload = json.dumps(result, ensure_ascii=False, default=str)
        with self._lock:
            conn = self._require_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT fp, result FROM idem_records WHERE scope = ? AND key = ?",
                    (scope, key),
                ).fetchone()
                if row is not None:
                    stored_fp, stored_result = row
                    if stored_fp != fp:
                        raise IdemConflict(scope, key)
                    conn.execute("COMMIT")
                    return json.loads(stored_result), False
                conn.execute(
                    "INSERT INTO idem_records (scope, key, fp, result) VALUES (?, ?, ?, ?)",
                    (scope, key, fp, payload),
                )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
        return json.loads(payload), True

    def get(self, scope: str, key: str) -> dict | None:
        """按 (scope, key) 取已存 result；不存在 → None。"""
        with self._lock:
            row = self._require_open().execute(
                "SELECT result FROM idem_records WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def seen(self, scope: str, key: str) -> bool:
        """(scope, key) 是否已写入过。"""
        with self._lock:
            row = self._require_open().execute(
                "SELECT 1 FROM idem_records WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        return row is not None

    def count(self, scope: str) -> int:
        """该 scope 下已写入的 key 数。"""
        with self._lock:
            row = self._require_open().execute(
                "SELECT COUNT(*) FROM idem_records WHERE scope = ?", (scope,)
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ---- 内部工具 ----

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("IdempotencyStore is closed")
        return self._conn

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
