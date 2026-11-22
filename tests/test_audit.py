"""Full tests for aipay_kit.audit (SPEC: specs/T2_audit.md). Written by bot as acceptance for dsh T2."""

import csv
import io
import json
import threading

import pytest

from aipay_kit.audit import GENESIS, AuditLog, record_hash, verify_jsonl


@pytest.fixture
def log():
    lg = AuditLog()
    yield lg
    lg.close()


def fill(lg, n=5):
    for i in range(n):
        lg.append("agent:kid1", "order.step", ref=f"o{i % 2}", data={"i": i, "note": "授权内支付"}, now_ms=1000 + i)


def test_chain_links(log):
    fill(log)
    rs = log.records()
    assert [r.seq for r in rs] == [1, 2, 3, 4, 5]
    assert rs[0].prev_hash == GENESIS
    for a, b in zip(rs, rs[1:]):
        assert b.prev_hash == a.hash
    for r in rs:
        assert r.hash == record_hash(r.seq, r.ts, r.actor, r.event, r.ref, r.data, r.prev_hash)
        assert len(r.hash) == 64


def test_default_data_and_ts(log):
    r = log.append("gateway", "notify.verified")
    assert r.data == {} and r.ref == "" and r.ts > 1_600_000_000_000


def test_export_verify_ok(log):
    fill(log)
    c = verify_jsonl(log.export_jsonl())
    assert c.ok and c.count == 5 and c.first_bad_seq is None and c.reason == ""


def test_empty_export_ok(log):
    c = verify_jsonl("")
    assert c.ok and c.count == 0


def _lines(log):
    return log.export_jsonl().splitlines()


def _join(lines):
    return "\n".join(lines) + "\n"


def test_tamper_data(log):
    fill(log)
    ls = _lines(log)
    o = json.loads(ls[2]); o["data"]["i"] = 999
    ls[2] = json.dumps(o, ensure_ascii=False)
    c = verify_jsonl(_join(ls))
    assert not c.ok and c.reason == "hash_mismatch" and c.first_bad_seq == 3


def test_delete_middle(log):
    fill(log)
    ls = _lines(log); del ls[1]
    c = verify_jsonl(_join(ls))
    assert not c.ok and c.reason in ("seq_gap", "prev_mismatch")


def test_swap_rows(log):
    fill(log)
    ls = _lines(log); ls[1], ls[2] = ls[2], ls[1]
    assert not verify_jsonl(_join(ls)).ok


def test_tamper_prev_hash(log):
    fill(log)
    ls = _lines(log)
    o = json.loads(ls[3]); o["prev_hash"] = "f" * 64
    ls[3] = json.dumps(o)
    c = verify_jsonl(_join(ls))
    assert not c.ok and c.first_bad_seq == 4


def test_parse_error(log):
    fill(log)
    ls = _lines(log); ls.insert(2, "{not json")
    c = verify_jsonl(_join(ls))
    assert not c.ok and c.reason == "parse_error"


def test_first_record_must_start_from_genesis(log):
    fill(log)
    ls = _lines(log)[1:]
    assert not verify_jsonl(_join(ls)).ok


def test_ref_filter(log):
    fill(log)
    assert {r.ref for r in log.records("o1")} == {"o1"}
    assert len(log.records("o1")) == 2
    rows = [json.loads(x) for x in log.export_jsonl("o0").splitlines()]
    assert len(rows) == 3 and {x["ref"] for x in rows} == {"o0"}


def test_csv(log):
    fill(log)
    rows = list(csv.reader(io.StringIO(log.export_csv())))
    assert rows[0] == ["seq", "ts", "actor", "event", "ref", "data", "prev_hash", "hash"]
    assert len(rows) == 6
    assert json.loads(rows[1][5])["note"] == "授权内支付"


def test_chinese_not_escaped(log):
    fill(log, 1)
    assert "授权内支付" in log.export_jsonl()


def test_reopen_file_db(tmp_path):
    p = str(tmp_path / "a.db")
    lg = AuditLog(p); fill(lg, 3); lg.close()
    lg = AuditLog(p); r = lg.append("user:u1", "refund.requested")
    assert r.seq == 4
    assert verify_jsonl(lg.export_jsonl()).ok
    lg.close()


def test_threads(tmp_path):
    lg = AuditLog(str(tmp_path / "t.db"))

    def work(k):
        for j in range(20):
            lg.append(f"t{k}", "e", data={"j": j})

    ts = [threading.Thread(target=work, args=(k,)) for k in range(10)]
    [t.start() for t in ts]; [t.join() for t in ts]
    rs = lg.records()
    assert len(rs) == 200 and len({r.seq for r in rs}) == 200
    assert verify_jsonl(lg.export_jsonl()).ok
    lg.close()


def test_no_mutation_api(log):
    for name in ("update", "delete", "remove", "clear"):
        assert not hasattr(log, name)
