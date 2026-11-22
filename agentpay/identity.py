"""Agent 与用户的 Ed25519 签名（演示用 RFC 9421 子集；完整实现见 Admit 产品 backend/app/services/sigverify）。

Agent 请求签名基串：
    "@method": GET
    "@path": /paid/resource/geo-report
    "x-mandate-id": m_xxx
    "payment-proof": <header 值或空>
    "@signature-params": keyid="kid";created=1800000000
请求头：Signature-Keyid、Signature-Created、Signature（base64）。
"""

from __future__ import annotations

import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAX_SKEW_S = 300


def new_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def pub_b64(k: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    pub = k.public_key() if isinstance(k, Ed25519PrivateKey) else k
    return base64.b64encode(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def load_pub(b64: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(b64, validate=True))


def verify(pub_b64_: str, msg: bytes, sig_b64: str) -> bool:
    try:
        load_pub(pub_b64_).verify(base64.b64decode(sig_b64, validate=True), msg)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sign(k: Ed25519PrivateKey, msg: bytes) -> str:
    return base64.b64encode(k.sign(msg)).decode()


def signature_base(method: str, path: str, mandate_id: str, payment_proof: str, keyid: str, created: int) -> bytes:
    return (
        f'"@method": {method.upper()}\n"@path": {path}\n"x-mandate-id": {mandate_id}\n'
        f'"payment-proof": {payment_proof}\n"@signature-params": keyid="{keyid}";created={created}'
    ).encode()


def canonical(obj: dict) -> bytes:
    return json.dumps({k: v for k, v in obj.items() if k != "signature"}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":")).encode()


def sign_obj(k: Ed25519PrivateKey, obj: dict) -> dict:
    return {**obj, "signature": sign(k, canonical(obj))}
