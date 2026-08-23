#!/usr/bin/env bash
# agent-memory 一键安装到 kimi-code：注册 MCP server + 安装 Skill。
#
# 用法（在仓库根目录执行）：
#   bash scripts/install_kimi_code.sh                     # 安装（蒸馏功能待补 LLM key）
#   AGENT_MEMORY_LLM_API_KEY=sk-xxx bash scripts/install_kimi_code.sh   # 顺带配好蒸馏模型 key
#
# 做的事：
#   1. 把 agent-memory 合并进 <KIMI_CODE_HOME>/mcp.json（已存在则更新该项，
#      其他 server 不动；改动前自动备份 .bak）；
#   2. 把 skills/agent-memory 复制到 <KIMI_CODE_HOME>/skills/agent-memory（覆盖旧版）；
#   3. 数据目录默认指向本仓库的 data/（多 agent 共享同一套记忆库）。
#
# 装完后：在 kimi-code 里 /reload（或开新会话），/mcp 里应能看到 agent-memory。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIMI_HOME="${KIMI_CODE_HOME:-$HOME/.kimi-code}"
MCP_JSON="$KIMI_HOME/mcp.json"
SKILL_SRC="$REPO_ROOT/skills/agent-memory"
SKILL_DST="$KIMI_HOME/skills/agent-memory"
API_KEY="${AGENT_MEMORY_LLM_API_KEY:-}"

# 找 python：优先本仓库 venv，其次 PATH 里的 python/python3
if [ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]; then
  PY="$REPO_ROOT/.venv/Scripts/python.exe"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PY="$REPO_ROOT/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  PY="python3"
fi

mkdir -p "$KIMI_HOME" "$KIMI_HOME/skills"

# ---- 1. 合并 mcp.json（保留既有 server，改动前备份） ----
if [ -f "$MCP_JSON" ]; then
  cp "$MCP_JSON" "$MCP_JSON.$(date +%Y%m%d-%H%M%S).bak"
fi

REPO_ROOT="$REPO_ROOT" MCP_JSON="$MCP_JSON" API_KEY="$API_KEY" "$PY" - <<'EOF'
import json, os
from pathlib import Path

mcp_json = Path(os.environ["MCP_JSON"])
repo = os.environ["REPO_ROOT"].replace("\\", "/")
api_key = os.environ["API_KEY"]

config = {"mcpServers": {}}
if mcp_json.exists():
    config = json.loads(mcp_json.read_text(encoding="utf-8"))
    config.setdefault("mcpServers", {})

entry = {
    "command": "uv",
    "args": ["run", "--project", repo, "python", "-m", "agent_memory.server.mcp_server"],
    "env": {"AGENT_MEMORY_DATA_DIR": f"{repo}/data"},
}
if api_key:
    entry["env"]["AGENT_MEMORY_LLM_API_KEY"] = api_key

config["mcpServers"]["agent-memory"] = entry
mcp_json.write_text(
    json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(f"[1/2] 已写入 {mcp_json} 的 mcpServers.agent-memory")
EOF

# ---- 2. 安装 Skill（覆盖旧版） ----
rm -rf "$SKILL_DST"
cp -r "$SKILL_SRC" "$SKILL_DST"
echo "[2/2] 已安装 Skill 到 $SKILL_DST"

echo ""
echo "安装完成。接下来："
echo "  1. 在 kimi-code 里执行 /reload（或开新会话）使其生效；"
echo "  2. /mcp 里应能看到 agent-memory；"
if [ -z "$API_KEY" ]; then
  echo "  3. 对话蒸馏功能需要 LLM key：编辑 $MCP_JSON 里 agent-memory 的 env，"
  echo "     填入 AGENT_MEMORY_LLM_API_KEY（或带 AGENT_MEMORY_LLM_API_KEY=sk-xxx 重跑本脚本）。"
fi
