import pytest

from aipay_kit import signing


@pytest.fixture(scope="module")
def key():
    return signing.generate_keypair()


def test_sign_content_sorted_and_skips_empty_and_sign():
    p = {"b": "2", "a": "1", "sign": "x", "c": "", "d": None, "e": " "}
    assert signing.sign_content(p) == "a=1&b=2"


def test_sign_content_notify_excludes_sign_type():
    p = {"sign_type": "RSA2", "trade_status": "TRADE_SUCCESS", "out_trade_no": "o1", "sign": "s"}
    assert signing.sign_content(p, exclude=("sign", "sign_type")) == "out_trade_no=o1&trade_status=TRADE_SUCCESS"


def test_roundtrip_and_tamper(key):
    p = {"out_trade_no": "o1", "total_amount": "0.01", "trade_status": "TRADE_SUCCESS", "sign_type": "RSA2"}
    p["sign"] = signing.rsa2_sign(signing.sign_content(p, exclude=("sign", "sign_type")), key)
    assert signing.verify_notify(p, key.public_key())
    assert not signing.verify_notify({**p, "total_amount": "100.00"}, key.public_key())
    assert not signing.verify_notify({**p, "sign_type": "RSA"}, key.public_key())
    assert not signing.verify_notify({k: v for k, v in p.items() if k != "sign"}, key.public_key())
    assert not signing.verify_notify({**p, "sign": "!!notbase64"}, key.public_key())


def test_wrong_key_rejected(key):
    other = signing.generate_keypair()
    p = {"a": "1"}
    s = signing.sign_params(p, key)
    assert signing.rsa2_verify(signing.sign_content(p), s, key.public_key())
    assert not signing.rsa2_verify(signing.sign_content(p), s, other.public_key())


def test_load_bare_base64_bodies(key):
    pub_body = b"".join(signing.public_pem(key.public_key()).splitlines()[1:-1]).decode()
    priv_body = b"".join(signing.private_pem(key).splitlines()[1:-1]).decode()
    pub = signing.load_public(pub_body)
    priv = signing.load_private(priv_body)
    s = signing.rsa2_sign("x=1", priv)
    assert signing.rsa2_verify("x=1", s, pub)
