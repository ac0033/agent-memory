"""agent-memory：本地长期记忆基础设施（agent 中立）。"""

from agent_memory.config import Settings, get_settings
from agent_memory.models import EvidenceRef, MemoryEntry, MemoryProposal

__version__ = "0.3.4"

__all__ = [
    "EvidenceRef",
    "MemoryEntry",
    "MemoryProposal",
    "Settings",
    "__version__",
    "get_settings",
]
