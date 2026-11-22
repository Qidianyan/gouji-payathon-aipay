"""Acceptance tests for aipay_kit.idempotency (SPEC specs/T6_idempotency.md). Bot-written."""

import threading

import pytest

from aipay_kit.idempotency import IdemConflict, IdempotencyStore, fingerprint


@pytest.fixture
def st():
    s = IdempotencyStore()
    yield s
    s.close()


def test_fingerprint_stable_and_order_free():
    assert fingerprint({"a": 1, "b": "中"}) == fingerprint({"b": "中", "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert len(fingerprint({})) == 64


def test_put_once_first_and_repeat(st):
    r, new = st.put_once("notify", "t1", "fp", {"ok": True, "说明": "入账"})
    assert new and r == {"ok": True, "说明": "入账"}
    r2, new2 = st.put_once("notify", "t1", "fp", {"ok": False})
    assert not new2 and r2 == {"ok": True, "说明": "入账"}
    assert st.get("notify", "t1") == {"ok": True, "说明": "入账"}


def test_conflict(st):
    st.put_once("fulfil", "t1", "a", {})
    with pytest.raises(IdemConflict) as e:
        st.put_once("fulfil", "t1", "b", {})
    assert e.value.scope == "fulfil" and e.value.key == "t1"


def test_scopes_independent(st):
    st.put_once("a", "k", "1", {"x": 1})
    _, new = st.put_once("b", "k", "2", {"x": 2})
    assert new and st.count("a") == 1 and st.count("b") == 1


def test_seen_get_missing(st):
    assert not st.seen("a", "k") and st.get("a", "k") is None
    st.put_once("a", "k", "1", {})
    assert st.seen("a", "k")


@pytest.mark.parametrize("scope,key", [("", "k"), ("s", "")])
def test_empty_rejected(st, scope, key):
    with pytest.raises(ValueError):
        st.put_once(scope, key, "1", {})


def test_persist(tmp_path):
    p = str(tmp_path / "i.db")
    a = IdempotencyStore(p); a.put_once("s", "k", "1", {"v": 1}); a.close()
    b = IdempotencyStore(p)
    assert b.put_once("s", "k", "1", {"v": 9}) == ({"v": 1}, False)
    b.close()


def test_threads_single_winner(tmp_path):
    s = IdempotencyStore(str(tmp_path / "t.db"))
    wins = []

    def go(i):
        _, new = s.put_once("fulfil", "trade1", "same", {"by": i})
        if new:
            wins.append(i)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(wins) == 1 and s.count("fulfil") == 1
    s.close()


def test_no_delete_api(st):
    for n in ("delete", "remove", "clear", "update"):
        assert not hasattr(st, n)


def test_none_or_nonstr_rejected():
    import pytest as _p
    from aipay_kit.idempotency import IdempotencyStore as S
    s = S()
    for args in [(None, "k", "f"), ("s", None, "f"), ("s", "k", None), ("s", 1, "f"), ("s", "k", "")]:
        with _p.raises(ValueError):
            s.put_once(*args, {})
