# Codex 多模型热切换

这是一个 Windows Codex 桌面端的本地路由与控制中心项目，目标是让官方 ChatGPT 登录/订阅通道与第三方 API 通道在同一个 Codex 任务内按 turn 边界切换。

当前状态：本地网页控制中心可启动；DeepSeek Responses provider、Windows Credential Manager 凭据写入和手动真实探测已接通。Codex 原生 picker、真实配置 apply/restore 和 Router 启停仍未完成桌面验收。

重要边界：

- Codex/app-server 继续拥有任务历史、GUI 显示和 compact。
- 官方身份不得转发给第三方 provider。
- 第三方密钥只进入 Windows Credential Manager，不进入 Git、JSON、TOML 或日志。
- 首版只支持上一 turn 完成或取消后的切换，不承诺生成中途迁移。

## 现在怎么试

在本仓库 PowerShell 中运行：

```powershell
Set-Location 'C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat\work\codex-model-switcher-product'
\.venv\Scripts\python.exe -m codex_model_switcher gui
```

如果还没有 `.venv`，先执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

命令会只监听 `127.0.0.1` 并打印本地地址。打开该地址后：

1. 在 DeepSeek API 卡片中输入新生成的 API key，点击保存；页面只显示“已配置”，不会回显 key。
2. 点击“探测”。这会按用户的明确点击发送一条最短 Responses 请求，并只显示成功/失败和耗时。
3. 用 Ctrl+C 停止控制中心。

当前不要把“探测成功”理解成 Codex 已经可以切换：网页是控制中心，不是第二个聊天窗口；真实聊天历史和 compact 仍由 Codex 官方 GUI 管理。配置 apply、Router start/stop 和原生 picker smoke 尚未开放为成功操作，避免误改真实 `C:\Users\kvxkf\.codex` 配置。

开发入口：

- 计划：`docs/superpowers/plans/2026-08-05-codex-dual-lane-hot-switch.md`
- 当前交接：`.context/handoff/2026-08-05-codex-model-switcher.md`
- 原始解压包和审查副本在仓库外，只作为只读参考。

下一步先执行计划的 Task 0，完成干净 Git 基线、测试骨架和秘密扫描，然后再派发 Luna A/B/C。
