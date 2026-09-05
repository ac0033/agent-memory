#!/usr/bin/env python3
"""每 N 轮对话触发一次强制记忆更新的 Stop hook（M5）。

挂在 kimi-code 的 Stop 事件上（assistant 即将结束一轮回复时触发）：
- 按 session_id 计数，计数存 <data_dir>/state/turn_counter.json；
- 计满 N 轮（默认 3，AGENT_MEMORY_REVIEW_TURN_INTERVAL 覆盖）时以退出码 2
  拦截本轮结束，stderr 的指令文本会被注入对话，让 agent 继续完成记忆蒸馏；
- 未计满时静默放行（退出码 0，无输出）。

data_dir 解析与记忆系统一致：AGENT_MEMORY_DATA_DIR 环境变量，缺省
~/.agent-memory/data。hook 按宿主惯例 fail-open：任何异常都不拦截对话。

调试开关：AGENT_MEMORY_HOOK_DEBUG=1 时把每次 Stop 事件的 payload 追加到
<data_dir>/logs/hook_debug.jsonl，用于实测 Stop 是否对 subagent 轮次触发、
payload 里有没有可区分 subagent 的字段（实测结论决定要不要加 subagent 跳过
逻辑）。同样 fail-open，写失败不影响计数与拦截。

注意：该 hook 注册在用户级 config.toml 后对所有项目的会话生效；hook 只负责
"到点必须做"的时机保证，"做什么、怎么沉淀"由指令文本 + 蒸馏管线规则决定。
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Windows 上 Python 的 stdout/stderr 被管道捕获时默认用系统区域编码（中文系统
# 为 GBK），而 kimi-code 按 UTF-8 读取 hook 输出——不重配的话中文指令会变乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_INSTRUCTION = """[agent-memory 强制记忆更新]
已到每 {interval} 轮一次的强制记忆更新点。请立刻执行：
1. 把最近 {interval} 轮对话（含刚结束的这轮）整理成 conversation JSON——
   每轮材料 = 用户的原始消息 + 紧邻其前的你的回复；
2. 顺手用 memory_wm_write 同步工作记忆——逐项检查目标/待办/决策/变量/
   备注是否仍然准确（刚完成的待办标 done、被推翻的决策删除、变量更新为
   当前值），不是只追加新状态；它是全量替换而非合并，写时带上检查后的
   完整状态，并把 turn_watermark 更新为当前轮数；
3. 调 memory_add（conversation_json 模式）走蒸馏管线——conversation_json
   推荐传 JSON 字符串（把数组序列化后再传；直接传数组服务端也会兼容）。
   只沉淀用户明确确认
   或同意过的内容：用户自己的陈述/要求/偏好可直接沉淀；你单方面提出而
   用户未表态的建议、方案、结论一律不沉淀。若蒸馏返回 archived_only 且
   原因是未配置服务端 LLM：改走宿主蒸馏——调 memory_distill_prompt 拿
   蒸馏协议，自行蒸馏后以 memory_add(distilled_json=...) 提交；
4. 若返回的 pending_review 非空，逐条向用户报告（内容 + 排队原因）并请其
   裁决：approve 入库 / modify 修改后入库 / discard 丢弃，用
   memory_review_resolve 落地；
5. 若本会话没有 memory_add 可用，或这段对话确实没有值得沉淀的内容，
   向用户说明一句即可。完成后正常结束本轮。"""


def _load_counters(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}  # 计数损坏按从头计（fail-open：hook 不因此拦对话）


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # 无法解析事件数据：放行，不影响主流程

    try:
        interval = int(os.environ.get("AGENT_MEMORY_REVIEW_TURN_INTERVAL", "3"))
    except ValueError:
        interval = 3
    if interval < 1:
        interval = 3

    data_dir = Path(
        os.environ.get("AGENT_MEMORY_DATA_DIR", "~/.agent-memory/data")
    ).expanduser()

    # 调试开关（fail-open）：把每次 Stop 事件的 payload 落盘，供实测分析
    if os.environ.get("AGENT_MEMORY_HOOK_DEBUG") == "1":
        try:
            debug_dir = data_dir / "logs"
            debug_dir.mkdir(parents=True, exist_ok=True)
            record = {"ts": datetime.now(UTC).isoformat(), "payload": payload}
            with (debug_dir / "hook_debug.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    counter_file = data_dir / "state" / "turn_counter.json"
    try:
        counter_file.parent.mkdir(parents=True, exist_ok=True)
        counters = _load_counters(counter_file)
        session_id = str(payload.get("session_id") or "default")
        count = int(counters.get(session_id, 0)) + 1
        if count < interval:
            counters[session_id] = count
            counter_file.write_text(
                json.dumps(counters, ensure_ascii=False), encoding="utf-8"
            )
            return 0
        # 计满：清零并拦截本轮结束，注入蒸馏指令
        counters[session_id] = 0
        counter_file.write_text(
            json.dumps(counters, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        return 0  # 状态文件读写失败：放行，不影响主流程

    print(_INSTRUCTION.format(interval=interval), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
