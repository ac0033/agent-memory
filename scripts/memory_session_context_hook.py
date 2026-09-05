#!/usr/bin/env python3
"""会话开头自动注入工作记忆的 hook（M9）。

挂在宿主的"首条用户消息"事件上（kimi-code 是 UserPromptSubmit，其他宿主用
等价事件）：每个会话只在第一条用户消息时触发一次，向 HTTP 常驻服务拉取
global + repo:<当前目录名> + agent:<宿主名> 三个 scope 的工作记忆渲染块，
非空则打印到 stdout（宿主会把它追加进上下文），空则静默。

脚本本体宿主中立：只做"读 stdin JSON → 请求 HTTP → 非空写 stdout、退出 0"。
宿主差异全部在注册方式与环境变量上：
- AGENT_MEMORY_AGENT_NAME：agent:<名字> scope 的宿主名，缺省 kimi-code；
- AGENT_MEMORY_HTTP_HOST / AGENT_MEMORY_HTTP_PORT：记忆服务地址，缺省
  127.0.0.1:8765；
- AGENT_MEMORY_DATA_DIR：状态文件目录，缺省 ~/.agent-memory/data；
- AGENT_MEMORY_WM_HOOK=off：整体关闭本 hook；
- AGENT_MEMORY_HOOK_DEBUG=1：把每次事件的 payload 落盘 logs/hook_debug_wm.jsonl。

每个会话只注入一次：状态文件 <data_dir>/state/wm_injected_sessions.json 记录
已注入的 session_id（保留最近 500 个）。hook 按宿主惯例 fail-open：任何异常
（服务不可达、payload 非法、状态文件损坏）都静默放行，不影响对话。
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from _hook_io import atomic_write_text, interprocess_lock

# Windows 上 Python 的 stdout/stderr 被管道捕获时默认用系统区域编码（中文系统
# 为 GBK），而宿主按 UTF-8 读取 hook 输出——不重配的话中文记忆块会变乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_TIMEOUT_SECONDS = 3
_MAX_TRACKED_SESSIONS = 500


def _slugify(name: str) -> str:
    """目录名 -> scope slug：小写、非法字符段折叠为单个连字符（与服务端归一化同口径）。"""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _derive_scopes(cwd: str, agent_name: str) -> list[str]:
    """global（总是）+ repo:<当前目录名>（能推出合法 slug 时）+ agent:<宿主名>。"""
    scopes = ["global"]
    slug = _slugify(Path(cwd).name)
    if slug:
        scopes.append(f"repo:{slug}")
    agent_slug = _slugify(agent_name)
    if agent_slug:
        scopes.append(f"agent:{agent_slug}")
    return scopes


def _load_injected(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}  # 状态损坏按未注入处理（fail-open：最坏是多注入一次，无害）


def main() -> int:
    if os.environ.get("AGENT_MEMORY_WM_HOOK", "").lower() == "off":
        return 0

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except (json.JSONDecodeError, ValueError):
        payload = {}  # 无法解析事件数据：按缺省值继续（字段都有回落）

    data_dir = Path(
        os.environ.get("AGENT_MEMORY_DATA_DIR", "~/.agent-memory/data")
    ).expanduser()

    # 调试开关（fail-open）：把每次事件的 payload 落盘，供实测分析
    if os.environ.get("AGENT_MEMORY_HOOK_DEBUG") == "1":
        try:
            debug_dir = data_dir / "logs"
            debug_dir.mkdir(parents=True, exist_ok=True)
            record = {"ts": datetime.now(UTC).isoformat(), "payload": payload}
            with (debug_dir / "hook_debug_wm.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    session_id = str(payload.get("session_id") or "default")
    cwd = str(payload.get("cwd") or os.getcwd())
    agent_name = os.environ.get("AGENT_MEMORY_AGENT_NAME", "kimi-code")

    # 每个 session 只注入一次。锁覆盖“检查→请求→提交”，避免两个宿主进程
    # 同时判定未注入；只有 HTTP 成功后才写状态，临时故障可在下一次重试。
    state_file = data_dir / "state" / "wm_injected_sessions.json"
    try:
        with interprocess_lock(data_dir / "state" / "wm_hook.lock"):
            state_file.parent.mkdir(parents=True, exist_ok=True)
            injected = _load_injected(state_file)
            if session_id in injected:
                return 0
            host = os.environ.get("AGENT_MEMORY_HTTP_HOST", "127.0.0.1")
            port = os.environ.get("AGENT_MEMORY_HTTP_PORT", "8765")
            scopes = _derive_scopes(cwd, agent_name)
            url = f"http://{host}:{port}/wm_blocks?" + urllib.parse.urlencode(
                {"scopes": ",".join(scopes)}
            )
            with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as resp:
                text = resp.read().decode("utf-8", errors="replace").strip()
            injected[session_id] = True
            while len(injected) > _MAX_TRACKED_SESSIONS:
                injected.pop(next(iter(injected)))
            atomic_write_text(state_file, json.dumps(injected, ensure_ascii=False))
    except Exception:
        return 0  # 服务不可达/路由错误：静默放行（fail-open）

    if text:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # 兜底 fail-open：任何未预期异常都不影响对话
