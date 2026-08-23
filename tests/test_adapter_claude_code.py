"""short_term/claude_code.py 测试：Claude Code 会话日志解析。

夹具按文献调研的格式规格（v2.1.x 源码分析 + 4200 个文件实测统计，本机无真实
样本）在 tmp_path 里造合成 JSONL：每行一个 JSON，判别符 type 只有 user/assistant
两类保留；user 记录分真实输入（字符串或首块非 tool_result 的块数组）与工具结果
回传（首块 tool_result）两种形态；assistant 记录按 message.id 聚合多帧 text。
"""

import json
from datetime import UTC, datetime

import pytest

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS
from agent_memory.short_term.claude_code import ClaudeCodeAdapter


def _write_log(tmp_path, records, name="session.jsonl"):
    """把记录列表写成 JSONL 文件；元素可以是 dict（序列化）或 str（原样写行，
    用于造坏 JSON 行）。"""
    path = tmp_path / name
    lines = [
        json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r
        for r in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _ts_ms(iso: str) -> int:
    """测试侧独立计算 ISO 8601 -> epoch 毫秒，作为断言的期望值。"""
    return int(
        datetime.fromisoformat(iso.replace("Z", "+00:00"))
        .replace(tzinfo=UTC)
        .timestamp()
        * 1000
    )


def _user_input(text: str | list, ts: str | None = None, **extra):
    """造一条真实用户输入记录；list 形式表示块数组 content。"""
    content = (
        [{"type": "text", "text": t} for t in text] if isinstance(text, list) else text
    )
    rec = {
        "type": "user",
        "uuid": "u-1",
        "parentUuid": None,
        "sessionId": "s-1",
        "isSidechain": False,
        "message": {"role": "user", "content": content},
    }
    if ts is not None:
        rec["timestamp"] = ts
    rec.update(extra)
    return rec


def _tool_result_user(blocks: list[dict], ts: str | None = None, **extra):
    """造一条工具结果回传形态的 user 记录（content 首块是 tool_result）。"""
    rec = {
        "type": "user",
        "uuid": "u-2",
        "sessionId": "s-1",
        "isSidechain": False,
        "toolUseResult": {"ignored": True},  # 顶层结构化补充，应被忽略
        "message": {"role": "user", "content": blocks},
    }
    if ts is not None:
        rec["timestamp"] = ts
    rec.update(extra)
    return rec


def _assistant(blocks: list[dict], msg_id: str = "msg-1", ts: str | None = None, **extra):
    rec = {
        "type": "assistant",
        "uuid": "a-1",
        "sessionId": "s-1",
        "isSidechain": False,
        "message": {"role": "assistant", "id": msg_id, "content": blocks},
    }
    if ts is not None:
        rec["timestamp"] = ts
    rec.update(extra)
    return rec


def _tool_use(block_id: str, name: str, tool_input: dict):
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


def _tool_result_block(call_id: str, content):
    return {"type": "tool_result", "tool_use_id": call_id, "content": content}


@pytest.fixture
def adapter():
    return ClaudeCodeAdapter()


# ---------------------------------------------------------------- 正常三类角色往返


def test_normal_user_assistant_tool_roundtrip(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            _user_input("查一下天气", ts="2025-06-01T10:00:00.000Z"),
            _assistant(
                [{"type": "text", "text": "我来查一下。"}],
                ts="2025-06-01T10:00:01.000Z",
            ),
            _assistant(
                [_tool_use("tu-1", "web_search", {"query": "北京天气"})],
                msg_id="msg-2",
                ts="2025-06-01T10:00:02.000Z",
            ),
            _tool_result_user(
                [_tool_result_block("tu-1", "晴，25 度")],
                ts="2025-06-01T10:00:03.000Z",
            ),
            _assistant(
                [{"type": "text", "text": "北京今天晴，25 度。"}],
                msg_id="msg-3",
                ts="2025-06-01T10:00:04.000Z",
            ),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("assistant", 0),
        ("tool", 0),
        ("assistant", 0),
    ]
    assert turns[0].content == "查一下天气"
    assert turns[0].ts == _ts_ms("2025-06-01T10:00:00.000Z")
    tool = turns[2]
    assert tool.tool_name == "web_search"
    assert json.loads(tool.content.split("\n→")[0]) == {"query": "北京天气"}
    assert "\n→ 晴，25 度" in tool.content
    assert "ignored" not in tool.content  # 顶层 toolUseResult 被忽略


# ---------------------------------------------------------------- 噪音与坏行


def test_noise_records_skipped(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            {"type": "summary", "summary": "旧会话摘要"},
            {"type": "system", "content": "系统提示"},
            {"type": "file-history-snapshot", "snapshot": {}},
            {"type": "permission-mode", "mode": "default"},
            {"type": "ai-title", "title": "天气查询"},
            {"type": "queue-operation", "op": "enqueue"},
            {"type": "progress", "data": {}},
            {"type": "summary", "summary": "周期性重复追加，仍跳过"},
            _user_input("你好"),
            _assistant([{"type": "text", "text": "你好！"}]),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [
        ("user", "你好"),
        ("assistant", "你好！"),
    ]


def test_sidechain_records_skipped(adapter, tmp_path):
    """isSidechain==true 是旧版子 agent 混写记录，一律跳过。"""
    path = _write_log(
        tmp_path,
        [
            _user_input("主线问题"),
            _user_input("子 agent 的用户侧记录", isSidechain=True),
            _assistant([{"type": "text", "text": "子 agent 的回复"}], isSidechain=True),
            _assistant([{"type": "text", "text": "主线回复"}]),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content, t.turn_index) for t in turns] == [
        ("user", "主线问题", 0),
        ("assistant", "主线回复", 0),
    ]


def test_is_meta_user_records_skipped(adapter, tmp_path):
    """isMeta==true 的 user 记录是本地命令回显，跳过且不触发轮次递增。"""
    path = _write_log(
        tmp_path,
        [
            _user_input("<command-name>/help</command-name>", isMeta=True),
            _user_input("真实问题"),
            _assistant([{"type": "text", "text": "回答"}]),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [("user", 0), ("assistant", 0)]


def test_bad_json_lines_and_missing_fields_skipped(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            "{这不是合法 JSON",
            "",
            json.dumps(["不是", "对象"]),
            {"type": "user"},  # 缺 message
            {"type": "user", "message": {"role": "user"}},  # 缺 content
            {"type": "user", "message": {"role": "user", "content": []}},  # 空块列表
            {"type": "assistant", "message": {"role": "assistant"}},  # 缺 content
            {"type": "assistant", "message": {"role": "assistant", "content": "字符串"}},
            _user_input("有效消息"),
            _assistant([{"type": "text", "text": "有效回复"}]),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [
        ("user", "有效消息"),
        ("assistant", "有效回复"),
    ]


# ---------------------------------------------------------------- 轮次编号


def test_turn_numbering_user_input_starts_new_turn(adapter, tmp_path):
    """真实用户输入 = 新一轮（0 起）；其后的 assistant/tool 归该轮。"""
    path = _write_log(
        tmp_path,
        [
            _user_input("第一轮"),
            _assistant([{"type": "text", "text": "回复一"}], msg_id="m1"),
            _assistant([_tool_use("tu-1", "bash", {"cmd": "ls"})], msg_id="m2"),
            _user_input("第二轮"),
            _assistant([{"type": "text", "text": "回复二"}], msg_id="m3"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("assistant", 0),
        ("tool", 0),
        ("user", 1),
        ("assistant", 1),
    ]


def test_tool_result_user_record_does_not_advance_turn(adapter, tmp_path):
    """工具结果回传形态的 user 记录不触发轮次递增。"""
    path = _write_log(
        tmp_path,
        [
            _user_input("跑个命令"),
            _assistant([_tool_use("tu-1", "bash", {"cmd": "ls"})], msg_id="m1"),
            _tool_result_user([_tool_result_block("tu-1", "ok")]),
            _tool_result_user([_tool_result_block("tu-orphan", "孤儿")]),
            _assistant([{"type": "text", "text": "跑完了"}], msg_id="m2"),
            _user_input("下一轮"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("tool", 0),
        ("assistant", 0),
        ("user", 1),
    ]


def test_assistant_before_any_user_gets_turn_zero(adapter, tmp_path):
    """防御：会话从 assistant 记录开始（恢复会话）时归第 0 轮不报错。"""
    path = _write_log(
        tmp_path,
        [_assistant([{"type": "text", "text": "恢复的现场"}]), _user_input("继续")],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [("assistant", 0), ("user", 0)]


# ---------------------------------------------------------------- 多帧聚合（规格点名的坑）


def test_assistant_split_records_aggregated_by_message_id(adapter, tmp_path):
    """一次 API 响应拆成多条连续 assistant 记录、共享 message.id：text 聚合，
    ts 取首条。"""
    path = _write_log(
        tmp_path,
        [
            _user_input("讲个故事"),
            _assistant(
                [{"type": "text", "text": "从前"}],
                msg_id="msg-x",
                ts="2025-06-01T10:00:00.000Z",
            ),
            _assistant(
                [{"type": "text", "text": "有座山"}],
                msg_id="msg-x",
                ts="2025-06-01T10:00:01.000Z",  # 后续帧的 ts 不覆盖首条
            ),
            _assistant(
                [{"type": "text", "text": "另一个响应"}],
                msg_id="msg-y",
                ts="2025-06-01T10:00:02.000Z",
            ),
        ],
    )
    turns = adapter.parse(path)
    assert [t.role for t in turns] == ["user", "assistant", "assistant"]
    assert turns[1].content == "从前\n有座山"
    assert turns[1].ts == _ts_ms("2025-06-01T10:00:00.000Z")
    assert turns[2].content == "另一个响应"


def test_thinking_blocks_skipped(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            _assistant(
                [
                    {"type": "thinking", "thinking": "用户在问天气，我应该……"},
                    {"type": "text", "text": "今天晴天。"},
                ]
            )
        ],
    )
    turns = adapter.parse(path)
    assert len(turns) == 1
    assert turns[0].content == "今天晴天。"
    assert "应该" not in turns[0].content


def test_thinking_only_record_produces_no_turn(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            _assistant([{"type": "thinking", "thinking": "纯思考帧，无 text"}]),
            _user_input("你好"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [("user", "你好")]


# ---------------------------------------------------------------- 工具调用/结果配对


def test_tool_result_block_array_content_joined(adapter, tmp_path):
    """tool_result 的 content 是 text 块数组时拼接。"""
    path = _write_log(
        tmp_path,
        [
            _assistant([_tool_use("tu-1", "bash", {"cmd": "ls"})]),
            _tool_result_user(
                [
                    _tool_result_block(
                        "tu-1",
                        [
                            {"type": "text", "text": "第一行"},
                            {"type": "text", "text": "第二行"},
                        ],
                    )
                ]
            ),
        ],
    )
    tool = adapter.parse(path)[0]
    assert "\n→ 第一行\n第二行" in tool.content


def test_tool_result_unpaired_ignored(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            _tool_result_user([_tool_result_block("tu-孤儿", "没人认领")]),
            _assistant([_tool_use("tu-1", "bash", {"cmd": "ls"})]),
        ],
    )
    turns = adapter.parse(path)
    assert len(turns) == 1
    assert turns[0].role == "tool"
    assert "孤儿" not in turns[0].content
    assert "没人认领" not in turns[0].content


def test_multiple_tool_results_in_one_user_record(adapter, tmp_path):
    """一条 user 记录可携带多个 tool_result 块，各自按 tool_use_id 配对。"""
    path = _write_log(
        tmp_path,
        [
            _assistant([_tool_use("tu-1", "read", {"path": "a"})], msg_id="m1"),
            _assistant([_tool_use("tu-2", "read", {"path": "b"})], msg_id="m2"),
            _tool_result_user(
                [
                    _tool_result_block("tu-1", "结果一"),
                    _tool_result_block("tu-2", "结果二"),
                ]
            ),
        ],
    )
    turns = adapter.parse(path)
    assert len(turns) == 2
    assert "\n→ 结果一" in turns[0].content
    assert "\n→ 结果二" in turns[1].content


def test_tool_result_output_truncated(adapter, tmp_path):
    long_output = "x" * (TOOL_OUTPUT_MAX_CHARS + 500)
    path = _write_log(
        tmp_path,
        [
            _assistant([_tool_use("tu-1", "bash", {"cmd": "big"})]),
            _tool_result_user([_tool_result_block("tu-1", long_output)]),
        ],
    )
    tool = adapter.parse(path)[0]
    assert "截断" in tool.content
    assert str(TOOL_OUTPUT_MAX_CHARS + 500) in tool.content  # 标注原长
    assert len(tool.content) < TOOL_OUTPUT_MAX_CHARS + 200


# ---------------------------------------------------------------- 时间戳


def test_timestamp_missing_gives_none(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [_user_input("没时间戳"), _assistant([{"type": "text", "text": "也没有"}])],
    )
    turns = adapter.parse(path)
    assert all(t.ts is None for t in turns)


def test_timestamp_invalid_gives_none(adapter, tmp_path):
    path = _write_log(tmp_path, [_user_input("你好", ts="不是时间戳")])
    assert adapter.parse(path)[0].ts is None


# ---------------------------------------------------------------- 用户输入的两种 content 形态


def test_user_input_block_array_joined(adapter, tmp_path):
    """content 是块数组且首块非 tool_result：拼接 text 块。"""
    path = _write_log(tmp_path, [_user_input(["第一段", "第二段"])])
    turns = adapter.parse(path)
    assert len(turns) == 1
    assert turns[0].role == "user"
    assert turns[0].content == "第一段\n第二段"


# ---------------------------------------------------------------- 文件不存在


def test_missing_file_raises(adapter, tmp_path):
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path / "session.jsonl")


def test_directory_path_raises(adapter, tmp_path):
    """不是文件的路径（目录）同样按 FileNotFoundError 处理。"""
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path)


def test_adapter_name():
    assert ClaudeCodeAdapter.name == "claude-code"
