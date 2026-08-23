"""DeepSeek harness（dsh）会话日志适配器。

格式要点（本机真实样本 + dsh 源码仓库双重印证）：
- 文件是 ~/.dsh/sessions/<projectKey>/<session-id>/session.jsonl.zstd，
  另有明文变体 session.jsonl；zstd 版是首尾相接的多帧容器（无字典带
  checksum），必须用 stream_reader 一次读全——decompressobj 只解第一帧；
- 首行 {'type': 'session', 'version': 0, ...}：version != 0 直接
  ValueError（fail-closed，官方无兼容承诺）；
- 事件行信封 {'type', 'seq', 'time'(epoch 毫秒), 'data', ...}，按 type 保留：
  - turn/start / turn/end：data['turn']（从 1 开始）维护当前轮游标，不产出
    Turn；输出 turn_index = turn - 1（对齐 0 起约定）。崩溃会话末尾缺
    turn/end 属正常；
  - user/message：只有 data['source']['kind'] == 'user' 才是真人输入
    （'plugin' / 'skill-catalog' 等是 harness 注入的合成上下文，体量大，
    丢弃）；正文取 data['content'] 中 type=='text' 块的 text 拼接；
    user/message 不带 turn 字段，用游标值；
  - assistant/message：data['turn'] / data['step'] 自带轮次；正文取
    data['message']['content'] 中 type=='text' 块的 text 拼接；
    type=='reasoning' 跳过；type=='tool-call' 块不展开（tool/call 事件是
    权威记录，展开会双计）；每个 step 一条独立 assistant Turn；
  - tool/call：tool Turn，tool_name=data['name']，content=data['arguments']
    （未解析 JSON 字符串，原样存），配对键 data['callId']；
  - tool/result：按 data['message']['content'][0]['toolCallId'] 配对回
    tool Turn，正文取该块 content 中 text 块拼接（截断到
    TOOL_OUTPUT_MAX_CHARS 后以 "\\n→ " 追加，isError 为真时加 [错误] 标注）；
    result 自身不带工具名，tool_name 从 call 反查，孤儿 result 忽略；
- 其余约 40 种 type（reasoning-chunks / text-chunks / tool-call-chunks
  打包行、assistant/chunk 流式增量、step/*、request/header、approval/*、
  todo/write、session/title 等）全是噪音，跳过。

防御性：单行 JSON 解析失败跳过；缺预期字段的记录跳过；zstd 撕裂尾部
（write-behind 残留不完整帧/末行无换行）截断容忍；文件不存在或不是文件
抛 FileNotFoundError。
"""

import io
import json
from pathlib import Path

import zstandard

from agent_memory.short_term.adapter import TOOL_OUTPUT_MAX_CHARS, Turn

_READ_CHUNK = 1 << 20  # 流式解压每次读 1 MiB


def _read_text(path: Path) -> str:
    """读会话日志全文。zstd 多帧容器一次流式读全；撕裂尾部（不完整帧）
    抛 ZstdError 时截断保留已解出的内容，不让残帧拖垮整个解析。"""
    raw = path.read_bytes()
    if not path.name.endswith(".zstd"):
        return raw.decode("utf-8", errors="replace")
    chunks: list[bytes] = []
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw))
    try:
        while True:
            chunk = reader.read(_READ_CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
    except zstandard.ZstdError:
        pass  # 撕裂尾部：已解出的完整帧照常使用
    return b"".join(chunks).decode("utf-8", errors="replace")


def _join_text_blocks(content: object) -> str:
    """拼接 content 块列表里 type=='text' 块的 text（多块之间换行分隔）。"""
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


def _parse_ts(rec: dict) -> int | None:
    ts = rec.get("time")
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return int(ts)
    return None


def _parse_turn_no(raw: object) -> int | None:
    """data['turn'] 从 1 开始；缺失或非法返回 None。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw >= 1:
        return raw
    return None


def _check_version(rec: object) -> None:
    """session 头记录 version != 0 时 fail-closed（官方无兼容承诺）。"""
    if isinstance(rec, dict) and rec.get("type") == "session":
        version = rec.get("version")
        if version != 0:
            raise ValueError(
                f"不支持的 dsh 会话日志版本 {version!r}（仅支持 version 0）"
            )


class DeepSeekHarnessAdapter:
    """dsh session.jsonl.zstd（version 0）适配器。

    解析规则见模块 docstring。tool/call 产出一个 Turn(role="tool")，
    配对的 tool/result 把输出截断到 TOOL_OUTPUT_MAX_CHARS 后追加；
    未配对的 result 忽略。
    """

    name = "deepseek-harness"

    def parse(self, path: Path) -> list[Turn]:
        if not path.is_file():
            raise FileNotFoundError(f"会话日志不存在或不是文件: {path}")
        turns: list[Turn] = []
        tool_turns: dict[str, Turn] = {}  # callId -> 待配对 result 的 tool Turn
        current_turn = 1  # 当前轮游标（1 起）；尚无 turn/start 时按第 1 轮算
        first_line = True

        for line in _read_text(path).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # 坏行跳过，不拖垮整个解析
            if first_line:
                first_line = False
                _check_version(rec)
            if not isinstance(rec, dict):
                continue
            rec_type = rec.get("type")
            ts = _parse_ts(rec)
            data = rec.get("data")
            if not isinstance(data, dict):
                data = {}
            # 自带轮次的事件顺带推进游标（turn/start 之外的记录也带 turn）
            turn_no = _parse_turn_no(data.get("turn"))
            if turn_no is not None:
                current_turn = max(current_turn, turn_no)

            if rec_type in ("turn/start", "turn/end"):
                continue  # 只用于维护游标（上面已做），不产出 Turn

            if rec_type == "user/message":
                source = data.get("source")
                if not isinstance(source, dict) or source.get("kind") != "user":
                    continue  # plugin / skill-catalog 等合成上下文，丢弃
                text = _join_text_blocks(data.get("content"))
                if not text:
                    continue
                turns.append(
                    Turn(
                        turn_index=current_turn - 1,
                        role="user",
                        content=text,
                        ts=ts,
                    )
                )

            elif rec_type == "assistant/message":
                if turn_no is None:
                    continue
                message = data.get("message")
                if not isinstance(message, dict):
                    continue
                # 只取 text 块：reasoning 跳过；tool-call 块不展开
                # （tool/call 事件是权威记录，展开会双计）
                text = _join_text_blocks(message.get("content"))
                if not text:
                    continue
                turns.append(
                    Turn(
                        turn_index=turn_no - 1,
                        role="assistant",
                        content=text,
                        ts=ts,
                    )
                )

            elif rec_type == "tool/call":
                name = data.get("name")
                call_id = data.get("callId")
                if not isinstance(name, str) or not isinstance(call_id, str):
                    continue
                arguments = data.get("arguments")
                content = (
                    arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
                )
                turn = Turn(
                    turn_index=(turn_no - 1) if turn_no is not None else current_turn - 1,
                    role="tool",
                    tool_name=name,
                    content=content,
                    ts=ts,
                )
                turns.append(turn)
                tool_turns[call_id] = turn

            elif rec_type == "tool/result":
                message = data.get("message")
                if not isinstance(message, dict):
                    continue
                blocks = message.get("content")
                if not isinstance(blocks, list) or not blocks:
                    continue
                result_block = blocks[0]
                if not isinstance(result_block, dict):
                    continue
                call_id = result_block.get("toolCallId")
                if not isinstance(call_id, str):
                    continue
                turn = tool_turns.get(call_id)
                if turn is None:
                    continue  # 未配对的 result（跨文件/截断的日志）忽略
                output = _join_text_blocks(result_block.get("content"))
                if result_block.get("isError") is True:
                    output = f"[错误] {output}"
                if len(output) > TOOL_OUTPUT_MAX_CHARS:
                    output = (
                        output[:TOOL_OUTPUT_MAX_CHARS]
                        + f"...[截断，原长 {len(output)} 字符]"
                    )
                turn.content += f"\n→ {output}"
            # 其余 type（chunks 打包行 / step/* / approval/* 等）：噪音，跳过

        return turns
