# 真实桌面 Smoke 验收清单（显式开关）

本清单用于在真实 Codex 桌面端手动验收官方→第三方→官方切换。执行前必须：

- 明确理解这会**修改真实 Codex 配置**（`config.toml` 与 `model_catalog_json` 指向的目录文件）。
- 全程保留本工具自动生成的原子备份与 SHA-256 证据；任何一步失败都先用“恢复原配置”回滚，
  再报告证据。
- 不读取、不打印、不提交真实 `auth.json`、`catalog.json`、cookie、Authorization 或密钥。

## 0. 前置条件（证据就绪检查）

- [ ] 已阅读 `docs/gate1-evidence-2026-08-06.md`，确认契约证据来源。
- [ ] `codex --version` 与本机安装一致（当前基线 codex-cli 0.133.0）。
- [ ] 本仓库工作区干净（`git status`），基线测试通过。
- [ ] 准备好两个不含隐私的 fixture 路径（可用 `tests/fixtures/catalogs/` 下的
  `safe-picker.json` 与 `bundled-native.json`，或按官方 schema 自建）。

## 1. 启动控制中心（显式开关）

```powershell
Set-Location '<PROJECT_ROOT>'
.\.venv\Scripts\python.exe -m codex_model_switcher gui --port 4317 --smoke
```

- 页面顶部 CODEX CONFIG 卡片应显示 smoke 提示（“开关已开启：apply/restore 会修改
  显式指定的配置并保留备份与 SHA-256 证据”）。
- 若未显示 smoke 提示，说明开关未生效，立即停止，不要继续。

## 2. 保存凭据并启动 Router（不动配置）

- 保存 DeepSeek API key（只进 Windows Credential Manager，页面不回显）。
- 点击“探测”：应显示成功/失败与耗时，不含密钥。
- 点击“启动 Router”：本机地址应显示 `http://127.0.0.1:4318/v1`。
- 记录 Router 地址（后面要填进 apply 请求）。

## 3. apply 前置 SHA-256（修改前证据）

在 apply 前先手动记录**将要修改的每个文件**的 SHA-256：

```powershell
Get-FileHash '<真实 config.toml 路径>' -Algorithm SHA256
Get-FileHash '<将作为 model_catalog_json 的目录文件路径>' -Algorithm SHA256
```

把输出贴进本清单（或单独的证据文件，不入库）。

## 4. 发送 apply 请求（只改显式指定路径）

在控制中心页面点击“应用 Codex 配置”前，确认请求 payload 显式携带：

```json
{
  "config_path": "C:\\Users\\<you>\\.codex\\config.toml",
  "catalog_path": "C:\\path\\to\\candidate-catalog.json",
  "bundled_catalog_path": "C:\\path\\to\\bundled-native.json",
  "native_catalog_path": "C:\\path\\to\\native-models.json"
}
```

> 说明：`native_catalog_path` 省略时会自动取 catalog 同名 `.native.json`。
> Router 地址固定为控制中心启动的 `http://127.0.0.1:4318/v1`。

预期响应：

```json
{
  "status": "ok",
  "configured": true,
  "smoke": true,
  "backup_path": "<自动生成的 .bak.<时间戳> 文件路径>",
  "config_path": "<config.toml 路径>",
  "original_sha256": "<64 hex>",
  "written_sha256": "<64 hex>",
  "timestamp": "<UTC 时间戳>"
}
```

验收点：

- [ ] 返回 `status=ok` 且 `smoke=true`。
- [ ] `original_sha256` 与第 3 步手动记录的 SHA-256 一致。
- [ ] `written_sha256` 不同于 `original_sha256`。
- [ ] `backup_path` 指向的备份文件存在，且其 SHA-256 等于 `original_sha256`。
- [ ] 页面 CODEX CONFIG 卡片显示“已应用”。

## 5. 桌面端观察（真实 Codex）

- [ ] 启动/聚焦 Codex 桌面应用，确认它能正常读取新配置（不报 config 解析错误）。
- [ ] 打开模型选择（picker），确认第三方模型出现在列表中且可选中。
- [ ] 在**同一 task** 内先用官方模型发一 turn，再切到第三方模型发下一 turn，
      最后切回官方模型再发一 turn（官方→第三方→官方）。
- [ ] 每 turn 只在上一个 turn 完成或取消后切换，不承诺生成中途迁移。
- [ ] 确认官方身份没有被转发给第三方 provider（第三方请求只携带第三方凭据；
      官方通道请求仍只到官方 host）。

## 6. compact / 工具 / 文件 / 重启恢复 smoke

- [ ] 触发一次 compact，确认摘要仍由官方 app-server 生成，第三方通道未生成第二份摘要。
- [ ] 在第三方 turn 中使用工具调用（如 shell/apply_patch），确认工具往返正常。
- [ ] 涉及文件附件的 turn 行为正常。
- [ ] 重启 Codex 桌面应用后，配置仍被正确加载，Router 仍在运行（或按文档重新启动）。
- [ ] 重启后恢复原配置（见第 7 步）后，Codex 行为与 apply 前一致。

## 7. restore 回滚（字节级恢复 + SHA-256 证据）

点击“恢复原配置”，预期响应：

```json
{
  "status": "ok",
  "configured": false,
  "smoke": true,
  "restored_path": "<config.toml 路径>",
  "original_sha256": "<64 hex>",
  "written_sha256": "<64 hex>"
}
```

验收点：

- [ ] 返回 `status=ok`。
- [ ] `config.toml` 文件字节与 apply 前完全一致（`original_sha256` 匹配）。
- [ ] 备份文件未被动过（哈希仍等于 apply 时的 `original_sha256`）。
- [ ] Codex 桌面端恢复正常（可继续用官方模型，picker 回到原样）。

## 8. 收尾与上报

- [ ] 所有 SHA-256 证据、backup 路径、时间戳记录到独立证据文件（不入库）。
- [ ] `git status` 干净；不提交任何真实配置、密钥、cookie 或 catalog 内容。
- [ ] 汇报时按“已编辑 / 已本地验证 / 已提交 / 已推送 / 仍未验证 / 具体阻塞证据”分类。

## 失败时的规则

1. 任何一步返回非 `ok`：**先点“恢复原配置”**，不要手动编辑 config.toml。
2. restore 也失败：使用第 4 步返回的 `backup_path` 手工恢复备份文件，
   并核对 SHA-256 与 `original_sha256` 一致后再让 Codex 读取。
3. 恢复成功后仍异常：停止使用本工具，报告证据（响应 JSON + SHA-256 + 时间戳），
   不要猜测原因或伪装成可用。
