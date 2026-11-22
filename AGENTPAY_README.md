# 作品说明（正式定稿版 v1.0）· Agent Pay 特殊赛道
## Admit × Facetry：对齐支付宝 APASS（KYA）四层可信授权与 A402 智能体商业护栏

### 1. 一句话价值主张
Admit × Facetry 在支付宝 AI 按量付费（HTTP 402 / `alipay-aipay`）之上构建完整的买方授权护栏与卖方核验交付闭环：全面对齐蚂蚁集团 **APASS（智能体可信身份基础设施 · Know Your Agent）** 四层密码学存证链，原生兼容官方 `@alipay/agent-payment@1.0.0`（`alipay-bot`）CLI，实现“越权分文不花、偏题重签确认、履约逐笔可验”。

### 2. 对齐支付宝 APASS（KYA）四层可信授权存证结构（`apass.kya.v1`）
每一笔成交订单均生成逐层锚定的四层 SHA-256 哈希链（`GENESIS → L1 → L2 → L3 → L4`）并由商户 RSA2 根私钥签名：
1. **Layer 1 · 人类预算签名（可信身份 / Human Mandate Signature）**：用户以 Ed25519 私钥签署 `admit.mandate.v1` 采购授权单（限定商户白名单、单笔上限、累计上限、类目、最大次数、有效期与意图关键词），并绑定买方 Agent 运行时 Ed25519 公钥（RFC 9421 请求签名 + TOFU 公钥钉扎）。
2. **Layer 2 · 商户商品锁定（持续核身与意图安全 / Merchant SKU Lock）**：商户网关执行 13 条 Admit 动态准入规则核验（未过期待付单同步预占预算防并发击穿；意图偏离触发 `HTTP 409 NEEDS_CONFIRMATION` 挂起待用户二次重签），核验通过后锁定商品与价格，签发带商户 RSA2 签名的 HTTP 402 账单。
3. **Layer 3 · 支付凭据签发（A402 凭据核验 / Payment Credential Issuance）**：买方钱包完成支付后携带 Base64URL `Payment-Proof` 重试；商户调用 `alipay.aipay.agent.payment.verify` 透传 `client_session` 验真，严格核对 `active=true`、`amount`、`out_trade_no` 与 `resource_id` 防串货、防重放。
4. **Layer 4 · 履约回执哈希链（可信存证 / Fulfillment Receipt Hash Chain）**：交付结构化商业核验报告并计算 `delivery_sha256`，调用 `alipay.aipay.agent.fulfillment.confirm` 回传支付宝履约确认，合并追加式 `AuditLog` 最新链头哈希签发可公开独立验签的履约回执包。

### 3. 双模协议兼容与高速稳定选品客户端
- **原生双模兼容 `Payment-Needed` 协议头**：同时支持文档平铺字段格式与官方 `A2MPaymentDemoController` / `@alipay/agent-payment@1.0.0`（`alipay-bot -- 402-buyer-pay`）分层 `{"protocol": {...}, "method": {...}}` Base64URL 编码结构（端点 `/a2m/resource/{sku}` 与响应头 `Payment-Needed-A2M`）。
- **亚 2 秒高速稳定买方选品客户端**：重构 `agentpay/llm_agent.py`，默认优先接入高可用内网代理高速模型（单次严格 8s 超时 + 瞬时超时/5xx/429 指数退避重试，彻底根除慢模型阻塞），严格限制仅能从目录内合法 SKU 选品，异常时无损回退确定性二元语法选品引擎。

### 4. 9 大越权 / 重复 / 误付红队拦截矩阵（工作台一键验证 `9/9 已拦截`）
| # | 攻击/异常场景 | 拦截响应与规则码 |
|---|---|---|
| 1 | 单笔金额超限 | `403 MANDATE_AMOUNT_PER_TX` |
| 2 | 商户不在白名单 | `403 MANDATE_MERCHANT_MISMATCH` |
| 3 | 授权单已过有效期 | `403 MANDATE_EXPIRED` |
| 4 | 累计预算或次数耗尽（含并发预占锁） | `403 MANDATE_AMOUNT_TOTAL` / `MANDATE_USES_EXHAUSTED` |
| 5 | 采购商品类目越界 | `403 MANDATE_CATEGORY` |
| 6 | LLM 选品偏离用户原始意图 | `409 CONFIRM_INTENT_DRIFT`（挂起待用户 Ed25519 一次性重签批准或拒绝） |
| 7 | 并发重复下单 / 同一凭证重复领货 | 复用待支付单；幂等返回历史交付物，`fulfillment.confirm` 仅记 1 次 |
| 8 | 伪造 `payment_proof` 或小额凭证串换高价商品 | `402 INVALID_PAYMENT_PROOF` / `PROOF_RESOURCE_MISMATCH` |
| 9 | 伪造 Agent 签名 / 密钥已吊销 / 时钟重放 | `401 AGENT_SIGNATURE_INVALID` / `AGENT_KEY_REVOKED` / `AGENT_SIGNATURE_STALE` |

### 5. 网关切换与参赛团队
- **网关切换**：支持通过 `secrets/aipay_sandbox.env` 或 `secrets/aipay_prod.env`（`chmod 600`）零代码切换支付宝官方沙箱或正式生产网关；未配置外部密钥时默认运行本地 RSA2 契约模拟网关。
- **参赛主体**：上海奇点砚科技有限公司（队长：王同鹤 ｜ 路演答辩：韩汶均 ｜ 核心研发：伊力哈木）。
