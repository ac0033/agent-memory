"""CLI 冒烟测试（typer CliRunner，monkeypatch 掉真 embedder）。"""

import pytest
from typer.testing import CliRunner

from agent_memory import cli
from agent_memory.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch, fake_embedder):
    """每个用例独立 data_dir + 假 embedder，不碰真实模型和用户目录。"""
    monkeypatch.setenv("AGENT_MEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "_get_embedder", lambda: fake_embedder)
    return tmp_path


def _add(**overrides):
    args = [
        "add",
        "--id", overrides.get("id", "m1"),
        "--content", overrides.get("content", "用户用 uv 管理环境"),
        "--type", overrides.get("type", "semantic"),
        "--scope", overrides.get("scope", "global"),
        "--confidence", overrides.get("confidence", "high"),
    ]
    return runner.invoke(app, args)


class TestSmoke:
    def test_add_search_list_stats_forget(self):
        r = _add()
        assert r.exit_code == 0, r.output
        assert "已入库：m1" in r.output

        r = runner.invoke(app, ["search", "uv", "--k", "5"])
        assert r.exit_code == 0, r.output
        assert "m1" in r.output

        r = runner.invoke(app, ["list"])
        assert r.exit_code == 0 and "m1" in r.output

        r = runner.invoke(app, ["stats"])
        assert r.exit_code == 0 and "记忆条目：1" in r.output

        r = runner.invoke(app, ["forget", "m1"])
        assert r.exit_code == 0 and "已删除" in r.output
        r = runner.invoke(app, ["list"])
        assert "m1" not in r.output

    def test_update_bumps_version(self):
        assert _add().exit_code == 0
        r = runner.invoke(app, ["update", "m1", "--confidence", "medium"])
        assert r.exit_code == 0 and "version=2" in r.output

    def test_add_warns_on_secrets_and_redacts(self):
        r = _add(content="我的 key 是 sk-projAbCdEfGh1234567890，别泄露")
        assert r.exit_code == 0
        assert "警告" in r.stderr and "sk_key" in r.stderr
        r = runner.invoke(app, ["list"])
        assert "sk-projAbCdEfGh1234567890" not in r.output
        assert "[REDACTED:sk_key]" in r.output

    def test_add_duplicate_fails(self):
        assert _add().exit_code == 0
        r = _add()
        assert r.exit_code == 1
        assert "已存在" in r.stderr

    def test_add_invalid_scope_fails(self):
        r = _add(scope="not a scope")
        assert r.exit_code == 1

    def test_forget_missing_fails(self):
        r = runner.invoke(app, ["forget", "ghost"])
        assert r.exit_code == 1

    def test_rebuild(self):
        assert _add(id="a", content="本项目使用 uv 管理 Python 环境").exit_code == 0
        assert _add(id="b", content="本项目开发端口固定为 8765").exit_code == 0
        r = runner.invoke(app, ["rebuild"])
        assert r.exit_code == 0 and "2 条记忆" in r.output

    def test_add_with_detail(self, tmp_path):
        """--detail 写入 Enhanced-Notes 段落，落库后可读回。"""
        from agent_memory.long_term.store.markdown_store import MarkdownStore

        r = runner.invoke(
            app,
            [
                "add", "--id", "m1",
                "--content", "用户用 uv 管理环境",
                "--detail", "用户在多次会话中明确要求用 uv 而不是 pip 管理环境。",
            ],
        )
        assert r.exit_code == 0, r.output
        got = MarkdownStore(tmp_path).get("m1")
        assert got.detail == "用户在多次会话中明确要求用 uv 而不是 pip 管理环境。"

    def test_evolve_dry_run(self, monkeypatch):
        """evolve --dry-run：产出提案、打印摘要，不改记忆层（fake LLM，不打真实 API）。"""
        from datetime import datetime

        import agent_memory.long_term.evolve.cycle as cycle_mod
        from agent_memory.long_term.evolve.cycle import CycleReport
        from agent_memory.models import EvolutionProposal

        class _FakeLLM:
            pass

        proposal = EvolutionProposal(
            id="evolve-20260820-ab12cd", changes=[],
            falsifiable_contract="契约", created_by="test",
            created_at=datetime(2026, 8, 20),
        )
        monkeypatch.setattr(
            cli.OpenAILLMClient, "from_settings", classmethod(lambda cls, settings: _FakeLLM())
        )

        def fake_cycle(settings, llm, embedder=None, scope=None, dry_run=False, now=None):
            return CycleReport(
                triggered=True, reason="测试触发", proposal=proposal,
                proposal_dir=None, dry_run=dry_run,
            )

        monkeypatch.setattr(cycle_mod, "run_evolution_cycle", fake_cycle)
        r = runner.invoke(app, ["evolve", "--dry-run"])
        assert r.exit_code == 0, r.output
        assert "dry-run" in r.output

    def test_evolve_without_llm_key_fails_closed(self, monkeypatch):
        monkeypatch.delenv("AGENT_MEMORY_LLM_API_KEY", raising=False)
        r = runner.invoke(app, ["evolve", "--dry-run"])
        assert r.exit_code == 1
        assert "AGENT_MEMORY_LLM_API_KEY" in r.output
