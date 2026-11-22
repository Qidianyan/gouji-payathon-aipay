"""作品 A：Agent Pay —— Admit（agent 验签 + 授权边界 + 审计）× Facetry（目录 + 付费交付）× 支付宝 402 按量付费。

链路：发现 → 用户签授权单 → agent 签名请求资源 → 授权校验 → precreate → 402（amtPaymentLink + Payment-Needed）
→ 钱包付款 → 带 Payment-Proof 重试 → verify（核对 active/金额/订单/资源/未履约）→ 交付 → fulfillment.confirm → 凭证包。
网关通过 aipay_kit.gateway.factory 选择：没有 secrets/aipay_sandbox.env 时是 MOCK。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from aipay_kit import apass as apass_mod
from aipay_kit import mandate as mx
from aipay_kit import proof as px
from aipay_kit import signing
from aipay_kit.audit import AuditLog, canonical_json
from aipay_kit.gateway.base import GatewayError, PrecreateRequest
from aipay_kit.idempotency import IdempotencyStore
from agentpay import identity
from agentpay.catalog import CATALOG

CN = timezone(timedelta(hours=8))
PAY_WINDOW_S = 15 * 60
MERCHANT_ID = "facetry-demo"
MERCHANT_NAME = "Facetry by elidot（可信智能体商业商户）"
MERCHANT_SELLER_UID = "2088621955019283"


@dataclass
class Order:
    out_trade_no: str
    mandate_id: str
    sku: str
    amount: Decimal
    agent_keyid: str
    amt_pre_pay_id: str
    pay_before: int
    status: str = "PENDING"  # PENDING | PAID | FULFILLED | EXPIRED
    trade_no: str = ""
    delivery_sha256: str = ""
    fulfil_confirmed: bool = False
    guardrail_decision: str = "ALLOW"
    proof_sha256: str = ""
    client_session_sha256: str = ""
    bill: dict = field(default_factory=dict)


@dataclass
class Confirmation:
    confirmation_id: str
    mandate_id: str
    sku: str
    agent_keyid: str
    codes: tuple[str, ...]
    approved: bool = False
    used: bool = False


class AgentPayService:
    def __init__(self, gateway, *, service_id: str, clock: Callable[[], float] = time.time,
                 audit_path: str = ":memory:", idem_path: str = ":memory:", seller_key=None) -> None:
        self.gw = gateway
        self.service_id = service_id
        self.clock = clock
        self.audit = AuditLog(audit_path)
        self.idem = IdempotencyStore(idem_path)
        self.seller_key = seller_key or signing.generate_keypair()  # 商家 RSA2 签名（402 账单 / 凭证包）
        self.agents: dict[str, dict] = {}          # keyid -> {pub, owner, revoked}
        self.user_keys: dict[str, str] = {}        # user_ref -> pinned pub (TOFU)
        self.mandates: dict[str, mx.Mandate] = {}
        self.mandate_docs: dict[str, dict] = {}
        self.orders: dict[str, Order] = {}
        self.by_trade: dict[str, str] = {}
        self.confirmations: dict[str, Confirmation] = {}
        self.lock = threading.RLock()

    def now(self) -> int:
        return int(self.clock())

    # ---------------------------------------------------------------- usage (paid + 未过期 pending 预占)
    def usage(self, mandate_id: str, exclude: str = "") -> mx.Usage:
        n, amt = 0, Decimal("0")
        for o in self.orders.values():
            if o.mandate_id != mandate_id or o.out_trade_no == exclude:
                continue
            if o.status in ("PAID", "FULFILLED") or (o.status == "PENDING" and o.pay_before > self.now()):
                n += 1
                amt += o.amount
        return mx.Usage(n, amt)

    def seller_sign(self, fields: dict) -> str:
        return signing.sign_params({k: v for k, v in fields.items() if k != "seller_signature"}, self.seller_key)


def _err(status: int, code: str, msg: str = "", **extra) -> JSONResponse:
    return JSONResponse({"code": code, "message": msg, **extra}, status_code=status)


def create_app(gateway, *, service_id: str = "mock-service", clock: Callable[[], float] = time.time,
               mock_router=None, **kw) -> FastAPI:
    svc = AgentPayService(gateway, service_id=service_id, clock=clock, **kw)
    app = FastAPI(title="Agent Pay demo · Admit × Facetry")
    app.state.svc = svc
    if mock_router is not None:
        app.include_router(mock_router)
    A = svc.audit

    # ------------------------------------------------------------ discovery
    def _gw_mode() -> str:
        if getattr(svc.gw, "is_mock", False):
            return "mock"
        url = str(getattr(svc.gw, "gateway_url", ""))
        return "prod" if ("openapi.alipay.com" in url and "sandbox" not in url) else "sandbox"

    @app.get("/.well-known/agent-service.json")
    def discovery() -> dict:
        return {
            "name": "Facetry 付费数据服务（Admit × APASS 四层守门）", "merchant_id": MERCHANT_ID, "merchant_name": MERCHANT_NAME,
            "payment": {"protocol": "HTTP 402 / 支付宝 AI 按量付费（alipay-aipay & A402）", "currency": "CNY", "max_amount_per_tx": "50.00",
                        "headers": ["amtPaymentLink", "Payment-Needed", "Payment-Needed-A2M", "Payment-Proof", "Payment-Validation"],
                        "cli_compatible": "@alipay/agent-payment@1.0.0 (alipay-bot)",
                        "gateway_mode": _gw_mode()},
            "auth": {
                "agent_signature": "Ed25519 (RFC 9421 subset)",
                "mandate": "admit.mandate.v1 (user Ed25519)",
                "apass_kya": {
                    "schema": apass_mod.APASS_SCHEMA,
                    "pillars": ["可信身份（人类预算签名）", "持续核身与意图安全（商户商品锁定）", "支付凭据签发（A402 核验）", "可信存证（履约回执哈希链）"],
                },
            },
            "endpoints": {"catalog": "/catalog", "resource": "/paid/resource/{sku}", "a2m_resource": "/a2m/resource/{sku}",
                          "mandates": "/mandates", "receipts": "/receipts/{trade_no}", "audit": "/audit/export"},
        }

    @app.get("/catalog")
    def catalog(q: str = "") -> dict:
        items = [i for i in CATALOG.values() if not q or q.lower() in (i["title"] + i["desc"]).lower()]
        return {"merchant_id": MERCHANT_ID, "items": items}

    # ------------------------------------------------------------ agents & mandates
    @app.post("/agents", status_code=201)
    async def register_agent(body: dict) -> dict:
        keyid, pub = body.get("keyid", ""), body.get("public_key", "")
        try:
            identity.load_pub(pub)
        except Exception:
            return _err(400, "BAD_KEY")
        with svc.lock:
            svc.agents[keyid] = {"pub": pub, "owner": body.get("owner", ""), "revoked": False}
        A.append(f"agent:{keyid}", "agent.registered", keyid, {"owner": body.get("owner", "")})
        return {"keyid": keyid}

    @app.post("/agents/{keyid}/revoke")
    def revoke_agent(keyid: str) -> dict:
        with svc.lock:
            if keyid not in svc.agents:
                return _err(404, "NOT_FOUND")
            svc.agents[keyid]["revoked"] = True
        A.append("admin", "agent.revoked", keyid)
        return {"keyid": keyid, "revoked": True}

    @app.post("/mandates", status_code=201)
    async def create_mandate(doc: dict) -> Response:
        need = ("type", "mandate_id", "user_ref", "merchant_ids", "max_amount_per_tx", "max_amount_total",
                "expires_at", "user_public_key", "signature")
        if any(k not in doc for k in need) or doc["type"] != "admit.mandate.v1":
            return _err(400, "MANDATE_MALFORMED")
        if not identity.verify(doc["user_public_key"], identity.canonical(doc), doc["signature"]):
            return _err(401, "MANDATE_SIGNATURE_INVALID")
        with svc.lock:
            pinned = svc.user_keys.setdefault(doc["user_ref"], doc["user_public_key"])
            if pinned != doc["user_public_key"]:
                return _err(401, "USER_KEY_MISMATCH", "该用户已绑定其他公钥")
            if doc["mandate_id"] in svc.mandates:
                return _err(409, "MANDATE_EXISTS")
            try:
                m = mx.Mandate(
                    mandate_id=doc["mandate_id"], user_ref=doc["user_ref"], merchant_ids=tuple(doc["merchant_ids"]),
                    currency=doc.get("currency", "CNY"), max_amount_per_tx=Decimal(doc["max_amount_per_tx"]),
                    max_amount_total=Decimal(doc["max_amount_total"]), expires_at=int(doc["expires_at"]),
                    not_before=int(doc.get("not_before", 0)), categories=tuple(doc.get("categories", ())),
                    max_uses=doc.get("max_uses"), single_use=bool(doc.get("single_use", False)),
                    agent_keyid=doc.get("agent_keyid"),
                    confirm_above=Decimal(doc["confirm_above"]) if doc.get("confirm_above") else None,
                    intent_keywords=tuple(doc.get("intent_keywords", ())))
            except Exception:
                return _err(400, "MANDATE_MALFORMED")
            svc.mandates[m.mandate_id] = m
            svc.mandate_docs[m.mandate_id] = doc
        A.append(f"user:{m.user_ref}", "mandate.created", m.mandate_id,
                 {"merchant_ids": list(m.merchant_ids), "max_amount_per_tx": str(m.max_amount_per_tx),
                  "max_amount_total": str(m.max_amount_total), "expires_at": m.expires_at,
                  "doc_sha256": hashlib.sha256(identity.canonical(doc)).hexdigest()})
        return JSONResponse({"mandate_id": m.mandate_id}, status_code=201)

    @app.post("/mandates/{mandate_id}/revoke")
    async def revoke_mandate(mandate_id: str, body: dict) -> Response:
        with svc.lock:
            m = svc.mandates.get(mandate_id)
            if m is None:
                return _err(404, "MANDATE_NOT_FOUND")
            pub = svc.user_keys[m.user_ref]
            if not identity.verify(pub, f"revoke:{mandate_id}".encode(), body.get("signature", "")):
                return _err(401, "USER_SIGNATURE_INVALID")
            svc.mandates[mandate_id] = replace(m, revoked=True)
        A.append(f"user:{m.user_ref}", "mandate.revoked", mandate_id)
        return JSONResponse({"mandate_id": mandate_id, "revoked": True})

    @app.post("/confirmations/{cid}")
    async def approve(cid: str, body: dict) -> Response:
        with svc.lock:
            c = svc.confirmations.get(cid)
            if c is None:
                return _err(404, "NOT_FOUND")
            m = svc.mandates[c.mandate_id]
            if not identity.verify(svc.user_keys[m.user_ref], f"confirm:{cid}".encode(), body.get("signature", "")):
                return _err(401, "USER_SIGNATURE_INVALID")
            c.approved = True
        A.append(f"user:{m.user_ref}", "confirmation.approved", c.mandate_id, {"confirmation_id": cid, "sku": c.sku})
        return JSONResponse({"confirmation_id": cid, "approved": True})

    # ------------------------------------------------------------ paid resource
    @app.get("/paid/resource/{sku}")
    async def resource(sku: str, request: Request,
                       x_mandate_id: str = Header(default=""), payment_proof: str = Header(default=""),
                       signature_keyid: str = Header(default=""), signature_created: str = Header(default="0"),
                       signature: str = Header(default=""), x_confirmation_id: str = Header(default="")) -> Response:
        now = svc.now()
        # 1. agent 身份
        agent = svc.agents.get(signature_keyid)
        try:
            created = int(signature_created)
        except ValueError:
            created = 0
        base = identity.signature_base("GET", request.url.path, x_mandate_id, payment_proof, signature_keyid, created)
        if agent is None or not identity.verify(agent["pub"], base, signature):
            A.append(f"agent:{signature_keyid or '?'}", "blocked", x_mandate_id, {"code": "AGENT_SIGNATURE_INVALID", "sku": sku})
            return _err(401, "AGENT_SIGNATURE_INVALID", "agent 签名无效或未注册")
        if agent["revoked"]:
            A.append(f"agent:{signature_keyid}", "blocked", x_mandate_id, {"code": "AGENT_KEY_REVOKED", "sku": sku})
            return _err(401, "AGENT_KEY_REVOKED")
        if abs(now - created) > identity.MAX_SKEW_S:
            A.append(f"agent:{signature_keyid}", "blocked", x_mandate_id, {"code": "AGENT_SIGNATURE_STALE", "sku": sku})
            return _err(401, "AGENT_SIGNATURE_STALE")
        item = CATALOG.get(sku)
        if item is None:
            return _err(404, "SKU_NOT_FOUND")
        m = svc.mandates.get(x_mandate_id)
        if m is None:
            A.append(f"agent:{signature_keyid}", "blocked", x_mandate_id, {"code": "MANDATE_NOT_FOUND", "sku": sku})
            return _err(403, "MANDATE_NOT_FOUND")
        if payment_proof:
            return _deliver(svc, m, item, signature_keyid, payment_proof)
        return _bill(svc, m, item, signature_keyid, x_confirmation_id)

    @app.get("/a2m/resource/{sku}")
    async def a2m_resource(sku: str, request: Request,
                           x_mandate_id: str = Header(default=""), payment_proof: str = Header(default=""),
                           signature_keyid: str = Header(default=""), signature_created: str = Header(default="0"),
                           signature: str = Header(default=""), x_confirmation_id: str = Header(default="")) -> Response:
        """原生兼容 @alipay/agent-payment@1.0.0（alipay-bot）分层 Payment-Needed 头的 A2M 资源端点。"""
        resp = await resource(
            sku, request, x_mandate_id=x_mandate_id, payment_proof=payment_proof,
            signature_keyid=signature_keyid, signature_created=signature_created,
            signature=signature, x_confirmation_id=x_confirmation_id,
        )
        if resp.status_code == 402 and "Payment-Needed-A2M" in resp.headers:
            resp.headers["Payment-Needed"] = resp.headers["Payment-Needed-A2M"]
        return resp

    # ------------------------------------------------------------ receipts / audit / orders
    @app.get("/orders/{out_trade_no}")
    def get_order(out_trade_no: str) -> Response:
        o = svc.orders.get(out_trade_no)
        if o is None:
            return _err(404, "NOT_FOUND")
        return JSONResponse({"out_trade_no": o.out_trade_no, "status": o.status, "sku": o.sku, "amount": f"{o.amount}",
                             "trade_no": o.trade_no, "mandate_id": o.mandate_id})

    @app.get("/receipts/{trade_no}")
    def receipt(trade_no: str) -> Response:
        out = svc.by_trade.get(trade_no)
        o = svc.orders.get(out or "")
        if o is None or o.status != "FULFILLED":
            return _err(404, "NOT_FOUND")
        recs = [r.__dict__ for r in A.records(o.out_trade_no)]
        all_recs = A.records()
        audit_head = all_recs[-1].hash if all_recs else ""
        agent_pub = svc.agents.get(o.agent_keyid, {}).get("pub", "")
        apass_doc = apass_mod.build_apass_attestation(
            mandate_doc=svc.mandate_docs[o.mandate_id],
            agent_keyid=o.agent_keyid,
            agent_public_key=agent_pub,
            merchant_id=MERCHANT_ID,
            sku=o.sku,
            bill=o.bill,
            amt_pre_pay_id=o.amt_pre_pay_id,
            guardrail_decision=o.guardrail_decision,
            trade_no=o.trade_no,
            payment_proof_sha256=o.proof_sha256,
            client_session_sha256=o.client_session_sha256,
            verified_amount=f"{o.amount}",
            verified_resource_id=f"/paid/resource/{o.sku}",
            delivery_sha256=o.delivery_sha256,
            fulfillment_confirmed=o.fulfil_confirmed,
            audit_head=audit_head,
            audit_count=len(all_recs),
            seller_private_key=svc.seller_key,
        )
        body = {
            "type": "agentpay.receipt.v1", "_mock": bool(getattr(svc.gw, "is_mock", False)),
            "merchant_id": MERCHANT_ID, "out_trade_no": o.out_trade_no, "trade_no": o.trade_no, "sku": o.sku,
            "amount": f"{o.amount}", "currency": "CNY", "mandate": svc.mandate_docs[o.mandate_id],
            "agent_keyid": o.agent_keyid, "bill": o.bill, "delivery_sha256": o.delivery_sha256,
            "fulfillment_confirmed": o.fulfil_confirmed, "audit": recs,
            "audit_head": audit_head,
            "apass_attestation": apass_doc,
        }
        body["merchant_signature"] = signing.rsa2_sign(canonical_json(body), svc.seller_key)
        return JSONResponse(body)

    @app.get("/merchant/public_key")
    def merchant_pub() -> PlainTextResponse:
        return PlainTextResponse(signing.public_pem(svc.seller_key.public_key()).decode())

    @app.get("/audit/export")
    def audit_export(format: str = "jsonl", ref: str | None = None) -> Response:
        if format == "csv":
            return Response(A.export_csv(ref), media_type="text/csv; charset=utf-8")
        return Response(A.export_jsonl(ref), media_type="application/x-ndjson; charset=utf-8")

    return app


def _bill(svc: AgentPayService, m: mx.Mandate, item: dict, keyid: str, confirmation_id: str) -> Response:
    A = svc.audit
    now = svc.now()
    amount = Decimal(item["price"])
    with svc.lock:
        # 重复下单：同 mandate+sku+agent 且未过期的待支付单直接复用
        for o in svc.orders.values():
            if (o.mandate_id, o.sku, o.agent_keyid, o.status) == (m.mandate_id, item["sku"], keyid, "PENDING") \
                    and o.pay_before > now:
                A.append(f"agent:{keyid}", "order.reused", o.out_trade_no, {"sku": item["sku"]})
                return _402(o)
        req = mx.PayRequest(MERCHANT_ID, amount, "CNY", item["category"], keyid, item["title"], now)
        v = mx.evaluate(m, svc.usage(m.mandate_id), req)
        if v.decision is mx.Decision.DENY:
            A.append(f"agent:{keyid}", "blocked", m.mandate_id, {"codes": list(v.codes), "sku": item["sku"], "amount": item["price"]})
            return _err(403, v.codes[0], "超出用户授权", codes=list(v.codes), remaining_amount=f"{v.remaining_amount}")
        guardrail_tag = "ALLOW"
        if v.decision is mx.Decision.NEEDS_CONFIRMATION:
            c = svc.confirmations.get(confirmation_id)
            ok = (c is not None and c.approved and not c.used and (c.mandate_id, c.sku, c.agent_keyid) ==
                  (m.mandate_id, item["sku"], keyid))
            if not ok:
                cid = "cf_" + secrets.token_hex(8)
                svc.confirmations[cid] = Confirmation(cid, m.mandate_id, item["sku"], keyid, v.codes)
                A.append(f"agent:{keyid}", "confirmation.required", m.mandate_id,
                         {"codes": list(v.codes), "sku": item["sku"], "confirmation_id": cid})
                return _err(409, "NEEDS_CONFIRMATION", "需要用户二次确认", codes=list(v.codes), confirmation_id=cid)
            c.used = True
            guardrail_tag = "CONFIRM_APPROVED"
        out = f"AP{datetime.fromtimestamp(now, CN):%Y%m%d%H%M%S}{secrets.token_hex(4)}"
        pay_before = now + PAY_WINDOW_S
        pb = datetime.fromtimestamp(pay_before, CN).strftime("%Y-%m-%d %H:%M:%S")
        try:
            pid = svc.gw.precreate(PrecreateRequest(out, svc.service_id, item["title"], item["price"], pb,
                                                    resource_id=f"/paid/resource/{item['sku']}"))
        except GatewayError as e:
            A.append("merchant", "precreate.failed", out, {"sub_code": e.sub_code})
            return _err(502, "CREATE_ORDER_ERROR", e.sub_code)
        app_id = str(getattr(svc.gw, "app_id", "9021000000000000"))
        seller_uid = str(getattr(svc.gw, "seller_id", "") or MERCHANT_SELLER_UID)
        bill = {"out_trade_no": out, "amount": item["price"], "currency": "CNY",
                "resource_id": f"/paid/resource/{item['sku']}", "pay_before": datetime.fromtimestamp(pay_before, CN).isoformat(),
                "goods_name": item["title"], "seller_name": MERCHANT_NAME, "service_id": svc.service_id,
                "seller_id": seller_uid, "seller_unique_id": seller_uid, "seller_app_id": app_id,
                "seller_sign_type": "RSA2", "seller_unique_id_key": "seller_id"}
        bill["seller_signature"] = svc.seller_sign(bill)
        o = Order(out, m.mandate_id, item["sku"], amount, keyid, pid, pay_before, guardrail_decision=guardrail_tag, bill=bill)
        svc.orders[out] = o
    A.append("merchant", "order.precreated", out, {"sku": item["sku"], "amount": item["price"], "amt_pre_pay_id": pid,
                                                    "mandate_id": m.mandate_id, "agent": keyid})
    return _402(o)


def _402(o: Order) -> Response:
    body = {"code": "Payment-Needed", "message": "需要支付", "out_trade_no": o.out_trade_no,
            "amount": f"{o.amount}", "currency": "CNY", "goods_name": o.bill["goods_name"], "amt_pre_pay_id": o.amt_pre_pay_id}
    return JSONResponse(body, status_code=402, headers={
        "amtPaymentLink": o.amt_pre_pay_id,
        "Payment-Needed": px.build_payment_needed(o.bill),
        "Payment-Needed-A2M": px.build_a2m_payment_needed(o.bill),
    })


def _deliver(svc: AgentPayService, m: mx.Mandate, item: dict, keyid: str, header: str) -> Response:
    A = svc.audit
    try:
        p = px.parse_payment_proof(header)
    except px.ProofError as e:
        A.append(f"agent:{keyid}", "blocked", m.mandate_id, {"code": "INVALID_PAYMENT_PROOF", "why": str(e)[:80]})
        return _err(400, "INVALID_PAYMENT_PROOF", "支付凭证格式错误")
    prior = svc.idem.get("fulfil", p.trade_no)
    if prior is not None:  # 重复提交同一凭证：不重复履约、不重复计次
        o = svc.orders[prior["out_trade_no"]]
        if o.sku != item["sku"]:
            A.append(f"agent:{keyid}", "blocked", o.out_trade_no, {"code": "PROOF_RESOURCE_MISMATCH", "sku": item["sku"]})
            return _err(402, "PROOF_RESOURCE_MISMATCH", "凭证不属于该资源")
        A.append(f"agent:{keyid}", "fulfil.duplicate", o.out_trade_no, {"trade_no": p.trade_no})
        return _deliver_response(o, prior["content"], already=True)
    v = svc.gw.verify_proof(p.payment_proof, p.trade_no, p.client_session)
    o = svc.orders.get(v.out_trade_no) if v.active else None
    resource = f"/paid/resource/{item['sku']}"
    reason = ""
    if not v.active:
        reason = "INVALID_PAYMENT_PROOF"
    elif o is None:
        reason = "UNKNOWN_ORDER"
    elif o.mandate_id != m.mandate_id or o.agent_keyid != keyid:
        reason = "PROOF_MANDATE_MISMATCH"
    elif v.resource_id != resource or o.sku != item["sku"]:
        reason = "PROOF_RESOURCE_MISMATCH"
    elif Decimal(v.amount) != o.amount:
        reason = "PROOF_AMOUNT_MISMATCH"
    if reason:
        A.append(f"agent:{keyid}", "blocked", v.out_trade_no or m.mandate_id, {"code": reason, "trade_no": p.trade_no})
        return _err(402, reason, "支付凭证无效或与订单不符")
    content = {"sku": item["sku"], "title": item["title"], "report": _make_report(item, o.out_trade_no),
               "generated_at": datetime.fromtimestamp(svc.now(), CN).isoformat()}
    stored, first = svc.idem.put_once("fulfil", p.trade_no, o.out_trade_no,
                                      {"out_trade_no": o.out_trade_no, "content": content})
    if not first:
        return _deliver_response(o, stored["content"], already=True)
    with svc.lock:
        o.status, o.trade_no = "PAID", p.trade_no
        o.proof_sha256 = hashlib.sha256(p.payment_proof.encode("utf-8")).hexdigest()
        o.client_session_sha256 = hashlib.sha256(p.client_session.encode("utf-8")).hexdigest()
        svc.by_trade[p.trade_no] = o.out_trade_no
    A.append("gateway", "payment.verified", o.out_trade_no, {"trade_no": p.trade_no, "amount": v.amount,
                                                             "resource_id": v.resource_id})
    o.delivery_sha256 = hashlib.sha256(canonical_json(content).encode()).hexdigest()
    A.append("merchant", "fulfilled", o.out_trade_no, {"delivery_sha256": o.delivery_sha256})
    o.fulfil_confirmed = svc.gw.fulfillment_confirm(p.trade_no)
    o.status = "FULFILLED"
    A.append("merchant", "fulfillment.confirmed", o.out_trade_no, {"trade_no": p.trade_no, "ok": o.fulfil_confirmed})
    return _deliver_response(o, content, already=False)


def _deliver_response(o: Order, content: dict, *, already: bool) -> Response:
    body = {"resource_id": f"/paid/resource/{o.sku}", "content": content, "trade_no": o.trade_no,
            "out_trade_no": o.out_trade_no, "already_fulfilled": already, "receipt": f"/receipts/{o.trade_no}"}
    return JSONResponse(body, headers={"Payment-Validation": px.build_payment_validation(
        o.trade_no, o.out_trade_no, f"/paid/resource/{o.sku}")})


def _make_report(item: dict, out_trade_no: str) -> dict:
    """确定性结构化商业核验交付物（订单级可重现，支撑 delivery_sha256 可信存证重算）。"""
    h = hashlib.sha256(f"{item['sku']}:{out_trade_no}".encode()).hexdigest()
    score = int(h[:2], 16) % 31 + 68
    checked = int(h[2:4], 16) % 40 + 18
    sku = item["sku"]
    if sku == "geo-report":
        metrics = {
            "citation_rate": f"{round(score * 0.72, 1)}%",
            "engines_audited": ["Qwen-Max", "DeepSeek-R1", "Gemini", "Perplexity"],
            "top_authority_domains": ["alipay.com", "elidot.com", "gov.cn"],
            "action_items": ["补齐 Schema.org Product/Offer 结构化价格字段", "补充 /.well-known/agent-service.json 服务元数据"],
        }
    elif sku == "catalog-audit":
        metrics = {
            "acp_required_fields_pass": f"{checked - 2}/{checked}",
            "missing_warnings": ["return_policy_days", "gtin_check_digit"],
            "apass_merchant_ready": True,
        }
    else:
        metrics = {
            "exported_skus": 100,
            "channels": ["Alipay-AI-Pay", "ChatGPT-Shopping", "Perplexity-Merchant", "Google-Merchant"],
            "feed_digest": h[:16],
        }
    return {"summary": f"{item['title']} 已生成", "score": score, "items_checked": checked, "metrics": metrics}
