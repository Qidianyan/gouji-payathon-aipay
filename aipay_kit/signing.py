"""RSA2 (SHA256withRSA) signing in the shape the Alipay open platform uses.

Sign content rule (official SDK `AlipaySignature.getSignCheckContentV1` / 402 example doc §5.4):
  drop `sign` (and for notify verification also `sign_type`), drop empty values, sort keys
  lexicographically, join as k=v with '&'. Signature = base64(RSA-PKCS1v15-SHA256(content, utf-8)).
Sources: https://aipay.alipay.com/docs/vibe-pay/ai-web-app-payment-qianyi/ai-web-app-payment-integration-guide.md
(notify verify via rsaCheckV1), alipay/ai code_example/aipay-402-example (seller signature).
"""

from __future__ import annotations

import base64
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def sign_content(params: Mapping[str, object], *, exclude: tuple[str, ...] = ("sign",)) -> str:
    items = []
    for k in sorted(params):
        if k in exclude:
            continue
        v = params[k]
        if v is None:
            continue
        s = str(v)
        if s.strip() == "":
            continue
        items.append(f"{k}={s}")
    return "&".join(items)


def generate_keypair(bits: int = 2048) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=bits)


def private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def public_pem(key: rsa.RSAPublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def load_private(pem_or_b64: str | bytes) -> rsa.RSAPrivateKey:
    data = _as_pem(pem_or_b64, "PRIVATE KEY")
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("not an RSA private key")
    return key


def load_public(pem_or_b64: str | bytes) -> rsa.RSAPublicKey:
    data = _as_pem(pem_or_b64, "PUBLIC KEY")
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("not an RSA public key")
    return key


def _as_pem(v: str | bytes, label: str) -> bytes:
    """Accept PEM or the bare base64 body Alipay consoles show (MIIB.../MIIE...)."""
    s = v.decode() if isinstance(v, bytes) else v
    s = s.strip()
    if s.startswith("-----BEGIN"):
        return s.encode()
    body = "".join(s.split())
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    return (f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n").encode()


def rsa2_sign(content: str, key: rsa.RSAPrivateKey) -> str:
    sig = key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


def rsa2_verify(content: str, signature_b64: str, key: rsa.RSAPublicKey) -> bool:
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    try:
        key.verify(sig, content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


def sign_params(params: Mapping[str, object], key: rsa.RSAPrivateKey) -> str:
    return rsa2_sign(sign_content(params), key)


def verify_notify(params: Mapping[str, str], key: rsa.RSAPublicKey) -> bool:
    """rsaCheckV1 equivalent: signature over all params except sign and sign_type."""
    sig = params.get("sign")
    if not sig:
        return False
    if params.get("sign_type", "RSA2") != "RSA2":
        return False
    return rsa2_verify(sign_content(params, exclude=("sign", "sign_type")), sig, key)
