# Codex Dual-Lane Hot Switch：公开开发计划

## 目标

在不转发官方身份、不创建第二套聊天历史的前提下，为 Codex 提供官方订阅通道与第三方 API 通道的 turn 边界路由能力。Codex/app-server 继续拥有任务、GUI 历史和 compact；本项目只负责控制面、模型目录和协议路由。

## 安全边界

- 第三方凭据只进入操作系统凭据后端，不进入 Git、JSON、TOML、日志或 HTTP 响应。
- 不读取、打印或提交真实 `auth.json`、`catalog.json`、cookie、Authorization 或用户任务数据。
- 不修改真实 Codex 配置，除非有显式 smoke 开关、原子备份、字节级恢复和 SHA-256 证据。
- 无法证明官方 picker、endpoint、认证、compact 或跨通道 token 语义时，必须 fail closed，不猜测。
- 服务只绑定 `127.0.0.1`。

## 当前已完成

- Python contracts：model route、capabilities、catalog、配置安全边界。
- Windows Credential Manager/keyring 抽象与敏感信息脱敏。
- Router：官方/第三方路由隔离、Responses/Chat 适配、真实 SSE、取消和 turn 关联约束。
- Loopback Router HTTP adapter：Responses、Chat Completions、SSE 与 correlation headers。
- 本地网页控制中心：provider、凭据、探测、Router 启停、反馈状态。
- 测试、lint、secret scan、dependency audit。

## Gate 1：必须先验证

在实现 native picker apply/restore 前，需要从当前安装客户端或官方文档取得不含隐私的证据，确认：

1. picker 可接受的 provider/catalog schema；
2. `client_version` 与模型目录版本关系；
3. 同一 thread 的下一 turn 是否可指定不同 model ID；
4. app-server 的 task/turn 边界与取消语义；
5. compact 是否完全由 app-server 管理；
6. 官方认证只到官方 host，第三方请求不会携带官方身份。

Gate 1 失败时，保持配置 apply/restore 阻断，不做 native picker 猜测实现。

## 后续工作顺序

1. Gate 1 证据审查与隔离 fixture。
2. 若 Gate 1 通过：实现受管配置 apply/restore、receipt 和原子恢复。
3. 在 fake app-server fixture 中验证同一任务的完成/取消后切换、compact 连续性、长文本、工具、文件、图片和重启恢复。
4. 完成桌面 smoke；真实配置 smoke 默认只读，修改必须显式开启并最终恢复。
5. 发布安装说明和回滚证据。

## 验证基线

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
node --check src/codex_model_switcher/static/app.js
```

当前基线：`358 passed, 1 skipped`。
