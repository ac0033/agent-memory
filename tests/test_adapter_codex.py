"""short_term/codex.py 测试：Codex CLI rollout-*.jsonl 解析。

夹具按实测格式（cli 0.140→0.147 信封一致）在 tmp_path 里造小型
rollout-*.jsonl：每行一个信封记录 {"timestamp", "type", "payload"}，
只保留 type=="response_item" 的记录——message（user/assistant）、
function_call(+output)、custom_tool_call(+output)；reasoning、
web_search_call、developer message、event_msg、session_meta 等全是噪音。
"""

import json

import pytest

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS
from agent_memory.short_term.codex import CodexAdapter

_TS = "2026-06-19T09:02:25.224Z"  # 信封时间戳格式样例


_ROLLOUT_NAME = "rollout-2026-06-19T09-01-24-019edf1c-e8ff-7862-b291-054010bdef2b.jsonl"


def _write_rollout(tmp_path, records, name=_ROLLOUT_NAME):
    """把记录列表写成 JSONL 文件；元素可以是 dict（序列化）或 str（原样写行，
    用于造坏 JSON 行）。"""
    path = tmp_path / name
    lines = [json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else r for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _envelope(payload: dict, timestamp: str | None = _TS):
    """包一层 rollout 信封；timestamp=None 表示记录不带时间戳。"""
    rec = {"type": "response_item", "payload": payload}
    if timestamp is not None:
        rec["timestamp"] = timestamp
    return rec


def _user_msg(text: str | list[str], timestamp: str | None = _TS):
    """造一条 user message；list 形式表示多个 input_text 块。"""
    blocks = [
        {"type": "input_text", "text": t}
        for t in ([text] if isinstance(text, str) else text)
    ]
    return _envelope(
        {"type": "message", "role": "user", "content": blocks}, timestamp
    )


def _assistant_msg(text: str | list[str], phase: str = "final_answer", timestamp: str | None = _TS):
    """造一条 assistant message；list 形式表示多个 output_text 块。"""
    blocks = [
        {"type": "output_text", "text": t}
        for t in ([text] if isinstance(text, str) else text)
    ]
    return _envelope(
        {"type": "message", "role": "assistant", "phase": phase, "content": blocks},
        timestamp,
    )


def _function_call(call_id: str, name: str, arguments: str):
    return _envelope(
        {
            "type": "function_call",
            "name": name,
            "arguments": arguments,
            "call_id": call_id,
        }
    )


def _function_call_output(call_id: str, output):
    return _envelope(
        {"type": "function_call_output", "call_id": call_id, "output": output}
    )


def _custom_tool_call(call_id: str, name: str, input_text: str):
    return _envelope(
        {
            "type": "custom_tool_call",
            "name": name,
            "input": input_text,
            "call_id": call_id,
        }
    )


def _custom_tool_call_output(call_id: str, output):
    return _envelope(
        {"type": "custom_tool_call_output", "call_id": call_id, "output": output}
    )


@pytest.fixture
def adapter():
    return CodexAdapter()


# ---------------------------------------------------------------- 正常三类角色往返


def test_user_assistant_tool_roundtrip(adapter, tmp_path):
    path = _write_rollout(
        tmp_path,
        [
            {"timestamp": _TS, "type": "session_meta", "payload": {"id": "x"}},
            _user_msg("查一下天气"),
            _assistant_msg("我先查一下。", phase="commentary"),
            _function_call("call_1", "shell_command", '{"command":"curl wttr.in"}'),
            _function_call_output("call_1", "晴，25 度"),
            _assistant_msg("北京今天晴，25 度。"),
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
    assert turns[1].content == "我先查一下。"
    tool = turns[2]
    assert tool.tool_name == "shell_command"
    # arguments 是未解析的 JSON 字符串，原样存；output 截断后追加 "\n→ "
    assert tool.content == '{"command":"curl wttr.in"}\n→ 晴，25 度'
    assert turns[3].content == "北京今天晴，25 度。"


def test_ts_parsed_to_epoch_ms(adapter, tmp_path):
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("带时间", timestamp="2026-06-19T09:02:25.224Z"),
            _assistant_msg("无时间戳", timestamp=None),
        ],
    )
    turns = adapter.parse(path)
    assert turns[0].ts == 1781859745224  # 2026-06-19T09:02:25.224Z 的 epoch 毫秒
    assert turns[1].ts is None


# ---------------------------------------------------------------- 噪音记录跳过


def test_noise_records_skipped(adapter, tmp_path):
    path = _write_rollout(
        tmp_path,
        [
            {"timestamp": _TS, "type": "session_meta", "payload": {"id": "x"}},
            {"timestamp": _TS, "type": "turn_context", "payload": {"turn_id": "t1"}},
            {"timestamp": _TS, "type": "world_state", "payload": {}},
            # event_msg 整类跳过（user_message/agent_message 与 response_item 重复）
            {"timestamp": _TS, "type": "event_msg",
             "payload": {"type": "user_message", "message": "重复的用户消息"}},
            {"timestamp": _TS, "type": "event_msg",
             "payload": {"type": "agent_message", "message": "重复的回复"}},
            {"timestamp": _TS, "type": "event_msg", "payload": {"type": "task_started"}},
            # developer message 跳过
            _envelope({"type": "message", "role": "developer",
                       "content": [{"type": "input_text", "text": "权限说明"}]}),
            # reasoning（思维链）与 web_search_call 跳过
            _envelope({"type": "reasoning",
                       "summary": [{"type": "summary_text", "text": "我在想……"}]}),
            _envelope({"type": "web_search_call", "status": "completed",
                       "action": {"type": "search", "query": "q"}}),
            _user_msg("真实用户消息"),
            _assistant_msg("真实回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [
        ("user", "真实用户消息"),
        ("assistant", "真实回复"),
    ]


def test_environment_context_message_skipped(adapter, tmp_path):
    """以 <environment_context> 开头的 user 消息是环境注入，整条跳过且不开轮。"""
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("<environment_context>\n  <cwd>D:\\proj</cwd>\n</environment_context>"),
            _user_msg("第一个真实问题"),
        ],
    )
    turns = adapter.parse(path)
    assert len(turns) == 1
    assert turns[0].turn_index == 0  # 环境注入不占轮次编号
    assert turns[0].content == "第一个真实问题"


# ---------------------------------------------------------------- 坏行与缺字段容忍


def test_bad_json_lines_skipped(adapter, tmp_path):
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("第一条"),
            "{这不是合法 JSON",
            "",
            "   ",
            json.dumps(["不是", "对象", "的", "JSON"]),
            json.dumps("不是对象的 JSON"),
            _assistant_msg("回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [("user", "第一条"), ("assistant", "回复")]


def test_records_missing_fields_skipped(adapter, tmp_path):
    path = _write_rollout(
        tmp_path,
        [
            {"type": "response_item"},  # 缺 payload
            _envelope({"type": "message", "role": "user"}),  # 缺 content
            _envelope({"type": "message", "role": "user", "content": []}),  # 空块
            _envelope({"type": "message", "role": "assistant", "content": []}),  # 空 assistant
            _envelope({"type": "function_call", "name": "shell"}),  # 缺 call_id
            _envelope({"type": "function_call", "call_id": "call_x"}),  # 缺 name
            {"timestamp": "不是合法时间戳", "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "坏时间戳消息"}]}},
            _user_msg("有效消息"),
            _assistant_msg("有效回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),  # 坏时间戳消息仍收，ts 为 None
        ("user", 1),
        ("assistant", 1),
    ]
    assert turns[0].ts is None


# ---------------------------------------------------------------- 块聚合与块类型过滤


def test_user_message_multi_block_joined(adapter, tmp_path):
    path = _write_rollout(tmp_path, [_user_msg(["第一块", "第二块"])])
    turns = adapter.parse(path)
    assert len(turns) == 1
    assert turns[0].content == "第一块\n第二块"


def test_message_blocks_filtered_by_type(adapter, tmp_path):
    """user 只拼 input_text、assistant 只拼 output_text，其他块类型忽略。"""
    path = _write_rollout(
        tmp_path,
        [
            _envelope(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "file:///x.png"},
                        {"type": "input_text", "text": "看图说话"},
                    ],
                }
            ),
            _assistant_msg(["第一段", "第二段"]),
        ],
    )
    turns = adapter.parse(path)
    assert turns[0].content == "看图说话"
    assert turns[1].content == "第一段\n第二段"


def test_assistant_commentary_and_final_both_kept(adapter, tmp_path):
    """phase 为 commentary / final_answer 的 assistant message 都收，各成一个 Turn。"""
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("改个文件"),
            _assistant_msg("先看一下现状。", phase="commentary"),
            _assistant_msg("改完了。", phase="final_answer"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [
        ("user", "改个文件"),
        ("assistant", "先看一下现状。"),
        ("assistant", "改完了。"),
    ]


# ---------------------------------------------------------------- 轮次编号


def test_turn_numbering(adapter, tmp_path):
    """真实 user 消息出现即开新轮（0 起），后续 assistant/tool 归该轮。"""
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("第一轮"),
            _assistant_msg("回复一"),
            _function_call("call_1", "shell_command", '{"command":"ls"}'),
            _function_call_output("call_1", "a.txt"),
            _user_msg("第二轮"),
            _assistant_msg("回复二"),
            _user_msg("第三轮"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("assistant", 0),
        ("tool", 0),
        ("user", 1),
        ("assistant", 1),
        ("user", 2),
    ]


def test_turn_numbering_env_context_between_turns(adapter, tmp_path):
    """轮间插入的环境注入消息（如 /compact 后重放）不抢轮次编号。"""
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("第一轮"),
            _assistant_msg("回复一"),
            _user_msg("<environment_context>\n  <cwd>D:\\proj</cwd>\n</environment_context>"),
            _user_msg("第二轮"),
            _assistant_msg("回复二"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("assistant", 0),
        ("user", 1),
        ("assistant", 1),
    ]


# ---------------------------------------------------------------- tool 调用 / 结果配对


def test_function_call_output_pairing(adapter, tmp_path):
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("跑个命令"),
            _function_call("call_1", "shell_command", '{"command":"ls"}'),
            _function_call_output("call_1", "a.txt\nb.txt"),
        ],
    )
    tool = adapter.parse(path)[1]
    assert tool.tool_name == "shell_command"
    assert tool.content == '{"command":"ls"}\n→ a.txt\nb.txt'


def test_custom_tool_call_uses_input_field(adapter, tmp_path):
    """custom_tool_call 的参数字段名是 input 而非 arguments，结果仍叫 output。"""
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("打个补丁"),
            _custom_tool_call("call_2", "apply_patch", "*** Begin Patch\n*** End Patch"),
            _custom_tool_call_output("call_2", "Success."),
        ],
    )
    tool = adapter.parse(path)[1]
    assert tool.tool_name == "apply_patch"
    assert tool.content == "*** Begin Patch\n*** End Patch\n→ Success."


def test_tool_output_unpaired_ignored(adapter, tmp_path):
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("hi"),
            _function_call_output("call_orphan", "孤儿结果"),  # 没有对应 call
            _function_call("call_1", "shell_command", '{"command":"ls"}'),
            _custom_tool_call_output("call_2", "另一个孤儿"),
        ],
    )
    turns = adapter.parse(path)
    assert len(turns) == 2
    assert "孤儿" not in turns[1].content


def test_tool_output_truncated(adapter, tmp_path):
    long_output = "x" * (TOOL_OUTPUT_MAX_CHARS + 500)
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("大输出"),
            _function_call("call_1", "shell_command", '{"command":"big"}'),
            _function_call_output("call_1", long_output),
        ],
    )
    tool = adapter.parse(path)[1]
    assert "截断" in tool.content
    assert str(TOOL_OUTPUT_MAX_CHARS + 500) in tool.content  # 标注原长
    output_part = tool.content.split("\n→ ")[1]
    assert len(output_part) < TOOL_OUTPUT_MAX_CHARS + 50


def test_tool_arguments_non_string_stringified(adapter, tmp_path):
    """防御：arguments 不是字符串时 JSON 化兜底（实测应为 JSON 字符串原样存）。"""
    path = _write_rollout(
        tmp_path,
        [
            _user_msg("hi"),
            _envelope(
                {
                    "type": "function_call",
                    "name": "shell_command",
                    "arguments": {"command": "ls"},
                    "call_id": "call_1",
                }
            ),
        ],
    )
    tool = adapter.parse(path)[1]
    assert tool.content == '{"command": "ls"}'


# ---------------------------------------------------------------- 首个 user 消息之前的防御


def test_assistant_tool_before_first_user_skipped(adapter, tmp_path):
    """首个真实 user 消息之前的 assistant/tool 记录防御性跳过（正常文件不会出现）。"""
    path = _write_rollout(
        tmp_path,
        [
            _assistant_msg("抢先的回复"),
            _function_call("call_1", "shell_command", '{"command":"ls"}'),
            _user_msg("第一条用户消息"),
            _assistant_msg("正常回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("assistant", 0),
    ]


# ---------------------------------------------------------------- 文件不存在


def test_missing_file_raises(adapter, tmp_path):
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path / "rollout-x.jsonl")


def test_directory_path_raises(adapter, tmp_path):
    """不是文件的路径（目录）同样按 FileNotFoundError 处理。"""
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path)
