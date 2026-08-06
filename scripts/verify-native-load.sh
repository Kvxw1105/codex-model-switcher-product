#!/usr/bin/env bash
# 验证当前安装的 codex 客户端能加载本项目生成的 native catalog。
#
# 安全边界：
#   - 使用隔离的临时 CODEX_HOME，不触碰真实 ~/.codex、auth、catalog、cookie。
#   - 只读本仓库 fixture / 临时生成物，不读取任何真实凭据。
#   - 脚本本身不含任何密钥。
#
# 用法（在仓库根目录、已有 .venv 的 bash 中）：
#   bash scripts/verify-native-load.sh
#
# 通过条件：
#   1. "catalog 加载" 模式输出恰好 1 个模型，slug 为 cms-deepseek-v4-flash，
#      且 stdout/stderr 无 error/failed。
#   2. 基线（无 model_catalog_json）输出官方 bundled 模型（数量 > 1），
#      证明 model_catalog_json 确实替换了 bundled catalog。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. 用仓库代码生成 native catalog（仅含 fixture 模型，无真实数据）
"$PYTHON" -X utf8 - "$WORK" "$PROJECT_ROOT" <<'PYEOF'
import json
import sys

sys.path.insert(0, sys.argv[2] + r"\src")
from codex_model_switcher.catalog import build_catalog, build_native_catalog, catalog_from_mapping
from codex_model_switcher.models import ModelCapability, ModelRoute

work = sys.argv[1]
route = ModelRoute(
    model_id="cms-deepseek-v4-flash",
    display_name="DeepSeek V4 Flash API",
    lane="third_party",
    provider_id="deepseek",
    upstream_model="deepseek-v4-flash",
    capability=ModelCapability(
        context_window=64_000,
        supports_responses=True,
        supports_streaming=True,
        supports_tools=True,
        supports_images=False,
        supports_files=False,
        supports_compaction_context=False,
    ),
)
catalog = build_catalog([route], client_version="0.133.0")
native = build_native_catalog(catalog_from_mapping(catalog), bundled_catalog=None)
with open(f"{work}/native-catalog.json", "w", encoding="utf-8") as handle:
    json.dump(native, handle, ensure_ascii=False, indent=2)
print("generated", native["models"][0]["slug"])
PYEOF

# 2. 构造隔离 CODEX_HOME + 受管配置（正向路径）
# TOML 基本字符串中反斜杠是转义符，路径统一转成正斜杠。
NATIVE_WIN="$(cygpath -w "$WORK/native-catalog.json" | tr '\\' '/')"
mkdir -p "$WORK/home"
cat > "$WORK/home/config.toml" <<EOF
model = "cms-deepseek-v4-flash"
model_provider = "deepseek"
model_catalog_json = "$NATIVE_WIN"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:4318/v1"
wire_api = "responses"
requires_openai_auth = false
EOF

HOME_WIN="$(cygpath -w "$WORK/home")"
echo "== catalog 加载模式 =="
timeout 30 env CODEX_HOME="$HOME_WIN" codex debug models > "$WORK/out.json" 2> "$WORK/err.txt" || {
    echo "FAIL: codex debug models 退出码非 0"; cat "$WORK/err.txt"; exit 1; }
"$PYTHON" -X utf8 - "$WORK/out.json" "$WORK/err.txt" <<'PYEOF'
import json
import sys

out, err = sys.argv[1], sys.argv[2]
with open(out, encoding="utf-8") as handle:
    data = json.load(handle)
with open(err, encoding="utf-8") as handle:
    err_text = handle.read()
models = data.get("models", [])
assert len(models) == 1, f"expected exactly 1 model, got {len(models)}"
assert models[0]["slug"] == "cms-deepseek-v4-flash", models[0]["slug"]
assert not any(word in err_text.lower() for word in ("error", "failed")), err_text
print("OK: 客户端加载了本地 native catalog（1 个模型），无 error")
PYEOF

# 3. 基线对照：无 model_catalog_json 时应回落到 bundled catalog
mkdir -p "$WORK/baseline"
cat > "$WORK/baseline/config.toml" <<EOF
model = "cms-deepseek-v4-flash"
model_provider = "deepseek"
[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:4318/v1"
wire_api = "responses"
EOF
BASELINE_WIN="$(cygpath -w "$WORK/baseline")"
echo "== 基线对照（无 model_catalog_json）=="
timeout 30 env CODEX_HOME="$BASELINE_WIN" codex debug models > "$WORK/base.json" 2>/dev/null
"$PYTHON" -X utf8 - "$WORK/base.json" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
models = data.get("models", [])
assert len(models) > 1, f"expected bundled models, got {len(models)}"
print(f"OK: 基线输出 {len(models)} 个 bundled 模型，证明 model_catalog_json 确实替换了 bundled")
PYEOF

echo "== 全部通过：当前 codex 客户端可加载本项目 native catalog =="
