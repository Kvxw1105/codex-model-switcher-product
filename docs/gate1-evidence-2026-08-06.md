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

## 5. Gate 1 结论

- picker 可接受的 provider/catalog schema：**已证实**（官方文档 + 本机 schema 生成器）。
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

## 6. 仍未验证（保持阻断的原因）

- 真实 Codex 客户端启动后是否接受本项目生成的具体 catalog 文件并显示在
  picker 中：仍需要桌面 smoke（隔离 CODEX_HOME 或显式开关 + 备份 + 恢复 +
  SHA-256 证据）。
- 同一 task 的官方→第三方→官方完整桌面切换：未做。
- compact、工具、文件、重启恢复的真实 Codex smoke：未做。
- 官方 endpoint / 认证 header 细节：未取得（约束禁止猜测，本项目不需要转发官方身份）。

因此 config apply/restore 继续遵守显式开关与原子备份/恢复/哈希证据约束，
在真实桌面验收完成前保持阻断，不伪装成可用。
