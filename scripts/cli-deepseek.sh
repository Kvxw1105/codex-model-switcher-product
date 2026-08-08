#!/usr/bin/env bash
# 通过 CLI 使用 DeepSeek 通道（不修改真实 ~/.codex 配置）。
#
# 背景：桌面端 Codex 26.730 的 model_catalog_json 会被在线模型列表覆盖
# （见 docs/gate1-evidence-2026-08-06.md 第 5b 节与 protocol-contract.md），
# 因此真实桌面端无法同屏显示第三方模型。本脚本提供一个隔离的
# CODEX_HOME + 完整 config，让 `codex exec` 直接走本地 Router 到第三方。
#
# 前置：
#   1. DeepSeek key 已写入系统凭据后端（本仓库 GUI 保存过一次即可）。
#   2. 本地 Router 在 127.0.0.1:4318 运行（控制中心“启动 Router”）。
#
# 用法：
#   bash scripts/cli-deepseek.sh "你的提示词"
#
# 可选：--model 指定模型（默认 cms-deepseek-v4-flash）。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
CODEX_BIN="${CODEX_BIN:-codex}"

MODEL="cms-deepseek-v4-flash"
PROMPT=""
if [[ "${1:-}" == "--model" ]]; then
  MODEL="$2"
  PROMPT="${3:-}"
else
  PROMPT="${1:-}"
fi
if [[ -z "$PROMPT" ]]; then
  echo "用法: bash scripts/cli-deepseek.sh [--model <id>] \"提示词\"" >&2
  exit 2
fi

WORK="$(mktemp -d)"
cleanup() {
  # 插件克隆可能短暂占用文件；先延迟再删，失败也不阻断。
  sleep 1
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

# 复用已生成的 native catalog（含官方 + DeepSeek），复制到隔离 HOME
NATIVE="$(cygpath -w "$PROJECT_ROOT" | sed 's#\\#/#g')"
if [[ ! -f "$USERPROFILE/.codex/model-catalogs/native-models.json" ]]; then
  echo "缺少 native-models.json：请先通过控制中心生成目录" >&2
  exit 3
fi
mkdir -p "$WORK/model-catalogs"
cp "$USERPROFILE/.codex/model-catalogs/native-models.json" "$WORK/model-catalogs/"

NATIVE_WIN="$(cygpath -w "$WORK/model-catalogs/native-models.json" | tr '\\' '/')"
cat > "$WORK/config.toml" <<EOF
model = "$MODEL"
model_provider = "deepseek"
model_catalog_json = "$NATIVE_WIN"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:4318/v1"
wire_api = "responses"
requires_openai_auth = false
EOF

HOME_WIN="$(cygpath -w "$WORK")"
echo "== codex exec（隔离 CODEX_HOME，不触碰真实配置）=="
printf '%s\n' "$PROMPT" | timeout 90 env CODEX_HOME="$HOME_WIN" \
  "$CODEX_BIN" exec --skip-git-repo-check -m "$MODEL"
