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
- apply 会在首次读取到 `os.replace` 之间持有同路径进程锁，并在临时文件替换前用原始字节做 compare-and-swap；发现并发编辑会拒绝覆盖。成功 apply 才创建包含写前字节和时间戳的备份，并以同目录临时文件加 `os.replace` 原子替换目标。receipt 保存写前/写后 SHA-256。
- restore 仅在当前文件仍匹配本项目最后一次写入的 hash 时执行；外部编辑会拒绝覆盖。备份 hash 也必须匹配，恢复结果按字节保留原文件。

## Gate 1：当前客户端原生 picker

状态：`FAIL / UNVERIFIED`。

本 worktree 没有安全可用的当前 Codex 客户端 schema 或 app-server 证据，且本任务禁止读取或输出真实 `catalog.json`、`auth.json`、token、cookie、Authorization 值。因此无法证明候选 `model_provider + model_catalog_json` 会被当前客户端接受，也不能把候选字段称为官方 picker schema。

代码中的 `verify_isolated_picker_contract` 只在隔离 `CODEX_HOME` 中做候选一致性检查，结果永远是 `passed: false`，即使 fixture 的 schema/version 与配置完全一致也不会宣称 native picker 通过。它检查：

1. `config.toml` 能被 TOML 解析；
2. provider 与候选目录匹配；
3. `model_catalog_json` 是与候选目录相同的 JSON；
4. 记录调用方提供的不含隐私的候选 schema/version 来源，但不把该字段当作当前客户端通过信号。

缺少真实外部 receipt 时结果明确为 `FAIL / UNVERIFIED`，即使本地配置语法正确也不能宣称 Gate 1 通过。`_register_receipt_for_registry_test` 只登记一个供 clone/identity contract 使用的内部测试对象，不进入 render/apply，也不代表 picker 证据；没有真实客户端启动、付费模型请求或 endpoint 猜测。进程内 capability/测试 seam 永远不等于 Gate 1 证据。

## app-server、turn 边界与 compact

当前安装客户端的 app-server 契约未在本轮获得可安全审查的证据，故以下结论保持未证实：同一 thread 的下一 turn 是否可指定另一个 model ID、完成/取消后的切换边界，以及是否存在稳定且不含隐私的 thread/turn correlation 字段。本项目不虚构 thread/turn ID，也不猜测请求 endpoint、请求头或认证行为。

compact 归 app-server/thread 权威。本项目的目录和配置模块不保存聊天历史、不生成第二份摘要，也不实现第二套 compact 状态。

在获得当前客户端或官方文档的非敏感证据前，不能报告官方上游 URL、请求头、认证行为、per-turn model 切换或跨通道 token 行为已验证；相关 Gate 继续保持失败/未验证。
