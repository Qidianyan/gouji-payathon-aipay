"""402 协议头编解码（T4）。

支持官方两套 402 约定与两个对称头：

- ``Payment-Needed``（402 响应头）：Base64URL 编码的 JSON，14 个字段见 ``NEEDED_FIELDS``。
- ``amtPaymentLink: <amt_pre_pay_id>``（另一份官方文档的 402 格式）：头值即预下单号。
- ``Payment-Proof``（重试请求头）：``{"protocol": {...}, "method": {...}}`` 结构。
- ``Payment-Validation``（200 响应头）：``{trade_no, out_trade_no, validated, resource_id}``。

纯函数模块：不碰网络、不验签（验签属于 aipay_kit.signing）。
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

__all__ = [
    "ProofError",
    "PaymentProof",
    "NEEDED_FIELDS",
    "PROTOCOL_NEEDED_FIELDS",
    "METHOD_NEEDED_FIELDS",
    "b64url_encode_json",
    "b64url_decode_json",
    "parse_payment_proof",
    "build_payment_proof",
    "build_payment_needed",
    "parse_payment_needed",
    "build_a2m_payment_needed",
    "parse_a2m_payment_needed",
    "parse_any_payment_needed",
    "build_payment_validation",
    "parse_payment_validation",
    "build_amt_payment_link",
    "parse_amt_payment_link",
]

# Payment-Proof 头值长度上限（字符数），超长直接拒绝，避免解码DoS。
_MAX_PROOF_HEADER_LEN = 8192
# 支付宝交易号 trade_no：纯数字，长度 16–64。
_TRADE_NO_MIN_LEN = 16
_TRADE_NO_MAX_LEN = 64
# 金额格式：元，最多两位小数，不允许负号/科学计数。
_AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]{1,2})?$")
_ALLOWED_CURRENCY = "CNY"


class ProofError(ValueError):
    """402 协议头编解码失败：格式非法、字段缺失或取值不合法。"""


def b64url_encode_json(obj: dict) -> str:
    """确定性 JSON（键排序、无空格、中文不转义）→ Base64URL，去掉末尾 '='。"""
    if not isinstance(obj, dict):
        raise ProofError("encode input must be a dict")
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    raw = text.encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode_json(s: str) -> dict:
    """Base64URL（或标准 Base64）解码为 JSON 对象；容忍有/无 '=' 填充。

    非 dict 或任何解码失败 → ProofError。
    """
    if not isinstance(s, str):
        raise ProofError("base64 input must be a string")
    # 标准 base64（+ /）与 urlsafe（- _）都接受；先统一字母表再补填充。
    t = s.strip().replace("+", "-").replace("/", "_").rstrip("=")
    t += "=" * (-len(t) % 4)
    try:
        raw = base64.b64decode(t, altchars=b"-_", validate=True)
        obj = json.loads(raw)  # bytes 可直接喂给 json.loads（默认 utf-8）
    except (ValueError, TypeError) as e:  # binascii.Error / JSONDecodeError 均为 ValueError
        raise ProofError(f"invalid base64url JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ProofError("decoded JSON is not an object")
    return obj


# ---------------------------------------------------------------------------
# Payment-Proof（重试请求头）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentProof:
    payment_proof: str
    trade_no: str
    client_session: str


def _validate_proof_fields(
    payment_proof: object, trade_no: object, client_session: object
) -> None:
    """三个字段都必须是非空字符串；trade_no 只能是数字且长度 16–64。"""
    for name, value in (
        ("payment_proof", payment_proof),
        ("trade_no", trade_no),
        ("client_session", client_session),
    ):
        if not isinstance(value, str) or not value:
            raise ProofError(f"{name} must be a non-empty string")
    if not (isinstance(trade_no, str) and trade_no.isascii() and trade_no.isdigit()):
        raise ProofError("trade_no must consist of digits only")
    if not (_TRADE_NO_MIN_LEN <= len(trade_no) <= _TRADE_NO_MAX_LEN):
        raise ProofError(
            f"trade_no length must be {_TRADE_NO_MIN_LEN}-{_TRADE_NO_MAX_LEN}"
        )


def parse_payment_proof(header: str) -> PaymentProof:
    """解析 Payment-Proof 头：Base64URL JSON → PaymentProof。"""
    if not isinstance(header, str):
        raise ProofError("Payment-Proof header must be a string")
    if len(header) > _MAX_PROOF_HEADER_LEN:
        raise ProofError(f"Payment-Proof header exceeds {_MAX_PROOF_HEADER_LEN} characters")
    obj = b64url_decode_json(header)
    protocol = obj.get("protocol")
    method = obj.get("method")
    if not isinstance(protocol, dict) or not isinstance(method, dict):
        raise ProofError("Payment-Proof requires protocol/method objects")
    payment_proof = protocol.get("payment_proof")
    trade_no = protocol.get("trade_no")
    client_session = method.get("client_session")
    _validate_proof_fields(payment_proof, trade_no, client_session)
    return PaymentProof(
        payment_proof=payment_proof,
        trade_no=trade_no,
        client_session=client_session,
    )


def build_payment_proof(p: PaymentProof) -> str:
    """由 PaymentProof 构造 Payment-Proof 头值（先做与 parse 相同的校验）。"""
    _validate_proof_fields(p.payment_proof, p.trade_no, p.client_session)
    return b64url_encode_json(
        {
            "protocol": {"payment_proof": p.payment_proof, "trade_no": p.trade_no},
            "method": {"client_session": p.client_session},
        }
    )


# ---------------------------------------------------------------------------
# Payment-Needed（402 响应头）与 amtPaymentLink
# ---------------------------------------------------------------------------

#: Payment-Needed 头解码后 JSON 的全部字段，按官方文档顺序。
NEEDED_FIELDS: tuple[str, ...] = (
    "out_trade_no",
    "amount",
    "currency",
    "resource_id",
    "pay_before",
    "seller_signature",
    "seller_sign_type",
    "seller_unique_id",
    "seller_name",
    "seller_id",
    "seller_app_id",
    "goods_name",
    "seller_unique_id_key",
    "service_id",
)

#: Payment-Needed 的必填子集。
_NEEDED_REQUIRED: tuple[str, ...] = (
    "out_trade_no",
    "amount",
    "currency",
    "resource_id",
    "pay_before",
    "goods_name",
)


def _validate_needed_fields(fields: dict) -> dict:
    """必填子集齐全、无未知字段、amount 形如 ^\\d+(\\.\\d{1,2})?$、currency == CNY。"""
    if not isinstance(fields, dict):
        raise ProofError("Payment-Needed payload must be a dict")
    unknown = sorted(k for k in fields if k not in NEEDED_FIELDS)
    if unknown:
        raise ProofError(f"unknown Payment-Needed fields: {', '.join(map(str, unknown))}")
    missing = [k for k in _NEEDED_REQUIRED if not isinstance(fields.get(k), str) or not fields[k].strip()]
    if missing:
        raise ProofError(f"missing required Payment-Needed fields: {', '.join(missing)}")
    amount = fields["amount"]
    if not isinstance(amount, str) or _AMOUNT_RE.fullmatch(amount) is None:
        raise ProofError(f"invalid amount: {amount!r}")
    currency = fields["currency"]
    if currency != _ALLOWED_CURRENCY:
        raise ProofError(f"unsupported currency: {currency!r}")
    return fields


def build_payment_needed(fields: dict) -> str:
    """由字段 dict 构造 Payment-Needed 头值（缺必填/未知字段/非法 amount/currency → ProofError）。"""
    _validate_needed_fields(fields)
    return b64url_encode_json(dict(fields))


def parse_payment_needed(header: str) -> dict:
    """解析平铺格式 Payment-Needed 头，校验规则与 build_payment_needed 相同。"""
    return _validate_needed_fields(b64url_decode_json(header))


PROTOCOL_NEEDED_FIELDS: tuple[str, ...] = (
    "out_trade_no",
    "amount",
    "currency",
    "resource_id",
    "pay_before",
    "seller_signature",
    "seller_sign_type",
    "seller_unique_id",
)

METHOD_NEEDED_FIELDS: tuple[str, ...] = (
    "seller_name",
    "seller_id",
    "seller_app_id",
    "goods_name",
    "seller_unique_id_key",
    "service_id",
)


def build_a2m_payment_needed(fields: dict) -> str:
    """构造与官方 A2MPaymentDemoController / @alipay/agent-payment@1.0.0 CLI 一致的分层 Payment-Needed 头。

    结构：{"protocol": {...}, "method": {...}} → Base64URL 编码。
    """
    _validate_needed_fields(fields)
    protocol = {k: fields[k] for k in PROTOCOL_NEEDED_FIELDS if fields.get(k)}
    method = {k: fields[k] for k in METHOD_NEEDED_FIELDS if fields.get(k)}
    return b64url_encode_json({"protocol": protocol, "method": method})


def parse_a2m_payment_needed(header: str) -> dict:
    """解析分层 {"protocol": {...}, "method": {...}} 格式的 Payment-Needed 头并校验全部字段。"""
    obj = b64url_decode_json(header)
    protocol = obj.get("protocol")
    method = obj.get("method")
    if not isinstance(protocol, dict) or not isinstance(method, dict):
        raise ProofError("layered Payment-Needed requires protocol/method objects")
    extra_top = set(obj) - {"protocol", "method"}
    if extra_top:
        raise ProofError(f"unexpected top-level keys: {sorted(extra_top)}")
    merged: dict = {}
    for k, v in protocol.items():
        if k not in PROTOCOL_NEEDED_FIELDS:
            raise ProofError(f"unknown protocol field: {k}")
        merged[k] = v
    for k, v in method.items():
        if k not in METHOD_NEEDED_FIELDS:
            raise ProofError(f"unknown method field: {k}")
        merged[k] = v
    return _validate_needed_fields(merged)


def parse_any_payment_needed(header: str) -> dict:
    """自适应解析平铺或分层（A2M / alipay-bot）Payment-Needed 头，统一返回 14 字段标准字典。"""
    obj = b64url_decode_json(header)
    if "protocol" in obj and "method" in obj:
        return parse_a2m_payment_needed(header)
    return _validate_needed_fields(obj)


def build_amt_payment_link(amt_pre_pay_id: str) -> str:
    """另一份官方文档的 402 格式：amtPaymentLink 头值即预下单号本身。"""
    if not isinstance(amt_pre_pay_id, str) or not amt_pre_pay_id.strip():
        raise ProofError("amt_pre_pay_id must be a non-empty string")
    return amt_pre_pay_id.strip()


def parse_amt_payment_link(header: str) -> str:
    """解析 amtPaymentLink 头，返回预下单号（首尾空白忽略）。"""
    if not isinstance(header, str) or not header.strip():
        raise ProofError("amtPaymentLink header must be a non-empty string")
    return header.strip()


# ---------------------------------------------------------------------------
# Payment-Validation（200 响应头）
# ---------------------------------------------------------------------------


def _validate_validation_fields(
    trade_no: object, out_trade_no: object, resource_id: object, validated: object
) -> None:
    for name, value in (
        ("trade_no", trade_no),
        ("out_trade_no", out_trade_no),
        ("resource_id", resource_id),
    ):
        if not isinstance(value, str) or not value:
            raise ProofError(f"{name} must be a non-empty string")
    if not isinstance(validated, bool):
        raise ProofError("validated must be a boolean")


def build_payment_validation(
    trade_no: str, out_trade_no: str, resource_id: str, validated: bool = True
) -> str:
    """由验证结果字段构造 Payment-Validation 头值。"""
    _validate_validation_fields(trade_no, out_trade_no, resource_id, validated)
    return b64url_encode_json(
        {
            "trade_no": trade_no,
            "out_trade_no": out_trade_no,
            "validated": validated,
            "resource_id": resource_id,
        }
    )


def parse_payment_validation(header: str) -> dict:
    """解析 Payment-Validation 头并做与 build 相同的字段校验。"""
    obj = b64url_decode_json(header)
    _validate_validation_fields(
        obj.get("trade_no"),
        obj.get("out_trade_no"),
        obj.get("resource_id"),
        obj.get("validated"),
    )
    return obj
