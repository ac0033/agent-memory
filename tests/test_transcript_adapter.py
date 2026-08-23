"""short_term/adapter.py 测试：kimi-code wire.jsonl 解析与适配器注册表。

夹具按实测格式（protocol_version 1.4/1.5）在 tmp_path 里造小型 wire.jsonl：
每行一个 JSON 记录，context.append_message 是用户输入，context.append_loop_event
承载 assistant 增量（content.part）与工具事件（tool.call / tool.result），
turn.ended 标轮次边界，其余 type 全是噪音。
"""

import json

import pytest

from agent_memory.short_term.adapter import (
    ADAPTERS,
    TOOL_OUTPUT_MAX_CHARS,
    KimiCodeWireAdapter,
    detect_adapter,
    get_adapter,
)


def _write_log(tmp_path, records, name="wire.jsonl"):
    """把记录列表写成 JSONL 文件；元素可以是 dict（序列化）或 str（原样写行，
    用于造坏 JSON 行）。"""
    path = tmp_path / name
    lines = [json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _user_msg(text: str | list[str], time: int | None = None):
    """造一条 context.append_message 用户消息；list 形式表示多块 content。"""
    blocks = [{"type": "text", "text": t} for t in ([text] if isinstance(text, str) else text)]
    rec = {"type": "context.append_message", "message": {"role": "user", "content": blocks}}
    if time is not None:
        rec["time"] = time
    return rec


def _text_part(turn_id: int, text: str, time: int | None = None):
    rec = {
        "type": "context.append_loop_event",
        "event": {
            "type": "content.part",
            "turnId": str(turn_id),
            "part": {"type": "text", "text": text},
        },
    }
    if time is not None:
        rec["time"] = time
    return rec


def _think_part(turn_id: int, text: str):
    return {
        "type": "context.append_loop_event",
        "event": {
            "type": "content.part",
            "turnId": str(turn_id),
            "part": {"type": "think", "text": text},
        },
    }


def _tool_call(turn_id: int, call_id: str, name: str, args: dict):
    return {
        "type": "context.append_loop_event",
        "event": {
            "type": "tool.call",
            "turnId": str(turn_id),
            "toolCallId": call_id,
            "name": name,
            "args": args,
        },
    }


def _tool_result(turn_id: int, call_id: str, output):
    return {
        "type": "context.append_loop_event",
        "event": {
            "type": "tool.result",
            "turnId": str(turn_id),
            "toolCallId": call_id,
            "result": {"output": output},
        },
    }


@pytest.fixture
def adapter():
    return KimiCodeWireAdapter()


# ---------------------------------------------------------------- 噪音与坏行


def test_noise_records_skipped(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            {"type": "metadata", "protocol_version": "1.5"},
            {"type": "config.update", "config": {}},
            {"type": "llm.request", "request": {}},
            {"type": "usage.record", "tokens": 1},
            {"type": "token_counting.start"},
            _user_msg("你好"),
            {"type": "turn.prompt", "prompt": "你好"},  # 与 append_message 重复，忽略
            {"type": "context.append_loop_event", "event": {"type": "step.begin", "turnId": "0"}},
            _text_part(0, "你好！"),
            {"type": "context.append_loop_event", "event": {"type": "step.end", "turnId": "0"}},
            {"type": "turn.ended", "turnId": 0},
        ],
    )
    turns = adapter.parse(path)
    # 只有一条用户消息（turn.prompt 不双计）+ 一条 assistant 回复
    assert [(t.role, t.content) for t in turns] == [("user", "你好"), ("assistant", "你好！")]


def test_bad_json_lines_skipped(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            _user_msg("第一条"),
            "{这不是合法 JSON",
            "",  # 空行
            "   ",
            json.dumps(["不是", "对象", "的", "JSON"]),
            _text_part(0, "回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [("user", "第一条"), ("assistant", "回复")]


def test_records_missing_fields_skipped(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            {"type": "context.append_message"},  # 缺 message
            {"type": "context.append_message", "message": {"role": "user"}},  # 缺 content
            # content 为空块列表（无文本）
            {"type": "context.append_message", "message": {"role": "user", "content": []}},
            {"type": "context.append_loop_event"},  # 缺 event
            {"type": "context.append_loop_event", "event": {"type": "content.part"}},  # 缺 turnId
            _user_msg("有效消息"),
            _text_part(0, "有效回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [("user", 0), ("assistant", 0)]


# ---------------------------------------------------------------- 用户消息 / assistant 聚合


def test_user_message_multi_block_joined(adapter, tmp_path):
    path = _write_log(tmp_path, [_user_msg(["第一块", "第二块"], time=1724300000000)])
    turns = adapter.parse(path)
    assert len(turns) == 1
    assert turns[0].role == "user"
    assert turns[0].content == "第一块\n第二块"
    assert turns[0].ts == 1724300000000


def test_assistant_text_parts_aggregated_think_skipped(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            _user_msg("讲个故事"),
            _think_part(0, "用户想听故事，我应该……"),  # 内部推理跳过
            _text_part(0, "从前"),
            _text_part(0, "有座山", time=1724300001000),
            _think_part(0, "再想想结尾"),
            _text_part(0, "。"),
        ],
    )
    turns = adapter.parse(path)
    assert len(turns) == 2
    assistant = turns[1]
    assert assistant.role == "assistant"
    assert assistant.content == "从前有座山。"  # 同一 turn 的 text part 直接拼接
    assert "应该" not in assistant.content and "结尾" not in assistant.content


def test_non_user_append_message_skipped(adapter, tmp_path):
    """append_message 里 role 不是 user 的记录（防御）不作为用户消息。"""
    path = _write_log(
        tmp_path,
        [
            {
                "type": "context.append_message",
                "message": {"role": "system", "content": [{"type": "text", "text": "系统提示"}]},
            },
            _user_msg("你好"),
        ],
    )
    turns = adapter.parse(path)
    assert [t.role for t in turns] == ["user"]


# ---------------------------------------------------------------- tool.call / tool.result 配对


def test_tool_call_result_paired(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            _user_msg("查一下天气"),
            _tool_call(0, "call-1", "web_search", {"query": "北京天气"}),
            _tool_result(0, "call-1", "晴，25 度"),
            _text_part(0, "北京今天晴，25 度。"),
        ],
    )
    turns = adapter.parse(path)
    tool = turns[1]
    assert tool.role == "tool"
    assert tool.tool_name == "web_search"
    assert tool.turn_index == 0
    assert json.loads(tool.content.split("\n→")[0]) == {"query": "北京天气"}
    assert "\n→ 晴，25 度" in tool.content
    assert turns[2].role == "assistant"


def test_tool_result_unpaired_ignored(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [
            _user_msg("hi"),
            _tool_result(0, "call-not-exist", "孤儿结果"),  # 没有对应 call
            _tool_call(0, "call-1", "bash", {"cmd": "ls"}),
            _tool_result(0, "call-2", "另一个孤儿"),
        ],
    )
    turns = adapter.parse(path)
    assert len(turns) == 2
    tool = turns[1]
    assert tool.tool_name == "bash"
    assert "孤儿" not in tool.content


def test_tool_result_output_truncated(adapter, tmp_path):
    long_output = "x" * (TOOL_OUTPUT_MAX_CHARS + 500)
    path = _write_log(
        tmp_path,
        [_tool_call(0, "call-1", "bash", {"cmd": "big"}), _tool_result(0, "call-1", long_output)],
    )
    tool = adapter.parse(path)[0]
    assert "截断" in tool.content
    assert str(TOOL_OUTPUT_MAX_CHARS + 500) in tool.content  # 标注原长
    assert len(tool.content) < TOOL_OUTPUT_MAX_CHARS + 200


def test_tool_result_non_string_output_stringified(adapter, tmp_path):
    path = _write_log(
        tmp_path,
        [_tool_call(0, "call-1", "bash", {}), _tool_result(0, "call-1", {"exit_code": 0})],
    )
    tool = adapter.parse(path)[0]
    assert '{"exit_code": 0}' in tool.content


# ---------------------------------------------------------------- 轮次编号


def test_turn_numbering_user_assigned_to_upcoming_turn(adapter, tmp_path):
    """用户消息不带 turnId，归为"已见过的最大 turnId + 1"（即将到来的那轮）。"""
    path = _write_log(
        tmp_path,
        [
            _user_msg("第一轮"),  # 初始 max=-1，归 turn 0
            _text_part(0, "回复一"),
            {"type": "turn.ended", "turnId": 0},
            _user_msg("第二轮"),  # max=0，归 turn 1
            _text_part(1, "回复二"),
            {"type": "turn.ended", "turnId": 1},
            _user_msg("第三轮"),  # max=1，归 turn 2
            _text_part(2, "回复三"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("assistant", 0),
        ("user", 1),
        ("assistant", 1),
        ("user", 2),
        ("assistant", 2),
    ]


def test_turn_numbering_robust_when_turn_events_missing(adapter, tmp_path):
    """防御：某轮 loop 事件缺失（轮次被中止）时，下一条用户消息不撞号。"""
    path = _write_log(
        tmp_path,
        [
            _user_msg("第一轮"),  # turn 0，该轮没有任何 loop 事件
            _user_msg("第二轮"),  # 归 turn 1 而不是再次归 turn 0
            _text_part(1, "回复二"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("user", 1),
        ("assistant", 1),
    ]


# ---------------------------------------------------------------- 注册表与识别


def test_get_adapter_by_name():
    ad = get_adapter("kimi-code-wire")
    assert ad.name == "kimi-code-wire"
    assert ADAPTERS["kimi-code-wire"] is ad


def test_get_adapter_unknown_raises():
    with pytest.raises(ValueError, match="kimi-code-wire"):  # 报错里列出可用适配器
        get_adapter("not-exist")


def test_detect_adapter_by_filename(tmp_path):
    path = _write_log(tmp_path, [_user_msg("hi")])
    assert detect_adapter(path).name == "kimi-code-wire"


def test_detect_adapter_unknown_filename_raises(tmp_path):
    path = _write_log(tmp_path, [_user_msg("hi")], name="session.log")
    with pytest.raises(ValueError, match="显式指定 adapter"):
        detect_adapter(path)


# ---------------------------------------------------------------- 文件不存在


def test_missing_file_raises(adapter, tmp_path):
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path / "wire.jsonl")


def test_directory_path_raises(adapter, tmp_path):
    """不是文件的路径（目录）同样按 FileNotFoundError 处理。"""
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path)
