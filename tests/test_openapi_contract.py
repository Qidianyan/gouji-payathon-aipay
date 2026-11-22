"""OpenApiGateway ↔ MOCK HTTP gateway contract tests + factory switching (bot-written)."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from aipay_kit import signing
from aipay_kit.gateway.base import GatewayError, PagePayRequest, PrecreateRequest
from aipay_kit.gateway.factory import load_gateway
from aipay_kit.gateway.mock import MockGateway
from aipay_kit.gateway.mock_server import build_app
from aipay_kit.gateway.openapi import OpenApiGateway, extract_response_text

APP_ID = "9021000000000000"


@pytest.fixture(scope="module")
def keys():
    return signing.generate_keypair()


@pytest.fixture
def stack(keys):
    notes = []
    gw = MockGateway(APP_ID, notifier=lambda url, p: (notes.append(p), "success")[1],
                     cashier_base="http://mock/mock/alipay")
    app = build_app(gw, {APP_ID: signing.public_pem(keys.public_key()).decode()})
    tc = TestClient(app, base_url="http://mock")
    cli = OpenApiGateway(APP_ID, signing.private_pem(keys).decode(), signing.public_pem(gw.alipay_public_key).decode(),
                         "http://mock/mock/alipay/gateway.do", client=tc, is_mock=True)
    return gw, cli, tc, notes


def test_extract_response_text():
    body = '{"a_response":{"x":"}{\\"","y":{"z":1}},"sign":"s"}'
    assert extract_response_text(body, "a_response") == '{"x":"}{\\"","y":{"z":1}}'
    assert extract_response_text(body, "b_response") is None


def test_page_pay_redirect_cashier_notify_query_refund(stack):
    gw, cli, tc, notes = stack
    url = cli.page_pay(PagePayRequest("o1", "9.90", "勾稽100次", "http://m/notify", "http://m/return"))
    u = urlparse(url)
    assert u.path == "/mock/alipay/gateway.do"
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q["method"] == "alipay.trade.page.pay" and q["sign_type"] == "RSA2"
    r = tc.get(u.path, params=q, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("/cashier/o1")
    page = tc.get("/mock/alipay/cashier/o1")
    assert "MOCK" in page.text and "9.90" in page.text
    r = tc.post("/mock/alipay/cashier/o1/pay", follow_redirects=False)
    assert r.status_code == 303 and "return" in r.headers["location"]
    assert len(notes) == 1 and cli.verify_notify(notes[0])
    s = cli.trade_query("o1")
    assert s.trade_status == "TRADE_SUCCESS" and s.trade_no == notes[0]["trade_no"]
    rf = cli.refund("o1", "9.90", "rf1")
    assert rf.fund_change and cli.trade_query("o1").trade_status == "TRADE_CLOSED"
    assert cli.refund("o1", "9.90", "rf1").fund_change is False


def test_query_not_exist_maps_error(stack):
    _, cli, _, _ = stack
    with pytest.raises(GatewayError) as e:
        cli.trade_query("nope")
    assert e.value.sub_code == "ACQ.TRADE_NOT_EXIST"


def test_bad_merchant_signature_rejected(stack, keys):
    gw, _, tc, _ = stack
    other = signing.generate_keypair()
    bad = OpenApiGateway(APP_ID, signing.private_pem(other).decode(), signing.public_pem(gw.alipay_public_key).decode(),
                         "http://mock/mock/alipay/gateway.do", client=tc)
    with pytest.raises(GatewayError) as e:
        bad.trade_query("o1")
    assert e.value.sub_code == "isv.invalid-signature"


def test_page_pay_bad_signature_400(stack):
    _, cli, tc, _ = stack
    url = cli.page_pay(PagePayRequest("o2", "1.00", "x", "http://m/n"))
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    q["biz_content"] = q["biz_content"].replace("1.00", "0.01")
    assert tc.get("/mock/alipay/gateway.do", params=q).status_code == 400


def test_response_signature_checked(stack, keys):
    gw, _, tc, _ = stack

    class Tamper:
        def post(self, url, data, headers):
            r = tc.post(url, data=data, headers=headers)
            class R: text = r.text.replace('"WAIT_BUYER_PAY"', '"TRADE_SUCCESS"')
            return R()

    gw.page_pay(PagePayRequest("o3", "1.00", "x", "http://m/n"))
    cli = OpenApiGateway(APP_ID, signing.private_pem(keys).decode(), signing.public_pem(gw.alipay_public_key).decode(),
                         "http://mock/mock/alipay/gateway.do", client=Tamper())
    with pytest.raises(GatewayError) as e:
        cli.trade_query("o3")
    assert e.value.sub_code == "RESPONSE_SIGN_INVALID"


def test_402_over_http(stack):
    gw, cli, tc, _ = stack
    pid = cli.precreate(PrecreateRequest("a1", "svc", "报告", "0.50", "2099-01-01 00:00:00", resource_id="/r/1"))
    bill = tc.get(f"/mock/alipay/a402/{pid}").json()
    assert bill["amount"] == "0.50" and "MOCK" in bill["banner"]
    proof = tc.post(f"/mock/alipay/a402/{pid}/pay").json()
    v = cli.verify_proof(proof["payment_proof"], proof["trade_no"], proof["client_session"])
    assert v.active and v.resource_id == "/r/1" and v.out_trade_no == "a1"
    assert not cli.verify_proof("bad", proof["trade_no"], proof["client_session"]).active
    assert cli.fulfillment_confirm(proof["trade_no"])
    assert not cli.fulfillment_confirm("1" * 32)


def test_402_over_limit_http(stack):
    _, cli, _, _ = stack
    with pytest.raises(GatewayError):
        cli.precreate(PrecreateRequest("a2", "svc", "报告", "51.00", "2099-01-01 00:00:00"))


# ---------------------------------------------------------------- factory
def test_factory_missing_file_is_mock(tmp_path, monkeypatch):
    monkeypatch.delenv("AIPAY_GATEWAY", raising=False)
    c = load_gateway(tmp_path / "none.env")
    assert c.mode == "mock" and c.gateway.is_mock


def _write(tmp_path, keys, mode=0o600, **extra):
    p = tmp_path / "aipay_sandbox.env"
    body = {"AIPAY_APP_ID": APP_ID,
            "AIPAY_PRIVATE_PKCS_KEY": "".join(signing.private_pem(keys).decode().splitlines()[1:-1]),
            "AIPAY_ALIPAY_PUBLIC_KEY": "".join(signing.public_pem(keys.public_key()).decode().splitlines()[1:-1])}
    body.update(extra)
    p.write_text("\n".join(f"{k}={v}" for k, v in body.items() if v is not None))
    p.chmod(mode)
    return p


def test_factory_sandbox(tmp_path, keys, monkeypatch):
    monkeypatch.delenv("AIPAY_GATEWAY", raising=False)
    c = load_gateway(_write(tmp_path, keys, AIPAY_SERVICE_ID="svc9"))
    assert c.mode == "sandbox" and not c.gateway.is_mock and c.service_id == "svc9"
    assert c.gateway.gateway_url == "https://openapi-sandbox.dl.alipaydev.com/gateway.do"


def test_factory_force_mock(tmp_path, keys, monkeypatch):
    monkeypatch.setenv("AIPAY_GATEWAY", "mock")
    assert load_gateway(_write(tmp_path, keys)).mode == "mock"


def test_factory_perms(tmp_path, keys, monkeypatch):
    monkeypatch.delenv("AIPAY_GATEWAY", raising=False)
    with pytest.raises(PermissionError):
        load_gateway(_write(tmp_path, keys, mode=0o644))


def test_factory_missing_var(tmp_path, keys, monkeypatch):
    monkeypatch.delenv("AIPAY_GATEWAY", raising=False)
    assert load_gateway(_write(tmp_path, keys, AIPAY_APP_ID="")).mode == "mock"


def test_factory_prod_refused(tmp_path, keys, monkeypatch):
    monkeypatch.delenv("AIPAY_GATEWAY", raising=False)
    with pytest.raises(RuntimeError):
        load_gateway(_write(tmp_path, keys, AIPAY_GATEWAY_URL="https://openapi.alipay.com/gateway.do"))
