"""Tests for code/scripts/verify_live_alipay_sandbox.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from aipay_kit import signing

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_live_alipay_sandbox.py"
SPEC = importlib.util.spec_from_file_location("verify_live_alipay_sandbox", SCRIPT_PATH)
assert SPEC and SPEC.loader
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


def test_self_test_passes_end_to_end():
    ok, results = MOD.run_self_test()
    assert ok is True
    names = {r.name for r in results}
    assert {
        "env_permissions",
        "env_variables",
        "rsa_key_load",
        "local_rsa2_signing",
        "notify_rsaCheckV1_invariants",
        "live_notify_verify",
        "live_outbound_rsa2_sign",
        "live_inbound_alipay_pubkey",
    }.issubset(names)
    assert all(r.passed for r in results)


def test_missing_env_file_fails_gracefully(tmp_path):
    ok, results = MOD.run_verification(tmp_path / "missing.env")
    assert ok is False
    assert results[0].name == "env_file" and not results[0].passed


def test_loose_permissions_rejected(tmp_path):
    p = tmp_path / "aipay_sandbox.env"
    p.write_text("AIPAY_APP_ID=123\n", encoding="utf-8")
    p.chmod(0o644)
    ok, results = MOD.run_verification(p)
    assert ok is False
    assert results[0].name == "env_permissions"


def test_placeholder_values_rejected(tmp_path):
    p = tmp_path / "aipay_sandbox.env"
    p.write_text(
        "AIPAY_APP_ID=YOUR_SANDBOX_APP_ID\n"
        "AIPAY_PRIVATE_PKCS_KEY=YOUR_PKCS8_APP_PRIVATE_KEY_BASE64\n"
        "AIPAY_ALIPAY_PUBLIC_KEY=YOUR_ALIPAY_PUBLIC_KEY_BASE64\n",
        encoding="utf-8",
    )
    p.chmod(0o600)
    ok, results = MOD.run_verification(p)
    assert ok is False
    assert any(r.name == "env_variables" and not r.passed for r in results)


def test_skip_network_with_valid_rsa_keys(tmp_path):
    app_key = signing.generate_keypair()
    alipay_key = signing.generate_keypair()
    p = tmp_path / "aipay_sandbox.env"
    p.write_text(
        "\n".join([
            "AIPAY_GATEWAY_URL=https://openapi-sandbox.dl.alipaydev.com/gateway.do",
            "AIPAY_APP_ID=9021000012345678",
            "AIPAY_SELLER_ID=2088102170001234",
            "AIPAY_PRIVATE_PKCS_KEY=" + "".join(signing.private_pem(app_key).decode().splitlines()[1:-1]),
            "AIPAY_ALIPAY_PUBLIC_KEY=" + "".join(signing.public_pem(alipay_key.public_key()).decode().splitlines()[1:-1]),
        ]) + "\n",
        encoding="utf-8",
    )
    p.chmod(0o600)
    ok, results = MOD.run_verification(p, skip_network=True)
    assert ok is True
    assert any(r.name == "local_rsa2_signing" and r.passed for r in results)
    assert any(r.name == "notify_rsaCheckV1_invariants" and r.passed for r in results)


def test_parse_notify_input_json_and_urlencoded():
    d1 = MOD._parse_notify_input('{"app_id":"9021","total_amount":"0.99"}')
    assert d1 == {"app_id": "9021", "total_amount": "0.99"}
    d2 = MOD._parse_notify_input("app_id=9021&trade_status=TRADE_SUCCESS&total_amount=0.99")
    assert d2 == {"app_id": "9021", "trade_status": "TRADE_SUCCESS", "total_amount": "0.99"}
