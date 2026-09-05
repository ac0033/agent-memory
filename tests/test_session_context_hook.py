"""scripts/memory_session_context_hook.py 测试：会话开头注入工作记忆。

hook 是独立脚本，用子进程 + stdin JSON 端到端验证：
- 每个 session 只在首条用户消息注入一次（stdout 输出工作记忆块）；
- 服务不可达 / 关闭开关 / 非法 payload 都静默放行（fail-open）。

HTTP 服务用 stdlib http.server 在本进程起假服务，记录请求 query 供断言。
"""

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOK = Path(__file__).parent.parent / "scripts" / "memory_session_context_hook.py"

SERVED_TEXT = "### scope: repo:demo\n\n## 工作记忆（当前任务状态）\n测试内容"


class _Handler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_GET(self):
        type(self).requests.append(self.path)
        if self.path.startswith("/wm_blocks"):
            body = SERVED_TEXT.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # 静默，不污染测试输出
        pass


class FakeServer:
    def __init__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        _Handler.requests = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def requests(self) -> list[str]:
        return list(_Handler.requests)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _run_hook(
    data_dir: Path,
    session_id: str = "s1",
    cwd: str = "D:/proj/demo",
    port: int | None = None,
    extra_env: dict[str, str] | None = None,
    payload: dict | None = None,
):
    # 复制完整环境再覆盖：Windows 上 Python 做网络请求依赖 SystemRoot 等系统变量，
    # 只传 PATH="" 会让 urlopen 静默失败（hook fail-open 吃掉异常，看不出原因）
    env = dict(os.environ)
    env["AGENT_MEMORY_DATA_DIR"] = str(data_dir)
    env.pop("AGENT_MEMORY_WM_HOOK", None)
    env.pop("AGENT_MEMORY_AGENT_NAME", None)
    env.pop("AGENT_MEMORY_HOOK_DEBUG", None)
    if port is not None:
        env["AGENT_MEMORY_HTTP_PORT"] = str(port)
    env.update(extra_env or {})
    body = payload if payload is not None else {"session_id": session_id, "cwd": cwd}
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_first_prompt_injects_once_per_session(tmp_path):
    server = FakeServer()
    try:
        first = _run_hook(tmp_path, port=server.port)
        assert first.returncode == 0
        assert SERVED_TEXT in first.stdout
        # 同 session 第二次不再注入，也不再请求服务
        n_requests = len(server.requests)
        second = _run_hook(tmp_path, port=server.port)
        assert second.returncode == 0
        assert second.stdout == ""
        assert len(server.requests) == n_requests
        # 新 session 重新注入
        third = _run_hook(tmp_path, session_id="s2", port=server.port)
        assert SERVED_TEXT in third.stdout
    finally:
        server.close()


def test_scopes_derived_from_cwd_and_agent_name(tmp_path):
    server = FakeServer()
    try:
        _run_hook(
            tmp_path,
            cwd="D:/proj/My_Proj",
            port=server.port,
            extra_env={"AGENT_MEMORY_AGENT_NAME": "claude-code"},
        )
        assert server.requests
        from urllib.parse import unquote_plus

        query = unquote_plus(server.requests[0])
        assert "global" in query
        assert "repo:my-proj" in query  # 目录名归一化为 kebab-case
        assert "agent:claude-code" in query
    finally:
        server.close()


def test_default_agent_name_is_kimi_code(tmp_path):
    server = FakeServer()
    try:
        _run_hook(tmp_path, port=server.port)
        from urllib.parse import unquote_plus

        assert "agent:kimi-code" in unquote_plus(server.requests[0])
    finally:
        server.close()


def test_server_unreachable_silent_pass(tmp_path):
    # 端口 9（discard 协议，通常无服务）：连接失败也静默放行
    result = _run_hook(tmp_path, port=9)
    assert result.returncode == 0
    assert result.stdout == ""


def test_failed_request_does_not_mark_session_injected(tmp_path):
    assert _run_hook(tmp_path, session_id="retry", port=9).stdout == ""
    server = FakeServer()
    try:
        retried = _run_hook(tmp_path, session_id="retry", port=server.port)
        assert SERVED_TEXT in retried.stdout
    finally:
        server.close()


def test_concurrent_same_session_is_recorded_once(tmp_path):
    server = FakeServer()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    lambda _i: _run_hook(tmp_path, session_id="shared", port=server.port),
                    range(4),
                )
            )
        assert sum(SERVED_TEXT in result.stdout for result in results) == 1
        assert len(server.requests) == 1
    finally:
        server.close()


def test_hook_disabled_by_env(tmp_path):
    server = FakeServer()
    try:
        result = _run_hook(
            tmp_path, port=server.port, extra_env={"AGENT_MEMORY_WM_HOOK": "off"}
        )
        assert result.returncode == 0
        assert result.stdout == ""
        assert server.requests == []  # 关闭时根本不请求
    finally:
        server.close()


def test_empty_payload_uses_defaults(tmp_path):
    server = FakeServer()
    try:
        result = _run_hook(tmp_path, port=server.port, payload={})
        assert result.returncode == 0
        assert SERVED_TEXT in result.stdout  # session_id/cwd 缺省也能跑
    finally:
        server.close()
