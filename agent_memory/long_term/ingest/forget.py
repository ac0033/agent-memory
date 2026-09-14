"""遗忘请求执行（v0.2，K12 被遗忘权）。

用户明确要求"忘掉某段内容"时（由蒸馏识别，或宿主直接调 memory_forget_request）：
1. 定位：用请求描述检索记忆与原文归档；描述里提到日期时，把当天的归档会话整段纳入候选；
2. 规划：LLM 判定要删哪些记忆、要从哪些原文行里擦掉哪一段（remove_text 必须是该行的原样子串），
   相邻但不属于遗忘范围的内容保留（"只删指定片段"）；
3. 执行：删除记忆（协调写入，索引同步）；原文行里的片段替换为占位符（D1 的有条件例外，
   框架 §2.6 第 4 条：用户明确请求、只删指定片段、审计只记元数据）；同步更新原文索引；
4. 审计：data/logs/forget_audit.jsonl 只追加，
   只记时间、请求所在会话、删除条数与行号、记忆 id 的哈希，
   不记任何被删内容。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent_memory.io_utils import atomic_write_text, interprocess_lock
from agent_memory.llm import LLMClient

PLACEHOLDER = "[已按用户要求删除]"

FORGET_SYSTEM = (
    "你是记忆删除执行员。用户明确要求删除某段内容。给你：遗忘请求的描述、请求所在会话的对话、候选"
    "记忆、候选原文行。\n"
    "请找出属于遗忘范围的内容：\n"
    "- memory_ids：内容属于遗忘范围的记忆 id（记忆里只要有一部分属于遗忘范围，也要列出；"
    "若同一条记忆里还有必须保留的其他信息，把它放进 rewrite，而不是整条删除）；\n"
    "- rewrite：[{id, new_content}]，去掉遗忘范围后保留其余信息的新正文；\n"
    "- raw_edits：[{line_ref, remove_text}]，line_ref 取候选原文行的编号，remove_text 必须是该行"
    "原文里"
    "连续出现的原样子串，只包含要删除的部分（助手复述了被删内容的，也要删）。\n"
    "严格限定范围：用户说要保留的、相邻但无关的信息，一律不删。确实找不到时返回空列表。"
)
FORGET_SCHEMA = (
    '{"memory_ids": ["..."], "rewrite": [{"id": "...", "new_content": "..."}], '
    '"raw_edits": [{"line_ref": "L3", "remove_text": "..."}], "reason": "一句话依据"}'
)

_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日|(\d{4})-(\d{2})-(\d{2})")


@dataclass
class ForgetReport:
    deleted: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    raw_lines: list[tuple[str, str, int]] = field(default_factory=list)
    reason: str = ""


def mentioned_dates(text: str, year: int) -> set[str]:
    out = set()
    for m in _DATE_RE.finditer(text):
        if m.group(1):
            out.add(f"{year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}")
        else:
            out.add(f"{m.group(3)}-{m.group(4)}-{m.group(5)}")
    return out


def plan(
    description: str,
    request_dialog: str,
    memories: list,
    raw_lines: list[tuple[str, dict]],
    llm: LLMClient,
) -> dict:
    mem_txt = (
        "\n".join(
            f"- id={e.id}：{e.content}" + (f"（细节：{e.detail}）" if e.detail else "")
            for e in memories
        )
        or "（无）"
    )
    raw_txt = (
        "\n".join(
            f"{ref}｜{r['source']}/{r['session_id']} 第 {r['line']} 行｜{r['role']}：{r['content']}"
            for ref, r in raw_lines
        )
        or "（无）"
    )
    user = (
        f"遗忘请求：{description}\n\n请求所在会话：\n{request_dialog or '（无）'}\n\n"
        f"候选记忆：\n{mem_txt}"
        "\n\n"
        f"候选原文行：\n{raw_txt}"
    )
    return llm.complete_json(FORGET_SYSTEM, user, FORGET_SCHEMA)


def scrub_raw_line(archive_file: Path, line: int, remove_text: str, lock_path: Path) -> str | None:
    """把 archive_file 第 line 行（1 起）内容里的 remove_text 替换为占位符。

    返回新内容；找不到返回 None。
    """
    with interprocess_lock(lock_path):
        lines = archive_file.read_text(encoding="utf-8").splitlines()
        if not (1 <= line <= len(lines)):
            return None
        try:
            rec = json.loads(lines[line - 1])
        except json.JSONDecodeError:
            return None
        content = str(rec.get("content") or "")
        if not remove_text or remove_text not in content:
            return None
        rec["content"] = content.replace(remove_text, PLACEHOLDER)
        lines[line - 1] = json.dumps(rec, ensure_ascii=False)
        atomic_write_text(archive_file, "\n".join(lines) + "\n")
        return rec["content"]


def audit(log_path: Path, *, request_ref: str, report: ForgetReport) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "request": request_ref,
        "deleted_memory_hashes": [
            hashlib.sha256(i.encode()).hexdigest()[:12] for i in report.deleted
        ],
        "rewritten_memory_hashes": [
            hashlib.sha256(i.encode()).hexdigest()[:12] for i in report.rewritten
        ],
        "raw_lines": [
            {"source": s, "session_id": sid, "line": ln} for s, sid, ln in report.raw_lines
        ],
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
