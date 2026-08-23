"""opencode 会话库适配器（opencode.db，SQLite WAL，只读打开）。

格式要点（本机 v1.18.9 实测：31 会话 / 1182 消息 / 4843 part）：
- opencode 没有 JSONL，全部会话存在单个 SQLite 库
  ~/.local/share/opencode/opencode.db（WAL 模式）。WAL 被运行中的
  opencode 持有，必须以 mode=ro 只读打开，读写打开有损坏风险；
- 表结构：session(id, parent_id, directory, time_created, time_updated, ...)；
  message(id, session_id, time_created, data-TEXT-JSON)；
  part(id, message_id, session_id, time_created, data-TEXT-JSON)。
  schema 会漂移，查询只 pin 需要的列，失败降级返回空而不是崩；
- 一个库里有多个会话（含 parent_id 非空的子会话），默认解析
  parent_id IS NULL 中 time_updated 最新的那个会话；
- part.data['type'] 判别：text 是正文（角色取所属 message 的
  data.role）；tool 是工具调用与结果同体（state.status==completed 取
  state.output，error 取 state.error，无需配对）；reasoning /
  step-start / step-finish / patch / file 全是噪音，跳过；
- 轮次边界：每条 role=='user' 的 message 开新一轮，turn_index 是
  user message 在会话内按 time_created 排序的序号（0 起）；同一轮内
  assistant message 的 data.parentID 指向该轮 user message id；
- bash 工具输出可能带 ANSI 转义（\\x1b[...m），入库前正则清洗。

防御性：单条 message/part 的 JSON 解析失败跳过，不拖垮整体；查询
因 schema 漂移失败时降级返回已解析部分（或空）；文件不存在或不是
文件抛 FileNotFoundError。
"""

import json
import re
import sqlite3
from pathlib import Path

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS, Turn

# bash 等工具输出里的 ANSI 颜色/样式转义序列
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _loads(raw: object) -> dict | None:
    """data 列是 TEXT-JSON；解析失败或非对象返回 None（坏记录跳过）。"""
    if not isinstance(raw, str):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _stringify(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _truncate(text: str) -> str:
    if len(text) > TOOL_OUTPUT_MAX_CHARS:
        return text[:TOOL_OUTPUT_MAX_CHARS] + f"...[截断，原长 {len(text)} 字符]"
    return text


class OpencodeAdapter:
    """opencode opencode.db（v1.18.9 实测）适配器。

    解析规则见模块 docstring。parse(path) 收 .db 路径，只读打开；
    会话选择语义：parent_id IS NULL 中 time_updated 最新的会话。
    tool part 产出一个 Turn(role="tool")，调用与结果同体无需配对，
    结果截断到 TOOL_OUTPUT_MAX_CHARS。
    """

    name = "opencode"

    def parse(self, path: Path) -> list[Turn]:
        if not path.is_file():
            raise FileNotFoundError(f"会话日志不存在或不是文件: {path}")
        # WAL 被运行中的 opencode 持有，必须只读打开
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return self._parse_conn(conn)
        finally:
            conn.close()

    def _parse_conn(self, conn: sqlite3.Connection) -> list[Turn]:
        session_id = self._pick_session(conn)
        if session_id is None:
            return []

        # message：只 pin 需要的列；schema 漂移导致查询失败时降级为空
        try:
            msg_rows = conn.execute(
                "SELECT id, time_created, data FROM message"
                " WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            return []

        msg_role: dict[str, str] = {}  # message id -> role
        msg_parent: dict[str, str] = {}  # assistant message id -> parentID
        user_order: list[str] = []  # user message id 按 time_created 排序
        for msg_id, _created, raw in msg_rows:
            if not isinstance(msg_id, str):
                continue
            data = _loads(raw)
            if data is None:
                continue  # 坏记录跳过
            role = data.get("role")
            if not isinstance(role, str):
                continue
            msg_role[msg_id] = role
            if role == "user":
                user_order.append(msg_id)
            else:
                parent = data.get("parentID")
                if isinstance(parent, str):
                    msg_parent[msg_id] = parent

        # turn_index = user message 在会话内按 time_created 排序的序号（0 起）
        msg_turn: dict[str, int] = {
            msg_id: i for i, msg_id in enumerate(user_order)
        }
        for msg_id, parent in msg_parent.items():
            if parent in msg_turn:
                msg_turn[msg_id] = msg_turn[parent]

        # part：按 time_created 排序逐条判别
        try:
            part_rows = conn.execute(
                "SELECT message_id, time_created, data FROM part"
                " WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            ).fetchall()
        except sqlite3.Error:
            return []  # 没有 part 表也能返回空而不是崩

        turns: list[Turn] = []
        for msg_id, created, raw in part_rows:
            if not isinstance(msg_id, str):
                continue
            turn_index = msg_turn.get(msg_id)
            role = msg_role.get(msg_id)
            if turn_index is None or role is None:
                continue  # 属于已跳过的坏 message 或未知 message
            data = _loads(raw)
            if data is None:
                continue  # 坏记录跳过
            ts = int(created) if isinstance(created, (int, float)) else None
            part_type = data.get("type")

            if part_type == "text":
                text = data.get("text")
                if not isinstance(text, str) or not text:
                    continue
                if role not in ("user", "assistant"):
                    continue
                turns.append(
                    Turn(
                        turn_index=turn_index,
                        role=role,  # type: ignore[arg-type]
                        content=text,
                        ts=ts,
                    )
                )
            elif part_type == "tool":
                turn = self._parse_tool_part(data, turn_index, ts)
                if turn is not None:
                    turns.append(turn)
            # reasoning / step-start / step-finish / patch / file 等：噪音，跳过

        return turns

    def _pick_session(self, conn: sqlite3.Connection) -> str | None:
        """默认会话：parent_id IS NULL 中 time_updated 最新者；查询失败降级 None。"""
        try:
            row = conn.execute(
                "SELECT id FROM session WHERE parent_id IS NULL"
                " ORDER BY time_updated DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None or not isinstance(row[0], str):
            return None
        return row[0]

    def _parse_tool_part(
        self, data: dict, turn_index: int, ts: int | None
    ) -> Turn | None:
        """tool part：调用与结果同体。content = 入参 JSON + 结果（若有）。"""
        tool_name = data.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return None
        state = data.get("state")
        state = state if isinstance(state, dict) else {}
        content = _stringify(state.get("input") if state.get("input") is not None else {})
        status = state.get("status")
        if status == "completed" and state.get("output") is not None:
            output = _truncate(_strip_ansi(_stringify(state["output"])))
            content += f"\n→ {output}"
        elif status == "error" and state.get("error") is not None:
            error = _truncate(_strip_ansi(_stringify(state["error"])))
            content += f"\n→ {error}"
        return Turn(
            turn_index=turn_index,
            role="tool",
            tool_name=tool_name,
            content=content,
            ts=ts,
        )
