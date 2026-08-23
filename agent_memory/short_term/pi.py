"""pi（pi-mono coding agent）会话日志适配器。

格式要点（来源：本地 v3 头记录实测 + 官方文档
packages/coding-agent/docs/session-format.md）：
- 文件：~/.pi/agent/sessions/<cwd 编码>/<ISO 时间戳>_<uuid>.jsonl，
  每行一个 JSON，外层都有 type；除头记录外带 id（8 位 hex）/
  parentId / timestamp（ISO 字符串）。
- 树结构陷阱：会话由 id/parentId 构成树，用户可从中间节点分支，
  文件里可能同时存在多条分支，按行序读会混入被放弃的分支。正确做法
  是从最后一条记录的 id 沿 parentId 走回根（leaf-to-root），反转后
  只解析这条活跃链上的记录。
- 头记录 type=='session' 含 version：v1 是线性无 id/parentId（按行序
  处理），当前 v3 是树。
- type=='message' 的记录按 message.role 判别——
  user：content 是纯字符串或块数组（取 type=='text' 块的 text 拼接，
    image 块跳过），ts 取 message.timestamp（Unix 毫秒整数）；
  assistant：content 块数组——text 块拼进 assistant Turn，thinking
    块跳过，toolCall 块各自产出独立 tool Turn（tool_name=block.name，
    content=block.arguments 的 JSON），按 block.id 登记待配对；
    stopReason 为 error/aborted 且 content 为空数组的整条跳过；
  toolResult：按 message.toolCallId 配对回 tool Turn，正文取 content
    里 text 块的 text 拼接，截断到 TOOL_OUTPUT_MAX_CHARS 后以
    '\\n→ ' 前缀追加，isError 为 true 时加 [错误] 标注；
  其余 role（bashExecution/custom/branchSummary/compactionSummary 等）
  跳过。
- 外层 type 除 'message' 外全跳过（session/model_change/
  thinking_level_change/compaction/branch_summary/custom/
  custom_message/label/session_info）。
- 轮次边界：无显式轮次字段，role=='user' 的消息开新一轮（0 起）。

防御性：单行 JSON 解析失败跳过；缺预期字段的记录跳过；文件不存在或
不是文件抛 FileNotFoundError。
"""

import json
from pathlib import Path

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS, Turn


def _parse_ts(raw: object) -> int | None:
    """message.timestamp 是 Unix 毫秒整数；缺失或非法返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    return None


def _join_text_blocks(content: object) -> str:
    """拼接 content 块数组里 type=='text' 块的 text（多块换行分隔）。

    content 也可能是纯字符串（user 消息的老格式），原样返回；
    其他形态返回空串。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"]
    ]
    return "\n".join(texts)


def _active_chain(records: list[dict]) -> list[dict]:
    """从最后一条记录的 id 沿 parentId 走回根，返回反转后的活跃链。

    v1（头记录 version==1）或文件里没有任何 id 时是线性格式，直接按
    行序返回。环状 parentId 用 visited 集合防御。
    """
    header = next((r for r in records if r.get("type") == "session"), None)
    if isinstance(header, dict) and header.get("version") == 1:
        return records
    by_id = {r["id"]: r for r in records if isinstance(r.get("id"), str)}
    leaf = next(
        (r for r in reversed(records) if isinstance(r.get("id"), str)), None
    )
    if leaf is None:
        return records  # 无线性外的定位手段，退回行序
    chain: list[dict] = []
    seen: set[int] = set()
    node: dict | None = leaf
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        node = by_id.get(node.get("parentId"))
    chain.reverse()
    return chain


class PiAdapter:
    """pi（pi-mono coding agent）会话日志（v3 树结构 / v1 线性）适配器。

    解析规则见模块 docstring。toolCall 块产出一个 Turn(role="tool")，
    配对的 toolResult 把输出截断到 TOOL_OUTPUT_MAX_CHARS 后追加；
    未配对的 toolResult 忽略。
    """

    name = "pi"

    def parse(self, path: Path) -> list[Turn]:
        if not path.is_file():
            raise FileNotFoundError(f"会话日志不存在或不是文件: {path}")
        records: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行跳过，不拖垮整个解析
            if isinstance(rec, dict):
                records.append(rec)

        turns: list[Turn] = []
        tool_turns: dict[str, Turn] = {}  # toolCallId -> 待配对 result 的 tool Turn
        current_turn = -1  # 当前轮次编号；user 消息开新一轮

        for rec in _active_chain(records):
            if rec.get("type") != "message":
                continue
            message = rec.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            ts = _parse_ts(message.get("timestamp"))

            if role == "user":
                text = _join_text_blocks(message.get("content"))
                if not text:
                    continue
                current_turn += 1
                turns.append(
                    Turn(turn_index=current_turn, role="user", content=text, ts=ts)
                )

            elif role == "assistant":
                content = message.get("content")
                blocks = content if isinstance(content, list) else []
                if not blocks and message.get("stopReason") in ("error", "aborted"):
                    continue  # 失败/中止且无内容的 assistant 消息整条跳过
                turn_index = max(current_turn, 0)
                text = _join_text_blocks(blocks)
                if text:
                    turns.append(
                        Turn(
                            turn_index=turn_index,
                            role="assistant",
                            content=text,
                            ts=ts,
                        )
                    )
                for block in blocks:
                    if not isinstance(block, dict) or block.get("type") != "toolCall":
                        continue
                    tool_name = block.get("name")
                    call_id = block.get("id")
                    if not isinstance(tool_name, str) or not isinstance(call_id, str):
                        continue
                    args = block.get("arguments")
                    turn = Turn(
                        turn_index=turn_index,
                        role="tool",
                        tool_name=tool_name,
                        content=json.dumps(
                            args if args is not None else {}, ensure_ascii=False
                        ),
                        ts=ts,
                    )
                    turns.append(turn)
                    tool_turns[call_id] = turn

            elif role == "toolResult":
                call_id = message.get("toolCallId")
                if not isinstance(call_id, str):
                    continue
                turn = tool_turns.get(call_id)
                if turn is None:
                    continue  # 未配对的 toolResult（截断/跨会话日志）忽略
                output = _join_text_blocks(message.get("content"))
                if message.get("isError") is True:
                    output = f"[错误] {output}"
                if turn.tool_name is None and isinstance(message.get("toolName"), str):
                    turn.tool_name = message["toolName"]
                if len(output) > TOOL_OUTPUT_MAX_CHARS:
                    output = (
                        output[:TOOL_OUTPUT_MAX_CHARS]
                        + f"...[截断，原长 {len(output)} 字符]"
                    )
                turn.content += f"\n→ {output}"
            # 其他 role（bashExecution/custom/branchSummary 等）：跳过

        return turns
