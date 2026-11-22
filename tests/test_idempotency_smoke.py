"""冒烟测试：aipay_kit.idempotency 通用幂等存储的基本可用性。"""

import datetime
import hashlib
import json

import pytest

from aipay_kit.idempotency import IdemConflict, IdempotencyStore, fingerprint


def test_put_once_and_replay_smoke():
    st = IdempotencyStore()
    try:
        res = {"trade_no": "t1", "status": "SUCCESS"}
        out, created = st.put_once("notify", "n1", fingerprint(res), res)
        assert created and out == res
        # 同 fp 重放：返回已存结果，不覆盖、不新增
        out2, created2 = st.put_once("notify", "n1", fingerprint(res), {"x": 1})
        assert not created2 and out2 == res
        assert st.get("notify", "n1") == res
        assert st.seen("notify", "n1") and st.count("notify") == 1
    finally:
        st.close()


def test_conflict_on_different_fingerprint_smoke():
    st = IdempotencyStore()
    try:
        st.put_once("fulfill", "t1", "fp-a", {"ok": True})
        with pytest.raises(IdemConflict) as e:
            st.put_once("fulfill", "t1", "fp-b", {"ok": False})
        assert e.value.scope == "fulfill" and e.value.key == "t1"
        # 冲突写入不生效，原数据保持不变
        assert st.get("fulfill", "t1") == {"ok": True}
        assert st.count("fulfill") == 1
    finally:
        st.close()


def test_fingerprint_canonical_smoke():
    a = {"b": 2, "a": [1, "中文"]}
    b = {"a": [1, "中文"], "b": 2}  # 键序不同
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) == hashlib.sha256(
        json.dumps(a, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    assert fingerprint(a) != fingerprint({"a": [1, "中文"], "b": 3})
    assert fingerprint({"dt": datetime.datetime(2026, 1, 1)})  # default=str 兜底


def test_empty_scope_or_key_rejected_smoke():
    st = IdempotencyStore()
    try:
        with pytest.raises(ValueError):
            st.put_once("", "k", "fp", {})
        with pytest.raises(ValueError):
            st.put_once("scope", "", "fp", {})
        assert not st.seen("", "k")
    finally:
        st.close()


def test_scope_isolation_and_file_persistence_smoke(tmp_path):
    p = str(tmp_path / "idem.db")
    a = IdempotencyStore(p)
    a.put_once("precreate", "o1", fingerprint({"a": 1}), {"a": 1})
    a.put_once("precreate", "o2", fingerprint({"b": 2}), {"b": 2})
    a.put_once("notify", "n1", fingerprint({"c": 3}), {"c": 3})
    a.close()
    b = IdempotencyStore(p)
    try:
        assert b.count("precreate") == 2 and b.count("notify") == 1 and b.count("other") == 0
        assert b.get("precreate", "o1") == {"a": 1}
        assert b.get("notify", "missing") is None and not b.seen("notify", "missing")
    finally:
        b.close()
