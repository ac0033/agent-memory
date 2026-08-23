"""short_term/deepseek_harness.py 测试：dsh session.jsonl(.zstd) 解析。

夹具按实测格式（version 0）在 tmp_path 里造合成日志：首行 session 头，
事件行信封 {'type','seq','time','data'}；zstd 变体用 ZstdCompressor 现场
压缩（可多帧拼接模拟真实文件），明文变体直接写 session.jsonl。
"""

import json

import pytest
import zstandard

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS
from agent_memory.short_term.deepseek_harness import DeepSeekHarnessAdapter


def _dumps(rec) -> str:
    return json.dumps(rec, ensure_ascii=False) if isinstance(rec, dict) else rec


def _write_plain(tmp_path, records, name="session.jsonl", trailing_newline=True):
    """把记录列表写成明文 JSONL；元素可以是 dict（序列化）或 str（原样写行）。"""
    path = tmp_path / name
    text = "\n".join(_dumps(r) for r in records)
    if trailing_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _write_zstd(tmp_path, frames, name="session.jsonl.zstd", tail_garbage=b""):
    """frames 是记录列表的列表：每个子列表压成独立一帧，首尾相接拼成多帧
    容器；tail_garbage 原样追加在末尾（模拟 write-behind 撕裂尾部）。"""
    compressor = zstandard.ZstdCompressor()
    blob = b"".join(
        compressor.compress(("\n".join(_dumps(r) for r in frame) + "\n").encode("utf-8"))
        for frame in frames
    )
    path = tmp_path / name
    path.write_bytes(blob + tail_garbage)
    return path


def _session_header(version=0):
    return {"type": "session", "version": version, "id": "s-1", "cwd": "D:/x"}


def _turn_marker(kind, turn, time=None):
    rec = {"type": f"turn/{kind}", "seq": 0, "data": {"turn": turn}}
    if time is not None:
        rec["time"] = time
    return rec


def _user_msg(text, kind="user", time=None, turn=None):
    blocks = [{"type": "text", "text": t} for t in ([text] if isinstance(text, str) else text)]
    data = {"content": blocks, "source": {"kind": kind}, "role": "user"}
    if turn is not None:
        data["turn"] = turn
    rec = {"type": "user/message", "seq": 0, "data": data}
    if time is not None:
        rec["time"] = time
    return rec


def _assistant_msg(turn, step, content, time=None):
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    rec = {
        "type": "assistant/message",
        "seq": 0,
        "data": {"turn": turn, "step": step, "message": {"role": "assistant", "content": content}},
    }
    if time is not None:
        rec["time"] = time
    return rec


def _tool_call(turn, step, call_id, name, arguments, time=None):
    rec = {
        "type": "tool/call",
        "seq": 0,
        "data": {
            "turn": turn,
            "step": step,
            "callId": call_id,
            "name": name,
            "arguments": arguments,
        },
    }
    if time is not None:
        rec["time"] = time
    return rec


def _tool_result(turn, step, call_id, output, is_error=False, time=None):
    texts = [output] if isinstance(output, str) else output
    rec = {
        "type": "tool/result",
        "seq": 0,
        "data": {
            "turn": turn,
            "step": step,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": call_id,
                        "content": [{"type": "text", "text": t} for t in texts],
                        "isError": is_error,
                    }
                ],
            },
        },
    }
    if time is not None:
        rec["time"] = time
    return rec


@pytest.fixture
def adapter():
    return DeepSeekHarnessAdapter()


# ---------------------------------------------------------------- 正常三类角色往返


def test_normal_roundtrip(adapter, tmp_path):
    path = _write_zstd(
        tmp_path,
        [
            [
                _session_header(),
                _turn_marker("start", 1, time=1787313691619),
                _user_msg("查一下天气", time=1787313691792),
                _assistant_msg(1, 1, "我查一下。", time=1787313692000),
                _tool_call(1, 1, "call_00_abc", "web_search", '{"query": "北京天气"}'),
                _tool_result(1, 1, "call_00_abc", "晴，25 度"),
                _assistant_msg(1, 2, "北京今天晴，25 度。"),
                _turn_marker("end", 1),
            ]
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
    assert turns[0].ts == 1787313691792
    tool = turns[2]
    assert tool.tool_name == "web_search"
    assert tool.content.startswith('{"query": "北京天气"}')
    assert "\n→ 晴，25 度" in tool.content


def test_plain_jsonl_variant(adapter, tmp_path):
    """明文 session.jsonl 变体（不压缩）同样可解析。"""
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _turn_marker("start", 1),
            _user_msg("你好"),
            _assistant_msg(1, 1, "你好！"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [("user", "你好"), ("assistant", "你好！")]


# ---------------------------------------------------------------- 噪音与坏行


def test_noise_records_skipped(adapter, tmp_path):
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            {"type": "request/header", "seq": 1, "data": {}},
            {"type": "reasoning-chunks", "seq": 2, "data": {"chunks": ["想……"]}},
            {"type": "text-chunks", "seq": 3, "data": {"chunks": ["流式增量"]}},
            {"type": "tool-call-chunks", "seq": 4, "data": {}},
            {"type": "assistant/chunk", "seq": 5, "data": {"text": "增量"}},
            {"type": "step/start", "seq": 6, "data": {"turn": 1, "step": 1}},
            {"type": "step/end", "seq": 7, "data": {"turn": 1, "step": 1}},
            {"type": "approval/asked", "seq": 8, "data": {}},
            {"type": "approval/decided", "seq": 9, "data": {}},
            {"type": "todo/write", "seq": 10, "data": {}},
            {"type": "session/title", "seq": 11, "data": {"title": "标题"}},
            _turn_marker("start", 1),
            _user_msg("你好"),
            _assistant_msg(1, 1, "你好！"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [("user", "你好"), ("assistant", "你好！")]


def test_bad_lines_skipped(adapter, tmp_path):
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _turn_marker("start", 1),
            "{这不是合法 JSON",
            "",
            "   ",
            json.dumps(["不是", "对象", "的", "JSON"]),
            json.dumps({"type": "user/message"}),  # 缺 data
            json.dumps({"type": "assistant/message", "data": {"turn": 1}}),  # 缺 message
            _user_msg("有效消息"),
            _assistant_msg(1, 1, "有效回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [("user", "有效消息"), ("assistant", "有效回复")]


def test_torn_tail_tolerated(adapter, tmp_path):
    """zstd 撕裂尾部（write-behind 残留不完整帧）：已解出的完整帧照常解析。"""
    compressor = zstandard.ZstdCompressor()
    frame1 = compressor.compress((_dumps(_session_header()) + "\n").encode("utf-8"))
    frame2 = compressor.compress(
        ("\n".join([_dumps(_turn_marker("start", 1)), _dumps(_user_msg("还在"))]) + "\n").encode(
            "utf-8"
        )
    )
    frame3 = compressor.compress((_dumps(_assistant_msg(1, 1, "丢了")) + "\n").encode("utf-8"))
    path = tmp_path / "session.jsonl.zstd"
    path.write_bytes(frame1 + frame2 + frame3[: len(frame3) // 2])  # 第三帧只剩一半
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [("user", "还在")]


def test_plain_last_line_without_newline(adapter, tmp_path):
    """明文变体末行无换行（write-behind）也能解析。"""
    path = _write_plain(
        tmp_path,
        [_session_header(), _turn_marker("start", 1), _user_msg("末行")],
        trailing_newline=False,
    )
    turns = adapter.parse(path)
    assert [t.content for t in turns] == ["末行"]


# ---------------------------------------------------------------- 版本 fail-closed


def test_version_not_zero_raises(adapter, tmp_path):
    path = _write_plain(tmp_path, [_session_header(version=1), _user_msg("hi")])
    with pytest.raises(ValueError, match="version"):
        adapter.parse(path)


def test_version_missing_raises(adapter, tmp_path):
    """session 头缺 version 字段同样 fail-closed（不等于 0）。"""
    path = _write_plain(tmp_path, [{"type": "session", "id": "s-1"}, _user_msg("hi")])
    with pytest.raises(ValueError):
        adapter.parse(path)


# ---------------------------------------------------------------- 用户消息来源过滤


def test_synthetic_user_messages_dropped(adapter, tmp_path):
    """plugin / skill-catalog / agent-instructions 等合成上下文不是真人输入。"""
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _turn_marker("start", 1),
            _user_msg("真人输入", kind="user"),
            _user_msg("runtime context snapshot ...", kind="plugin"),
            _user_msg("skill catalog ...", kind="skill-catalog"),
            _user_msg("agent instructions ...", kind="agent-instructions"),
            {"type": "user/message", "seq": 9,
             "data": {"content": [{"type": "text", "text": "无来源"}]}},
            _assistant_msg(1, 1, "回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.content) for t in turns] == [("user", "真人输入"), ("assistant", "回复")]


def test_user_message_multi_block_joined(adapter, tmp_path):
    path = _write_plain(
        tmp_path,
        [_session_header(), _turn_marker("start", 1), _user_msg(["第一块", "第二块"], time=1000)],
    )
    turns = adapter.parse(path)
    assert len(turns) == 1
    assert turns[0].content == "第一块\n第二块"
    assert turns[0].ts == 1000


# ---------------------------------------------------------------- assistant 块聚合


def test_assistant_reasoning_skipped_tool_call_not_expanded(adapter, tmp_path):
    """reasoning 块跳过；assistant/message 里的 tool-call 块不展开（tool/call
    事件是权威记录，展开会双计）。"""
    content = [
        {"type": "reasoning", "text": "内部推理不该出现"},
        {"type": "text", "text": "先看一眼目录。"},
        {
            "type": "tool-call",
            "id": "call_00_x",
            "name": "bash",
            "arguments": '{"cmd": "ls"}',
        },
    ]
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _turn_marker("start", 1),
            _user_msg("看看目录"),
            _assistant_msg(1, 1, content),
            _tool_call(1, 1, "call_00_x", "bash", '{"cmd": "ls"}'),
            _tool_result(1, 1, "call_00_x", "a.txt"),
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.tool_name) for t in turns] == [
        ("user", None),
        ("assistant", None),
        ("tool", "bash"),
    ]
    assert turns[1].content == "先看一眼目录。"
    assert "内部推理" not in turns[1].content
    assert "ls" not in turns[1].content  # tool-call 块没有混进 assistant 正文


def test_assistant_multi_text_block_joined(adapter, tmp_path):
    content = [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}]
    path = _write_plain(
        tmp_path,
        [_session_header(), _turn_marker("start", 1), _assistant_msg(1, 1, content)],
    )
    assert adapter.parse(path)[0].content == "第一段\n第二段"


def test_assistant_empty_text_skipped(adapter, tmp_path):
    """只有 reasoning 块的 assistant/message 不产出空 Turn。"""
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _turn_marker("start", 1),
            _assistant_msg(1, 1, [{"type": "reasoning", "text": "只在想"}]),
            _assistant_msg(1, 2, "最终回复"),
        ],
    )
    turns = adapter.parse(path)
    assert [t.content for t in turns] == ["最终回复"]


# ---------------------------------------------------------------- 轮次编号


def test_turn_numbering_one_based_cursor(adapter, tmp_path):
    """turn/start 从 1 开始，输出 0 起；user/message 不带 turn 用游标值。"""
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _turn_marker("start", 1),
            _user_msg("第一轮"),
            _assistant_msg(1, 1, "回复一"),
            _turn_marker("end", 1),
            _turn_marker("start", 2),
            _user_msg("第二轮"),
            _assistant_msg(2, 1, "回复二"),
            _turn_marker("end", 2),
            _turn_marker("start", 3),
            _user_msg("第三轮"),
            _assistant_msg(3, 1, "回复三"),
            # 崩溃会话末尾缺 turn/end 属正常
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


def test_user_message_before_any_turn_start(adapter, tmp_path):
    """防御：turn/start 缺失时游标默认第 1 轮，turn_index 归 0。"""
    path = _write_plain(tmp_path, [_session_header(), _user_msg("没有 turn/start")])
    turns = adapter.parse(path)
    assert turns[0].turn_index == 0


# ---------------------------------------------------------------- tool/call 与 tool/result 配对


def test_tool_arguments_kept_raw_string(adapter, tmp_path):
    """arguments 是未解析 JSON 字符串，原样存（不重新序列化）。"""
    raw_args = '{"command": "ls",  "weird spacing": true}'
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _tool_call(1, 1, "c1", "pwsh", raw_args),
        ],
    )
    tool = adapter.parse(path)[0]
    assert tool.content == raw_args


def test_tool_result_unpaired_ignored(adapter, tmp_path):
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _turn_marker("start", 1),
            _user_msg("hi"),
            _tool_result(1, 1, "call-not-exist", "孤儿结果"),
            _tool_call(1, 1, "c1", "bash", "{}"),
            _tool_result(1, 1, "c2", "另一个孤儿"),
        ],
    )
    turns = adapter.parse(path)
    assert len(turns) == 2
    assert "孤儿" not in turns[1].content


def test_tool_result_multi_block_and_error_flag(adapter, tmp_path):
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _tool_call(1, 1, "c1", "bash", "{}"),
            _tool_result(1, 1, "c1", ["第一行", "第二行"], is_error=True),
        ],
    )
    tool = adapter.parse(path)[0]
    assert "\n→ [错误] 第一行\n第二行" in tool.content


def test_tool_result_output_truncated(adapter, tmp_path):
    long_output = "x" * (TOOL_OUTPUT_MAX_CHARS + 500)
    path = _write_plain(
        tmp_path,
        [
            _session_header(),
            _tool_call(1, 1, "c1", "bash", "{}"),
            _tool_result(1, 1, "c1", long_output),
        ],
    )
    tool = adapter.parse(path)[0]
    assert "截断" in tool.content
    assert str(TOOL_OUTPUT_MAX_CHARS + 500) in tool.content  # 标注原长
    assert len(tool.content) < TOOL_OUTPUT_MAX_CHARS + 200


def test_tool_call_non_string_arguments_stringified(adapter, tmp_path):
    """防御：arguments 不是字符串时按 JSON 序列化。"""
    path = _write_plain(
        tmp_path,
        [_session_header(), _tool_call(1, 1, "c1", "bash", {"cmd": "ls"})],
    )
    tool = adapter.parse(path)[0]
    assert json.loads(tool.content) == {"cmd": "ls"}


# ---------------------------------------------------------------- 多帧 zstd


def test_multi_frame_zstd(adapter, tmp_path):
    """多帧容器：每帧独立压缩再拼接，一次流式读全（decompressobj 只解第一帧）。"""
    path = _write_zstd(
        tmp_path,
        [
            [_session_header(), _turn_marker("start", 1), _user_msg("第一帧的用户消息")],
            [_assistant_msg(1, 1, "第二帧的回复"), _turn_marker("end", 1)],
            [_turn_marker("start", 2), _user_msg("第三帧的用户消息")],
        ],
    )
    turns = adapter.parse(path)
    assert [(t.role, t.turn_index) for t in turns] == [
        ("user", 0),
        ("assistant", 0),
        ("user", 1),
    ]


# ---------------------------------------------------------------- 文件不存在


def test_missing_file_raises(adapter, tmp_path):
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path / "session.jsonl.zstd")


def test_directory_path_raises(adapter, tmp_path):
    """不是文件的路径（目录）同样按 FileNotFoundError 处理。"""
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path)
