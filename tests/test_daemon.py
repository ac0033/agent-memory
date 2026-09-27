"""server/daemon.py 与 /health 的测试：探活、端口被占、代码指纹、状态口径。

真正拉起后台进程（加载模型）的端到端检查不在单元测试里做，见 docs/usage.md 的冒烟步骤。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agent_memory import __version__
from agent_memory.config import Settings
from agent_memory.server import daemon


def _serve(handler_cls):
    httpd = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class _Foreign(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


class _Ours(BaseHTTPRequestHandler):
    fingerprint = "old"

    def do_GET(self):
        body = json.dumps(
            {"service": "agent-memory", "pid": 4242, "fingerprint": type(self).fingerprint}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _settings(tmp_path, port: int) -> Settings:
    return Settings(data_dir=tmp_path, http_port=port)


def test_probe_nobody_listening_returns_none(tmp_path):
    assert daemon.probe(_settings(tmp_path, 9)) is None
    assert daemon.status(_settings(tmp_path, 9))["state"] == "stopped"


def test_probe_foreign_process_is_deterministic_error(tmp_path):
    httpd = _serve(_Foreign)
    try:
        settings = _settings(tmp_path, httpd.server_address[1])
        with pytest.raises(daemon.DaemonError, match="不是本版本"):
            daemon.probe(settings)
        assert daemon.status(settings)["state"] == "foreign"
        with pytest.raises(daemon.DaemonError):
            daemon.ensure_running(settings)  # 端口被占时不拉起、不杀别人的进程
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_status_running_vs_stale_by_fingerprint(tmp_path):
    httpd = _serve(_Ours)
    try:
        settings = _settings(tmp_path, httpd.server_address[1])
        _Ours.fingerprint = "old"
        assert daemon.status(settings)["state"] == "stale"
        _Ours.fingerprint = daemon.code_fingerprint()
        info = daemon.status(settings)
        assert info["state"] == "running" and info["pid"] == 4242
        assert daemon.ensure_running(settings)["pid"] == 4242  # 最新的就直接用，不重启
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_fingerprint_changes_with_code(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(daemon, "_PACKAGE_DIR", pkg)
    before = daemon.code_fingerprint()
    assert before == daemon.code_fingerprint()
    (pkg / "a.py").write_text("x = 22\n", encoding="utf-8")
    assert daemon.code_fingerprint() != before


def test_health_route_reports_identity(tmp_path, fake_embedder):
    from starlette.testclient import TestClient

    from agent_memory.long_term.store.index_db import IndexDB
    from agent_memory.long_term.store.markdown_store import MarkdownStore
    from agent_memory.server.http_server import build_http_server
    from agent_memory.server.mcp_server import MemoryService

    index = IndexDB(tmp_path / "index.db")
    service = MemoryService(
        Settings(data_dir=tmp_path), MarkdownStore(tmp_path), index, fake_embedder, None
    )
    server = build_http_server(service, "127.0.0.1", 8765, fingerprint="fp", idle_minutes=30)
    app = server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1")
    try:
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            info = client.get("/health").json()
    finally:
        index.close()
    assert info["service"] == "agent-memory"
    assert info["version"] == __version__
    assert info["fingerprint"] == "fp" and info["idle_minutes"] == 30


def test_activity_tracker_counts_inflight_as_busy():
    from agent_memory.server.http_server import ActivityTracker

    tracker = ActivityTracker()
    tracker.begin()
    assert tracker.idle_seconds() == 0.0
    tracker.end()
    assert tracker.idle_seconds() >= 0.0


def test_proxy_lists_same_tools_without_loading_service():
    import anyio

    from agent_memory.server.stdio_proxy import build_proxy

    tools = anyio.run(build_proxy(Settings()).list_tools)
    names = {t.name for t in tools}
    assert len(names) == 25 and "memory_search" in names
