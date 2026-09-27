"""agent_memory/hooks.py 与 `agent-memory hook <名字>` 的测试。

函数级测试直接传 env 字典（不读配置文件）；另有几条经 CLI 子进程的端到端测试，
验证 stdin/stdout/退出码约定与 fail-open。
"""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_memory import hooks
from agent_memory.working.models import WorkingMemory
from agent_memory.working.store import WorkingMemoryStore


def _env(tmp_path, **extra) -> dict[str, str]:
    return {"AGENT_MEMORY_DATA_DIR": str(tmp_path), **extra}


def _write_wm(tmp_path, scope: str, goal: str) -> None:
    WorkingMemoryStore(tmp_path).write(WorkingMemory(scope=scope, goal=goal))


def _cli_hook(tmp_path, args: list[str], stdin: str, **extra) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("AGENT_MEMORY_"):
            env.pop(key)
    env["AGENT_MEMORY_CONFIG"] = str(tmp_path / "no-config.env")  # 不读真实用户配置
    env.update(_env(tmp_path, **extra))
    return subprocess.run(
        [sys.executable, "-c", "from agent_memory.cli import app; app()", "hook", *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


# ---------------------------------------------------------------- turn


def _turn(tmp_path, session_id="s1", **extra):
    payload = {"hook_event_name": "Stop", "session_id": session_id}
    return hooks.turn(payload, _env(tmp_path, **extra))


def _count(tmp_path, session_id="s1") -> int:
    path = tmp_path / "state" / "turn_counter.json"
    if not path.exists():
        return 0
    return json.loads(path.read_text(encoding="utf-8")).get(session_id, 0)


def test_turn_counts_then_blocks_and_resets(tmp_path):
    assert _turn(tmp_path).exit_code == 0
    assert _turn(tmp_path).exit_code == 0
    assert _count(tmp_path) == 2
    third = _turn(tmp_path)
    assert third.exit_code == 2
    assert "强制记忆更新" in third.stderr and "memory_add" in third.stderr
    assert "确认" in third.stderr  # 指令含用户确认资格规则
    assert _count(tmp_path) == 0
    assert _turn(tmp_path).exit_code == 0
    assert _count(tmp_path) == 1


def test_turn_sessions_count_independently(tmp_path):
    _turn(tmp_path, session_id="a")
    _turn(tmp_path, session_id="a")
    assert _turn(tmp_path, session_id="b").exit_code == 0
    assert _turn(tmp_path, session_id="a").exit_code == 2


def test_turn_interval_from_settings(tmp_path):
    assert _turn(tmp_path, AGENT_MEMORY_REVIEW_TURN_INTERVAL="1").exit_code == 2


def test_turn_skips_when_already_continuing_from_block(tmp_path):
    payload = {"session_id": "s1", "stop_hook_active": True}
    assert hooks.turn(payload, _env(tmp_path)).exit_code == 0
    assert _count(tmp_path) == 0  # 被拦下后继续的那一轮不计数


def test_turn_corrupt_counter_starts_over(tmp_path):
    counter = tmp_path / "state" / "turn_counter.json"
    counter.parent.mkdir(parents=True)
    counter.write_text("{broken", encoding="utf-8")
    assert _turn(tmp_path).exit_code == 0
    assert _count(tmp_path) == 1


def test_turn_debug_log(tmp_path):
    _turn(tmp_path, session_id="dbg", AGENT_MEMORY_HOOK_DEBUG="1")
    record = json.loads((tmp_path / "logs" / "hook_debug_turn.jsonl").read_text(encoding="utf-8"))
    assert record["payload"]["session_id"] == "dbg"
    assert not (tmp_path / "logs" / "hook_debug_wm.jsonl").exists()


def test_cli_turn_exit_codes_and_invalid_stdin(tmp_path):
    payload = json.dumps({"session_id": "cli"})
    blocked = _cli_hook(tmp_path, ["turn"], payload, AGENT_MEMORY_REVIEW_TURN_INTERVAL="1")
    assert blocked.returncode == 2
    assert "强制记忆更新" in blocked.stderr
    assert _cli_hook(tmp_path, ["turn"], "not json").returncode == 0


def test_cli_hook_fails_open_on_invalid_config(tmp_path):
    bad = _cli_hook(tmp_path, ["turn"], "{}", AGENT_MEMORY_REVIEW_TURN_INTERVAL="zero")
    assert bad.returncode == 0 and bad.stdout == ""


# ---------------------------------------------------------------- wm-inject


def test_wm_inject_renders_nonempty_scopes(tmp_path):
    _write_wm(tmp_path, "repo:my-proj", "项目目标")
    _write_wm(tmp_path, "agent:claude-code", "宿主目标")
    out = hooks.wm_inject({"cwd": "D:/work/My_Proj"}, _env(tmp_path), agent="claude-code").stdout
    assert "### scope: repo:my-proj" in out and "项目目标" in out
    assert "### scope: agent:claude-code" in out and "宿主目标" in out
    assert "scope: global" not in out  # 空的 scope 不注入
    assert out.index("repo:my-proj") < out.index("agent:claude-code")


def test_wm_inject_agent_scope_only_when_named(tmp_path):
    assert hooks.derive_scopes("D:/work/demo", None) == ["global", "repo:demo"]
    assert hooks.derive_scopes("D:/work/demo", "Claude Code")[-1] == "agent:claude-code"
    env = _env(tmp_path, AGENT_MEMORY_AGENT_NAME="host-a")
    _write_wm(tmp_path, "agent:host-a", "由环境变量指定")
    assert "由环境变量指定" in hooks.wm_inject({"cwd": "D:/x/demo"}, env).stdout


def test_wm_inject_every_time_without_once(tmp_path):
    _write_wm(tmp_path, "global", "全局目标")
    payload = {"session_id": "s1", "cwd": "D:/x/demo"}
    assert "全局目标" in hooks.wm_inject(payload, _env(tmp_path)).stdout
    assert "全局目标" in hooks.wm_inject(payload, _env(tmp_path)).stdout


def test_wm_inject_once_per_session(tmp_path):
    _write_wm(tmp_path, "global", "全局目标")
    payload = {"session_id": "s1", "cwd": "D:/x/demo"}
    assert "全局目标" in hooks.wm_inject(payload, _env(tmp_path), once=True).stdout
    assert hooks.wm_inject(payload, _env(tmp_path), once=True).stdout == ""
    other = {"session_id": "s2", "cwd": "D:/x/demo"}
    assert "全局目标" in hooks.wm_inject(other, _env(tmp_path), once=True).stdout


def test_wm_inject_once_concurrent_same_session(tmp_path):
    _write_wm(tmp_path, "global", "全局目标")
    payload = {"session_id": "shared", "cwd": "D:/x/demo"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        outs = list(
            pool.map(lambda _i: hooks.wm_inject(payload, _env(tmp_path), once=True), range(4))
        )
    assert sum("全局目标" in o.stdout for o in outs) == 1


def test_wm_inject_disabled(tmp_path):
    _write_wm(tmp_path, "global", "全局目标")
    assert hooks.wm_inject({}, _env(tmp_path, AGENT_MEMORY_WM_HOOK="off")).stdout == ""


def test_cli_wm_inject_prints_blocks(tmp_path):
    _write_wm(tmp_path, "global", "全局目标")
    result = _cli_hook(tmp_path, ["wm-inject"], json.dumps({"cwd": str(tmp_path)}))
    assert result.returncode == 0
    assert "全局目标" in result.stdout


# ---------------------------------------------------------------- surface


class _SurfaceHandler(BaseHTTPRequestHandler):
    bodies: list[dict] = []

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            info = {"service": "agent-memory", "pid": 1, "fingerprint": "x"}
            self._send(200, json.dumps(info).encode(), "application/json")
        else:
            self._send(404, b"", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        type(self).bodies.append(json.loads(self.rfile.read(length)))
        self._send(200, "<surfaced_memories>提醒</surfaced_memories>".encode(), "text/plain")

    def log_message(self, *args):
        pass


@pytest.fixture
def surface_server():
    httpd = HTTPServer(("127.0.0.1", 0), _SurfaceHandler)
    _SurfaceHandler.bodies = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_surface_posts_message_scope_and_recent_turns(tmp_path, surface_server):
    env = _env(tmp_path, AGENT_MEMORY_HTTP_PORT=str(surface_server))
    first = hooks.surface({"prompt": "第一句", "session_id": "s", "cwd": "D:/x/Demo"}, env)
    assert "提醒" in first.stdout
    hooks.surface({"prompt": "第二句", "session_id": "s", "cwd": "D:/x/Demo"}, env)
    last = _SurfaceHandler.bodies[-1]
    assert last["message"] == "第二句"
    assert last["scope"] == "repo:demo"
    assert last["recent_turns"] == ["第一句"]


def test_surface_starts_daemon_when_absent(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(hooks, "_start_daemon_in_background", lambda s: started.append(s))
    env = _env(tmp_path, AGENT_MEMORY_HTTP_PORT="9")  # 没人监听
    result = hooks.surface({"prompt": "你好"}, env)
    assert result.stdout == "" and result.exit_code == 0
    assert len(started) == 1


def test_surface_disabled_or_empty_message(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "_start_daemon_in_background", lambda s: pytest.fail("不应拉起"))
    off = _env(tmp_path, AGENT_MEMORY_SURFACE_HOOK="off")
    assert hooks.surface({"prompt": "x"}, off).stdout == ""
    assert hooks.surface({"prompt": "  "}, _env(tmp_path)).stdout == ""


def test_cli_reads_utf8_payload_regardless_of_locale(tmp_path):
    # 宿主发来的事件 JSON 是 UTF-8 字节，路径里常有中文；不能按系统区域编码解码
    payload = json.dumps({"session_id": "中文会话", "transcript_path": "D:/work/中文目录/x.jsonl"},
                         ensure_ascii=False).encode("utf-8")
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("AGENT_MEMORY_"):
            env.pop(key)
    env.update(AGENT_MEMORY_CONFIG=str(tmp_path / "none.env"), AGENT_MEMORY_DATA_DIR=str(tmp_path),
               AGENT_MEMORY_REVIEW_TURN_INTERVAL="1", PYTHONIOENCODING="gbk")
    result = subprocess.run(
        [sys.executable, "-c", "from agent_memory.cli import app; app()", "hook", "turn"],
        input=payload, capture_output=True, env=env,
    )
    assert result.returncode == 2
    counters = json.loads((tmp_path / "state" / "turn_counter.json").read_text(encoding="utf-8"))
    assert counters == {"中文会话": 0}
