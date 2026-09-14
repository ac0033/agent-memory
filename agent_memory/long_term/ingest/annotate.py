"""写入后回读核验（v0.2：P27 完整度自评 + P13 回读查漏）。

对每条记忆，拿它 evidence 指向的原始对话做对照，由 LLM 判两件事：
- completeness：content + detail 是否保住了原文里这件事的全部具体细节
  （数值、枚举项、格式、名单、时限）；
  只有概括时记为 gist，渲染时会提示"细节不全，需要时回原文取"；
- consistent：记忆与原文是否一致（数值、日期、单位、对象任何一处不符都算不一致），
  不一致记 verify_flag=mismatch。

只标注、不改写记忆正文：改写要走对账与人工复核，不在这里静默发生（fail-closed）。
LLM 失败时不标注（保持 None），不影响写入结果。
"""

from __future__ import annotations

from agent_memory.llm import LLMClient
from agent_memory.models import MemoryEntry

ANNOTATE_SYSTEM = (
    "你是记忆核验员。给你若干条记忆，以及它们所引用的原始对话片段。逐条判断：\n"
    "1. completeness：complete 表示记忆（content 与 detail 合起来）已经包含原文中关于这件事的全部"
    "具体细节"
    "（数值、枚举的每一项、格式、名单、时限、条件）；gist 表示只有概括，或者漏掉了原文里的具体细"
    "节。"
    "原文本身就只有一句简单事实、记忆也完整复述了它时，判 complete。\n"
    "2. consistent：记忆与原文是否一致。数值、日期、单位、对象、条件有任何一处不符，都判 false；"
    "只是措辞不同、意思相同的判 true。\n"
    "只依据给出的原文判断，不要猜测原文之外的内容。"
)
ANNOTATE_SCHEMA = (
    '{"items": [{"id": "记忆 id", "completeness": "complete 或 gist", '
    '"consistent": true, "note": "一句话依据"}]}'
)


def annotate_entries(
    entries: list[MemoryEntry], sources: dict[str, str], llm: LLMClient
) -> dict[str, dict]:
    """返回 {entry_id: {"completeness": ..., "verify_flag": ...}}。

    sources 为 entry_id -> 原文片段。
    """
    todo = [e for e in entries if sources.get(e.id)]
    if not todo:
        return {}
    blocks = []
    for e in todo:
        blocks.append(
            f"### 记忆 {e.id}\ncontent：{e.content}\ndetail：{e.detail or '（无）'}\n"
            f"原文：\n{sources[e.id]}"
        )
    parsed = llm.complete_json(ANNOTATE_SYSTEM, "\n\n".join(blocks), ANNOTATE_SCHEMA)
    out: dict[str, dict] = {}
    ids = {e.id for e in todo}
    for it in parsed.get("items") or []:
        if not isinstance(it, dict) or it.get("id") not in ids:
            continue
        comp = it.get("completeness") if it.get("completeness") in {"complete", "gist"} else None
        consistent = it.get("consistent")
        out[it["id"]] = {
            "completeness": comp,
            "verify_flag": "mismatch" if consistent is False else None,
        }
    return out
