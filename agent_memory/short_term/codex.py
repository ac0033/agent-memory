"""OpenAI Codex CLI 会话日志适配器（rollout-*.jsonl）。

格式要点（已实测确认，本机 128 个 rollout 文件，cli 0.140→0.147 信封一致）：
文件位于 ~/.codex/sessions/YYYY/MM/DD/rollout-<UTC时间戳>-<uuid>.jsonl
（archived_sessions/ 下同格式）。每行一个信封记录
{"timestamp": ISO8601 字符串, "type": str, "payload": {...}}，
ts 取 timestamp 转 epoch 毫秒。只保留 type=="response_item" 且 payload.type
为以下值的记录：
- message + role=="user"：拼接 content 块里 type=="input_text" 的 text；
  以 "<environment_context>" 开头的是环境注入，整条跳过；
- message + role=="assistant"：拼接 content 块里 type=="output_text" 的
  text（payload.phase 为 commentary/final_answer 都算，不区分；多个
  message item 各自成一个 Turn，不做跨 item 聚合）；
- function_call：tool Turn，tool_name=payload.name，content=payload.arguments
  （未解析的 JSON 字符串，原样存），配对键 payload.call_id；
- function_call_output：按 call_id 配对回 tool Turn，payload.output 截断后
  追加 "\\n→ "；
- custom_tool_call / custom_tool_call_output：同上，但参数字段名是
  payload.input 而非 arguments（结果仍叫 output）。

跳过：reasoning（思维链）、web_search_call、role=="developer" 的 message、
event_msg 整类（user_message/agent_message 与 response_item 重复，其余是
元数据）、session_meta / turn_context / world_state 等一切其他 type。

轮次边界：过滤后的真实 user 消息出现即开新轮（0 起），后续 assistant/tool
归该轮；首个真实 user 消息之前的 assistant/tool 记录防御性跳过（正常文件
不会出现——developer 与环境注入消息已被过滤）。

防御性：单行 JSON 解析失败跳过；缺预期字段的记录跳过；文件不存在或不是
文件抛 FileNotFoundError。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS, Turn

# 环境注入消息的前缀：以它开头的 user 消息整条跳过，不算真实用户输入
_ENV_CONTEXT_PREFIX = "<environment_context>"


def _parse_ts(rec: dict) -> int | None:
    """信封 timestamp 是 ISO8601 字符串，转 epoch 毫秒；缺失或非法返回 None。"""
    raw = rec.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:  # 防御：无时区按 UTC 处理
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _join_text_blocks(content: object, block_type: str) -> str:
    """拼接 message content 块列表里指定 type 的 text（多块之间换行分隔）。"""
    if not isinstance(content, list):
        return ""
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == block_type
        and isinstance(block.get("text"), str)
        and block["text"]
    ]
    return "\n".join(texts)


def _stringify(raw: object) -> str:
    """arguments/input/output 预期是字符串，原样返回；其他形态 JSON 化兜底。"""
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    return json.dumps(raw, ensure_ascii=False)


class CodexAdapter:
    """Codex CLI rollout-*.jsonl 适配器。

    解析规则见模块 docstring。function_call / custom_tool_call 产出一个
    Turn(role="tool")，配对的 output 把结果截断到 TOOL_OUTPUT_MAX_CHARS 后
    追加；未配对的 output 忽略。
    """

    name = "codex"

    def parse(self, path: Path) -> list[Turn]:
        if not path.is_file():
            raise FileNotFoundError(f"会话日志不存在或不是文件: {path}")
        turns: list[Turn] = []
        tool_turns: dict[str, Turn] = {}  # call_id -> 待配对 output 的 tool Turn
        current_turn = -1  # 当前轮次编号；真实 user 消息出现即 +1 开新轮

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行跳过，不拖垮整个解析
            if not isinstance(rec, dict) or rec.get("type") != "response_item":
                continue  # event_msg / session_meta / turn_context 等整类跳过
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            ts = _parse_ts(rec)
            item_type = payload.get("type")

            if item_type == "message":
                role = payload.get("role")
                if role == "user":
                    text = _join_text_blocks(payload.get("content"), "input_text")
                    if not text or text.startswith(_ENV_CONTEXT_PREFIX):
                        continue  # 空消息 / 环境注入不算真实用户输入
                    current_turn += 1
                    turns.append(
                        Turn(
                            turn_index=current_turn,
                            role="user",
                            content=text,
                            ts=ts,
                        )
                    )
                elif role == "assistant":
                    if current_turn < 0:
                        continue  # 首个真实 user 消息之前的记录防御性跳过
                    text = _join_text_blocks(payload.get("content"), "output_text")
                    if not text:
                        continue
                    turns.append(
                        Turn(
                            turn_index=current_turn,
                            role="assistant",
                            content=text,
                            ts=ts,
                        )
                    )
                # role=="developer" 及其余：跳过

            elif item_type in ("function_call", "custom_tool_call"):
                if current_turn < 0:
                    continue
                name = payload.get("name")
                call_id = payload.get("call_id")
                if not isinstance(name, str) or not isinstance(call_id, str):
                    continue
                # 参数字段：function_call 叫 arguments，custom_tool_call 叫 input
                args_key = (
                    "arguments" if item_type == "function_call" else "input"
                )
                turn = Turn(
                    turn_index=current_turn,
                    role="tool",
                    tool_name=name,
                    content=_stringify(payload.get(args_key)),
                    ts=ts,
                )
                turns.append(turn)
                tool_turns[call_id] = turn

            elif item_type in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                if not isinstance(call_id, str):
                    continue
                turn = tool_turns.get(call_id)
                if turn is None:
                    continue  # 未配对的 output（跨文件/截断的日志）忽略
                output = _stringify(payload.get("output"))
                if len(output) > TOOL_OUTPUT_MAX_CHARS:
                    output = (
                        output[:TOOL_OUTPUT_MAX_CHARS]
                        + f"...[截断，原长 {len(output)} 字符]"
                    )
                turn.content += f"\n→ {output}"
            # reasoning / web_search_call 及其余 payload.type：跳过

        return turns
