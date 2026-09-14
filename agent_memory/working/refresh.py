"""工作记忆即时整理（v0.2：P08 T0 整理 + P07 按任务的状态结构）。

基线里工作记忆完全靠宿主 agent 按 hook 指令手写（全量替换）。这里改为服务端整理：
把当前工作记忆 + 最近几轮对话交给 LLM，产出更新后的完整状态——
- 旧取值被改掉时直接替换（不保留两个版本），被推翻的决策移除；
- 已完成的步骤标 done，下一步保持 pending 并按计划顺序排列；
- 约束（constraints）与未决问题（open_questions）单列；
- 同一 scope 里并行的其他任务放进 subtasks，互不串线；打断插进来的闲聊不写进状态。
"""

from __future__ import annotations

import json

REFRESH_SYSTEM = (
    "你负责维护 agent 的工作记忆（当前任务状态）。给你当前工作记忆（JSON）和最近几轮对话，请输出"
    "更新后的完整工作记忆。规则：\n"
    "1. 全量输出：仍然有效的旧内容要带上；\n"
    "2. 约束或取值被改掉时直接替换成新值（旧值作废，不要同时保留），被推翻的决策删除；\n"
    "3. todos 按计划顺序列出每一步：已完成的 status=done，未完成的 pending；\n"
    "4. constraints 放用户定下的硬约束；open_questions 放尚未确定、需要问谁的问题（问题解决后移除"
    "）；"
    "variables 放关键取值（键值都用文本）；\n"
    "5. 同时推进多个任务时，goal/constraints/todos 等顶层字段写“当前正在做的任务”，其他任务各自放"
    "进 subtasks，"
    "每个 subtask 有自己的 name、goal、constraints、todos、open_questions、variables，任务之间不"
    "要混写；"
    "用户切回某个任务时，把它提到顶层，原顶层任务移进 subtasks；\n"
    "6. 与任务无关的闲聊、临时插问的技术问题，不要写进工作记忆。"
)
REFRESH_SCHEMA = """{
  "goal": "...",
  "constraints": ["..."],
  "decisions": ["..."],
  "variables": {"k": "v"},
  "todos": [{"content": "...", "status": "done 或 pending"}],
  "open_questions": ["..."],
  "notes": ["..."],
  "subtasks": [{"name": "...", "goal": "...", "constraints": [],
                "todos": [{"content": "...", "status": "pending"}],
                "open_questions": [], "variables": {}}]
}"""


def refresh_payload(current: dict | None, conversation: list[dict], llm) -> dict:
    cur = json.dumps(current or {}, ensure_ascii=False)
    convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in conversation)
    return llm.complete_json(
        REFRESH_SYSTEM, f"当前工作记忆：\n{cur}\n\n最近几轮对话：\n{convo}", REFRESH_SCHEMA
    )
