"""http_server.py 测试：HTTP 常驻服务的静态路由与 MCP 握手。

不走真实网络端口：用 starlette TestClient 直接打 streamable_http_app 返回的
Starlette 应用（json_response=True，协议应答走纯 JSON 而不是 SSE）。
"""

import json

import pytest

from agent_memory.config import Settings
from agent_memory.server.http_server import build_bootstrap_text, build_http_server
from agent_memory.server.mcp_server import MemoryService
from agent_memory.store.index_db import IndexDB
from agent_memory.store.markdown_store import MarkdownStore


class FakeLLM:
    def complete(self, system: str, user: str) -> str:
        return "ok"

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        return {"memories": []}


@pytest.fixture
def http_app(tmp_path, fake_embedder):
    settings = Settings(data_dir=tmp_path)
    index = IndexDB(tmp_path / "index.db")
    service = MemoryService(settings, MarkdownStore(tmp_path), index, fake_embedder, FakeLLM())
    server = build_http_server(service, "127.0.0.1", 8765)
    app = server.streamable_http_app(json_response=True, host="127.0.0.1")
    yield app
    index.close()


@pytest.fixture
def client(http_app):
    from starlette.testclient import TestClient

    # base_url 用真实监听地址：MCP 传输层会校验 Host 头（防 DNS rebinding），
    # TestClient 默认的 testserver 会被拒绝
    with TestClient(http_app, base_url="http://127.0.0.1:8765") as c:
        yield c


def test_skill_md_served(client):
    resp = client.get("/SKILL.md")
    assert resp.status_code == 200
    assert "agent-memory" in resp.text
    assert "人工复核交互" in resp.text  # 与仓库 SKILL.md 同步


def test_bootstrap_served_with_urls(client):
    resp = client.get("/bootstrap")
    assert resp.status_code == 200
    assert "http://127.0.0.1:8765/mcp" in resp.text
    assert "http://127.0.0.1:8765/SKILL.md" in resp.text
    assert "scope" in resp.text  # 引导里含作用域纪律速览


def test_bootstrap_text_uses_given_host_port():
    text = build_bootstrap_text("192.168.1.5", 9000)
    assert "http://192.168.1.5:9000/mcp" in text


def _rpc(client, method, params=None, rpc_id=1, session_id=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    return client.post(
        "/mcp",
        content=json.dumps(
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}
        ),
        headers=headers,
    )


def _initialize(client) -> str:
    resp = _rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    )
    assert resp.status_code == 200
    session_id = resp.headers.get("mcp-session-id")
    assert session_id
    # 握手后按协议回 initialized 通知
    client.post(
        "/mcp",
        content=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "mcp-session-id": session_id,
        },
    )
    return session_id


def test_mcp_handshake_and_tool_call_over_http(client):
    """端到端：initialize → tools/list → tools/call memory_review_list。"""
    session_id = _initialize(client)

    resp = _rpc(client, "tools/list", rpc_id=2, session_id=session_id)
    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert {
        "memory_search",
        "memory_add",
        "memory_feedback",
        "memory_update",
        "memory_forget",
        "memory_review_list",
        "memory_review_resolve",
    } <= names

    resp = _rpc(
        client,
        "tools/call",
        {"name": "memory_review_list", "arguments": {}},
        rpc_id=3,
        session_id=session_id,
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["pending_review_count"] == 0


def test_mcp_search_gate_blocks_over_http(client, http_app, tmp_path, entry_factory):
    """复核门在 HTTP 传输下同样生效（队列有积压时 search 返回 blocked）。"""
    from agent_memory.ingest.review_queue import write_review_queue

    write_review_queue(
        [entry_factory(entry_id="low-x", confidence="low")], tmp_path, reason="测试"
    )
    session_id = _initialize(client)
    resp = _rpc(
        client,
        "tools/call",
        {"name": "memory_search", "arguments": {"query": "测试查询"}},
        rpc_id=2,
        session_id=session_id,
    )
    assert resp.status_code == 200
    payload = json.loads(resp.json()["result"]["content"][0]["text"])
    assert payload["status"] == "blocked"
    assert payload["pending_review_count"] == 1
