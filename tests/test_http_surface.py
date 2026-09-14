"""v0.2 /surface 路由：宿主"用户提交消息"hook 用它拉主动浮现块（免 MCP 握手）。"""

import pytest

from agent_memory.config import Settings
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.server.http_server import build_http_server
from agent_memory.server.mcp_server import MemoryService
from tests.conftest import make_entry


class _CopilotLLM:
    def complete(self, system, user):
        return "ok"

    def complete_json(self, system, user, schema_description):
        if "记忆副手" in system:
            return {
                "surface": [{"id": "dev-os", "relation": "direct", "why": "脚本要在 Windows 上跑"}]
            }
        return {"entities": [], "constraints": [], "principles": []}


@pytest.fixture
def surface_client(tmp_path, fake_embedder):
    from starlette.testclient import TestClient

    index = IndexDB(tmp_path / "index.db")
    service = MemoryService(
        Settings(data_dir=tmp_path), MarkdownStore(tmp_path), index, fake_embedder, _CopilotLLM()
    )
    server = build_http_server(service, "127.0.0.1", 8765)
    app = server.streamable_http_app(json_response=True, host="127.0.0.1")
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield service, c
    index.close()
    service.raw_index.close()


def test_surface_route_returns_block(surface_client):
    service, client = surface_client
    service.writer.create(make_entry("dev-os", "用户的开发机是 Windows。"))
    resp = client.post("/surface", json={"message": "帮我写个 Windows 部署脚本", "scope": "global"})
    assert resp.status_code == 200
    assert "<surfaced_memories>" in resp.text and "脚本要在 Windows 上跑" in resp.text


def test_surface_route_rejects_bad_input(surface_client):
    _, client = surface_client
    assert client.post("/surface", json={"scope": "global"}).status_code == 400
    assert client.post("/surface", json={"message": "x", "scope": "bad scope!"}).status_code == 400
    assert client.post("/surface", content=b"not json").status_code == 400
