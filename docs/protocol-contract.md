# 桌面契约与 Gate 1 证据

本文件只记录本 worktree 能安全复现的契约和验证边界。fixture 使用 `example.invalid` 语义的假值；没有复制真实 catalog、认证头、token、cookie 或用户路径。

## 已实现的可证实契约

- `ModelCapability` 与 `ModelRoute` 是 frozen dataclass。
- 每个 route 必须显式提供全部能力字段；缺失能力会拒绝加载，因此不会为未知模型填充统一 context window。
- route 的 `model_id`、`provider_id` 和 `upstream_model` 必须是稳定、非空且无空白的标识；display name 必须包含 `Official` 或 `API` lane 标记。
- 目录生成从调用方提供的模型缓存读取 `client_version`，不会硬编码客户端版本。当前 fixture 只是假版本，用于证明动态传递路径。
- 配置只写入受管区块中的 `model_provider` 与 `model_catalog_json`。没有写入 upstream URL、Authorization、cookie 或凭据字段。
- apply 会创建包含写前字节和时间戳的备份，并以同目录临时文件加 `os.replace` 原子替换目标。receipt 保存写前/写后 SHA-256。
- restore 仅在当前文件仍匹配本项目最后一次写入的 hash 时执行；外部编辑会拒绝覆盖。备份 hash 也必须匹配，恢复结果按字节保留原文件。

## Gate 1：当前客户端原生 picker

状态：`FAIL / UNVERIFIED`。

本 worktree 没有安全可用的当前 Codex 客户端 schema 或 app-server 证据，且本任务禁止读取或输出真实 `catalog.json`、`auth.json`、token、cookie、Authorization 值。因此无法证明候选 `model_provider + model_catalog_json` 会被当前客户端接受，也不能把候选字段称为官方 picker schema。

代码中的 `verify_isolated_picker_contract` 只在隔离 `CODEX_HOME` 中检查：

1. `config.toml` 能被 TOML 解析；
2. provider 与候选目录匹配；
3. `model_catalog_json` 是与候选目录相同的 JSON；
4. 调用方显式提供不含隐私的当前客户端 `schema_version` 与 `client_version` 证据。

缺少第 4 项时结果明确为失败，即使本地配置语法正确也不能宣称 Gate 1 通过。测试只使用 tmp 目录和假值；没有真实客户端启动、付费模型请求或 endpoint 猜测。

## app-server、turn 边界与 compact

当前安装客户端的 app-server 契约未在本轮获得可安全审查的证据，故以下结论保持未证实：同一 thread 的下一 turn 是否可指定另一个 model ID、完成/取消后的切换边界，以及是否存在稳定且不含隐私的 thread/turn correlation 字段。本项目不虚构 thread/turn ID，也不猜测请求 endpoint、请求头或认证行为。

compact 归 app-server/thread 权威。本项目的目录和配置模块不保存聊天历史、不生成第二份摘要，也不实现第二套 compact 状态。

在获得当前客户端或官方文档的非敏感证据前，不能报告官方上游 URL、请求头、认证行为、per-turn model 切换或跨通道 token 行为已验证；相关 Gate 继续保持失败/未验证。
