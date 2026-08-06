# Codex Model Switcher：公开交接说明

## 当前交付状态

- 当前主线提交：`c7ab39a`（控制中心操作反馈）；前一提交 `de14a6e`（本地 Router HTTP 生命周期）。
- 工作区在交接前应保持干净。
- 自动化验证：`358 passed, 1 skipped`。
- 已通过：ruff、JavaScript 语法检查、秘密模式扫描、detect-secrets、pip-audit。
- 本轮没有读取、提交或输出真实 API key、cookie、Authorization、Codex auth/config 内容。

## 已可用能力

1. 本地网页控制中心，只绑定 loopback。
2. 第三方凭据写入注入的凭据后端，页面不回显密钥。
3. DeepSeek Responses provider 的显式探测。
4. 本地 Router HTTP 服务：默认 `http://127.0.0.1:4318/v1`。
5. Responses、Chat Completions、SSE 流式入口。
6. Router 请求要求 `X-Codex-Task-Id` 和 `X-Codex-Turn-Id`，并通过稳定模型 ID 路由。
7. GUI 对保存、探测、刷新、Router 启停和配置动作显示 pending/success/error/timeout 状态。

## 已知边界

- “应用 Codex 配置”和“恢复原配置”当前必须保持阻断；它们不能伪装成成功，也不能猜测当前 Codex picker 的 schema、官方 endpoint、认证头或 compact 语义。
- Gate 1（当前 Codex 原生 picker、per-turn model、app-server 与官方认证契约）仍是 `FAIL / UNVERIFIED`。
- Router 是本地第三方 API 代理，不等于 Codex 原生 picker 已切换。
- 当前 Router 不承诺生成中途迁移、官方/第三方跨通道 token 传递或第二套聊天历史。

## 本地启动

在仓库根目录执行：

```powershell
.\.venv\Scripts\python.exe -m codex_model_switcher gui --port 4317
```

打开控制中心后，先保存凭据并按需探测，再点击“启动 Router”。旧 GUI 进程必须先退出；浏览器需要 `Ctrl+F5` 重新加载静态脚本。

## 下一位 agent 的第一步

1. 先运行 `git status --short --branch` 和全量测试。
2. 不要读取或打印真实 Codex 配置、auth、catalog、cookie 或任何密钥。
3. 只在取得当前客户端或官方文档的非敏感证据后处理 Gate 1。
4. 若 Gate 1 仍失败，保持配置 apply/restore 阻断，并完善手动 Router 使用路径；不要猜测 native picker 集成。
5. 任何真实配置 smoke 都必须显式开关、字节级备份、恢复和前后 SHA-256 证据。

## 2026-08-06 续（8f486a0 之后的推进）

- Gate 1 契约证据已取得并落盘：`docs/gate1-evidence-2026-08-06.md`（官方 config.toml
  schema、codex-cli 0.133.0 的 app-server schema 生成器、openai/codex 开源源码）。
  `docs/protocol-contract.md` 的 Gate 1 状态更新为 `CONTRACT VERIFIED / RUNTIME UNVERIFIED`。
- 真实客户端运行时收据仍未取得：picker 是否实际显示第三方模型、官方→第三方→官方切换、
  compact/工具/重启恢复的桌面 smoke 未做，config apply/restore 仍保持阻断（不伪装可用）。
- Router 手动路径已完善：503/504 区分、流式错误处理、README 手动 curl 验证与错误对照表。
- 已实现显式 smoke 开关：`gui --smoke`（默认关闭）。开启后 apply/restore 必须显式提供
  `config_path`/`catalog_path`/`bundled_catalog_path`，自动生成原子备份、字节级恢复与
  前后 SHA-256 证据。验收步骤见 `docs/smoke-acceptance-checklist.md`。
- 修复 native ModelInfo 字段名与官方契约一致（`supports_reasoning_summary_parameter`）。
- 自动化验证基线：`369 passed, 1 skipped`；ruff、node --check 通过。
