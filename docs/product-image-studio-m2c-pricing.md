# Product Image Studio M2C — Verified APIYI Pricing Evidence

本文件记录 M2C-A 里程碑的权威定价证据。只有满足证据标准（APIYI 官方公开价格文档 /
官方控制台 / 官方合同）的模型才允许 `pricing_status: exact`。证据检索时间（retrieval
timestamp）：**2026-08-01T01:59Z（UTC）**。检索方式：只读 HTTP 抓取官方文档页，未调用任何
生成端点，real APIYI calls = 0。

## 结论速览

| repository model | provider model ID | 判定 | 价格 |
| --- | --- | --- | --- |
| nano_banana_2 | `gemini-3.1-flash-image` | **exact** | $0.055 / 次（按次计费，512px–4K 统一价） |
| nano_banana_lite | `gemini-3.1-flash-lite-image` | unknown（证据存在但官方标注"售价暂定"，本轮不解锁） | — |
| gpt_image_2_vip | `gpt-image-2-vip` | unknown（官方公告 `size` 参数 2026-06-23 起失效，合同会误导） | — |
| gpt_image_2 | `gpt-image-2` | unknown（按 token 实计，动态价格，无固定单价可版本化） | — |

## E1 — Nano Banana 2（exact）

- Provider：APIYI
- repository model name：`nano_banana_2`
- API model ID：`gemini-3.1-flash-image`（GA 正式名）
- pricing display name：Nano Banana 2 按次计费
- request mode：generation / edit（文生图与图片编辑同一按次价格；参考图不改变按次单价）
- price unit：**per request（按次）**
- currency：USD
- exact amount：**$0.055 / 次**
- resolution：512px / 1K / 2K / 4K 统一按次价（不区分分辨率）
- aspect ratio 约束：14 种宽高比（1:1, 1:4, 4:1, 1:8, 8:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9），按次价不变
- reference image 数量是否影响价格：否（按次固定价；参考图数量仅影响按量 token 计费模式，本合同不采用该模式）
- output 数量是否影响价格：单次请求返回 1 张（Gemini generateContent 同步单图；Studio 端强制恰好 1 张）
- quality 档位是否影响价格：该模型无 quality 档位参数
- effective date：**2026-03-01**（官方更新日志"Nano Banana 2 价格调整：按次计费 $0.055/次"生效日；2026-04-20 仅微调按量价格，按次价未变；2026-05-29 官方公告 GA 名 `gemini-3.1-flash-image` 上线且"价格不变"）
- retrieval timestamp：2026-08-01T01:59Z
- source title：API易文档中心《Nano Banana 系列价格总览》《Nano Banana 2 生图/编辑》《网站公告（更新日志）》
- source URL：
  - https://docs.apiyi.com/api-capabilities/nano-banana-pricing
  - https://docs.apiyi.com/api-capabilities/nano-banana-2-image/overview
  - https://docs.apiyi.com/changelog
- source type：**public_official**（APIYI 官方文档中心）
- evidence digest：`sha256:0c4dec06c0d2420f22d19d7183e9250c20f041ad955cf430faa2d421f467df0f`
  （对规范化证据记录 {"provider","provider_model_id","amount","unit","currency","effective_at","sources"} 的 SHA-256）
- 是否含税：官方页面未声明（记为 unknown）
- 是否存在地域差异：官方页面未声明（记为 unknown）
- 证据适用的精确 model ID：`gemini-3.1-flash-image`（GA 名，2026-05-29 官方公告与 `-preview` 名同价）

### 关键官方表述摘录（简短）

1. 价格总览：「Nano Banana 2 … 按次计费 $0.055/次，1-4K 统一按次价格」。
2. 更新日志 2026-05-29：「推出去掉 `-preview` 的正式模型名 `gemini-3.1-flash-image`（Nano Banana 2）… API易已同步上线支持。原有 `-preview` 名仍可正常调用、**价格不变**」。
3. 更新日志 2026-03-01：「Nano Banana 2 价格调整：按次计费 $0.055/次」。
4. 使用指南：「图片 API 全部为**同步调用**：没有异步任务 ID，客户端断开连接结果即丢失、**但请求仍会计费**」——证实不存在服务端状态查询端点，且已接受请求在响应丢失时可能已被计费（M2B reconcile_required 设计的官方依据）。

### 对首次单发（temu_model_full_front, 3:4, 2K, 恰好 1 张）的回答

- 预计收费：**$0.055**（按次固定，2K 与 4K 同价）。
- 是否固定收费：是（按次计费令牌下为固定单价）。
- references 是否改变收费：否。
- 失败/超时可能如何收费：已被 Provider 接受的请求即使响应丢失"仍会计费"（官方表述）；被 4xx 拒绝的请求官方未声明会计费。因此 Studio 的 unknown/reconcile 账务语义保持不变。
- 一次请求是否可能返回多张：同步单图；Studio 端对多结果按 malformed 拒绝。
- hard max cost 建议：**$0.06**（单价固定，上限仅作护栏）。
- **前提条件（部署侧，API 不可验证）**：APIYI 令牌的 Billing model 必须设为「按次计费 / Pay-per-request」。官方说明令牌类型决定计费方式；若令牌为「按量优先」，同一调用将按 token 实计（2K 约 $0.045 预估）。该前提必须写入单发 runbook 由操作员确认。

## E2 — Nano Banana Lite（本轮保持 unknown）

- API model ID：`gemini-3.1-flash-lite-image`，官方价格：按次 $0.025/次（1K），按量约 $0.018/张。
- 证据来源同上（价格总览 + 2026-07-01 上线公告），但官方更新日志明确标注「**刚上线售价暂定，如有变动会公告**」。
- 判定：价格为官方暂定（provisional），不满足本轮"权威、明确、可版本化"的解锁门槛；保持 `pricing_status: unknown`，Live 继续阻断。后续价格稳定后可按同一流程版本化。

## E3 — GPT-Image-2-VIP（本轮保持 unknown）

- API model ID：`gpt-image-2-vip`（官逆 Codex 线），官方价格：固定 $0.03/张（与 gpt-image-2-all 同价）。
- 证据来源：https://docs.apiyi.com/api-capabilities/gpt-image-2/vs-gpt-image-2-all （2026-07-15，public_official）。
- 判定：官方明确「`size` 参数自 2026-06-23 起失效，固定自适应 1K 出图，暂无恢复预期」。本仓库 config 仍为该模型配置 30 档精确尺寸表（`exact_size: true`），与官方现状冲突；此时标记 exact 会让 Pricing Contract 的 supported_output_sizes 失真。保持 unknown；若官方恢复 `size`，需先修正模型能力配置再版本化价格。

## E4 — GPT-Image-2（本轮保持 unknown）

- API model ID：`gpt-image-2`（官转），按 token 实计，官方参考区间约 $0.03–$0.2+/张（随 size/quality/prompt 长度变化）。
- 判定：动态定价，不存在可版本化的固定单价；保持 unknown。`estimated_cost_mode: dynamic` 保留。
