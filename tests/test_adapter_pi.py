"""PiAdapter（pi-mono coding agent 会话日志）的单元测试。

夹具全部用 tmp_path 合成，覆盖：正常三类角色往返、树结构分支取舍、
噪音记录跳过、坏行容忍、轮次编号、工具调用/结果配对、超长截断、
文件不存在报错。
"""

import json
from pathlib import Path

import pytest

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS
from agent_memory.short_term.pi import PiAdapter

adapter = PiAdapter()


def _write(path: Path, lines: list[object]) -> Path:
    """把若干记录写成 JSONL；str 原样写入（用来造坏行），dict 序列化。"""
    text = "\n".join(
        line if isinstance(line, str) else json.dumps(line, ensure_ascii=False)
        for line in lines
    )
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _session(version: int = 3) -> dict:
    return {"type": "session", "version": version, "id": "session-uuid"}


def _msg(rec_id: str, parent: str | None, message: dict) -> dict:
    return {
        "type": "message",
        "id": rec_id,
        "parentId": parent,
        "timestamp": "2026-08-23T10:00:00.000Z",
        "message": message,
    }


def _user(rec_id: str, parent: str | None, text: str, ts: int = 1784648097858) -> dict:
    return _msg(
        rec_id,
        parent,
        {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "timestamp": ts,
        },
    )


def _assistant(
    rec_id: str, parent: str | None, blocks: list[dict], stop: str = "stop"
) -> dict:
    return _msg(
        rec_id,
        parent,
        {
            "role": "assistant",
            "content": blocks,
            "stopReason": stop,
            "timestamp": 1784648103907,
        },
    )


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": args}


def _tool_result(
    rec_id: str, parent: str | None, call_id: str, text: str, is_error: bool = False
) -> dict:
    return _msg(
        rec_id,
        parent,
        {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": "bash",
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
            "timestamp": 1784648105000,
        },
    )


def test_normal_roundtrip(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            _user("u1", None, "帮我看看这个目录"),
            _assistant("a1", "u1", [{"type": "text", "text": "好的，我先列一下文件"}]),
            _user("u2", "a1", "再详细点"),
            _assistant(
                "a2",
                "u2",
                [
                    {"type": "text", "text": "执行命令"},
                    _tool_call("call-1", "bash", {"command": "ls -la"}),
                ],
            ),
            _tool_result("r1", "a2", "call-1", "total 0"),
        ],
    )
    turns = adapter.parse(path)
    assert [t.role for t in turns] == ["user", "assistant", "user", "assistant", "tool"]
    assert turns[0].turn_index == 0 and turns[0].content == "帮我看看这个目录"
    assert turns[0].ts == 1784648097858
    assert turns[1].turn_index == 0 and turns[1].content == "好的，我先列一下文件"
    assert turns[2].turn_index == 1
    assert turns[3].turn_index == 1
    tool = turns[4]
    assert tool.tool_name == "bash"
    assert json.loads(tool.content.split("\n→ ")[0]) == {"command": "ls -la"}
    assert tool.content.endswith("\n→ total 0")


def test_abandoned_branch_excluded(tmp_path: Path) -> None:
    """同一文件两条分支：只有 leaf 所在的活跃链被解析。"""
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            _user("u1", None, "原始问题"),
            _assistant("a1", "u1", [{"type": "text", "text": "原始回答"}]),
            # 分支 1：从 a1 分出去，后来被放弃
            _user("u2", "a1", "被放弃的追问"),
            _assistant("a2", "u2", [{"type": "text", "text": "被放弃的回答"}]),
            # 分支 2：同样从 a1 分出，文件靠后，是当前活跃分支
            _user("u3", "a1", "活跃分支的追问"),
            _assistant("a3", "u3", [{"type": "text", "text": "活跃分支的回答"}]),
        ],
    )
    turns = adapter.parse(path)
    contents = [t.content for t in turns]
    assert contents == ["原始问题", "原始回答", "活跃分支的追问", "活跃分支的回答"]
    assert not any("被放弃" in c for c in contents)


def test_noise_records_skipped(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            {"type": "model_change", "id": "n1", "parentId": None},
            {"type": "thinking_level_change", "id": "n2", "parentId": "n1"},
            {"type": "label", "id": "n3", "parentId": "n2"},
            _user("u1", "n3", "你好"),
            # 非 message role 的记录也跳过
            _msg("x1", "u1", {"role": "bashExecution", "command": "ls"}),
            _msg("x2", "x1", {"role": "compactionSummary", "summary": "..."}),
            _assistant("a1", "x2", [{"type": "text", "text": "你好！有什么可以帮你？"}]),
            {"type": "compaction", "id": "n4", "parentId": "a1"},
        ],
    )
    turns = adapter.parse(path)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[1].content == "你好！有什么可以帮你？"


def test_bad_lines_tolerated(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            "{not valid json",
            _user("u1", None, "第一问"),
            json.dumps(["不是", "对象", "的", "记录"]),
            "",  # 空行
            _assistant("a1", "u1", [{"type": "text", "text": "第一答"}]),
            '{"type":"message"',  # 截断的半行
        ],
    )
    turns = adapter.parse(path)
    assert [t.content for t in turns] == ["第一问", "第一答"]


def test_turn_numbering(tmp_path: Path) -> None:
    """user 消息开新一轮（0 起），其后的 assistant/tool 同轮。"""
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            _user("u1", None, "问题一"),
            _assistant("a1", "u1", [{"type": "text", "text": "回答一"}]),
            _user("u2", "a1", "问题二"),
            _assistant(
                "a2", "u2", [_tool_call("c1", "read", {"path": "a.py"})]
            ),
            _tool_result("r1", "a2", "c1", "ok"),
            _user("u3", "r1", "问题三"),
        ],
    )
    turns = adapter.parse(path)
    assert [t.turn_index for t in turns] == [0, 0, 1, 1, 2]
    assert [t.role for t in turns] == [
        "user",
        "assistant",
        "user",
        "tool",
        "user",
    ]


def test_tool_call_result_pairing(tmp_path: Path) -> None:
    """多个 toolCall 各自配对；未配对的 toolResult 忽略。"""
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            _user("u1", None, "跑两个命令"),
            _assistant(
                "a1",
                "u1",
                [
                    _tool_call("c1", "bash", {"command": "pwd"}),
                    _tool_call("c2", "bash", {"command": "ls"}),
                ],
            ),
            _tool_result("r1", "a1", "c2", "file.txt"),
            _tool_result("r2", "r1", "c1", "/home"),
            _tool_result("r3", "r2", "c-unknown", "没人认领"),
        ],
    )
    turns = adapter.parse(path)
    tool_turns = [t for t in turns if t.role == "tool"]
    assert len(tool_turns) == 2
    by_cmd = {
        json.loads(t.content.split("\n→ ")[0])["command"]: t for t in tool_turns
    }
    assert by_cmd["pwd"].content.endswith("\n→ /home")
    assert by_cmd["ls"].content.endswith("\n→ file.txt")
    assert all("没人认领" not in t.content for t in turns)


def test_tool_output_truncated(tmp_path: Path) -> None:
    long_output = "x" * (TOOL_OUTPUT_MAX_CHARS + 100)
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            _user("u1", None, "cat 大文件"),
            _assistant("a1", "u1", [_tool_call("c1", "read", {"path": "big.txt"})]),
            _tool_result("r1", "a1", "c1", long_output),
        ],
    )
    (tool_turn,) = [t for t in adapter.parse(path) if t.role == "tool"]
    marker = f"...[截断，原长 {len(long_output)} 字符]"
    assert tool_turn.content.endswith(marker)
    body = tool_turn.content.split("\n→ ")[1]
    assert len(body[: TOOL_OUTPUT_MAX_CHARS]) == TOOL_OUTPUT_MAX_CHARS
    assert body == "x" * TOOL_OUTPUT_MAX_CHARS + marker


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path / "不存在.jsonl")


def test_blocks_aggregation_and_skips(tmp_path: Path) -> None:
    """thinking 块与 image 块跳过；user 纯字符串 content 兼容；多个 text 块拼接。"""
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            _msg(
                "u1",
                None,
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "第一段"},
                        {"type": "image", "url": "data:..."},
                        {"type": "text", "text": "第二段"},
                    ],
                    "timestamp": 1784648097858,
                },
            ),
            _assistant(
                "a1",
                "u1",
                [
                    {"type": "thinking", "thinking": "内部推理不进 transcript"},
                    {"type": "text", "text": "回复一"},
                    {"type": "text", "text": "回复二"},
                ],
            ),
            _msg(
                "u2",
                "a1",
                {"role": "user", "content": "纯字符串的用户消息", "timestamp": 1},
            ),
        ],
    )
    turns = adapter.parse(path)
    assert [t.content for t in turns] == [
        "第一段\n第二段",
        "回复一\n回复二",
        "纯字符串的用户消息",
    ]
    assert not any("内部推理" in t.content for t in turns)


def test_error_aborted_empty_assistant_skipped(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            _user("u1", None, "问一句"),
            # stopReason=error 且 content 为空：整条跳过
            _msg(
                "a1",
                "u1",
                {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "timestamp": 1784648103907,
                },
            ),
            # stopReason=aborted 且 content 为空：同样跳过
            _msg(
                "a2",
                "a1",
                {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "aborted",
                    "timestamp": 1784648104000,
                },
            ),
            # 有内容但 stopReason=error：保留部分内容
            _msg(
                "a3",
                "a2",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "写了一半被中断"}],
                    "stopReason": "error",
                    "timestamp": 1784648105000,
                },
            ),
        ],
    )
    turns = adapter.parse(path)
    assert [t.content for t in turns] == ["问一句", "写了一半被中断"]


def test_tool_result_error_annotated(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(),
            _user("u1", None, "跑个会挂的命令"),
            _assistant("a1", "u1", [_tool_call("c1", "bash", {"command": "boom"})]),
            _tool_result("r1", "a1", "c1", "command not found", is_error=True),
        ],
    )
    (tool_turn,) = [t for t in adapter.parse(path) if t.role == "tool"]
    assert tool_turn.content.endswith("\n→ [错误] command not found")


def test_v1_linear_fallback(tmp_path: Path) -> None:
    """version==1 是线性无 id/parentId，按行序解析。"""
    path = _write(
        tmp_path / "s.jsonl",
        [
            _session(version=1),
            {"type": "message", "message": {"role": "user", "content": "老格式问题"}},
            {
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "老格式回答"}],
                },
            },
        ],
    )
    turns = adapter.parse(path)
    assert [t.content for t in turns] == ["老格式问题", "老格式回答"]
    assert turns[0].ts is None
