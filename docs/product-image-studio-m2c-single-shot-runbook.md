# Product Image Studio M2C — First Single-Shot Acceptance Runbook

**状态：`READY_FOR_USER_AUTHORIZED_SINGLE_SHOT`。本文件只是准备，未经用户明确授权不得执行。**

本 runbook 描述系统解锁后的**第一次、也是唯一一次**真实付费验收调用。它不是本轮
（M2C-A）交付的一部分；本轮只交付可验证的精确成本预览，Production 保持
`LIVE_GENERATION_ENABLED=false`、`APIYI_API_KEY` 为空。

## 验收对象

- Shot：`temu_model_full_front`（恰好一个 Shot）
- 数量：恰好 **1** 张输出
- 模型：`nano_banana_2`（API model ID `gemini-3.1-flash-image`）
- Pricing Contract：`apiyi-nb2-per-request-2026-03-01`，$0.055/次（按次，2K 与 4K 同价）
- 预计收费：**$0.055**（固定）；hard max cost 建议 **$0.06**
- 自动 retry：0；自动 regeneration：0；M3 QA：关闭
- 证据：`docs/product-image-studio-m2c-pricing.md`（E1）

## 执行前的用户授权清单（缺一不可）

1. 用户明确授权**恰好一次**真实付费调用（$0.055）。
2. 用户在 APIYI 控制台确认所用令牌的 **Billing model = 按次计费（Pay-per-request）**。
   官方规则：令牌类型决定计费方式；若为「按量优先」，同一调用将按 token 实计
   （2K 预估约 $0.045）。这是 API 无法验证的部署侧前提。
3. 向用户展示并获确认：
   - 项目、Shot、Provider/Model（`gemini-3.1-flash-image`）
   - 三类 clean references（product/detail/style），确认 annotation preview 不进入 payload
   - 完整 PromptPackage（rendered prompt + negative prompt）
   - 输出尺寸/比例（3:4，2K）
   - Pricing Contract：版本、effective date、单价、证据来源与 digest
   - estimated $0.055 与 hard max $0.06
   - idempotency key 短标识（前 12 位）

## 执行步骤（授权后）

1. 临时配置真实 `APIYI_API_KEY`（写入 Production `.env`，不回显、不入库、不入日志）。
2. 临时设置 `LIVE_GENERATION_ENABLED=true`。
3. `docker compose up -d` 使配置生效，确认 `/health`。
4. 通过 CLI 创建恰好一个 Shot 的 Live Job：
   `tif studio generate-live <project> <plan> --mode live --provider apiyi --model nano_banana_2 --shot-id <temu_model_full_front shot id> --max-cost 0.06 --confirm-paid-generation`
5. 记录：Candidate SHA256、provider request ID（若响应携带）、提交/完成时间、
   ledger 条目、attempt/job 状态、reconciliation 状态。
6. 人工结构性评估 Candidate（构图、产品保真、文字/水印）。

## 执行后必须立即恢复锁定

1. `LIVE_GENERATION_ENABLED=false`。
2. 从 `.env` 移除真实 `APIYI_API_KEY`（恢复为空）。
3. `docker compose up -d`，确认 `/health` 与 `provider-status` 回到 locked/not_configured。
4. 核对 ledger 只有这一条真实调用记录；real APIYI calls 总数 = 1。

## 异常处置

- timeout / 响应丢失 / 任何不确定结果：**不得重试**。attempt 会进入
  `reconcile_required`；按 M2B 语义人工对账（官方说明：已接受请求即使响应丢失也可能计费）。
- 4xx 拒绝：记录错误码，未产生生成，允许在修复原因后由用户再次授权一次新调用。
- 任何意外多次扣费迹象：立即停止，保留现场，报告用户。

## 禁止事项

- 不得在一次授权内执行第二次真实调用。
- 不得启用自动 retry / 自动 regeneration / 整 plan Live。
- 不得执行 M3 QA/返修。
- 不得把验收 Candidate 直接当作正式商品图发布（需用户人工确认）。
