# 桌面契约与 Gate 1 证据

本文件只记录本 worktree 能安全复现的契约和验证边界。fixture 使用 `example.invalid` 语义的假值；没有复制真实 catalog、认证头、token、cookie 或用户路径。

## 已实现的可证实契约

- `ModelCapability` 与 `ModelRoute` 是 frozen dataclass。
- 每个 route 必须显式提供全部能力字段；缺失能力会拒绝加载，因此不会为未知模型填充统一 context window。
- route 的 `model_id`、`provider_id` 和 `upstream_model` 必须是稳定、非空且无空白的标识；display name 必须包含 `Official` 或 `API` lane 标记。
- 目录生成从调用方提供的模型缓存读取 `client_version`，不会硬编码客户端版本。没有 schema 证据时，候选明确带有 `schema_version: null` 和 `verification_status: UNVERIFIED`；`picker-v1` 不再是默认或通过信号。
- 配置只写入受管区块中的 `model_provider` 与 `model_catalog_json`。没有写入 upstream URL、Authorization、cookie 或凭据字段。
- `render_managed_config` 与 `apply_managed_config` 只接受由模块内部可信流程登记的 opaque writer capability；验证同时要求 receipt 对象本身的 `id` 命中私有 identity registry、registry 的 `weakref.ref(receipt) is receipt`，并校验 registry 封存字段与候选 catalog SHA-256。`PickerVerificationReceipt` 没有 public/protected 构造器、`_from_verifier` 或 `issue_receipt` 路径；当前没有真实 receipt 时 render/apply 必须拒绝。
- `TrustedPickerVerifier` 只是未来真实外部 verifier 的证据读取抽象，本项目没有 concrete implementation，也不接受调用方注入 `evidence_provider`，更不会从 untrusted subclass/provider 生成 receipt。私有 registry 不是当前客户端证据，且不向 caller 暴露 registry/seal。
- 受管区块 start/end marker 必须各自独占完整行且恰好一对；marker 出现在用户注释、TOML 字符串、嵌入文本或重复区块时直接拒绝替换。
- apply 会在首次读取、当前 hash 检查、备份、临时文件写入和最终替换期间同时持有同路径临界区。Windows 用 `CreateFileW(FILE_FLAG_OPEN_REQUIRING_OPLOCK)` 在打开目标时取得 OS 级 namespace 保护，并用 `LockFileEx` 做协作进程内的全文件锁；单独的 `LockFileEx` 不被当作跨进程 rename 防线。替换使用 TxF：在同一 `CreateTransaction` 中把旧目标移到临时 shadow、建立新临时文件到目标的硬链接、删除两个临时名字，并用 `CreateFileTransactedW` 预先打开带 `share read/write`（拒绝 `DELETE/RENAME`）的新目标句柄，随后才 `CommitTransaction`。提交返回时新句柄已经存在，因此没有 `ReplaceFileW` 返回到 relock 的路径窗口；TxF/相关 API 不可用时 fail-closed，不回退到普通 `os.replace`。成功 apply 才创建包含写前字节和时间戳的原子备份，receipt 保存写前/写后 SHA-256。
- 上述 Windows 强保证限定在支持这些 API 语义的本地文件系统；网络盘或特殊文件系统若不能取得 oplock/事务能力会拒绝操作，不把降级路径宣称为全局保护。
- 非 Windows 使用进程锁和同目录 sidecar `flock`，只保证遵守该协作锁的进程；不参与锁的外部编辑仍可能造成 TOCTOU，代码不会把该 fallback 宣称为全局防护。若平台没有 `fcntl`，获取协作锁会 fail-closed，拒绝无锁写入。Windows 锁获取失败、锁定冲突或最终原子替换失败会拒绝操作。
- restore 也在同一目标文件临界区内读取当前 hash、校验备份、写临时文件并替换；当前文件不是本项目最后一次写入的 hash 时拒绝，备份 hash 也必须匹配，恢复结果按字节保留原文件。

## Gate 1：当前客户端原生 picker

状态：`CONTRACT VERIFIED / RUNTIME LOAD VERIFIED / PICKER UNVERIFIED`。

证据文件：`docs/gate1-evidence-2026-08-06.md`（只含官方文档、当前客户端 schema
生成器、官方开源源码与隔离 CODEX_HOME 实测的不含隐私证据；不复制真实
catalog、auth、token、cookie）。复现脚本：`scripts/verify-native-load.sh`。

- picker 可接受的 provider/catalog schema：官方 `config.toml` 参考明确列出
  `model_provider`、`model_providers.<id>`（`name`/`base_url`/`env_key`/
  `requires_openai_auth`/`wire_api = "responses"` 等）与 `model_catalog_json`；
  `config-advanced.md` 给出完整 TOML 示例。
- 运行时加载收据（实测）：隔离 CODEX_HOME 中 `model_catalog_json` 指向本项目
  生成的 native catalog 时，当前客户端（codex-cli 0.133.0）`codex debug models`
  成功加载并替换 bundled（exit 0、输出本项目模型、无 error）；去掉
  `model_catalog_json` 后回落官方 bundled。字段名以当前客户端为准
  （`supports_reasoning_summaries`，非 main 分支的新名）。
- `client_version` 与模型目录版本关系：官方源码确认 `ModelsResponse` 是
  `/models` 与 `model_catalog_json` 的共同结构，`ClientVersion` 是语义版本
  三元组，models cache 按 `client_version` 键控。
- 同一 thread 的下一 turn 可指定不同 model ID：官方 schema
  `TurnStartParams.model`（"Override the model for this turn and subsequent turns"）。
- turn 边界与取消：官方 schema `TurnStatus`、`TurnInterruptParams`（threadId+turnId）、
  `TurnSteerParams.expectedTurnId`。
- compact：官方 schema `ThreadCompactStartParams` 是 thread 级操作。
- 官方认证只到官方 host：`requires_openai_auth` 默认 `false`；官方认证 header
  细节未取得也不猜测。

代码中的 `verify_isolated_picker_contract` 只在隔离 `CODEX_HOME` 中做候选一致性检查，结果永远是 `passed: false`，即使 fixture 的 schema/version 与配置完全一致也不会宣称 native picker 通过。它检查：

1. `config.toml` 能被 TOML 解析；
2. provider 与候选目录匹配；
3. `model_catalog_json` 是与候选目录相同的 JSON；
4. 记录调用方提供的不含隐私的候选 schema/version 来源，但不把该字段当作当前客户端通过信号。

缺少真实外部 receipt 时结果明确为 `FAIL / UNVERIFIED`，即使本地配置语法正确也不能宣称 Gate 1 通过。`_register_receipt_for_registry_test` 只登记一个供 clone/identity contract 使用的内部测试对象，不进入 render/apply，也不代表 picker 证据；没有真实客户端启动、付费模型请求或 endpoint 猜测。进程内 capability/测试 seam 永远不等于 Gate 1 证据。

## app-server、turn 边界与 compact

当前安装客户端（codex-cli 0.133.0）自带的 `codex app-server generate-json-schema`
已生成不含隐私的协议 schema（证据见 `docs/gate1-evidence-2026-08-06.md`）。据此：

- `TurnStartParams.model` 允许同一 thread 的下一 turn 指定不同 model ID；
- `TurnStatus`（completed/interrupted/failed/inProgress）、`TurnInterruptParams`
  （threadId+turnId）与 `TurnSteerParams.expectedTurnId` 定义了 turn 边界和取消语义；
- `ThreadCompactStartParams` 是 thread 级 compact 操作；
- app-server 提供 `ConfigValueWriteParams`/`ConfigReadParams` 配置读写方法与
  `ModelReroutedNotification`（threadId/turnId/fromModel/toModel）。

仍保持未证实的部分：真实客户端运行时行为（picker 是否显示第三方模型、
compact 具体内容、跨通道 token 语义、官方 endpoint 与认证 header）不以任何
推断冒充验证；catalog 加载已在隔离 CODEX_HOME 实测通过（RUNTIME LOAD
VERIFIED），picker 显示与完整桌面切换在完整桌面 smoke 完成前仍标注
PICKER UNVERIFIED。
compact 归 app-server/thread 权威。本项目的目录和配置模块不保存聊天历史、
不生成第二份摘要，也不实现第二套 compact 状态。
