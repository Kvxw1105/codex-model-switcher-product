# Codex 多模型热切换

这是一个 Windows Codex 桌面端的本地路由与控制中心项目，目标是让官方 ChatGPT 登录/订阅通道与第三方 API 通道在同一个 Codex 任务内按 turn 边界切换。

当前状态：本地网页控制中心可启动；DeepSeek Responses provider、Windows Credential Manager 凭据写入、手动真实探测和本地 Router 启停已接通；真实 Codex 配置 apply/restore 仍明确停在 picker 外部验证门之前。

重要边界：

- Codex/app-server 继续拥有任务历史、GUI 显示和 compact。
- 官方身份不得转发给第三方 provider。
- 第三方密钥只进入 Windows Credential Manager，不进入 Git、JSON、TOML 或日志。
- 首版只支持上一 turn 完成或取消后的切换，不承诺生成中途迁移。

## 现在怎么试

在本仓库 PowerShell 中运行：

```powershell
Set-Location '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m codex_model_switcher gui
```

如果还没有 `.venv`，先执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

命令会只监听 `127.0.0.1` 并打印本地地址。打开该地址后：

1. 在 DeepSeek API 卡片中输入新生成的 API key，点击保存；页面只显示“已配置”，不会回显 key。
2. 点击“探测”。这会按用户的明确点击发送一条最短 Responses 请求，并只显示成功/失败和耗时。
3. 点击“启动 Router”后，本机代理地址会显示在操作结果中，默认是 `http://127.0.0.1:4318/v1`；调用时必须提供 `X-Codex-Task-Id` 和 `X-Codex-Turn-Id`。
4. 用 Ctrl+C 停止控制中心；也可以点击“停止 Router”。

### 手动验证 Router（不需要 Codex 集成）

Router 只是本地代理，不是 Codex 原生 picker 的开关。可以在命令行直接发
Responses 请求验证路由回路（在 Router 启动后、本机 PowerShell 中执行）：

```powershell
$body = @{
    model  = "<模型 ID，见控制中心目录>"
    input  = "hi"
    stream = $false
} | ConvertTo-Json
Invoke-RestMethod `
    -Method Post `
    -Uri "http://127.0.0.1:4318/v1/responses" `
    -Headers @{
        "Content-Type"     = "application/json"
        "X-Codex-Task-Id"  = "manual-task"
        "X-Codex-Turn-Id"  = "manual-turn"
    } `
    -Body $body
```

预期返回一个 JSON 对象（含 `id`/`status`）。常见错误对照：

- `404 not_found`：路径不是 `/v1/responses` 或 `/v1/chat/completions`。
- `400 invalid_json` / `400 invalid_request`：请求体不是合法 JSON 或缺少 `model` 字段。
- `400 missing_correlation`：缺少 `X-Codex-Task-Id` 或 `X-Codex-Turn-Id`。
- `409 turn_in_progress`：同一 `task+turn` 已有活动请求，等待其完成或取消。
- `503 router_not_running`：Router 已停止，回到控制中心重新“启动 Router”。
- `504 router_timeout`：上游 130 秒内未返回；检查凭据与网络后重试。

### 真实桌面验收开关（默认关闭）

“应用 Codex 配置”和“恢复原配置”默认保持 `412 blocked`，不会改动真实 Codex 配置。
只有显式以 `--smoke` 启动控制中心才允许真实 apply/restore，且每次 apply 必须显式
提供 `config_path`、`catalog_path`、`bundled_catalog_path`；apply 会自动生成原子备份、
字节级恢复和前后 SHA-256 证据。完整验收步骤见 `docs/smoke-acceptance-checklist.md`。

```powershell
.\.venv\Scripts\python.exe -m codex_model_switcher gui --port 4317 --smoke
```

当前不要把“探测成功”或“Router 启动”理解成 Codex 原生 picker 已经切换：网页是控制中心，不是第二个聊天窗口；真实聊天历史和 compact 仍由 Codex 官方 GUI 管理。Gate 1 的契约证据（官方 config.toml schema、per-turn model 覆盖、app-server turn/compact 契约）已取得，见 `docs/gate1-evidence-2026-08-06.md`；但真实桌面 smoke（picker 显示第三方模型、官方→第三方→官方切换、compact/工具/重启恢复）仍未完成，因此 apply/restore 保持阻断，不伪装成可用。

开发入口：

- 计划：`docs/superpowers/plans/2026-08-05-codex-dual-lane-hot-switch.md`
- 当前交接：`.context/handoff/2026-08-06-public-handoff.md`
- 原始解压包和审查副本在仓库外，只作为只读参考。

当前开发已完成本地 Router HTTP 适配和控制中心启停回路；下一步仍需在真实 Codex picker 上取得外部验证收据，之后才能实现并验收真实配置 apply/restore。
