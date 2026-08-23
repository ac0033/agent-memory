"""Claude Code 会话日志适配器。

格式来源声明：本解析器的格式规格全部来自文献调研（Claude Code v2.1.x 源码
分析 + 约 4200 个真实会话文件的字段统计），编写时本机没有真实样本可回归；
首次接入真实日志后应人工核对一次解析结果。

格式要点：~/.claude/projects/<munged项目路径>/<session-uuid>.jsonl，每行一个
JSON，判别符 rec['type']——只有 'user' 和 'assistant' 两类保留，其余约 20 种
元数据 type（system/attachment/summary/file-history-snapshot/permission-mode/
mode/ai-title/custom-title/last-prompt/tag/agent-*/pr-link/task-summary/
queue-operation/progress 等）全跳过；元数据类会周期性重复追加，不能假设每种
type 只出现一次。公共字段 uuid/parentUuid/timestamp（ISO 8601，转 epoch 毫秒）
/sessionId/isSidechain——isSidechain==true 是旧版子 agent 混写在主文件里的记录，
一律跳过。

user 记录（rec['message']['role']=='user'）两种形态：
(a) 真实用户输入——content 是纯字符串，或块数组且首块 type != 'tool_result'；
    正文取字符串本身或拼接 type=='text' 块的 text；带 rec['isMeta']==true 的是
    本地命令回显，跳过。真实用户输入 = 新一轮（无显式轮次字段，轮次号
    max_turn_index+1，从 0 起）。
(b) 工具结果回传——content 块数组首块 type=='tool_result'：每个 tool_result
    块按 block['tool_use_id'] 配对回之前登记的 tool Turn，结果文本
    （block['content']，字符串或 text 块数组）截断后追加 '\\n→ <output>'；
    顶层 rec['toolUseResult'] 是结构化补充，忽略。该形态不触发轮次递增。

assistant 记录（role=='assistant'）：content 是块数组——type=='text' 拼进
assistant Turn；type=='thinking' 跳过；type=='tool_use' 独立产出 tool Turn
（tool_name=block['name']，content=json.dumps(block['input'])，按 block['id']
登记待配对）。关键坑：一次 API 响应拆成多条连续 assistant 记录、每条只含一个
content 块，它们共享同一个 message.id——按 message.id 聚合 text（同一 id 的
ts 取首条）。

防御性：坏 JSON 行/缺预期字段的记录跳过不拖垮整体；timestamp 缺失或非法给
ts=None 不报错；文件不存在或不是文件抛 FileNotFoundError。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS, Turn


def _parse_ts(rec: dict) -> int | None:
    """rec['timestamp'] 是 ISO 8601 字符串，转 epoch 毫秒；缺失/非法返回 None。"""
    raw = rec.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # 防御：无时区的按 UTC 处理
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _join_text_blocks(content: object) -> str:
    """拼接块数组里 type=='text' 块的 text（多块换行分隔）；其他形态返回空串。"""
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


def _result_text(content: object) -> str:
    """tool_result 块的结果文本：字符串原样，text 块数组拼接，其他形态 JSON 化。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _join_text_blocks(content)
    return json.dumps(content, ensure_ascii=False)


class ClaudeCodeAdapter:
    """Claude Code ~/.claude/projects/<项目目录>/<session-uuid>.jsonl 适配器。

    解析规则见模块 docstring。tool_use 块产出一个 Turn(role="tool")，后续
    tool_result 形态的 user 记录按 tool_use_id 把结果截断到
    TOOL_OUTPUT_MAX_CHARS 后追加；未配对的结果忽略。
    """

    name = "claude-code"

    def parse(self, path: Path) -> list[Turn]:
        if not path.is_file():
            raise FileNotFoundError(f"会话日志不存在或不是文件: {path}")
        turns: list[Turn] = []
        assistant_turns: dict[str, Turn] = {}  # message.id -> 聚合中的 assistant Turn
        tool_turns: dict[str, Turn] = {}  # tool_use block id -> 待配对结果的 tool Turn
        current_turn = -1  # 当前轮次编号；真实用户输入 +1（从 0 起）

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行跳过，不拖垮整个解析
            if not isinstance(rec, dict):
                continue
            rec_type = rec.get("type")
            if rec_type not in ("user", "assistant"):
                continue  # 元数据噪音（约 20 种，周期性重复）全跳过
            if rec.get("isSidechain") is True:
                continue  # 旧版子 agent 混写记录
            message = rec.get("message")
            if not isinstance(message, dict):
                continue
            ts = _parse_ts(rec)

            if rec_type == "user":
                if message.get("role") != "user":
                    continue
                if rec.get("isMeta") is True:
                    continue  # 本地命令回显，不是真实用户输入
                content = message.get("content")
                blocks = content if isinstance(content, list) else None
                is_tool_result = bool(
                    blocks
                    and isinstance(blocks[0], dict)
                    and blocks[0].get("type") == "tool_result"
                )
                if is_tool_result:
                    self._pair_tool_results(blocks, tool_turns)
                    continue  # 工具结果回传不触发轮次递增
                text = (
                    content
                    if isinstance(content, str)
                    else _join_text_blocks(blocks)
                )
                if not text:
                    continue
                current_turn += 1
                turns.append(
                    Turn(turn_index=current_turn, role="user", content=text, ts=ts)
                )
                continue

            # rec_type == "assistant"
            if message.get("role") != "assistant":
                continue
            blocks = message.get("content")
            if not isinstance(blocks, list):
                continue
            turn_index = max(current_turn, 0)  # 防御：assistant 先于用户输入出现
            msg_id = message.get("id")
            msg_id = msg_id if isinstance(msg_id, str) and msg_id else None

            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                block_id = block.get("id")
                if not isinstance(name, str) or not isinstance(block_id, str):
                    continue
                tool_input = block.get("input")
                turn = Turn(
                    turn_index=turn_index,
                    role="tool",
                    tool_name=name,
                    content=json.dumps(
                        tool_input if tool_input is not None else {}, ensure_ascii=False
                    ),
                    ts=ts,
                )
                turns.append(turn)
                tool_turns[block_id] = turn

            text = _join_text_blocks(blocks)  # thinking 块天然被跳过
            if not text:
                continue
            existing = assistant_turns.get(msg_id) if msg_id is not None else None
            if existing is not None:
                # 同一 message.id 的后续记录（一次 API 响应拆多条）：聚合 text，
                # ts 保留首条的
                existing.content = (
                    f"{existing.content}\n{text}" if existing.content else text
                )
            else:
                turn = Turn(
                    turn_index=turn_index, role="assistant", content=text, ts=ts
                )
                turns.append(turn)
                if msg_id is not None:
                    assistant_turns[msg_id] = turn

        return turns

    @staticmethod
    def _pair_tool_results(blocks: list, tool_turns: dict[str, Turn]) -> None:
        """把 tool_result 形态 user 记录里的每个块配对回登记的 tool Turn。"""
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            if not isinstance(call_id, str):
                continue
            turn = tool_turns.get(call_id)
            if turn is None:
                continue  # 未配对的结果（跨文件/截断的日志）忽略
            output = _result_text(block.get("content"))
            if len(output) > TOOL_OUTPUT_MAX_CHARS:
                output = (
                    output[:TOOL_OUTPUT_MAX_CHARS]
                    + f"...[截断，原长 {len(output)} 字符]"
                )
            turn.content += f"\n→ {output}"
