"""事件边界打包（v0.2：P05 压缩前抢救 + P06 情节卡片）。

在上下文压缩前、会话结束时等事件边界，把"之后还会用到、但压缩最容易丢"的东西原样抽出来：
标识符、端口、路径、报错原文、数值、约束、决策、改过的文件。产物：
- 一张情节卡片（episodic 记忆，completeness=complete，evidence 指向归档原文）；
- 约束同步进工作记忆的 constraints，保证压缩之后仍在"手头"。
卡片里的细节要求逐字照抄原文（字符串核对），不允许改写数字和标识符。
"""

from __future__ import annotations

EPISODE_SYSTEM = (
    "上下文马上要被压缩。请从下面这段对话里抽出之后工作还会用到、但压缩时最容易丢的信息：\n"
    "- title：这段对话在做什么（一句话）；\n"
    "- details：具体细节，逐字照抄原文（主机名、端口、库名、账号、环境变量名、路径、文件名、报错"
    "原文、数值、"
    "cron 表达式、请求头等），不要改写、不要概括，每条一项；\n"
    "- constraints：用户定下的硬约束与规则（原意保留）；\n"
    "- decisions：已确认的决策；\n"
    "- file_changes：本段改过的文件及改动（“路径：改动”）。\n"
    "与当前任务无关的闲聊、通用技术问答不要抽。没有的项给空数组。"
)
EPISODE_SCHEMA = (
    '{"title": "...", "details": ["..."], "constraints": ["..."], "decisions": ["..."], '
    '"file_changes": ["..."]}'
)


def extract(conversation: list[dict], llm) -> dict:
    text = "\n".join(
        f"[{i + 1}] {m.get('role')}: {m.get('content')}" for i, m in enumerate(conversation)
    )
    out = llm.complete_json(EPISODE_SYSTEM, text, EPISODE_SCHEMA)
    clean = {"title": str(out.get("title") or "").strip()}
    for k in ("details", "constraints", "decisions", "file_changes"):
        clean[k] = [str(x).strip() for x in (out.get(k) or []) if str(x).strip()][:20]
    return clean


def card_text(card: dict, limit: int = 780) -> str:
    parts = []
    for label, key in (
        ("细节", "details"),
        ("约束", "constraints"),
        ("决策", "decisions"),
        ("改动", "file_changes"),
    ):
        if card.get(key):
            parts.append(f"{label}：" + "；".join(card[key]))
    text = "\n".join(parts)
    return text[:limit]
