"""工作记忆即时整理（v0.2：P08 T0 整理 + P07 按任务的状态结构；v0.2.2 起增量更新）。

基线里工作记忆完全靠宿主 agent 按 hook 指令手写（全量替换）。这里改为服务端整理：
把当前工作记忆 + 最近几轮对话交给 LLM，产出要改动的字段——
- 旧取值被改掉时直接替换（不保留两个版本），被推翻的决策移除；
- 已完成的步骤标 done，下一步保持 pending 并按计划顺序排列；
- 约束（constraints）与未决问题（open_questions）单列；
- 同一 scope 里并行的其他任务放进 subtasks，互不串线；打断插进来的闲聊不写进状态；
- 对话涉及多个任务时，进展和取值归到所属的任务；急活告一段落或用户切回时，被打断的任务回到顶层；
- 取值改了不等于已完成的步骤要返工；task_name 只在有并行任务时才需要。

增量（v0.2.2）：v0.2 每次让 LLM 重写整份工作记忆，输出量随状态变大而增长（评测里 2 条长用例
触发了 206 次整理，每次约 4 千字输出）。现在 LLM 只输出有变化的字段，由 apply_patch 合并；
什么都没变时输出 {}。
"""

from __future__ import annotations

import json

FIELDS = (
    "task_name",
    "goal",
    "constraints",
    "decisions",
    "variables",
    "todos",
    "open_questions",
    "notes",
    "subtasks",
)

REFRESH_SYSTEM = (
    "你负责维护 agent 的工作记忆（当前任务状态）。给你当前工作记忆（JSON）和最近几轮对话，请输出"
    "工作记忆里需要改动的字段（增量更新）。规则：\n"
    "1. 只输出有变化的顶层字段，每个输出的字段给出它的完整新值（列表给完整列表）；没有变化的字段"
    "不要输出；什么都没变时输出 {}。当前工作记忆为空时，输出所有已知字段；\n"
    "2. variables 按键合并：只写新增或改了值的键，要删除的键写 null；\n"
    "3. 约束或取值被改掉时直接替换成新值（旧值作废，不要同时保留），被推翻的决策删除；\n"
    "4. todos 按计划顺序列出每一步：已完成的 status=done，未完成的 pending；约束或取值改了，"
    "不等于已完成的步骤要重做——已完成的步骤保持 done，除非用户明确说要返工；\n"
    "5. constraints 放用户定下的硬约束；open_questions 放尚未确定、需要问谁的问题（问题解决后移除"
    "）；variables 放关键取值（键值都用文本）；\n"
    "6. 同时推进多个任务时，task_name/goal/constraints/todos 等顶层字段写“当前正在做的任务”，其他"
    "任务各自放进 subtasks，每个 subtask 有自己的 name、goal、constraints、todos、open_questions、"
    "variables，任务之间不要混写；task_name 与 subtask 的 name 用同一套简称（沿用对话里的叫法）。"
    "只有一个任务时不输出 task_name；\n"
    "7. 对话涉及不止一个任务时（包括第二个任务刚出现、subtasks 还是空的时候），每条进展和取值归到它"
    "所属的那个任务：属于其他任务的就写进那个 subtask（输出完整的 subtasks 列表），"
    "不要写到顶层任务上；整场对话只有一个任务时不适用本条；\n"
    "8. 用户切回某个任务、或插进来的急活/打断告一段落时，把被打断的任务提回顶层，原顶层任务移进 "
    "subtasks（这时顶层字段与 subtasks 都要输出）；\n"
    "9. 与任务无关的闲聊、临时插问的技术问题，不要写进工作记忆。"
)
REFRESH_SCHEMA = """只包含有变化的字段（每个字段都可省略；什么都没变时为 {}）：
{
  "goal": "...",
  "constraints": ["..."],
  "decisions": ["..."],
  "variables": {"k": "新值", "要删除的键": null},
  "todos": [{"content": "...", "status": "done 或 pending"}],
  "open_questions": ["..."],
  "notes": ["..."],
  "task_name": "当前任务的简称（只在有并行任务时输出）",
  "subtasks": [{"name": "...", "goal": "...", "constraints": [],
                "todos": [{"content": "...", "status": "pending"}],
                "open_questions": [], "variables": {}}]
}"""


def apply_patch(current: dict | None, patch: dict) -> dict:
    """把增量合并进当前工作记忆：给出的字段整体替换，variables 按键合并（值为 null 即删除）。"""
    merged = dict(current or {})
    for k in FIELDS:
        v = patch.get(k)
        if v is None:
            continue
        if k == "variables" and isinstance(v, dict):
            new = dict(merged.get("variables") or {})
            for vk, vv in v.items():
                if vv is None:
                    new.pop(vk, None)
                else:
                    new[vk] = vv
            v = new
        merged[k] = v
    return merged


def refresh_payload(current: dict | None, conversation: list[dict], llm) -> dict:
    """调 LLM 做增量整理，返回合并后的完整工作记忆。"""
    cur = json.dumps(current or {}, ensure_ascii=False)
    convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in conversation)
    patch = llm.complete_json(
        REFRESH_SYSTEM, f"当前工作记忆：\n{cur}\n\n最近几轮对话：\n{convo}", REFRESH_SCHEMA
    )
    return apply_patch(current, patch if isinstance(patch, dict) else {})
