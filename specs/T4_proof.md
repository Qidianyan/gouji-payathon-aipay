# T4 实现：402 协议头编解码 aipay_kit/proof.py

## 背景（官方，见 ../materials/official_refs/A2M智能收产品对接文档.md 与 docs_ai-receive_MACHINE_PAY.md）
- 402 响应头 `Payment-Needed`：Base64URL 编码的 JSON（字段 out_trade_no, amount, currency, resource_id, pay_before, seller_signature, seller_sign_type, seller_unique_id, seller_name, seller_id, seller_app_id, goods_name, seller_unique_id_key, service_id）。另一份官方文档用头 `amtPaymentLink: <amt_pre_pay_id>`，两个都要支持。
- 重试请求头 `Payment-Proof`：Base64URL JSON `{"protocol":{"payment_proof":..,"trade_no":..},"method":{"client_session":..}}`。
- 200 响应头 `Payment-Validation`：Base64URL JSON `{trade_no, out_trade_no, validated, resource_id}`。

## 新建文件
- `aipay_kit/proof.py`
- `tests/test_proof_smoke.py`（3–5 条）

## 接口（必须一致）
```python
class ProofError(ValueError): ...

def b64url_encode_json(obj: dict) -> str:   # json.dumps(ensure_ascii=False, separators=(",",":"), sort_keys=True) → utf-8 → urlsafe_b64encode，去掉末尾 '='
def b64url_decode_json(s: str) -> dict:     # 容忍有/无 '=' 填充，也容忍标准 base64（+ /）；非 dict 或解码失败 → ProofError

@dataclass(frozen=True)
class PaymentProof:
    payment_proof: str
    trade_no: str
    client_session: str

def parse_payment_proof(header: str) -> PaymentProof:
    # 三个字段都必须是非空字符串；trade_no 只能是数字且长度 16–64；header 长度 > 8192 → ProofError
def build_payment_proof(p: PaymentProof) -> str

NEEDED_FIELDS: tuple[str, ...]  # 上面 14 个字段名，按背景里的顺序
def build_payment_needed(fields: dict) -> str     # 缺 out_trade_no/amount/currency/resource_id/pay_before/goods_name 任一 → ProofError；未知字段 → ProofError
def parse_payment_needed(header: str) -> dict     # 同样校验
def build_payment_validation(trade_no: str, out_trade_no: str, resource_id: str, validated: bool = True) -> str
def parse_payment_validation(header: str) -> dict
```
- amount 必须是形如 `^\d+(\.\d{1,2})?$` 的字符串，否则 ProofError。currency 只允许 "CNY"。

## 验收
- `.venv/bin/python -m pytest -q` 全部通过。
