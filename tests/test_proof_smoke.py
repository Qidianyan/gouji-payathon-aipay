"""冒烟测试：aipay_kit.proof 的 402 协议头编解码基本可用性（完整矩阵见后续 tests/test_proof.py）。"""

import base64
import json

import pytest

from aipay_kit.proof import (
    NEEDED_FIELDS,
    PaymentProof,
    ProofError,
    b64url_decode_json,
    b64url_encode_json,
    build_amt_payment_link,
    build_payment_needed,
    build_payment_proof,
    build_payment_validation,
    parse_amt_payment_link,
    parse_payment_needed,
    parse_payment_proof,
    parse_payment_validation,
)


def _needed_fields() -> dict:
    return {
        "out_trade_no": "ORDER_1744721077123",
        "amount": "1000",
        "currency": "CNY",
        "resource_id": "res_001",
        "pay_before": "2026-05-01T00:00:00+08:00",
        "seller_signature": "sig_xxx",
        "seller_sign_type": "RSA2",
        "seller_unique_id": "suid_xxx",
        "seller_name": "测试商家",
        "seller_id": "2088xxxxxxxxxxxx",
        "seller_app_id": "app_xxx",
        "goods_name": "AI 生成内容服务",
        "seller_unique_id_key": "seller_id",
        "service_id": "svc_001",
    }


def test_b64url_roundtrip_padding_and_std_base64():
    obj = {"goods_name": "中文商品", "amount": "10.5"}
    encoded = b64url_encode_json(obj)
    assert "=" not in encoded  # 去掉了末尾填充
    assert b64url_decode_json(encoded) == obj
    assert b64url_decode_json(encoded + "=" * (-len(encoded) % 4)) == obj  # 有填充也能解
    std = base64.b64encode(json.dumps(obj, ensure_ascii=False).encode("utf-8")).decode("ascii")
    assert b64url_decode_json(std) == obj  # 标准 base64（+ /）也能解


def test_payment_proof_roundtrip_official_shape():
    p = PaymentProof(
        payment_proof="7cf8a6a93c924e13eaa4bf20c3a487f30d1fbdb759a1f229b4091b7d0158xxxx",
        trade_no="2026040900828111317760000001",  # 纯数字，长度 28
        client_session="xxxxxxxxxxxxxsCiAgICAic2Vzc2lvbklkIjogInh4IiwKIn0=",
    )
    header = build_payment_proof(p)
    assert parse_payment_proof(header) == p
    assert json.loads(base64.urlsafe_b64decode(header + "=="))["protocol"]["trade_no"] == p.trade_no


def test_payment_needed_roundtrip_with_chinese_goods_name():
    fields = _needed_fields()
    header = build_payment_needed(fields)
    assert parse_payment_needed(header) == fields
    assert NEEDED_FIELDS == (
        "out_trade_no", "amount", "currency", "resource_id", "pay_before", "seller_signature",
        "seller_sign_type", "seller_unique_id", "seller_name", "seller_id", "seller_app_id",
        "goods_name", "seller_unique_id_key", "service_id",
    )
    assert len(NEEDED_FIELDS) == 14


def test_payment_validation_and_amt_payment_link_roundtrip():
    header = build_payment_validation("2026040900828111317760000001", "ORDER_1", "res_001")
    assert parse_payment_validation(header) == {
        "trade_no": "2026040900828111317760000001",
        "out_trade_no": "ORDER_1",
        "validated": True,
        "resource_id": "res_001",
    }
    assert build_payment_validation("t" * 16, "o", "r", validated=False).count("=") == 0
    link = "amtpp_v1_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    assert build_amt_payment_link(link) == parse_amt_payment_link(link) == link


def test_invalid_inputs_raise_proof_error():
    f = _needed_fields()
    with pytest.raises(ProofError):  # 缺必填
        build_payment_needed({k: v for k, v in f.items() if k != "goods_name"})
    with pytest.raises(ProofError):  # 未知字段
        build_payment_needed({**f, "extra": "x"})
    for bad_amount in ("1.234", "-1", "abc", 1000):  # 非法 amount（含非字符串）
        with pytest.raises(ProofError):
            build_payment_needed({**f, "amount": bad_amount})
    with pytest.raises(ProofError):  # currency 非 CNY
        build_payment_needed({**f, "currency": "USD"})

    with pytest.raises(ProofError):  # 空字符串字段
        parse_payment_proof(
            b64url_encode_json(
                {"protocol": {"payment_proof": "", "trade_no": "2026040900828111317760000001"},
                 "method": {"client_session": "cs"}}
            )
        )
    with pytest.raises(ProofError):  # trade_no 非数字
        parse_payment_proof(
            b64url_encode_json(
                {"protocol": {"payment_proof": "p", "trade_no": "abc"},
                 "method": {"client_session": "cs"}}
            )
        )
    with pytest.raises(ProofError):  # trade_no 过短
        parse_payment_proof(
            b64url_encode_json(
                {"protocol": {"payment_proof": "p", "trade_no": "123"},
                 "method": {"client_session": "cs"}}
            )
        )
    with pytest.raises(ProofError):  # trade_no 过长
        parse_payment_proof(
            b64url_encode_json(
                {"protocol": {"payment_proof": "p", "trade_no": "1" * 65},
                 "method": {"client_session": "cs"}}
            )
        )
    with pytest.raises(ProofError):  # 超长 header
        parse_payment_proof("A" * 8193)
    with pytest.raises(ProofError):  # 非 JSON
        parse_payment_proof("not-json!!!")
    with pytest.raises(ProofError):  # JSON 数组
        parse_payment_proof(base64.urlsafe_b64encode(b"[1,2]").decode().rstrip("="))
