"""按密钥环境文件选择网关（零硬编码假流水）：
- 存在合规 chmod 600 的密钥文件且变量齐全 → OpenApiGateway（沙箱 mode="sandbox" / 生产 mode="prod"）；
- 生产网关（openapi.alipay.com）须显式声明 AIPAY_ALLOW_PROD=true 安全门禁，防误触扣真钱；
- 未配置密钥文件或设置 AIPAY_GATEWAY=mock → 本地契约测试网关 MockGateway（mode="mock"）。

变量名与支付宝 AI 付控制台一致：AIPAY_APP_ID、AIPAY_PRIVATE_PKCS_KEY、AIPAY_ALIPAY_PUBLIC_KEY；
可选：AIPAY_GATEWAY_URL、AIPAY_SERVICE_ID（402 服务 ID）、AIPAY_SELLER_ID（2088 商户号）、AIPAY_ALLOW_PROD。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from aipay_kit.gateway.mock import MockGateway
from aipay_kit.gateway.openapi import PROD_URL, SANDBOX_URL, OpenApiGateway

SECRETS_DIR = Path(__file__).resolve().parents[3] / "secrets"
DEFAULT_SECRETS = SECRETS_DIR / "aipay_sandbox.env"
PROD_SECRETS = SECRETS_DIR / "aipay_prod.env"
REQUIRED = ("AIPAY_APP_ID", "AIPAY_PRIVATE_PKCS_KEY", "AIPAY_ALIPAY_PUBLIC_KEY")


@dataclass(frozen=True)
class GatewayChoice:
    gateway: object
    mode: str          # "mock" | "sandbox" | "prod"
    reason: str
    service_id: str


def read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _resolve_secrets_path(path: str | os.PathLike | None) -> Path:
    if path is not None:
        return Path(path)
    if os.environ.get("AIPAY_SECRETS"):
        return Path(os.environ["AIPAY_SECRETS"])
    if os.environ.get("AIPAY_GATEWAY") == "prod" and PROD_SECRETS.exists():
        return PROD_SECRETS
    return DEFAULT_SECRETS


def load_gateway(path: str | os.PathLike | None = None, **mock_kwargs) -> GatewayChoice:
    if os.environ.get("AIPAY_GATEWAY") == "mock":
        return GatewayChoice(MockGateway(**mock_kwargs), "mock", "环境变量强制 MOCK", "mock-service")
    p = _resolve_secrets_path(path)
    if not p.exists():
        return GatewayChoice(MockGateway(**mock_kwargs), "mock", f"{p.name} 未配置，启用本地契约模拟网关", "mock-service")
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise PermissionError(f"{p} 权限过宽，需 chmod 600")
    env = read_env_file(p)
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        return GatewayChoice(MockGateway(**mock_kwargs), "mock", f"缺少变量 {missing}", "mock-service")
    url = env.get("AIPAY_GATEWAY_URL") or (PROD_URL if os.environ.get("AIPAY_GATEWAY") == "prod" else SANDBOX_URL)
    allow_prod = (env.get("AIPAY_ALLOW_PROD", "").lower() == "true"
                  or os.environ.get("AIPAY_ALLOW_PROD", "").lower() == "true")
    if url == PROD_URL and not allow_prod:
        raise RuntimeError("正式生产网关需显式配置 AIPAY_ALLOW_PROD=true 确认签约后启用")
    gw = OpenApiGateway(
        env["AIPAY_APP_ID"],
        env["AIPAY_PRIVATE_PKCS_KEY"],
        env["AIPAY_ALIPAY_PUBLIC_KEY"],
        url,
        seller_id=env.get("AIPAY_SELLER_ID", ""),
    )
    mode = "prod" if url == PROD_URL else "sandbox"
    reason = "已加载正式生产网关密钥" if mode == "prod" else "已读取沙箱密钥"
    return GatewayChoice(gw, mode, reason, env.get("AIPAY_SERVICE_ID", ""))
