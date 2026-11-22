"""Append-only audit log with a hash chain (T2).

Every record stores the hash of the previous one, so a full export can be
independently re-verified with verify_jsonl — used for the
「交易过程留痕 / 可信存证」demo. Storage is stdlib sqlite3 (file path or ":memory:").
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any

GENESIS = "0" * 64  # 第一条记录的 prev_hash 固定值


@dataclass(frozen=True)
class AuditRecord:
    seq: int          # 从 1 开始连续
    ts: int           # unix 毫秒
    actor: str        # 如 "agent:kid123" / "user:u1" / "gateway" / "merchant"
    event: str        # 如 "mandate.created" / "order.precreated" / "fulfilled"
    ref: str          # 关联 id（out_trade_no / mandate_id），可空字符串
    data: dict[str, Any]  # JSON 可序列化
    prev_hash: str
    hash: str


def canonical_json(obj: Any) -> str:
    """确定性 JSON：键排序、无空格、中文不转义。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(
    seq: int, ts: int, actor: str, event: str, ref: str,
    data: dict[str, Any], prev_hash: str,
) -> str:
    """记录体 canonical JSON 的 sha256 hex。"""
    body = {
        "seq": seq,
        "ts": ts,
        "actor": actor,
        "event": event,
        "ref": ref,
        "data": data,
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


class AuditLog:
    """追加式哈希链审计日志。只有 append / records / 导出 / close，无修改删除接口。"""

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS audit_records ("
        "seq INTEGER PRIMARY KEY,"
        "ts INTEGER NOT NULL,"
        "actor TEXT NOT NULL,"
        "event TEXT NOT NULL,"
        "ref TEXT NOT NULL DEFAULT '',"
        "data TEXT NOT NULL DEFAULT '{}',"
        "prev_hash TEXT NOT NULL,"
        "hash TEXT NOT NULL)"
    )

    def __init__(self, path: str = ":memory:") -> None:
        self._path = path
        # check_same_thread=False：并发访问统一由 self._lock 串行化
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._conn.execute(self._SCHEMA)

    def append(
        self,
        actor: str,
        event: str,
        ref: str = "",
        data: dict[str, Any] | None = None,
        *,
        now_ms: int | None = None,
    ) -> AuditRecord:
        """追加一条记录；读最后一条与写入在同一 sqlite 事务内完成（线程安全）。"""
        ts = now_ms if now_ms is not None else time.time_ns() // 1_000_000
        payload: dict[str, Any] = {} if data is None else dict(data)
        with self._lock:
            conn = self._require_open()
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT seq, hash FROM audit_records ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    seq, prev_hash = 1, GENESIS
                else:
                    seq, prev_hash = int(row[0]) + 1, row[1]
                digest = record_hash(seq, ts, actor, event, ref, payload, prev_hash)
                conn.execute(
                    "INSERT INTO audit_records (seq, ts, actor, event, ref, data, prev_hash, hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (seq, ts, actor, event, ref, canonical_json(payload), prev_hash, digest),
                )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return AuditRecord(seq, ts, actor, event, ref, payload, prev_hash, digest)

    def records(self, ref: str | None = None) -> list[AuditRecord]:
        """按 seq 升序返回全部记录；ref 非 None 时只返回该 ref 的记录。"""
        sql = (
            "SELECT seq, ts, actor, event, ref, data, prev_hash, hash FROM audit_records"
            + (" WHERE ref = ?" if ref is not None else "")
            + " ORDER BY seq ASC"
        )
        with self._lock:
            rows = self._require_open().execute(sql, (ref,) if ref is not None else ()).fetchall()
        return [
            AuditRecord(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), r[6], r[7])
            for r in rows
        ]

    def export_jsonl(self, ref: str | None = None) -> str:
        """每行一个 canonical_json(asdict(record))，末尾带换行。"""
        return "".join(canonical_json(asdict(r)) + "\n" for r in self.records(ref))

    def export_csv(self, ref: str | None = None) -> str:
        """表头 seq,ts,actor,event,ref,data,prev_hash,hash；data 列为 canonical_json。"""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["seq", "ts", "actor", "event", "ref", "data", "prev_hash", "hash"])
        for r in self.records(ref):
            writer.writerow(
                [r.seq, r.ts, r.actor, r.event, r.ref, canonical_json(r.data), r.prev_hash, r.hash]
            )
        return buf.getvalue()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_open(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("AuditLog is closed")
        return self._conn


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    count: int
    first_bad_seq: int | None
    reason: str  # "" 或 "hash_mismatch" / "prev_mismatch" / "seq_gap" / "parse_error"


def verify_jsonl(text: str) -> ChainCheck:
    """独立校验完整导出（ref=None）：seq 从 1 连续、首条 prev_hash==GENESIS、
    每条 prev_hash==上一条 hash、hash 重算一致。空文本 → ok=True, count=0。"""
    prev_hash = GENESIS
    expected_seq = 1
    count = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("not a JSON object")
            seq = obj["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool):
                raise ValueError("bad seq")
            rec = AuditRecord(
                seq=seq,
                ts=obj["ts"],
                actor=obj["actor"],
                event=obj["event"],
                ref=obj["ref"],
                data=obj["data"],
                prev_hash=obj["prev_hash"],
                hash=obj["hash"],
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return ChainCheck(False, count, None, "parse_error")
        if rec.seq != expected_seq:
            return ChainCheck(False, count, rec.seq, "seq_gap")
        if rec.prev_hash != prev_hash:
            return ChainCheck(False, count, rec.seq, "prev_mismatch")
        if record_hash(rec.seq, rec.ts, rec.actor, rec.event, rec.ref, rec.data, rec.prev_hash) != rec.hash:
            return ChainCheck(False, count, rec.seq, "hash_mismatch")
        prev_hash = rec.hash
        expected_seq += 1
        count += 1
    return ChainCheck(True, count, None, "")
