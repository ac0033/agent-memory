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
#   3. 把 agents/coder.md 装到 <KIMI_CODE_HOME>/agents/coder.md（覆盖内置 coder
#      subagent：摘掉记忆库写类工具，只留只读；已存在则先备份 .bak）；
#   4. 把会话开头工作记忆注入 hook（UserPromptSubmit ->
#      scripts/memory_session_context_hook.py）注册进 <KIMI_CODE_HOME>/config.toml
#      （已注册则跳过；改动前自动备份 .bak）。
# 数据目录默认指向本仓库的 data/（多 agent 共享同一套记忆库）。
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
print(f"[1/4] 已写入 {mcp_json} 的 mcpServers.agent-memory")
EOF

# ---- 2. 安装 Skill（覆盖旧版） ----
rm -rf "$SKILL_DST"
cp -r "$SKILL_SRC" "$SKILL_DST"
echo "[2/4] 已安装 Skill 到 $SKILL_DST"

# ---- 3. 安装 coder subagent 覆盖文件（记忆库对 subagent 只读的硬闸） ----
AGENT_DST="$KIMI_HOME/agents/coder.md"
mkdir -p "$KIMI_HOME/agents"
if [ -f "$AGENT_DST" ]; then
  cp "$AGENT_DST" "$AGENT_DST.$(date +%Y%m%d-%H%M%S).bak"
fi
cp "$REPO_ROOT/agents/coder.md" "$AGENT_DST"
echo "[3/4] 已安装 coder subagent 覆盖文件到 $AGENT_DST（记忆写类工具已从 coder 工具面摘除）"

# ---- 4. 注册会话开头工作记忆注入 hook（幂等，写进 config.toml） ----
CONFIG_TOML="$KIMI_HOME/config.toml"
if [ -f "$CONFIG_TOML" ] && grep -q "memory_session_context_hook" "$CONFIG_TOML"; then
  echo "[4/4] 会话开头工作记忆注入 hook 已在 $CONFIG_TOML 注册，跳过"
else
  # hook command 需要 Windows 风格绝对路径（kimi-code 按本机路径执行）
  WIN_REPO_ROOT="$(cd "$REPO_ROOT" && { pwd -W 2>/dev/null || pwd; } | tr '\\' '/')"
  HOOK_PY="$PY"
  case "$HOOK_PY" in
    /*) HOOK_PY="$(cd "$(dirname "$PY")" && { pwd -W 2>/dev/null || pwd; } | tr '\\' '/')/$(basename "$PY")" ;;
  esac
  if [ -f "$CONFIG_TOML" ]; then
    cp "$CONFIG_TOML" "$CONFIG_TOML.$(date +%Y%m%d-%H%M%S).bak"
  fi
  cat >> "$CONFIG_TOML" <<EOF

# agent-memory 会话开头工作记忆注入 hook：每个 session 首条用户消息时，
# 向本地记忆服务拉取 global + repo:<当前目录> + agent:kimi-code 的工作记忆块注入上下文
#（AGENT_MEMORY_WM_HOOK=off 可整体关闭；脚本 fail-open，服务不可达静默放行）。
[[hooks]]
event = "UserPromptSubmit"
command = "$HOOK_PY $WIN_REPO_ROOT/scripts/memory_session_context_hook.py"
timeout = 5
EOF
  echo "[4/4] 已注册会话开头工作记忆注入 hook 到 $CONFIG_TOML"
fi

echo ""
echo "安装完成。接下来："
echo "  1. 在 kimi-code 里执行 /reload（或开新会话）使其生效；"
echo "  2. /mcp 里应能看到 agent-memory；coder subagent 将不再持有记忆写类工具；"
echo "  3. 每个会话的首条消息会自动注入相关工作记忆（空的不注入）；"
if [ -z "$API_KEY" ]; then
  echo "  4. 对话蒸馏有两条路：编辑 $MCP_JSON 里 agent-memory 的 env 填"
  echo "     AGENT_MEMORY_LLM_API_KEY 走服务端蒸馏；不配 key 时 agent 会按"
  echo "     SKILL.md 的宿主蒸馏流程（memory_distill_prompt -> distilled_json）自行蒸馏。"
fi
