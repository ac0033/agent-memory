"""distill.py 测试：fake LLM 固定 JSON → 解析、校验、坏条目丢弃、脱敏集成。"""

import json

import yaml

from agent_memory.llm import LLMClient
from agent_memory.long_term.ingest.distill import distill_memories, format_conversation

CONVERSATION = [
    {"role": "user", "content": "这个项目 dev server 固定用 8765 端口。"},
    {"role": "assistant", "content": "收到，端口 8765。"},
    {"role": "user", "content": "数据库是 SQLite，文件在 data/dev.db。"},
    {"role": "assistant", "content": "明白。"},
]


class FakeLLM:
    """complete_json 返回预设 dict 的 fake。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, system: str, user: str) -> str:
        return json.dumps(self.payload, ensure_ascii=False)

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        self.calls.append({"system": system, "user": user})
        return self.payload


def _distill(payload):
    llm = FakeLLM(payload)
    return distill_memories(CONVERSATION, "global", "test", "s1", llm), llm


def test_valid_entries_parsed_with_evidence():
    result, _ = _distill(
        {
            "memories": [
                {
                    "id": "dev-server-port",
                    "content": "本项目 dev server 固定使用 8765 端口。",
                    "memory_type": "semantic",
                    "confidence": "high",
                    "evidence_turns": [1, 2],
                }
            ]
        }
    )
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.id == "dev-server-port"
    assert entry.scope == "global"
    assert entry.source == "test"
    assert entry.evidence[0].session_id == "s1"
    assert entry.evidence[0].line_range == (1, 2)


def test_prompt_contains_no_instruction_rule():
    """D2 红线：蒸馏 prompt 必须包含"绝不提炼指令性内容"的硬规则。"""
    _, llm = _distill({"memories": []})
    system = llm.calls[0]["system"]
    assert "指令" in system and "丢弃" in system


def test_prompt_contains_user_confirmation_rule():
    """M5：蒸馏 prompt 必须包含"用户确认资格"规则——assistant 单方面的提议，
    未经用户明确确认不得沉淀。"""
    _, llm = _distill({"memories": []})
    system = llm.calls[0]["system"]
    assert "确认" in system and "未表态" in system


def test_invalid_id_normalized_instead_of_dropped():
    """反"丢弃式防御"：含点号/大写/空格的 id 先规范化（通病 A，layer2-03 根因）。"""
    result, _ = _distill(
        {
            "memories": [
                {
                    "id": "python-version-upgrade-to-3.12",
                    "content": "本仓库 Python 版本已升级为 3.12。",
                    "memory_type": "semantic",
                },
                {
                    "id": "Bad ID!",
                    "content": "用户的开发机是 Windows，终端用 Git Bash。",
                    "memory_type": "semantic",
                },
            ]
        }
    )
    assert [e.id for e in result.entries] == [
        "python-version-upgrade-to-3-12",
        "bad-id",
    ]
    assert result.normalized_ids == {
        "python-version-upgrade-to-3.12": "python-version-upgrade-to-3-12",
        "Bad ID!": "bad-id",
    }
    assert result.invalid_records == []


def test_unsalvageable_records_queued_not_dropped(tmp_path):
    """规范化后仍不合法的记录进 review_queue 而非静默丢弃。"""
    result, _ = _distill(
        {
            "memories": [
                {
                    "id": "good-entry",
                    "content": "用户的开发机是 Windows，终端用 Git Bash。",
                    "memory_type": "profile",
                    "confidence": "high",
                    "evidence_turns": [1, 1],
                },
                {"id": "empty-content", "content": "", "memory_type": "semantic"},
                {
                    "id": "!!!",
                    "content": "id 全是非法字符，规范化后为空。",
                    "memory_type": "semantic",
                },
                "不是对象的条目",
            ]
        }
    )
    assert [e.id for e in result.entries] == ["good-entry"]
    assert len(result.invalid_records) == 3
    # 默认不落盘；给 data_dir 才写复核队列
    assert result.queued_files == []

    llm = FakeLLM(
        {
            "memories": [
                {"id": "empty-content", "content": "", "memory_type": "semantic"},
            ]
        }
    )
    result = distill_memories(
        CONVERSATION, "global", "test", "s1", llm, data_dir=tmp_path
    )
    assert len(result.queued_files) == 1
    payload = yaml.safe_load(result.queued_files[0].read_text(encoding="utf-8"))
    assert payload["raw_record"]["id"] == "empty-content"
    assert "蒸馏" in payload["reason"]


def test_confidence_and_detail_normalized():
    """confidence 非法值降为 medium；detail 超长截断，不丢整条。"""
    long_detail = "前因后果。" * 200  # 1000 字符，超 DETAIL_MAX_CHARS=800
    result, _ = _distill(
        {
            "memories": [
                {
                    "id": "conf-normalized",
                    "content": "本项目 dev server 固定使用 8765 端口。",
                    "memory_type": "semantic",
                    "confidence": "VERY-HIGH",
                    "detail": long_detail,
                }
            ]
        }
    )
    entry = result.entries[0]
    assert entry.confidence == "medium"
    assert len(entry.detail) == 800
    assert result.normalized_fields == 2


def test_redact_integrated_and_short_content_dropped():
    result, _ = _distill(
        {
            "memories": [
                {
                    "id": "with-secret",
                    "content": "用户调试时用过 api_key = abcdefgh12345678 这个测试 key。",
                    "memory_type": "semantic",
                },
                {"id": "too-short", "content": "端口 80", "memory_type": "semantic"},
            ]
        }
    )
    assert [e.id for e in result.entries] == ["with-secret"]
    assert "[REDACTED:api_key]" in result.entries[0].content
    assert result.redacted_hits["with-secret"] == ["api_key"]
    assert result.dropped_redacted == 1  # "端口 80" 不足 10 字符


def test_empty_conversation_returns_empty():
    llm = FakeLLM({"memories": []})
    result = distill_memories([], "global", "test", "s1", llm)
    assert result.entries == []
    assert llm.calls == []  # 空对话不应调 LLM


def test_memories_not_a_list_queued_invalid():
    result, _ = _distill({"memories": "不是一个数组"})
    assert result.entries == []
    assert len(result.invalid_records) == 1
    assert "不是数组" in result.invalid_records[0][1]


def test_evidence_turns_clamped_to_range():
    result, _ = _distill(
        {
            "memories": [
                {
                    "id": "clamped",
                    "content": "本项目数据库为 SQLite，文件路径 data/dev.db。",
                    "memory_type": "semantic",
                    "evidence_turns": [3, 99],  # 超出对话长度，应被夹紧
                }
            ]
        }
    )
    assert result.entries[0].evidence[0].line_range == (3, len(CONVERSATION))


def test_detail_parsed_and_optional():
    """detail 字段：提供时解析进条目，缺省时为 None（老数据兼容）。"""
    result, _ = _distill(
        {
            "memories": [
                {
                    "id": "with-detail",
                    "content": "本项目 dev server 固定使用 8765 端口。",
                    "detail": "用户在会话开头定下这个约束，因为 8000 已被另一个项目占用。",
                    "memory_type": "semantic",
                },
                {
                    "id": "without-detail",
                    "content": "本项目数据库为 SQLite，文件路径 data/dev.db。",
                    "memory_type": "semantic",
                },
            ]
        }
    )
    assert result.entries[0].detail == "用户在会话开头定下这个约束，因为 8000 已被另一个项目占用。"
    assert result.entries[1].detail is None


def test_detail_is_redacted_too():
    """detail 同样过脱敏（红线 D2），命中计数合并进 redacted_hits。"""
    result, _ = _distill(
        {
            "memories": [
                {
                    "id": "detail-secret",
                    "content": "用户分享过调试用的凭据片段。",
                    "detail": "调试时用户贴出过 api_key = abcdefgh12345678，属测试用途。",
                    "memory_type": "semantic",
                }
            ]
        }
    )
    assert "[REDACTED:api_key]" in result.entries[0].detail
    assert "api_key" in result.redacted_hits["detail-secret"]


def test_prompt_encourages_detail():
    """蒸馏 prompt 应鼓励产出 detail（Enhanced Notes 段落）。"""
    _, llm = _distill({"memories": []})
    assert "detail" in llm.calls[0]["system"]


def test_format_conversation_numbers_turns():
    text = format_conversation(CONVERSATION)
    assert "[turn 1] user:" in text
    assert "[turn 4] assistant:" in text


def test_fake_llm_satisfies_protocol():
    assert isinstance(FakeLLM({}), LLMClient)
