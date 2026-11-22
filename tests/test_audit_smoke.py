"""冒烟测试：aipay_kit.audit 哈希链审计日志的基本可用性（完整矩阵见 tests/test_audit.py）。"""

import csv
import io
import json

from aipay_kit.audit import GENESIS, AuditLog, record_hash, verify_jsonl


def test_append_builds_chain():
    lg = AuditLog()
    try:
        r1 = lg.append("agent:kid1", "mandate.created", ref="m1", data={"amount": 1}, now_ms=1000)
        r2 = lg.append("gateway", "order.precreated", ref="o1", now_ms=2000)
        assert r1.seq == 1 and r1.prev_hash == GENESIS
        assert r2.seq == 2 and r2.prev_hash == r1.hash
        assert r1.hash == record_hash(r1.seq, r1.ts, r1.actor, r1.event, r1.ref, r1.data, r1.prev_hash)
        assert len(r1.hash) == 64
    finally:
        lg.close()


def test_export_jsonl_and_verify_roundtrip():
    lg = AuditLog()
    try:
        for i in range(4):
            lg.append("user:u1", "order.step", ref=f"o{i}", data={"i": i, "备注": "中文"}, now_ms=1000 + i)
        text = lg.export_jsonl()
        assert text.endswith("\n")
        assert "中文" in text  # ensure_ascii=False
        c = verify_jsonl(text)
        assert c.ok and c.count == 4 and c.first_bad_seq is None and c.reason == ""
    finally:
        lg.close()


def test_ref_filter_and_csv_shape():
    lg = AuditLog()
    try:
        for i in range(3):
            lg.append("merchant", "fulfilled", ref="a" if i % 2 else "b", data={"i": i}, now_ms=1000 + i)
        assert [r.seq for r in lg.records("a")] == [2]
        rows = [json.loads(x) for x in lg.export_jsonl("b").splitlines()]
        assert [r["seq"] for r in rows] == [1, 3]

        csv_rows = list(csv.reader(io.StringIO(lg.export_csv())))
        assert csv_rows[0] == ["seq", "ts", "actor", "event", "ref", "data", "prev_hash", "hash"]
        assert len(csv_rows) == 4  # 表头 + 3 条
        assert json.loads(csv_rows[1][5]) == {"i": 0}
    finally:
        lg.close()


def test_verify_flags_tampered_data():
    lg = AuditLog()
    try:
        for i in range(3):
            lg.append("agent:kid1", "e", data={"i": i}, now_ms=1000 + i)
        lines = lg.export_jsonl().splitlines()
        obj = json.loads(lines[1])
        obj["data"]["i"] = 999
        lines[1] = json.dumps(obj, ensure_ascii=False)
        c = verify_jsonl("\n".join(lines) + "\n")
        assert not c.ok and c.reason == "hash_mismatch" and c.first_bad_seq == 2
    finally:
        lg.close()


def test_file_db_reopen_continues_chain(tmp_path):
    p = str(tmp_path / "audit.db")
    lg = AuditLog(p)
    lg.append("gateway", "order.precreated", ref="o1", now_ms=1000)
    lg.close()
    lg = AuditLog(p)
    try:
        r = lg.append("gateway", "fulfilled", ref="o1", now_ms=2000)
        assert r.seq == 2
        assert verify_jsonl(lg.export_jsonl()).ok
    finally:
        lg.close()
