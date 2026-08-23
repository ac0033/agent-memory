"""OpencodeAdapter 的测试：全部用 tmp_path 里 sqlite3 现场建库造夹具。

覆盖：正常三类角色往返、多轮编号、噪音 part 跳过、坏行容忍、
工具调用 completed/error、ANSI 清洗、超长截断、多会话选择
（最新根会话、子会话不参与）、schema 漂移降级、文件不存在报错。
"""

import json
import sqlite3

import pytest

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS
from agent_memory.short_term.opencode import OpencodeAdapter


def _create_db(
    path,
    sessions: list[dict],
    messages: list[dict],
    parts: list[dict],
) -> None:
    """现场建一个最小 opencode.db：三表只建适配器用到的列。

    sessions/messages/parts 的每条 dict 里 data 字段可以是 dict（自动
    序列化）或 str（原样写入，用来造坏行）。
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT,"
        " time_created INTEGER, time_updated INTEGER)"
    )
    conn.execute(
        "CREATE TABLE message (id TEXT, session_id TEXT,"
        " time_created INTEGER, data TEXT)"
    )
    conn.execute(
        "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
        " time_created INTEGER, data TEXT)"
    )
    for s in sessions:
        conn.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            (
                s["id"],
                s.get("parent_id"),
                s.get("directory", "/tmp/proj"),
                s.get("time_created", 0),
                s.get("time_updated", 0),
            ),
        )
    for m in messages:
        data = m["data"]
        conn.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            (
                m["id"],
                m["session_id"],
                m.get("time_created", 0),
                json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else data,
            ),
        )
    for p in parts:
        data = p["data"]
        conn.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            (
                p["id"],
                p["message_id"],
                p["session_id"],
                p.get("time_created", 0),
                json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else data,
            ),
        )
    conn.commit()
    conn.close()


def _msg(mid, sid, created, role, parent_id=None):
    data = {"role": role}
    if parent_id is not None:
        data["parentID"] = parent_id
    return {"id": mid, "session_id": sid, "time_created": created, "data": data}


def _part(pid, mid, sid, created, data):
    return {
        "id": pid,
        "message_id": mid,
        "session_id": sid,
        "time_created": created,
        "data": data,
    }


@pytest.fixture
def adapter():
    return OpencodeAdapter()


def test_normal_three_roles_roundtrip(tmp_path, adapter):
    """user → assistant → tool(completed) 一轮完整往返。"""
    db = tmp_path / "opencode.db"
    _create_db(
        db,
        sessions=[{"id": "s1", "time_updated": 100}],
        messages=[
            _msg("m1", "s1", 1000, "user"),
            _msg("m2", "s1", 2000, "assistant", parent_id="m1"),
        ],
        parts=[
            _part("p1", "m1", "s1", 1001, {"type": "text", "text": "帮我看下日志"}),
            _part("p2", "m2", "s1", 2001, {"type": "text", "text": "好的，我查一下"}),
            _part(
                "p3",
                "m2",
                "s1",
                2002,
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {"command": "tail -5 app.log"},
                        "output": "line1\nline2",
                    },
                },
            ),
        ],
    )
    turns = adapter.parse(db)
    assert [t.role for t in turns] == ["user", "assistant", "tool"]
    assert all(t.turn_index == 0 for t in turns)
    assert turns[0].content == "帮我看下日志"
    assert turns[0].ts == 1001
    assert turns[1].content == "好的，我查一下"
    tool = turns[2]
    assert tool.tool_name == "bash"
    assert json.loads(tool.content.split("\n→ ")[0]) == {"command": "tail -5 app.log"}
    assert tool.content.endswith("\n→ line1\nline2")


def test_turn_index_follows_user_message_order(tmp_path, adapter):
    """turn_index = user message 按 time_created 排序的序号（0 起）。"""
    db = tmp_path / "opencode.db"
    _create_db(
        db,
        sessions=[{"id": "s1", "time_updated": 100}],
        messages=[
            _msg("m1", "s1", 1000, "user"),
            _msg("m2", "s1", 2000, "assistant", parent_id="m1"),
            _msg("m3", "s1", 3000, "user"),
            _msg("m4", "s1", 4000, "assistant", parent_id="m3"),
        ],
        parts=[
            _part("p1", "m1", "s1", 1001, {"type": "text", "text": "第一轮"}),
            _part("p2", "m2", "s1", 2001, {"type": "text", "text": "回复一"}),
            _part("p3", "m3", "s1", 3001, {"type": "text", "text": "第二轮"}),
            _part("p4", "m4", "s1", 4001, {"type": "text", "text": "回复二"}),
        ],
    )
    turns = adapter.parse(db)
    assert [(t.turn_index, t.role) for t in turns] == [
        (0, "user"),
        (0, "assistant"),
        (1, "user"),
        (1, "assistant"),
    ]


def test_noise_part_types_skipped(tmp_path, adapter):
    """reasoning / step-start / step-finish / patch / file 全部跳过。"""
    db = tmp_path / "opencode.db"
    _create_db(
        db,
        sessions=[{"id": "s1", "time_updated": 100}],
        messages=[_msg("m1", "s1", 1000, "user")],
        parts=[
            _part("p0", "m1", "s1", 999, {"type": "step-start"}),
            _part("p1", "m1", "s1", 1001, {"type": "text", "text": "正文"}),
            _part("p2", "m1", "s1", 1002, {"type": "reasoning", "text": "内心活动"}),
            _part("p3", "m1", "s1", 1003, {"type": "step-finish"}),
            _part("p4", "m1", "s1", 1004, {"type": "patch", "files": []}),
            _part("p5", "m1", "s1", 1005, {"type": "file", "path": "a.py"}),
        ],
    )
    turns = adapter.parse(db)
    assert len(turns) == 1
    assert turns[0].content == "正文"


def test_bad_rows_tolerated(tmp_path, adapter):
    """坏 JSON 的 message / part 跳过，不拖垮整体解析。"""
    db = tmp_path / "opencode.db"
    _create_db(
        db,
        sessions=[{"id": "s1", "time_updated": 100}],
        messages=[
            {"id": "bad", "session_id": "s1", "time_created": 500, "data": "{oops"},
            _msg("m1", "s1", 1000, "user"),
            {"id": "m2", "session_id": "s1", "time_created": 2000, "data": "[1,2]"},
        ],
        parts=[
            _part("p1", "m1", "s1", 1001, {"type": "text", "text": "好记录"}),
            {"id": "p2", "message_id": "m1", "session_id": "s1",
             "time_created": 1002, "data": "not json at all"},
            _part("p3", "bad", "s1", 1003, {"type": "text", "text": "孤儿 part"}),
        ],
    )
    turns = adapter.parse(db)
    assert len(turns) == 1
    assert turns[0].content == "好记录"


def test_tool_error_status(tmp_path, adapter):
    """state.status=='error' 时取 state.error 追加。"""
    db = tmp_path / "opencode.db"
    _create_db(
        db,
        sessions=[{"id": "s1", "time_updated": 100}],
        messages=[
            _msg("m1", "s1", 1000, "user"),
            _msg("m2", "s1", 2000, "assistant", parent_id="m1"),
        ],
        parts=[
            _part("p1", "m1", "s1", 1001, {"type": "text", "text": "跑一下"}),
            _part(
                "p2",
                "m2",
                "s1",
                2001,
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "error",
                        "input": {"command": "boom"},
                        "error": "Tool execution aborted",
                    },
                },
            ),
        ],
    )
    turns = adapter.parse(db)
    assert len(turns) == 2
    assert turns[1].role == "tool"
    assert turns[1].content.endswith("\n→ Tool execution aborted")


def test_tool_output_ansi_stripped_and_truncated(tmp_path, adapter):
    """bash 输出 ANSI 转义清洗；超长截断并标注原长。"""
    db = tmp_path / "opencode.db"
    long_output = "[32m" + "x" * (TOOL_OUTPUT_MAX_CHARS + 100) + "[0m"
    _create_db(
        db,
        sessions=[{"id": "s1", "time_updated": 100}],
        messages=[
            _msg("m1", "s1", 1000, "user"),
            _msg("m2", "s1", 2000, "assistant", parent_id="m1"),
        ],
        parts=[
            _part("p1", "m1", "s1", 1001, {"type": "text", "text": "查"}),
            _part(
                "p2",
                "m2",
                "s1",
                2001,
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {
                        "status": "completed",
                        "input": {},
                        "output": long_output,
                    },
                },
            ),
        ],
    )
    turns = adapter.parse(db)
    body = turns[1].content.split("\n→ ", 1)[1]
    assert "" not in body  # ANSI 已清洗
    assert body.endswith(f"...[截断，原长 {len(long_output) - 9} 字符]")
    # 截断发生在清洗之后：原长 = 去掉两段转义序列（\x1b[32m 为 5 字符、\x1b[0m 为 4 字符）后的长度
    assert body[:TOOL_OUTPUT_MAX_CHARS] == "x" * TOOL_OUTPUT_MAX_CHARS


def test_session_selection_latest_root(tmp_path, adapter):
    """多会话：取 parent_id IS NULL 中 time_updated 最新者；子会话不参与。"""
    db = tmp_path / "opencode.db"
    _create_db(
        db,
        sessions=[
            {"id": "old", "time_updated": 100},
            {"id": "new", "time_updated": 200},
            # 子会话即使 time_updated 最新也不选
            {"id": "child", "parent_id": "new", "time_updated": 999},
        ],
        messages=[
            _msg("m1", "old", 1000, "user"),
            _msg("m2", "new", 1000, "user"),
            _msg("m3", "child", 1000, "user"),
        ],
        parts=[
            _part("p1", "m1", "old", 1001, {"type": "text", "text": "旧会话"}),
            _part("p2", "m2", "new", 1001, {"type": "text", "text": "新会话"}),
            _part("p3", "m3", "child", 1001, {"type": "text", "text": "子会话"}),
        ],
    )
    turns = adapter.parse(db)
    assert [t.content for t in turns] == ["新会话"]


def test_schema_drift_degrades_to_empty(tmp_path, adapter):
    """schema 漂移（缺 part 表 / 缺列）时降级返回空，不抛异常。"""
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE session (id TEXT, parent_id TEXT, time_updated INTEGER)"
    )
    conn.execute("INSERT INTO session VALUES ('s1', NULL, 100)")
    # 没有 message / part 表
    conn.commit()
    conn.close()
    assert adapter.parse(db) == []


def test_file_not_found(tmp_path, adapter):
    with pytest.raises(FileNotFoundError):
        adapter.parse(tmp_path / "opencode.db")
