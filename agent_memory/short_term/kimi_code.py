"""kimi-code wire.jsonl 适配器（M7b，自 adapter.py 迁入）。

格式要点（已实测确认，protocol_version 1.4/1.5）：每行一个 JSON 记录，按 type 区分——
- context.append_message：用户输入（message.content 是块列表，拼接 text；
  消息本身不带 turnId，归为"即将到来的那一轮"）；
- context.append_loop_event：event 子类型 content.part（type=text 是
  assistant 回复增量，同一 turn 多 part 聚合拼接；type=think 是内部推理，
  跳过）/ tool.call（name + args + toolCallId）/ tool.result（按
  toolCallId 配对回 call，output 追加到该 tool Turn）/ step.*（跳过）；
- turn.ended：轮次边界（只用于维护最大 turnId）；
- turn.prompt 与 append_message 内容重复，忽略（避免用户消息双计）；
- 其余 type（metadata / llm.request / usage.record 等）全是噪音，跳过。

防御性：单行 JSON 解析失败跳过；缺预期字段的记录跳过；文件不存在或
不是文件抛 FileNotFoundError。
"""

import json
from pathlib import Path

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS, Turn


def _join_content_blocks(content: object) -> str:
    """拼接 wire 消息 content 块列表里的 text（多块之间换行分隔）。

    content 也可能是纯字符串（防御），原样返回；其他形态返回空串。
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


def _parse_turn_id(raw: object) -> int | None:
    """loop 事件的 turnId 是字符串数字，int 化；缺失或非法返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _parse_ts(rec: dict) -> int | None:
    ts = rec.get("time")
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    return None


def _stringify_output(output: object) -> str:
    if isinstance(output, str):
        return output
    return json.dumps(output, ensure_ascii=False)


class KimiCodeWireAdapter:
    """kimi-code wire.jsonl（protocol_version 1.4/1.5）适配器。

    解析规则见模块 docstring。tool.call 产出一个 Turn(role="tool")，
    配对的 tool.result 把 output 截断到 TOOL_OUTPUT_MAX_CHARS 后追加；
    未配对的 result 忽略。
    """

    name = "kimi-code-wire"

    def parse(self, path: Path) -> list[Turn]:
        if not path.is_file():
            raise FileNotFoundError(f"会话日志不存在或不是文件: {path}")
        turns: list[Turn] = []
        assistant_turns: dict[int, Turn] = {}  # turnId -> 聚合中的 assistant Turn
        tool_turns: dict[str, Turn] = {}  # toolCallId -> 待配对 result 的 tool Turn
        max_turn_id = -1  # 已见过的最大轮次编号；用户消息归 max+1

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
            ts = _parse_ts(rec)

            if rec_type == "context.append_message":
                message = rec.get("message")
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                text = _join_content_blocks(message.get("content"))
                if not text:
                    continue
                turn_index = max_turn_id + 1
                turns.append(
                    Turn(turn_index=turn_index, role="user", content=text, ts=ts)
                )
                # 防御：把 max 提到用户消息这一轮——即使该轮 loop 事件缺失
                # （比如轮次被中止），下一条用户消息也不会与本轮撞号
                max_turn_id = max(max_turn_id, turn_index)

            elif rec_type == "context.append_loop_event":
                event = rec.get("event")
                if not isinstance(event, dict):
                    continue
                turn_id = _parse_turn_id(event.get("turnId"))
                if turn_id is None:
                    continue
                max_turn_id = max(max_turn_id, turn_id)
                subtype = event.get("type")

                if subtype == "content.part":
                    part = event.get("part")
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "think":
                        continue  # 内部推理不进 transcript
                    if part.get("type") != "text" or not isinstance(part.get("text"), str):
                        continue
                    turn = assistant_turns.get(turn_id)
                    if turn is None:
                        turn = Turn(
                            turn_index=turn_id, role="assistant", content="", ts=ts
                        )
                        assistant_turns[turn_id] = turn
                        turns.append(turn)
                    turn.content += part["text"]  # 流式增量直接拼接

                elif subtype == "tool.call":
                    name = event.get("name")
                    call_id = event.get("toolCallId")
                    if not isinstance(name, str) or not isinstance(call_id, str):
                        continue
                    args = event.get("args")
                    turn = Turn(
                        turn_index=turn_id,
                        role="tool",
                        tool_name=name,
                        content=json.dumps(args if args is not None else {}, ensure_ascii=False),
                        ts=ts,
                    )
                    turns.append(turn)
                    tool_turns[call_id] = turn

                elif subtype == "tool.result":
                    call_id = event.get("toolCallId")
                    if not isinstance(call_id, str):
                        continue
                    turn = tool_turns.get(call_id)
                    if turn is None:
                        continue  # 未配对的 result（跨文件/截断的日志）忽略
                    result = event.get("result")
                    output = _stringify_output(
                        result.get("output") if isinstance(result, dict) else result
                    )
                    if len(output) > TOOL_OUTPUT_MAX_CHARS:
                        output = (
                            output[:TOOL_OUTPUT_MAX_CHARS]
                            + f"...[截断，原长 {len(output)} 字符]"
                        )
                    turn.content += f"\n→ {output}"
                # step.begin / step.end 及其余子类型：跳过

            elif rec_type == "turn.ended":
                turn_id = _parse_turn_id(rec.get("turnId"))
                if turn_id is not None:
                    max_turn_id = max(max_turn_id, turn_id)
            # turn.prompt 与其余 type：噪音，跳过

        return turns
