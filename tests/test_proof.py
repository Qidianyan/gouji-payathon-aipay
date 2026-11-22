"""Acceptance tests for aipay_kit.proof (SPEC specs/T4_proof.md). Written by bot."""

import base64
import json

import pytest

from aipay_kit.proof import (
    NEEDED_FIELDS, PaymentProof, ProofError, b64url_decode_json, b64url_encode_json,
    build_payment_needed, build_payment_proof, build_payment_validation, parse_payment_needed,
    parse_payment_proof, parse_payment_validation,
)

TRADE = "2026040900828111317760000001"
OFFICIAL = {"protocol": {"payment_proof": "7cf8a6a93c92", "trade_no": TRADE}, "method": {"client_session": "ImNsaWVudFNlc3N"}}
NEEDED = {"out_trade_no": "ORDER_1", "amount": "0.01", "currency": "CNY", "resource_id": "/paid/resource/r1",
          "pay_before": "2026-11-11T13:00:00+08:00", "goods_name": "AI 可见度体检报告"}


def std_b64(obj):
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_fields_list():
    assert len(NEEDED_FIELDS) == 14 and NEEDED_FIELDS[0] == "out_trade_no" and NEEDED_FIELDS[-1] == "service_id"


def test_roundtrip_json_no_padding_and_chinese():
    s = b64url_encode_json({"a": "中文", "b": 1})
    assert "=" not in s and "+" not in s and "/" not in s
    assert b64url_decode_json(s) == {"a": "中文", "b": 1}


def test_decode_with_padding_and_standard_b64():
    obj = {"x": "??>>~~" * 5}
    s = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
    assert b64url_decode_json(s) == obj
    assert b64url_decode_json(std_b64(obj)) == obj


@pytest.mark.parametrize("bad", ["", "!!!!", base64.urlsafe_b64encode(b"[1,2]").decode(),
                                 base64.urlsafe_b64encode(b"not json").decode()])
def test_decode_errors(bad):
    with pytest.raises(ProofError):
        b64url_decode_json(bad)


def test_official_payment_proof():
    p = parse_payment_proof(std_b64(OFFICIAL))
    assert p == PaymentProof("7cf8a6a93c92", TRADE, "ImNsaWVudFNlc3N")
    assert parse_payment_proof(build_payment_proof(p)) == p


def _mut(path, value):
    o = json.loads(json.dumps(OFFICIAL))
    a, b = path
    if value is None:
        del o[a][b]
    else:
        o[a][b] = value
    return b64url_encode_json(o)


@pytest.mark.parametrize("path,value", [
    (("protocol", "payment_proof"), None), (("protocol", "payment_proof"), ""),
    (("protocol", "trade_no"), None), (("protocol", "trade_no"), "abc123"),
    (("protocol", "trade_no"), "1" * 15), (("protocol", "trade_no"), "1" * 65),
    (("method", "client_session"), None), (("method", "client_session"), ""),
    (("protocol", "payment_proof"), 123),
])
def test_proof_invalid(path, value):
    with pytest.raises(ProofError):
        parse_payment_proof(_mut(path, value))


def test_proof_trade_no_bounds_ok():
    for n in (16, 64):
        assert parse_payment_proof(_mut(("protocol", "trade_no"), "9" * n)).trade_no == "9" * n


def test_proof_header_too_long():
    with pytest.raises(ProofError):
        parse_payment_proof("A" * 8193)


def test_needed_roundtrip():
    h = build_payment_needed({**NEEDED, "service_id": "svc1", "seller_sign_type": "RSA2"})
    assert parse_payment_needed(h)["goods_name"] == "AI 可见度体检报告"


@pytest.mark.parametrize("drop", ["out_trade_no", "amount", "currency", "resource_id", "pay_before", "goods_name"])
def test_needed_missing(drop):
    with pytest.raises(ProofError):
        build_payment_needed({k: v for k, v in NEEDED.items() if k != drop})


@pytest.mark.parametrize("over", [{"amount": "1.234"}, {"amount": "-1"}, {"amount": "abc"}, {"currency": "USD"}, {"evil": "x"}])
def test_needed_invalid(over):
    with pytest.raises(ProofError):
        build_payment_needed({**NEEDED, **over})
    with pytest.raises(ProofError):
        parse_payment_needed(b64url_encode_json({**NEEDED, **over}))


def test_validation_roundtrip():
    d = parse_payment_validation(build_payment_validation(TRADE, "ORDER_1", "/r/1"))
    assert d == {"trade_no": TRADE, "out_trade_no": "ORDER_1", "resource_id": "/r/1", "validated": True}


@pytest.mark.parametrize("val", ["", "  ", 5])
def test_needed_required_must_be_nonempty_string(val):
    with pytest.raises(ProofError):
        build_payment_needed({**NEEDED, "out_trade_no": val})
