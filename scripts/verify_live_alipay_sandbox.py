#!/usr/bin/env python3
"""支付宝 AI 付（PAYATHON 2026）沙箱 / 正式网关一键连通性与验签校验脚本。

功能清单（单命令跑通，严禁打印私钥明文）：
1. 校验密钥文件存在性、chmod 600 权限及必填字段完整性（拦截未替换的 YOUR_* 占位值）；
2. 加载应用 PKCS8 私钥与支付宝公钥，验证 RSA 模长（≥2048 bit）及本地 RSA2 签名/防篡改自环；
3. 生成标准 alipay.trade.page.pay、page_pay_form、alipay.trade.wap.pay 与 A2M 商家账单签名；
4. 向支付宝开放平台网关发起只读零资金探测请求（alipay.trade.query，查询不存在探测单号）：
   - 确认出站 RSA2 请求签名被支付宝网关认可（返回 ACQ.TRADE_NOT_EXIST 而非 isv.invalid-signature）；
   - 使用 AIPAY_ALIPAY_PUBLIC_KEY 实时验证支付宝返回 JSON 原文的 RSA2 响应签名；
5. 校验异步通知 rsaCheckV1（OpenApiGateway.verify_notify）安全边界，并支持传入真实捕获的回调报文验真；
6. 支持 --self-test 模式：在无外部密钥时通过本地契约 HTTP 网关执行全链路自检。

用法：
  cd code && .venv/bin/python scripts/verify_live_alipay_sandbox.py
  cd code && .venv/bin/python scripts/verify_live_alipay_sandbox.py --env ../secrets/aipay_prod.env
  cd code && .venv/bin/python scripts/verify_live_alipay_sandbox.py --notify-file /tmp/notify.json
  cd code && .venv/bin/python scripts/verify_live_alipay_sandbox.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# 确保从任意工作目录直接运行脚本时都能导入 aipay_kit
CODE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_DIR.parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from aipay_kit import signing  # noqa: E402
from aipay_kit.gateway.base import PagePayRequest, WapPayRequest  # noqa: E402
from aipay_kit.gateway.factory import DEFAULT_SECRETS, PROD_SECRETS, read_env_file  # noqa: E402
from aipay_kit.gateway.openapi import OpenApiGateway, PROD_URL, SANDBOX_URL, extract_response_text  # noqa: E402

PLACEHOLDER_PREFIXES = ("YOUR_", "REPLACE_", "CHANGE_ME", "<")
REQUIRED_VARS = ("AIPAY_APP_ID", "AIPAY_PRIVATE_PKCS_KEY", "AIPAY_ALIPAY_PUBLIC_KEY")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def _is_placeholder(val: str) -> bool:
    s = val.strip()
    if not s:
        return True
    upper = s.upper()
    return any(upper.startswith(p) for p in PLACEHOLDER_PREFIXES)


def _pub_fingerprint(pub_key) -> str:
    return hashlib.sha256(signing.public_pem(pub_key)).hexdigest()[:16]


def _parse_notify_input(raw: str) -> dict[str, str]:
    text = raw.strip()
    if not text:
        raise ValueError("异步通知报文为空")
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("JSON 通知报文必须为对象")
        return {str(k): "" if v is None else str(v) for k, v in data.items()}
    parsed = parse_qs(text, keep_blank_values=True)
    if not parsed:
        raise ValueError("无法解析异步通知报文（需为 JSON 或 application/x-www-form-urlencoded）")
    return {k: v[0] for k, v in parsed.items()}


def _build_signed_notify(app_id: str, seller_id: str, alipay_priv_key) -> dict[str, str]:
    params: dict[str, str] = {
        "notify_time": "2026-11-19 12:00:00",
        "notify_type": "trade_status_sync",
        "notify_id": "20261119002221200000000001",
        "app_id": app_id,
        "charset": "utf-8",
        "version": "1.0",
        "sign_type": "RSA2",
        "trade_no": "2026111922001400000500000001",
        "out_trade_no": "GJ20261119VERIFY0001",
        "trade_status": "TRADE_SUCCESS",
        "total_amount": "0.99",
    }
    if seller_id:
        params["seller_id"] = seller_id
    content = signing.sign_content(params, exclude=("sign", "sign_type"))
    params["sign"] = signing.rsa2_sign(content, alipay_priv_key)
    return params


def run_verification(
    env_path: Path,
    *,
    client: Any = None,
    notify_payload: dict[str, str] | None = None,
    skip_network: bool = False,
) -> tuple[bool, list[CheckResult]]:
    results: list[CheckResult] = []

    # 1. 文件存在性与权限检查
    if not env_path.exists():
        results.append(CheckResult(
            "env_file",
            False,
            f"未找到密钥文件 {env_path}。请先执行：cp secrets/aipay_sandbox.env.example secrets/aipay_sandbox.env && chmod 600 secrets/aipay_sandbox.env",
        ))
        return False, results

    mode = env_path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        results.append(CheckResult(
            "env_permissions",
            False,
            f"密钥文件权限过宽（{oct(mode & 0o777)}），请先执行：chmod 600 {env_path}",
        ))
        return False, results
    results.append(CheckResult("env_permissions", True, f"{env_path.name} 权限合规（0600）"))

    # 2. 必填字段与占位符检测
    env = read_env_file(env_path)
    missing_or_placeholder = [k for k in REQUIRED_VARS if _is_placeholder(env.get(k, ""))]
    if missing_or_placeholder:
        results.append(CheckResult(
            "env_variables",
            False,
            f"存在未填写或仍为占位符的必填字段：{', '.join(missing_or_placeholder)}（请从支付宝开放平台控制台粘贴真实凭证）",
        ))
        return False, results

    gateway_url = env.get("AIPAY_GATEWAY_URL") or SANDBOX_URL
    allow_prod = env.get("AIPAY_ALLOW_PROD", "").lower() == "true" or os.environ.get("AIPAY_ALLOW_PROD", "").lower() == "true"
    if gateway_url == PROD_URL and not allow_prod:
        results.append(CheckResult(
            "prod_guard",
            False,
            "检测到生产网关 https://openapi.alipay.com/gateway.do，但未显式开启 AIPAY_ALLOW_PROD=true 安全门禁",
        ))
        return False, results

    env_kind = "prod" if gateway_url == PROD_URL else ("sandbox" if gateway_url == SANDBOX_URL else "custom")
    app_id = env["AIPAY_APP_ID"]
    seller_id = env.get("AIPAY_SELLER_ID", "")
    if _is_placeholder(seller_id):
        seller_id = ""
    service_id = env.get("AIPAY_SERVICE_ID", "")
    if _is_placeholder(service_id):
        service_id = ""
    results.append(CheckResult(
        "env_variables",
        True,
        f"mode={env_kind} gateway={gateway_url} app_id={app_id[:4]}***{app_id[-4:] if len(app_id) >= 8 else ''} seller_id={'已配置' if seller_id else '未填(可选)'} service_id={'已配置' if service_id else '未填(A402可选)'}",
    ))

    # 3. 加载 RSA2 密钥对并做密码学强度与签名自环校验
    try:
        app_priv = signing.load_private(env["AIPAY_PRIVATE_PKCS_KEY"])
        alipay_pub = signing.load_public(env["AIPAY_ALIPAY_PUBLIC_KEY"])
    except Exception as exc:
        results.append(CheckResult("rsa_key_load", False, f"密钥格式解析失败：{exc}"))
        return False, results

    if app_priv.key_size < 2048 or alipay_pub.key_size < 2048:
        results.append(CheckResult(
            "rsa_key_strength",
            False,
            f"RSA2 要求密钥长度 ≥2048 bit（当前 app={app_priv.key_size}, alipay={alipay_pub.key_size}）",
        ))
        return False, results

    results.append(CheckResult(
        "rsa_key_load",
        True,
        f"app_private={app_priv.key_size}bit(pub_sha256={_pub_fingerprint(app_priv.public_key())}) alipay_public={alipay_pub.key_size}bit(sha256={_pub_fingerprint(alipay_pub)})",
    ))

    gw = OpenApiGateway(
        app_id,
        env["AIPAY_PRIVATE_PKCS_KEY"],
        env["AIPAY_ALIPAY_PUBLIC_KEY"],
        gateway_url,
        seller_id=seller_id,
        client=client,
    )

    # 4. 本地 RSA2 签名构造校验（page.pay / page_pay_form / wap.pay / A2M 账单）
    notify_url = env.get("AIPAY_NOTIFY_URL") or "https://example.com/alipay/notify"
    return_url = env.get("AIPAY_RETURN_URL") or "https://example.com/order.html"
    probe_out_trade_no = f"PAYATHONPROBE{int(time.time())}"

    page_url = gw.page_pay(PagePayRequest(probe_out_trade_no, "0.99", "勾稽5次极速体验包", notify_url, return_url))
    wap_url = gw.wap_pay(WapPayRequest(probe_out_trade_no, "0.99", "勾稽5次极速体验包", notify_url, return_url))
    form_html = gw.page_pay_form(PagePayRequest(probe_out_trade_no, "0.99", "勾稽5次极速体验包", notify_url, return_url))

    q_page = {k: v[0] for k, v in parse_qs(urlparse(page_url).query).items()}
    q_wap = {k: v[0] for k, v in parse_qs(urlparse(wap_url).query).items()}
    page_sig_ok = signing.rsa2_verify(signing.sign_content(q_page), q_page.get("sign", ""), app_priv.public_key())
    wap_sig_ok = signing.rsa2_verify(signing.sign_content(q_wap), q_wap.get("sign", ""), app_priv.public_key())
    tampered_q = dict(q_page)
    tampered_q["biz_content"] = tampered_q["biz_content"].replace("0.99", "0.01")
    tamper_rejected = not signing.rsa2_verify(signing.sign_content(tampered_q), tampered_q["sign"], app_priv.public_key())

    a2m_fields = {
        "amount": "0.50",
        "currency": "CNY",
        "goods_name": "AI 可见度体检报告",
        "out_trade_no": probe_out_trade_no,
        "pay_before": "2026-12-09 23:59:59",
        "resource_id": "/paid/resource/geo-report",
        "seller_id": seller_id or "2088000000000000",
        "service_id": service_id or "svc-payathon-probe",
    }
    a2m_sig = gw.sign_a2m_bill(a2m_fields)
    a2m_sig_ok = signing.rsa2_verify(signing.sign_content(a2m_fields), a2m_sig, app_priv.public_key())

    local_sign_ok = page_sig_ok and wap_sig_ok and tamper_rejected and a2m_sig_ok and ('id="alipay_submit"' in form_html)
    results.append(CheckResult(
        "local_rsa2_signing",
        local_sign_ok,
        "page.pay / page_pay_form / wap.pay / A2M bill RSA2 签名与防篡改校验通过" if local_sign_ok else "本地 RSA2 签名校验失败",
    ))
    if not local_sign_ok:
        return False, results

    # 5. 异步通知回调 rsaCheckV1 安全边界校验（及可选真实通知报文验真）
    ephemeral_alipay_key = signing.generate_keypair()
    synth_gw = OpenApiGateway(
        app_id,
        env["AIPAY_PRIVATE_PKCS_KEY"],
        signing.public_pem(ephemeral_alipay_key.public_key()).decode(),
        gateway_url,
        seller_id=seller_id or "2088999900001111",
        client=client,
    )
    good_notify = _build_signed_notify(app_id, synth_gw.seller_id, ephemeral_alipay_key)
    bad_amount_notify = dict(good_notify, total_amount="99.00")
    bad_app_notify = dict(good_notify, app_id="0000000000000000")
    bad_seller_notify = dict(good_notify, seller_id="2088000099998888")
    bad_signtype_notify = dict(good_notify, sign_type="RSA")

    notify_invariants_ok = (
        synth_gw.verify_notify(good_notify)
        and not synth_gw.verify_notify(bad_amount_notify)
        and not synth_gw.verify_notify(bad_app_notify)
        and not synth_gw.verify_notify(bad_seller_notify)
        and not synth_gw.verify_notify(bad_signtype_notify)
    )
    results.append(CheckResult(
        "notify_rsaCheckV1_invariants",
        notify_invariants_ok,
        "verify_notify(rsaCheckV1) 正例验签 + 金额篡改/错 app_id/错 seller_id/非 RSA2 全拦截通过"
        if notify_invariants_ok
        else "verify_notify 安全边界校验未通过",
    ))
    if not notify_invariants_ok:
        return False, results

    if notify_payload is not None:
        live_notify_ok = gw.verify_notify(notify_payload)
        results.append(CheckResult(
            "live_notify_verify",
            live_notify_ok,
            f"实测异步通知报文 rsaCheckV1 验签通过（out_trade_no={notify_payload.get('out_trade_no', '-')}, trade_status={notify_payload.get('trade_status', '-')}）"
            if live_notify_ok
            else "实测异步通知报文 rsaCheckV1 验签失败（请核对 AIPAY_ALIPAY_PUBLIC_KEY、AIPAY_APP_ID 与 AIPAY_SELLER_ID）",
        ))
        if not live_notify_ok:
            return False, results

    # 6. 实网/网关只读探活（alipay.trade.query）：一次请求双向核验出站私钥签名 + 入站支付宝公钥验签
    if skip_network:
        results.append(CheckResult("live_gateway_probe", True, "已按 --skip-network 跳过实时网关网络探测"))
        return all(r.passed for r in results), results

    try:
        params = gw._common("alipay.trade.query", {"out_trade_no": probe_out_trade_no})
        resp = gw._client.post(
            gw.gateway_url,
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        )
        body = resp.text
        raw_resp = extract_response_text(body, "alipay_trade_query_response")
        if raw_resp is None:
            results.append(CheckResult(
                "live_gateway_probe",
                False,
                f"网关返回非标准 OpenAPI JSON（HTTP {resp.status_code}）：{body[:180]}",
            ))
            return False, results

        parsed_body = json.loads(body)
        resp_data = json.loads(raw_resp)
        resp_sign = parsed_body.get("sign", "")
        code = str(resp_data.get("code", ""))
        sub_code = str(resp_data.get("sub_code", ""))
        sub_msg = str(resp_data.get("sub_msg") or resp_data.get("msg") or "")

        # 支付宝验签我方私钥通过的标准信号：10000 或 40004/ACQ.TRADE_NOT_EXIST
        outbound_ok = (code == "10000") or (code == "40004" and sub_code == "ACQ.TRADE_NOT_EXIST")
        results.append(CheckResult(
            "live_outbound_rsa2_sign",
            outbound_ok,
            f"支付宝网关认可我方 RSA2 签名（code={code}, sub_code={sub_code or 'NONE'}）"
            if outbound_ok
            else f"支付宝网关拒绝请求（code={code}, sub_code={sub_code}, msg={sub_msg}）——请核对 APP_ID 与应用公钥/私钥匹配关系",
        ))

        # 使用配置的 AIPAY_ALIPAY_PUBLIC_KEY 校验支付宝返回体原串的 RSA2 签名
        if resp_sign:
            inbound_ok = signing.rsa2_verify(raw_resp, resp_sign, gw.alipay_public_key)
            results.append(CheckResult(
                "live_inbound_alipay_pubkey",
                inbound_ok,
                "支付宝网关返回体 RSA2 验签通过（AIPAY_ALIPAY_PUBLIC_KEY 与 notify 回调验签公钥一致且有效）"
                if inbound_ok
                else "支付宝网关返回体 RSA2 验签失败——请确认 AIPAY_ALIPAY_PUBLIC_KEY 填的是「支付宝公钥」而非「应用公钥」",
            ))
        else:
            results.append(CheckResult(
                "live_inbound_alipay_pubkey",
                False,
                f"网关错误响应未携带 sign（code={code}, sub_code={sub_code}），无法完成入站公钥验签",
            ))
    except Exception as exc:
        results.append(CheckResult("live_gateway_probe", False, f"实时网关通信异常：{exc}"))
        return False, results

    return all(r.passed for r in results), results


def run_self_test() -> tuple[bool, list[CheckResult]]:
    """无外部密钥时通过本地契约 HTTP 网关完整演练 RSA2 签名、探活响应验签与异步通知回调验签。"""
    from fastapi.testclient import TestClient

    from aipay_kit.gateway.mock import MOCK_SELLER_ID, MockGateway
    from aipay_kit.gateway.mock_server import build_app

    app_id = "9021000000000999"
    seller_id = MOCK_SELLER_ID
    app_keys = signing.generate_keypair()
    captured_notifies: list[dict[str, str]] = []

    mock_gw = MockGateway(
        app_id,
        notifier=lambda _url, payload: (captured_notifies.append(payload), "success")[1],
        cashier_base="http://mock/mock/alipay",
    )
    mock_app = build_app(mock_gw, {app_id: signing.public_pem(app_keys.public_key()).decode()})
    tc = TestClient(mock_app, base_url="http://mock")

    # 通过 mock 网关执行一次真实收银台付款以捕获带真实 RSA2 签名的异步通知报文
    cli_for_notify = OpenApiGateway(
        app_id,
        signing.private_pem(app_keys).decode(),
        signing.public_pem(mock_gw.alipay_public_key).decode(),
        "http://mock/mock/alipay/gateway.do",
        seller_id=seller_id,
        client=tc,
    )
    pay_url = cli_for_notify.page_pay(
        PagePayRequest("SELF_TEST_001", "0.99", "勾稽自检订单", "http://mock/alipay/notify", "http://mock/return")
    )
    parsed = urlparse(pay_url)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    tc.get(parsed.path, params=q, follow_redirects=False)
    tc.post("/mock/alipay/cashier/SELF_TEST_001/pay", follow_redirects=False)
    live_notify = captured_notifies[0] if captured_notifies else None

    with tempfile.TemporaryDirectory(prefix="payathon_verify_") as tmp:
        env_file = Path(tmp) / "aipay_sandbox.env"
        priv_b64 = "".join(signing.private_pem(app_keys).decode().splitlines()[1:-1])
        pub_b64 = "".join(signing.public_pem(mock_gw.alipay_public_key).decode().splitlines()[1:-1])
        env_file.write_text(
            "\n".join([
                "AIPAY_GATEWAY_URL=http://mock/mock/alipay/gateway.do",
                f"AIPAY_APP_ID={app_id}",
                f"AIPAY_SELLER_ID={seller_id}",
                "AIPAY_SERVICE_ID=svc-self-test",
                f"AIPAY_PRIVATE_PKCS_KEY={priv_b64}",
                f"AIPAY_ALIPAY_PUBLIC_KEY={pub_b64}",
            ]) + "\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        return run_verification(env_file, client=tc, notify_payload=live_notify)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="支付宝 AI 付（PAYATHON 2026）沙箱/正式网关 RSA2 签名与回调验签自检")
    parser.add_argument("--env", type=Path, default=None, help="密钥文件路径（默认 secrets/aipay_sandbox.env；传 prod 可用 secrets/aipay_prod.env）")
    parser.add_argument("--prod", action="store_true", help="校验正式生产密钥文件 secrets/aipay_prod.env")
    parser.add_argument("--notify-payload", type=str, default=None, help="可选：粘贴真实支付宝 POST /alipay/notify 回调报文（JSON 或 URL 表单）做 rsaCheckV1 验签")
    parser.add_argument("--notify-file", type=Path, default=None, help="可选：从文件读取真实支付宝异步通知回调报文做 rsaCheckV1 验签")
    parser.add_argument("--skip-network", action="store_true", help="仅做本地密钥加载、RSA2 签名与回调边界校验，不发起外网请求")
    parser.add_argument("--self-test", action="store_true", help="使用本地契约 HTTP 网关与临时 RSA-2048 密钥跑通全流程自检（零外网依赖）")
    args = parser.parse_args(argv)

    notify_payload: dict[str, str] | None = None
    if args.notify_payload:
        notify_payload = _parse_notify_input(args.notify_payload)
    elif args.notify_file:
        notify_payload = _parse_notify_input(args.notify_file.read_text(encoding="utf-8"))

    if args.self_test:
        ok, results = run_self_test()
        banner = "SELF-TEST (Local RSA2 Contract Gateway)"
    else:
        env_path = args.env or (PROD_SECRETS if args.prod else DEFAULT_SECRETS)
        ok, results = run_verification(env_path, notify_payload=notify_payload, skip_network=args.skip_network)
        banner = f"LIVE VERIFY ({env_path})"

    print(f"=== PAYATHON 2026 Alipay Gateway Verify :: {banner} ===")
    for item in results:
        tag = "PASS" if item.passed else "FAIL"
        print(f"  [{tag}] {item.name}: {item.detail}")
    print(f"=== SUMMARY: {'ALL PASSED' if ok else 'FAILED'} ({sum(r.passed for r in results)}/{len(results)}) ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
