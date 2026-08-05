# Codex 多模型热切换 Codex 交接包

- 生成时间：2026-08-05
- Codex 项目：项目目录已创建，Codex Desktop 项目登记待用户完成
- 工作目录：`C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat\work\codex-model-switcher-product`
- 来源会话：`019fd105-652a-79c0-989f-6c24aa4f791e`
- 状态：进行中

## 目标

在 Windows Codex 桌面端保留官方 ChatGPT 登录/订阅通道，同时在官方模型选择器中加入第三方 API 模型；在同一个 Codex task 内按 turn 边界切换，并保证 GUI 历史、compact、长文、工具/文件输入和 Router/Codex 重启恢复不丢失。

## 已完成（已验证）

- 已审查原始 ZIP 和解压副本的代码结构、已有功能和主要缺口。
- 已确认原始解压副本不是 Git 仓库，主文件是单体 `codex_switcher.py`，原目录还存在日志、PID、备份和凭据风险。
- 已确认不能把含真实凭据风险的 `catalog.json` 带入产品仓库。
- 已制定完整开发计划，包含 6 个 Luna 的职责、文件所有权、依赖顺序、Gate、测试和桌面验收。
- 已创建本地产品目录，并准备只提交安全骨架，不复制原始 catalog、auth、日志或用户状态。

## 关键决策

- 新产品目录与原始审查目录分离。
- Codex/app-server 是任务历史和 compact 的唯一权威；本项目不创建第二套聊天历史。
- 官方 route 和第三方 route 做认证硬隔离。
- 模型切换发生在 turn 完成或取消之后。
- 先通过 Gate 1 验证当前 Codex 桌面版本的 picker、per-turn model 和官方认证契约，再进入 Router 开发。
- 低价模型承担常规实现；只有官方 endpoint、compact 语义、并发取消或跨通道安全问题才升级高价模型审查。

## 关键文件与路径

- 开发计划：`C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat\docs\superpowers\plans\2026-08-05-codex-dual-lane-hot-switch.md`
- 产品仓库：`C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat\work\codex-model-switcher-product`
- 原始审查副本：`C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat\work\codex-model-switcher-inspect-fresh\codex-model-switcher`

## 当前状态与约束

- 产品仓库目前只有 `.gitignore`、README 和本交接文件；功能实现尚未开始。
- 当前 Codex app 工具可以列出项目、在已有项目创建新 task、管理 task，但没有可调用的“创建 Codex Desktop 项目”或“把当前调用中的 task 自身移动到项目”动作。
- 当前 task 是 `projectId=null`，cwd 是 `C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat`。
- 不得修改真实 `C:\Users\kvxkf\.codex\config.toml`、`auth.json` 或现有 Codex 任务，除非最终 smoke 使用显式开关、备份和字节级恢复。
- 不得输出任何 token、cookie、Authorization 值或原 catalog 内容。

## 未解决问题

- 需要用户在 Codex Desktop UI 中把产品目录登记为本地项目，并尝试将当前 task 移入该项目。
- 需要在当前安装的 Codex 版本上重新验证原生 picker、per-turn model、官方认证上游和 compact 形状。
- 需要按计划执行 Task 0，然后派发 Luna A/B/C。

## 下一步

1. 在 Codex Desktop 中创建/登记本地项目，目录选择产品仓库：`C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat\work\codex-model-switcher-product`。
2. 若当前对话菜单提供“移动到项目 / Move to project”，将本 task 移入该项目。
3. 若没有移动入口，在项目内新建一个 Codex task，并先读取本文件和开发计划，再继续 Task 0。
4. 在项目仓库初始化 Git、测试骨架、计划副本和秘密扫描。
5. 只有 Task 0 通过后，才并行派发 Luna A/B/C。

## 新对话恢复指令

请先读取 `C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat\work\codex-model-switcher-product\.context\handoff\2026-08-05-codex-model-switcher.md` 和 `C:\Users\kvxkf\Documents\Codex\2026-08-05\new-chat\docs\superpowers\plans\2026-08-05-codex-dual-lane-hot-switch.md`，确认当前 Git 状态后从 Task 0 继续；不要重复审查原 ZIP，不要读取或打印真实凭据，不要修改真实 `.codex` 配置。

