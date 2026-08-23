"""短期记忆 transcript 适配层（M7b）。

短期记忆 = 当前会话的完整对话记录，载体是 agent 运行时的原生日志（本层
不新建任何文件）。本模块是适配层的核心：统一的 Turn 模型、TranscriptAdapter
协议、适配器注册表与按路径的自动识别。各宿主的解析器一文件一个：

- kimi_code.py：kimi-code wire.jsonl（首个实现，格式已实测）；
- claude_code.py / codex.py / opencode.py / pi.py / deepseek_harness.py：
  各宿主的会话日志解析器。

接新 runtime 只需新增一个模块并把适配器注册进 ADAPTERS，上层不变。
detect_adapter 按路径特征猜测宿主格式，猜不到就要求调用方显式指定
（fail-closed 不瞎猜）。
"""

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

# tool 结果追加到 tool Turn 时的截断上限（超长标注原长）
TOOL_OUTPUT_MAX_CHARS = 2000


class Turn(BaseModel):
    """一轮对话里的一条干净记录。

    turn_index 是轮次编号（0 起）；tool_name 仅 role="tool" 时有值；
    ts 是毫秒时间戳（日志记录里有时间就带上，没有则 None）。
    """

    turn_index: int
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None
    ts: int | None = None


class TranscriptAdapter(Protocol):
    """会话日志适配器协议：按名标识，把日志文件解析成轮次序列。"""

    name: str

    def parse(self, path: Path) -> list[Turn]: ...


from agent_memory.short_term.claude_code import ClaudeCodeAdapter  # noqa: E402
from agent_memory.short_term.codex import CodexAdapter  # noqa: E402
from agent_memory.short_term.deepseek_harness import DeepSeekHarnessAdapter  # noqa: E402
from agent_memory.short_term.kimi_code import KimiCodeWireAdapter  # noqa: E402
from agent_memory.short_term.opencode import OpencodeAdapter  # noqa: E402
from agent_memory.short_term.pi import PiAdapter  # noqa: E402

ADAPTERS: dict[str, TranscriptAdapter] = {
    a.name: a
    for a in (
        KimiCodeWireAdapter(),
        ClaudeCodeAdapter(),
        CodexAdapter(),
        OpencodeAdapter(),
        PiAdapter(),
        DeepSeekHarnessAdapter(),
    )
}


def get_adapter(name: str) -> TranscriptAdapter:
    """按名取适配器；未知名字抛 ValueError 并列出可用适配器。"""
    try:
        return ADAPTERS[name]
    except KeyError:
        available = ", ".join(sorted(ADAPTERS))
        raise ValueError(
            f"未知的 transcript 适配器 {name!r}，可用适配器: {available}"
        ) from None


def detect_adapter(path: Path) -> TranscriptAdapter:
    """按日志路径特征猜测适配器；猜不到抛 ValueError 提示显式指定（不瞎猜）。"""
    name = path.name
    lower_parts = [p.lower() for p in path.parts]
    if name == "wire.jsonl":
        return ADAPTERS[KimiCodeWireAdapter.name]
    if name == "opencode.db":
        return ADAPTERS[OpencodeAdapter.name]
    if name == "session.jsonl.zstd" or (
        name == "session.jsonl" and ".dsh" in lower_parts
    ):
        return ADAPTERS[DeepSeekHarnessAdapter.name]
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        return ADAPTERS[CodexAdapter.name]
    if ".pi" in lower_parts and "sessions" in lower_parts and name.endswith(".jsonl"):
        return ADAPTERS[PiAdapter.name]
    if ".claude" in lower_parts and "projects" in lower_parts and name.endswith(".jsonl"):
        return ADAPTERS[ClaudeCodeAdapter.name]
    available = ", ".join(sorted(ADAPTERS))
    raise ValueError(
        f"无法按路径 {path.name!r} 识别会话日志格式，请显式指定 adapter"
        f"（可用适配器: {available}）"
    )
