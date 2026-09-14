#!/usr/bin/env python3
"""主动联想 hook（v0.2，P24–P26）：用户每提交一条消息，请记忆服务判断要不要主动提醒。

挂在宿主的"用户提交消息"事件上（Claude Code / kimi-code 是 UserPromptSubmit，其他宿主用等价事件）：
读 stdin 的事件 JSON（取 prompt 与 cwd）→ POST 到 HTTP 常驻服务的 /surface →
返回的 <surfaced_memories> 块非空就打印到 stdout（宿主会把它追加进上下文），空则静默。

记忆副手按"精确率优先"判断：只有不提就可能出错或遗漏、或原理相通值得点明时才浮现，
所以大多数消息会得到空结果、不打扰。

环境变量（与会话开头 hook 同一套口径）：
- AGENT_MEMORY_HTTP_HOST / AGENT_MEMORY_HTTP_PORT：记忆服务地址，缺省 127.0.0.1:8765；
- AGENT_MEMORY_DATA_DIR：状态文件目录，缺省 ~/.agent-memory/data（记录每个会话最近几条消息作线索）；
- AGENT_MEMORY_SURFACE_HOOK=off：整体关闭；
- AGENT_MEMORY_SURFACE_TIMEOUT：请求超时秒数，缺省 25（副手要调两次 LLM）。
按宿主惯例 fail-open：服务不可达、超时、payload 非法都静默放行，不影响对话。
"""

import json
import os
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

from _hook_io import atomic_write_text, interprocess_lock

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_MAX_SESSIONS = 200
_RECENT = 3


def _scope(cwd: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", Path(cwd).name.lower()).strip("-")
    return f"repo:{slug}" if slug else "global"


def main() -> int:
    if os.environ.get("AGENT_MEMORY_SURFACE_HOOK", "").lower() == "off":
        return 0
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
    except (json.JSONDecodeError, ValueError):
        return 0
    raw = payload.get("prompt") or payload.get("user_prompt") or payload.get("message") or ""
    message = str(raw).strip()
    if not message:
        return 0
    cwd = str(payload.get("cwd") or os.getcwd())
    session_id = str(payload.get("session_id") or "default")
    data_dir = Path(os.environ.get("AGENT_MEMORY_DATA_DIR", "~/.agent-memory/data")).expanduser()
    state_file = data_dir / "state" / "surface_recent_turns.json"
    recent: list[str] = []
    try:
        with interprocess_lock(data_dir / "state" / "surface_hook.lock"):
            state_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = {}
            except (OSError, json.JSONDecodeError):
                state = {}
            recent = list(state.get(session_id) or [])[-_RECENT:]
            state.pop(session_id, None)
            state[session_id] = (recent + [message[:500]])[-_RECENT:]
            while len(state) > _MAX_SESSIONS:
                state.pop(next(iter(state)))
            atomic_write_text(state_file, json.dumps(state, ensure_ascii=False))
    except Exception:
        recent = []
    host = os.environ.get("AGENT_MEMORY_HTTP_HOST", "127.0.0.1")
    port = os.environ.get("AGENT_MEMORY_HTTP_PORT", "8765")
    body = json.dumps({"message": message, "scope": _scope(cwd), "recent_turns": recent,
                       "date": date.today().isoformat()}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"http://{host}:{port}/surface", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        timeout = float(os.environ.get("AGENT_MEMORY_SURFACE_TIMEOUT", "25"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
