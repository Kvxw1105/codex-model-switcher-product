# Gate 1 证据：当前 Codex 客户端 picker / app-server 契约（2026-08-06）

本文件只记录不含隐私的官方来源证据与可复现的取证命令。没有复制真实
`auth.json`、`catalog.json`、cookie、Authorization 或任何用户路径内容。
证据来源分三类：官方文档、当前安装客户端自带的 schema 生成器、官方开源源码。

## 1. 本机环境

- 当前安装客户端：`codex-cli 0.133.0`（npm 全局安装，`codex --version`）。
- 取证命令（本机可复现，输出到临时目录，不入库）：

```powershell
codex app-server generate-json-schema --out <DIR>
codex app-server generate-ts --out <DIR>   # 可选 TS 绑定
```

生成物是官方客户端自带的协议 schema（JSON Schema + 方法名枚举），
不含任何真实凭据或用户数据。

## 2. 官方文档证据（developers.openai.com/codex/config-reference.md）

以下键在官方 `config.toml` 参考中明确列出（用户级配置）：

- `model_provider`：`string`，`Provider id from model_providers (default: openai)`。
- `model_catalog_json`：`string (path)`，`Optional path to a JSON model catalog loaded on startup`。
- `model_providers.<id>`：自定义 provider 表，字段包括
  `name`、`base_url`、`env_key`、`env_key_instructions`、
  `experimental_bearer_token`、`requires_openai_auth`（默认 `false`）、
  `wire_api`（唯一支持值 `responses`）、`query_params`、`http_headers`、
  `env_http_headers`、`request_max_retries`、`stream_max_retries`、
  `stream_idle_timeout_ms`、`supports_websockets`、`supports_standalone_web_search`、
  `auth.command` / `auth.args` / `auth.timeout_ms` / `auth.refresh_interval_ms` / `auth.cwd`。
- 内置 provider id 保留：`openai`、`ollama`、`lmstudio`（`amazon-bedrock` 也是内置），
  自定义 provider 不能覆盖这些 id。
- `config-advanced.md` 给出了完整 TOML 示例：`model_provider = "proxy"` 配合
  `[model_providers.proxy] name/base_url/env_key`，以及 `wire_api = "responses"`
  与 `[model_providers.<id>.auth]` 命令式取 token 的写法。
- 官方文档说明：项目级 `.codex/config.toml` 不能覆盖
  `model_provider` / `model_providers` / `openai_base_url` 等键，必须写用户级配置。
- 结论：本项目的候选字段（`model_provider` + `model_providers.<id>` +
  `model_catalog_json`）是官方 picker 接受的真实 schema，不再是无依据猜测。

## 3. 当前安装客户端 schema 生成器证据（codex-cli 0.133.0）

`codex app-server generate-json-schema` 生成的协议定义（不含隐私）中：

- `TurnStartParams.model`：`Override the model for this turn and subsequent turns.`
  —— 官方协议允许同一 thread 的下一 turn 指定不同 model ID（turn 边界切换的官方支持）。
- `TurnStatus` 枚举：`completed | interrupted | failed | inProgress`。
- `TurnInterruptParams`：`threadId` + `turnId`（按 turn 取消的官方参数）。
- `TurnSteerParams`：`expectedTurnId` 前置条件（请求失败当它不匹配当前活动 turn）。
- `ThreadCompactStartParams`：`threadId`（compact 是 thread 级官方操作）。
- `ThreadSettings`：含 `model` 与 `modelProvider` 字段。
- `ModelListResponse` / `Model`：picker 模型列表契约（`id`、`model`、`displayName`、
  `description`、`hidden`、`isDefault`、`defaultReasoningEffort`、
  `supportedReasoningEfforts`、`serviceTiers`、`inputModalities` 等）。
- `ConfigValueWriteParams` / `ConfigReadParams` / `ConfigWriteResponse`：
  app-server 提供配置读写方法（`keyPath`、`value`、`mergeStrategy` =
  `replace | upsert`、`filePath` 默认用户 `config.toml`、`expectedVersion`、
  返回 `version` 与 `status = ok | okOverridden`）。
- `ModelReroutedNotification`：`threadId`、`turnId`、`fromModel`、`toModel`、`reason`。
- `ModelProviderCapabilitiesReadResponse`：`imageGeneration`、`namespaceTools`、`webSearch`。

## 4. 官方开源源码证据（github.com/openai/codex, main）

- `codex-rs/protocol/src/openai_models.rs`：
  - `ModelsResponse { models: Vec<ModelInfo> }` 是 `/models` 与
    `model_catalog_json` 文件的共同结构（`config/mod.rs` 的
    `load_catalog_json` 把它解析为 `ModelsResponse`，且要求至少一个 model）。
  - `ModelInfo` 字段：`slug`、`display_name`、`description`、
    `default_reasoning_level`、`supported_reasoning_levels`、`shell_type`
    （枚举含 `Disabled`，snake_case 序列化为 `"disabled"`）、`visibility`
    （枚举 `List` 序列化为 `"list"`）、`supported_in_api`、`priority`、
    `context_window`、`max_context_window`、`auto_compact_token_limit`、
    `effective_context_window_percent`、`input_modalities`（`text`/`image`/`audio`）、
    `truncation_policy` 等。
  - `ClientVersion(pub i32, pub i32, pub i32)`：语义版本三元组，
    在 JSON 中编码为数组（如 `[0, 62, 0]`）。
  - `codex-rs/models-manager/src/lib.rs`：`client_version_to_whole()` 把客户端
    版本转成 `MAJOR.MINOR.PATCH` 字符串，models cache 按 `client_version`
    键控，版本不一致时刷新（`cache_entry.client_version != client_version`）。
- 结论：`client_version` 与模型目录版本关系已由官方源码确认；
  本项目 `build_catalog_from_model_cache` 从调用方提供的模型缓存读取
  `client_version` 的做法与官方语义一致。

## 5. 运行时加载收据（codex-cli 0.133.0 实测）

在隔离 CODEX_HOME 中实测当前安装客户端（详见 `scripts/verify-native-load.sh`，
仓库内可复现）：

- 构造隔离 `CODEX_HOME` + `config.toml`，其中 `model_catalog_json` 指向本项目
  生成的 native catalog，`model_provider = "deepseek"` 配合 `[model_providers.deepseek]`
  （`base_url = http://127.0.0.1:4318/v1`、`wire_api = "responses"`、
  `requires_openai_auth = false`）。
- 运行 `codex debug models`：**exit 0**，输出恰好 1 个模型
  （`cms-deepseek-v4-flash`），字段（`slug`/`display_name`/`shell_type`=
  `"disabled"`/`visibility`=`"list"`/`context_window`/`input_modalities` 等）
  被完整解析，stdout/stderr 无 error/failed。0.6 秒内返回，无网络等待。
- 基线对照（去掉 `model_catalog_json`）：输出官方 bundled 的 6 个模型
  （`gpt-5.5` 等）。两组差异证明 `model_catalog_json` 确实被当前客户端
  读取并**替换**了 bundled catalog——与官方 `config/mod.rs` 的
  `load_catalog_json` 语义一致。
- 重要字段名发现：当前 0.133.0 客户端序列化/接受 `supports_reasoning_summaries`；
  GitHub main 分支（更新版本）才改名 `supports_reasoning_summary_parameter`。
  本项目以**当前安装客户端**为权威基准，native catalog 输出
  `supports_reasoning_summaries`。

结论：**当前客户端能加载本项目生成的 catalog 文件**这一运行时收据已取得
（RUNTIME LOAD VERIFIED）。仍缺的是 picker 显示、官方→第三方→官方切换、
compact/工具/重启的完整桌面交互收据。

## 5b. 原生请求实测（codex exec → 本地 Router → 真实上游）

在隔离 CODEX_HOME 中运行 `codex exec`（0.133.0），config 指向本项目 native
catalog + `[model_providers.deepseek]`（`base_url = http://127.0.0.1:4318/v1`），
本地 Router 转接到真实 DeepSeek Responses 上游，端到端连续多次成功
（回复 `OK`、`tokens used` 正常）。实测确认：

- 原生请求确实发往 `model_providers.<id>.base_url` 的 `/v1/responses`，
  且 `model` 取 native catalog 中的 slug、`instructions` 取
  `base_instructions`。
- **关联头是官方 `X-Codex-Turn-Metadata`**（JSON 含 `session_id`/`thread_id`/
  `turn_id`/`thread_source`），不是自定义的 `X-Codex-Task-Id`/
  `X-Codex-Turn-Id`。Router 已兼容解析该头（`_correlation_ids`）。
- 第三方请求**不携带 Authorization**（`requires_openai_auth = false` 生效），
  官方身份不会转发给第三方 provider。
- 端到端偶发挂起来自上游瞬时慢响应与 codex 客户端重连等待；重试即恢复。

结论：**同一 task/thread 内把第三方 provider 配置进真实 codex 客户端并完成
真实 turn** 的运行时收据已取得（RUNTIME TURN VERIFIED）。仍缺的是官方通道
turn、picker 显示与 compact/工具/重启的桌面交互收据。

## 7. 桌面端 26.730 的 model_catalog_json 覆盖行为（实测，2026-08-08）

在真实桌面端（WindowsApps 版 OpenAI.Codex 26.730，内核 ≈ client_version
0.147.0）实测：

- 现象：config.toml 受管区块（`model_provider = "deepseek"` + `model_catalog_json`
  指向 11 模型 native catalog）在桌面端**账户/设置区显示 provider=deepseek**，
  但模型列表只有 8 个官方在线模型（无 `cms-deepseek-v4-flash`）。
- 根因：桌面端 26.730 启动时用 `OnlineIfUncached` 从官方 `/models` 拉取并写入
  `models_cache.json`（`client_version = 0.147.0`），**在线模型列表覆盖/取代了
  `model_catalog_json` 提供的 catalog**。CLI 0.133.0 则用 `model_catalog` 替换
  bundled（两者对同一配置行为不同）。
- 证据：`models_cache.json` 的 `fetched_at` 与桌面端启动时间吻合；
  同一 config 下 CLI `codex debug models` 显示 11 个（含 DeepSeek），
  桌面端 model/list 路径被在线列表取代。
- 结论：**`model_catalog_json` 在桌面端 26.730 上不可靠（被在线覆盖）**，
  真实桌面端同屏共存第三方模型暂不可行。这是客户端版本行为差异，不是
  本项目配置错误。

### 可用通道（已验证）

- **CLI 通道**：隔离 CODEX_HOME + 完整 provider config + 本地 Router →
  真实 DeepSeek turn 成功。可复现：`scripts/cli-deepseek.sh "提示词"`。
  该脚本不修改真实 `~/.codex`。
- 桌面端恢复官方模型列表：移除本项目受管区块后，桌面端重启即回到
  官方在线模型（含账户专属模型如 gpt-5.6-luna）。

## 8. Gate 1 结论

- picker 可接受的 provider/catalog schema：**已证实**（官方文档 + 本机 schema 生成器 +
  运行时加载收据）。
- `client_version` 与模型目录版本关系：**已证实**（官方源码）。
- 同一 thread 的下一 turn 可指定不同 model ID：**已证实**（官方 schema
  `TurnStartParams.model`）。
- app-server 的 turn 边界与取消语义：**已证实**（`TurnStatus`、
  `TurnInterruptParams`、`TurnSteerParams.expectedTurnId`）。
- compact 归属：**已证实为 thread 级官方操作**（`ThreadCompactStartParams`）；
  具体摘要内容仍由官方 app-server 管理，本项目不生成第二份摘要。
- 官方认证只到官方 host：`requires_openai_auth` 默认 `false` + 独立
  `env_key`/`auth` 机制（官方文档）；第三方 provider 不使用 OpenAI 认证。
  官方认证 header 的具体值**未取得也不猜测**（遵守约束）。

## 9. 仍未验证（保持阻断的原因）

- picker 是否显示本项目 catalog 中的第三方模型并可在真实 Codex 桌面端选中：
  需要完整桌面 smoke（`gui --smoke` + `docs/smoke-acceptance-checklist.md`）。
- 同一 task 的官方→第三方→官方完整桌面切换：未做。
- compact、工具、文件、重启恢复的真实 Codex smoke：未做。
- 官方 endpoint / 认证 header 细节：未取得（约束禁止猜测，本项目不需要转发官方身份）。

因此 config apply/restore 继续遵守显式开关与原子备份/恢复/哈希证据约束，
在完整桌面验收完成前保持阻断，不伪装成可用。
