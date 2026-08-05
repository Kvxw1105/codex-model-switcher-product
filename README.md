# Codex 多模型热切换

这是一个 Windows Codex 桌面端的本地路由与控制中心项目，目标是让官方 ChatGPT 登录/订阅通道与第三方 API 通道在同一个 Codex 任务内按 turn 边界切换。

当前状态：项目骨架已创建，功能实现尚未开始。

重要边界：

- Codex/app-server 继续拥有任务历史、GUI 显示和 compact。
- 官方身份不得转发给第三方 provider。
- 第三方密钥只进入 Windows Credential Manager，不进入 Git、JSON、TOML 或日志。
- 首版只支持上一 turn 完成或取消后的切换，不承诺生成中途迁移。

开发入口：

- 计划：`docs/superpowers/plans/2026-08-05-codex-dual-lane-hot-switch.md`
- 当前交接：`.context/handoff/2026-08-05-codex-model-switcher.md`
- 原始解压包和审查副本在仓库外，只作为只读参考。

下一步先执行计划的 Task 0，完成干净 Git 基线、测试骨架和秘密扫描，然后再派发 Luna A/B/C。
