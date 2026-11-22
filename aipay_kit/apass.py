"""APASS（智能体可信身份基础设施 · Know Your Agent / KYA）四层可信授权与存证链（apass.kya.v1）。

严格对齐支付宝 AI 付 APASS / alipay-aipay 四大核心支柱与四层密码学哈希链：
- Layer 1 · 人类预算签名（Human Mandate Signature / 可信身份）：
    用户 Ed25519 私钥签署 admit.mandate.v1 授权单 + Agent 运行时 Ed25519 keyid 绑定。
- Layer 2 · 商户商品锁定（Merchant SKU Lock / 持续核身与意图安全）：
    商户 Admit 13 条动态准入核验结论 + 待付预算占额锁 + 商户 RSA2 签名的 HTTP 402 账单。
- Layer 3 · 支付凭据签发（Payment Credential Issuance / A402 支付核验）：
    支付宝 alipay.aipay.agent.payment.verify 验真后的 active 凭据、trade_no 与 client_session 摘要。
- Layer 4 · 履约回执哈希链（Fulfillment Receipt Hash Chain / 可信存证）：
    交付物内容 SHA-256 指纹 + fulfillment.confirm 回执状态 + 追加式审计链头哈希 + 商户 RSA2 根签名。

四层之间通过 prev_hash 逐层锚定（GENESIS -> L1 -> L2 -> L3 -> L4），任意一层任何字节被篡改均可独立检出。
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from aipay_kit import signing
from aipay_kit.audit import GENESIS, canonical_json

APASS_SCHEMA = "apass.kya.v1"

LAYER_SPECS: tuple[tuple[int, str, str, str], ...] = (
    (1, "human_mandate_signature", "可信身份 · 人类预算签名", "Ed25519 (User Mandate + Agent RFC 9421)"),
    (2, "merchant_sku_lock", "持续核身 & 意图安全 · 商户商品锁定", "Admit 13-Rule Guardrail + RSA2 402 Bill"),
    (3, "payment_credential_issuance", "A402 凭据核验 · 支付凭据签发", "alipay.aipay.agent.payment.verify"),
    (4, "fulfillment_receipt_hash_chain", "可信存证 · 履约回执哈希链", "SHA-256 Chain + fulfillment.confirm + RSA2"),
)


def sha256_hex(text: str | bytes) -> str:
    raw = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(raw).hexdigest()


def _layer_digest(layer: int, stage: str, prev_hash: str, payload: dict[str, Any]) -> str:
    envelope = {
        "schema": APASS_SCHEMA,
        "layer": layer,
        "stage": stage,
        "prev_hash": prev_hash,
        "payload": payload,
    }
    return sha256_hex(canonical_json(envelope))


def _verify_ed25519_mandate(mandate_doc: dict[str, Any]) -> bool:
    pub_b64 = mandate_doc.get("user_public_key", "")
    sig_b64 = mandate_doc.get("signature", "")
    if not isinstance(pub_b64, str) or not isinstance(sig_b64, str) or not pub_b64 or not sig_b64:
        return False
    unsigned = {k: v for k, v in mandate_doc.items() if k != "signature"}
    msg = canonical_json(unsigned).encode("utf-8")
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64, validate=True))
        pub.verify(base64.b64decode(sig_b64, validate=True), msg)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class ApassVerifyResult:
    ok: bool
    root_hash: str
    verified_layers: int
    failed_layer: int | None = None
    reason: str = ""


def build_apass_attestation(
    *,
    mandate_doc: dict[str, Any],
    agent_keyid: str,
    agent_public_key: str,
    merchant_id: str,
    sku: str,
    bill: dict[str, Any],
    amt_pre_pay_id: str,
    guardrail_decision: str,
    trade_no: str,
    payment_proof_sha256: str,
    client_session_sha256: str,
    verified_amount: str,
    verified_resource_id: str,
    delivery_sha256: str,
    fulfillment_confirmed: bool,
    audit_head: str,
    audit_count: int,
    seller_private_key,
) -> dict[str, Any]:
    """构建完整的四层 APASS · KYA 可信授权与履约存证链，并由商户私钥签署根哈希。"""
    l1_payload = {
        "mandate_id": mandate_doc["mandate_id"],
        "mandate_type": mandate_doc.get("type", "admit.mandate.v1"),
        "user_ref": mandate_doc["user_ref"],
        "user_public_key": mandate_doc["user_public_key"],
        "mandate_doc_sha256": sha256_hex(canonical_json({k: v for k, v in mandate_doc.items() if k != "signature"})),
        "user_signature": mandate_doc["signature"],
        "budget_cap": {
            "currency": mandate_doc.get("currency", "CNY"),
            "max_amount_per_tx": str(mandate_doc["max_amount_per_tx"]),
            "max_amount_total": str(mandate_doc["max_amount_total"]),
            "max_uses": mandate_doc.get("max_uses"),
            "expires_at": int(mandate_doc["expires_at"]),
            "categories": list(mandate_doc.get("categories", ())),
            "intent_keywords": list(mandate_doc.get("intent_keywords", ())),
        },
        "agent_runtime": {
            "agent_keyid": agent_keyid,
            "agent_public_key": agent_public_key,
            "auth_scheme": "Ed25519-RFC9421",
        },
    }
    l1_hash = _layer_digest(1, LAYER_SPECS[0][1], GENESIS, l1_payload)

    l2_payload = {
        "merchant_id": merchant_id,
        "sku": sku,
        "out_trade_no": bill["out_trade_no"],
        "resource_id": bill["resource_id"],
        "goods_name": bill["goods_name"],
        "locked_amount": bill["amount"],
        "currency": bill["currency"],
        "pay_before": bill["pay_before"],
        "service_id": bill.get("service_id", ""),
        "amt_pre_pay_id": amt_pre_pay_id,
        "guardrail_decision": guardrail_decision,
        "seller_sign_type": bill.get("seller_sign_type", "RSA2"),
        "seller_signature": bill["seller_signature"],
        "bill_fields": {k: v for k, v in bill.items() if k != "seller_signature"},
    }
    l2_hash = _layer_digest(2, LAYER_SPECS[1][1], l1_hash, l2_payload)

    l3_payload = {
        "trade_no": trade_no,
        "out_trade_no": bill["out_trade_no"],
        "verified_amount": verified_amount,
        "verified_resource_id": verified_resource_id,
        "active": True,
        "payment_proof_sha256": payment_proof_sha256,
        "client_session_sha256": client_session_sha256,
        "verify_method": "alipay.aipay.agent.payment.verify",
    }
    l3_hash = _layer_digest(3, LAYER_SPECS[2][1], l2_hash, l3_payload)

    l4_payload = {
        "trade_no": trade_no,
        "out_trade_no": bill["out_trade_no"],
        "delivery_sha256": delivery_sha256,
        "fulfillment_confirmed": bool(fulfillment_confirmed),
        "confirm_method": "alipay.aipay.agent.fulfillment.confirm",
        "audit_head": audit_head,
        "audit_count": int(audit_count),
    }
    l4_hash = _layer_digest(4, LAYER_SPECS[3][1], l3_hash, l4_payload)

    layers = [
        {"layer": 1, "stage": LAYER_SPECS[0][1], "pillar": LAYER_SPECS[0][2], "crypto": LAYER_SPECS[0][3],
         "prev_hash": GENESIS, "hash": l1_hash, "payload": l1_payload},
        {"layer": 2, "stage": LAYER_SPECS[1][1], "pillar": LAYER_SPECS[1][2], "crypto": LAYER_SPECS[1][3],
         "prev_hash": l1_hash, "hash": l2_hash, "payload": l2_payload},
        {"layer": 3, "stage": LAYER_SPECS[2][1], "pillar": LAYER_SPECS[2][2], "crypto": LAYER_SPECS[2][3],
         "prev_hash": l2_hash, "hash": l3_hash, "payload": l3_payload},
        {"layer": 4, "stage": LAYER_SPECS[3][1], "pillar": LAYER_SPECS[3][2], "crypto": LAYER_SPECS[3][3],
         "prev_hash": l3_hash, "hash": l4_hash, "payload": l4_payload},
    ]
    doc: dict[str, Any] = {
        "schema": APASS_SCHEMA,
        "framework": "Alipay APASS · Know Your Agent (KYA) & ACT Protocol",
        "mandate_doc": mandate_doc,
        "layers": layers,
        "root_hash": l4_hash,
    }
    doc["merchant_root_signature"] = signing.rsa2_sign(
        canonical_json({"schema": APASS_SCHEMA, "root_hash": l4_hash, "layers": layers}),
        seller_private_key,
    )
    return doc


def verify_apass_attestation(doc: dict[str, Any], merchant_public_key) -> ApassVerifyResult:
    """独立校验 APASS 四层存证包：逐层 prev_hash/hash 重算 + L1 用户 Ed25519 验签 + L2 账单验签 + L4 根签名校验。"""
    if not isinstance(doc, dict) or doc.get("schema") != APASS_SCHEMA:
        return ApassVerifyResult(False, "", 0, 1, "SCHEMA_MISMATCH")
    layers = doc.get("layers")
    mandate_doc = doc.get("mandate_doc")
    if not isinstance(layers, list) or len(layers) != 4 or not isinstance(mandate_doc, dict):
        return ApassVerifyResult(False, "", 0, 1, "MALFORMED_LAYERS")

    pub = signing.load_public(merchant_public_key) if isinstance(merchant_public_key, (str, bytes)) else merchant_public_key
    prev = GENESIS
    for idx, spec in enumerate(LAYER_SPECS):
        expected_num, expected_stage, _, _ = spec
        entry = layers[idx]
        if not isinstance(entry, dict) or entry.get("layer") != expected_num or entry.get("stage") != expected_stage:
            return ApassVerifyResult(False, "", idx, expected_num, "LAYER_META_MISMATCH")
        if entry.get("prev_hash") != prev:
            return ApassVerifyResult(False, "", idx, expected_num, "PREV_HASH_BROKEN")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return ApassVerifyResult(False, "", idx, expected_num, "PAYLOAD_INVALID")
        recomputed = _layer_digest(expected_num, expected_stage, prev, payload)
        if entry.get("hash") != recomputed:
            return ApassVerifyResult(False, "", idx, expected_num, "LAYER_HASH_TAMPERED")

        if expected_num == 1:
            if not _verify_ed25519_mandate(mandate_doc):
                return ApassVerifyResult(False, "", 0, 1, "L1_MANDATE_SIGNATURE_INVALID")
            unsigned_sha = sha256_hex(canonical_json({k: v for k, v in mandate_doc.items() if k != "signature"}))
            if payload.get("mandate_doc_sha256") != unsigned_sha or payload.get("user_signature") != mandate_doc.get("signature"):
                return ApassVerifyResult(False, "", 0, 1, "L1_MANDATE_DIGEST_MISMATCH")
        elif expected_num == 2:
            bill_fields = payload.get("bill_fields")
            bill_sig = payload.get("seller_signature", "")
            if not isinstance(bill_fields, dict) or not signing.rsa2_verify(signing.sign_content(bill_fields), bill_sig, pub):
                return ApassVerifyResult(False, "", 1, 2, "L2_BILL_SIGNATURE_INVALID")
        elif expected_num == 3:
            l2 = layers[1]["payload"]
            if (payload.get("active") is not True
                    or payload.get("out_trade_no") != l2.get("out_trade_no")
                    or payload.get("verified_amount") != l2.get("locked_amount")
                    or payload.get("verified_resource_id") != l2.get("resource_id")):
                return ApassVerifyResult(False, "", 2, 3, "L3_CREDENTIAL_MISMATCH")
        elif expected_num == 4:
            l3 = layers[2]["payload"]
            if (payload.get("trade_no") != l3.get("trade_no")
                    or payload.get("out_trade_no") != l3.get("out_trade_no")
                    or not payload.get("delivery_sha256")):
                return ApassVerifyResult(False, "", 3, 4, "L4_FULFILLMENT_MISMATCH")

        prev = recomputed

    root_hash = doc.get("root_hash", "")
    if root_hash != prev:
        return ApassVerifyResult(False, "", 3, 4, "ROOT_HASH_MISMATCH")
    root_sig = doc.get("merchant_root_signature", "")
    signed_text = canonical_json({"schema": APASS_SCHEMA, "root_hash": root_hash, "layers": layers})
    if not isinstance(root_sig, str) or not signing.rsa2_verify(signed_text, root_sig, pub):
        return ApassVerifyResult(False, root_hash, 3, 4, "ROOT_SIGNATURE_INVALID")
    return ApassVerifyResult(True, root_hash, 4, None, "")
