"""config.py 测试：默认值与环境变量覆盖，非法配置 fail-closed。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_memory.config import Settings, get_settings


class TestDefaults:
    def test_default_values(self):
        s = get_settings(env={})
        assert s.data_dir == Path("~/.agent-memory/data").expanduser()
        assert s.embedding_model == "BAAI/bge-m3"
        assert s.rerank_enabled is False
        assert s.reranker_model == "BAAI/bge-reranker-v2-m3"
        assert s.llm_base_url is None
        assert s.llm_api_key is None
        assert s.llm_model is None
        assert s.judge_llm_base_url is None
        assert s.judge_llm_api_key is None
        assert s.judge_llm_model is None
        assert s.recall_budget_chars == 2000
        assert s.stale_days == 90
        # M4a 整理循环阈值
        assert s.evolve_interval_days == 7
        assert s.evolve_new_entries_threshold == 50
        assert s.evolve_review_backlog_threshold == 10
        assert s.evolve_merge_max_distance == 0.25
        assert s.evolve_stale_sample_size == 10
        assert s.evolve_retention_query_count == 10
        # M5 人工复核交互
        assert s.review_gate == "ask"
        assert s.review_turn_interval == 3
        # M6 HTTP 常驻服务
        assert s.http_host == "127.0.0.1"
        assert s.http_port == 8765

    def test_direct_construction_matches(self):
        assert Settings() == get_settings(env={})


class TestEnvOverrides:
    def test_data_dir_override(self):
        s = get_settings(env={"AGENT_MEMORY_DATA_DIR": "/tmp/am-data"})
        assert s.data_dir == Path("/tmp/am-data")

    def test_llm_overrides(self):
        s = get_settings(
            env={
                "AGENT_MEMORY_LLM_BASE_URL": "https://api.example.com/v1",
                "AGENT_MEMORY_LLM_API_KEY": "sk-fake-key",
                "AGENT_MEMORY_LLM_MODEL": "fake-distiller-7b",
            }
        )
        assert s.llm_base_url == "https://api.example.com/v1"
        assert s.llm_api_key == "sk-fake-key"
        assert s.llm_model == "fake-distiller-7b"

    def test_judge_llm_overrides_independent_of_llm(self):
        # 评委模型与蒸馏模型是两套独立配置（异源互审）
        s = get_settings(
            env={
                "AGENT_MEMORY_LLM_MODEL": "family-a-model",
                "AGENT_MEMORY_JUDGE_LLM_MODEL": "family-b-model",
            }
        )
        assert s.llm_model == "family-a-model"
        assert s.judge_llm_model == "family-b-model"

    def test_rerank_enabled_override(self):
        assert get_settings(env={"AGENT_MEMORY_RERANK_ENABLED": "true"}).rerank_enabled is True

    def test_evolve_overrides(self):
        s = get_settings(
            env={
                "AGENT_MEMORY_EVOLVE_INTERVAL_DAYS": "3",
                "AGENT_MEMORY_EVOLVE_NEW_ENTRIES_THRESHOLD": "20",
                "AGENT_MEMORY_EVOLVE_REVIEW_BACKLOG_THRESHOLD": "5",
                "AGENT_MEMORY_EVOLVE_MERGE_MAX_DISTANCE": "0.15",
                "AGENT_MEMORY_EVOLVE_STALE_SAMPLE_SIZE": "6",
                "AGENT_MEMORY_EVOLVE_RETENTION_QUERY_COUNT": "8",
            }
        )
        assert s.evolve_interval_days == 3
        assert s.evolve_new_entries_threshold == 20
        assert s.evolve_review_backlog_threshold == 5
        assert s.evolve_merge_max_distance == 0.15
        assert s.evolve_stale_sample_size == 6
        assert s.evolve_retention_query_count == 8
        assert get_settings(env={"AGENT_MEMORY_RERANK_ENABLED": "1"}).rerank_enabled is True
        assert get_settings(env={"AGENT_MEMORY_RERANK_ENABLED": "false"}).rerank_enabled is False

    def test_numeric_overrides(self):
        s = get_settings(
            env={
                "AGENT_MEMORY_RECALL_BUDGET_CHARS": "5000",
                "AGENT_MEMORY_STALE_DAYS": "30",
            }
        )
        assert s.recall_budget_chars == 5000
        assert s.stale_days == 30

    def test_review_overrides(self):
        s = get_settings(
            env={
                "AGENT_MEMORY_REVIEW_GATE": "strict",
                "AGENT_MEMORY_REVIEW_TURN_INTERVAL": "5",
            }
        )
        assert s.review_gate == "strict"
        assert s.review_turn_interval == 5

    def test_http_overrides(self):
        s = get_settings(
            env={"AGENT_MEMORY_HTTP_HOST": "0.0.0.0", "AGENT_MEMORY_HTTP_PORT": "9000"}
        )
        assert s.http_host == "0.0.0.0"
        assert s.http_port == 9000

    def test_unrelated_env_vars_ignored(self):
        s = get_settings(env={"AGENT_MEMORY_UNKNOWN_THING": "x", "PATH": "/usr/bin"})
        assert s == Settings()


class TestFailClosed:
    @pytest.mark.parametrize("bad", ["0", "-1", "abc", "2.5"])
    def test_invalid_recall_budget_chars(self, bad):
        with pytest.raises(ValidationError):
            get_settings(env={"AGENT_MEMORY_RECALL_BUDGET_CHARS": bad})

    def test_invalid_stale_days(self):
        with pytest.raises(ValidationError):
            get_settings(env={"AGENT_MEMORY_STALE_DAYS": "-3"})

    def test_invalid_bool(self):
        with pytest.raises(ValidationError):
            get_settings(env={"AGENT_MEMORY_RERANK_ENABLED": "maybe"})

    def test_invalid_review_gate(self):
        with pytest.raises(ValidationError):
            get_settings(env={"AGENT_MEMORY_REVIEW_GATE": "sometimes"})

    def test_invalid_review_turn_interval(self):
        with pytest.raises(ValidationError):
            get_settings(env={"AGENT_MEMORY_REVIEW_TURN_INTERVAL": "0"})

    def test_invalid_http_port(self):
        with pytest.raises(ValidationError):
            get_settings(env={"AGENT_MEMORY_HTTP_PORT": "70000"})
