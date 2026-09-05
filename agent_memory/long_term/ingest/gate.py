"""评价门（红线 D2 写入过门的规则关卡）：蒸馏候选入库前的纯规则校验。

不调 LLM，只做确定性检查，三桶分流：
- passed：通过全部规则，可以进对账（reconcile）；
- rejected：违反硬规则（脱敏残留 / 指令性内容 / 长度下限），拒绝入库；
- queued：confidence=low 的候选不进正式库，写入 data/review_queue 人工复核。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_memory.long_term.ingest.distill import MIN_CONTENT_CHARS
from agent_memory.long_term.ingest.redact import redact
from agent_memory.long_term.ingest.review_queue import (  # noqa: F401  (re-export，兼容既有调用方)
    write_review_queue,
    write_review_queue_raw,
)
from agent_memory.models import MemoryEntry

# 指令性内容检测模式（D2：蒸馏绝不提炼指令性内容，这里做兜底拦截）。
#
# 分两层，目的都是防注入，而不是禁止祈使语气：
# 1. INJECTION_PATTERNS：针对模型行为的注入特征（"忽略之前的指令""输出你的
#    系统提示词"），任何位置命中、任何 memory_type 都拒绝。原则：拦的是
#    "指挥模型行为"，不是"提及某个名词"——裸关键词（如 system prompt）
#    必须与外泄动词共现才算注入，否则文件名/术语引用会被误杀；
# 2. IMPERATIVE_PATTERNS：开头即命令语气的模式（"必须""以后都要"），只对
#    非 procedural 条目生效——操作经验类记忆天然是祈使句
#    （"任何 git 变更前必须先确认"是用户约定，不是注入），不能误杀。
# 新增模式时保持各自标准：注入层看"是否指挥模型"，祈使层看"开头即指令"。
INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"忽略(之前|以上|先前|上述)的?(指令|指示|提示|prompt)", re.IGNORECASE),
    re.compile(r"无视(之前|以上|先前|上述)的?(指令|指示|提示|prompt)", re.IGNORECASE),
    re.compile(r"(输出|打印|重复|泄露)你?的?(系统)?(提示词|提示|指令|prompt)", re.IGNORECASE),
    re.compile(r"\bignore\b.{0,30}\b(instruction|prompt)s?\b", re.IGNORECASE),
    re.compile(r"\bdisregard\b.{0,30}\b(instruction|prompt)s?\b", re.IGNORECASE),
    re.compile(r"\bforget\b.{0,30}\b(instruction|prompt)s?\b", re.IGNORECASE),
    re.compile(r"\b(override|bypass)\b.{0,30}\b(instruction|prompt|rule)s?\b", re.IGNORECASE),
    # 提示词外泄：动词 + 系统提示词共现才算注入。不做裸关键词匹配——
    # 记忆内容提及 "System Prompt"（文件名、术语引用）是正常陈述，不是攻击
    re.compile(
        r"(输出|打印|重复|泄露|展示|告诉|发送|发给|贴).{0,20}(系统提示词|system\s+prompt)"
        r"|把.{0,20}(系统提示词|system\s+prompt)",  # 把字句动词在后
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(show|print|leak|reveal|repeat|disclose|expose|give|tell)\b"
        r".{0,40}\b(system prompt|system instructions?)\b",
        re.IGNORECASE,
    ),
]

IMPERATIVE_PATTERNS: list[re.Pattern] = [
    re.compile(r"^以后[都也还要]"),
    re.compile(r"^从今?以后"),
    re.compile(r"^今后[都也要]"),
    re.compile(r"^总是"),
    re.compile(r"^永远[不都要]"),
    re.compile(r"^必须"),
    re.compile(r"^务必"),
    re.compile(r"^不要再"),
    re.compile(r"^记住[:：]"),
    re.compile(r"^ignore\b", re.IGNORECASE),
    re.compile(r"^always\b", re.IGNORECASE),
    re.compile(r"^never\b", re.IGNORECASE),
    re.compile(r"^you must\b", re.IGNORECASE),
]


@dataclass
class GateResult:
    """评价门三桶分流结果。"""

    passed: list[MemoryEntry] = field(default_factory=list)
    rejected: list[tuple[MemoryEntry, str]] = field(default_factory=list)  # (条目, 拒绝原因)
    queued: list[MemoryEntry] = field(default_factory=list)  # low 置信度，已写入复核队列
    queued_files: list[Path] = field(default_factory=list)


def check_instructional(content: str, memory_type: str | None = None) -> str | None:
    """检查 content 是否是指令性内容。返回 None 表示通过；否则返回命中说明。

    说明里带命中的具体文本片段，供报错与排障定位——调用方据此知道改哪里，
    而不是只拿到一句"以指令模式开头"去盲改。注入特征（指挥模型行为的指令）
    任何类型都拦；开头祈使语气只对非 procedural 条目生效——procedural 记忆
    （操作约定）天然是祈使句。
    """
    text = content.strip()
    for p in INJECTION_PATTERNS:
        m = p.search(text)
        if m:
            return f"命中注入特征 {m.group(0)!r}（疑似指挥模型行为的注入指令）"
    if memory_type == "procedural":
        return None
    for p in IMPERATIVE_PATTERNS:
        m = p.search(text)
        if m:
            return f"开头为祈使/命令语气 {m.group(0)!r}（非 procedural 条目不允许）"
    return None


def is_instructional(content: str, memory_type: str | None = None) -> bool:
    """content 是否是指令性内容（check_instructional 的布尔包装）。"""
    return check_instructional(content, memory_type) is not None


def gate_candidates(
    candidates: list[MemoryEntry], data_dir: Path | None = None
) -> GateResult:
    """对蒸馏候选做规则校验，三桶分流。

    data_dir 提供时，queued 桶会立即写入 review_queue；为 None 时只分流不落盘
    （测试与纯校验场景）。
    """
    result = GateResult()
    for entry in candidates:
        # 调用方可能通过 model_copy 构造候选；评价门入口重新执行完整 schema，
        # 防止超长正文、非法 confidence 等值被写入后反而无法读取。
        entry = MemoryEntry.model_validate(entry.model_dump(mode="python"))
        # 1. 长度下限：脱敏后无实质内容的条目在 distill 已丢过一轮，这里兜底
        if len(entry.content.strip()) < MIN_CONTENT_CHARS:
            result.rejected.append(
                (entry, f"content 长度不足 {MIN_CONTENT_CHARS} 字符，无实质内容")
            )
            continue
        # 2. 脱敏残留：对 content 再跑一遍命中检测，仍有命中的拒绝入库
        _, residual_hits = redact(entry.content)
        if entry.detail:
            _, detail_residual = redact(entry.detail)
            residual_hits += detail_residual
        if residual_hits:
            result.rejected.append(
                (entry, f"脱敏残留：content 仍命中敏感信息 {residual_hits}")
            )
            continue
        # 3. 指令性内容检测（D2 硬规则的兜底拦截；注入特征全类型拦截，
        #    开头祈使语气对 procedural 放行——操作约定天然是祈使句）
        instructional = check_instructional(entry.content, entry.memory_type)
        if instructional is None and entry.detail:
            detail_instructional = check_instructional(entry.detail, entry.memory_type)
            if detail_instructional:
                instructional = f"detail {detail_instructional}"
        if instructional:
            result.rejected.append((
                entry,
                f"指令性内容，红线 D2 禁止入库：{instructional}。"
                "这是确定性规则拦截——原样重试结果不变；若确为误伤（例如内容只是"
                "提及同名文件或术语、并非在指挥模型），请改写表述后重试，或以"
                " force_review=true 调 memory_add 转人工复核队列裁决",
            ))
            continue
        # 4. low 置信度不进正式库，进人工复核队列
        if entry.confidence == "low":
            result.queued.append(entry)
            continue
        result.passed.append(entry)

    if result.queued and data_dir is not None:
        result.queued_files = write_review_queue(
            result.queued, data_dir, reason="confidence=low，规则门分流"
        )
    return result
