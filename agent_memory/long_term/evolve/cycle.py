"""睡眠学习循环编排（五步：触发 → 定向 → 整合 → 验证 → 修剪）。

run_evolution_cycle：
1. 触发：collect_store_stats + should_run，不触发直接返回；
2. 定向：scope 参数决定本次整理范围（None = 全库）；
3. 整合：build_proposal 产出提案，写到 data/review_queue/evolution/<ts>/；
4. 验证：verify_proposal 三档（boundary / retention / safety，任一不过即否决）；
5. 修剪：三档全过才 apply_proposal（快照 + 应用 + 审计日志）；否则提案连同
   验证结果留在 review_queue/evolution/<ts>/ 交人工（不直接改记忆层）。

dry_run 只到提案为止：产出并落盘提案、打印摘要，不验证、不应用。
运行状态（上次整理时间）记 data/state/evolution_state.json。
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_memory.config import Settings
from agent_memory.io_utils import atomic_write_text
from agent_memory.llm import LLMClient, ValidatingLLMClient
from agent_memory.long_term.evolve.apply import ApplyReport, apply_proposal
from agent_memory.long_term.evolve.consolidate import build_proposal, save_proposal
from agent_memory.long_term.evolve.trigger import collect_store_stats, should_run
from agent_memory.long_term.evolve.verify import VerifyReport, verify_proposal
from agent_memory.long_term.retrieve.embedder import get_embedder
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import EvolutionProposal

_STATE_DIR = "state"
_STATE_FILE = "evolution_state.json"


def load_last_run_at(data_dir: Path) -> datetime | None:
    """读上次整理时间；状态文件不存在返回 None（从未运行）。"""
    path = Path(data_dir) / _STATE_DIR / _STATE_FILE
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(record["last_run_at"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        raise ValueError(f"整理循环状态文件损坏：{path}: {e}") from e


def save_last_run_at(data_dir: Path, at: datetime) -> None:
    state_dir = Path(data_dir) / _STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        state_dir / _STATE_FILE,
        json.dumps({"last_run_at": at.isoformat()}, ensure_ascii=False),
    )


@dataclass
class CycleReport:
    """一次整理循环的完整记录。"""

    triggered: bool
    reason: str  # 触发原因 / 未触发原因
    proposal: EvolutionProposal | None = None
    proposal_dir: Path | None = None
    verify: VerifyReport | None = None
    apply: ApplyReport | None = None
    dry_run: bool = False

    @property
    def applied(self) -> bool:
        return self.apply is not None


def run_evolution_cycle(
    settings: Settings,
    llm: LLMClient,
    embedder=None,
    scope: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> CycleReport:
    """跑一次睡眠学习循环。dry_run 只到提案为止。"""
    now = now or datetime.now()
    llm = ValidatingLLMClient(llm)
    embedder = embedder or get_embedder(settings)
    store = MarkdownStore(settings.data_dir)
    index = IndexDB(settings.data_dir / "index.db")
    try:
        last_run_at = load_last_run_at(settings.data_dir)
        stats = collect_store_stats(store, settings.data_dir, last_run_at)
        triggered, reason = should_run(stats, last_run_at, settings, now=now)
        if not triggered:
            return CycleReport(triggered=False, reason=reason, dry_run=dry_run)

        proposal = build_proposal(store, index, embedder, settings, llm, scope=scope, now=now)
        proposal_dir = save_proposal(proposal, settings.data_dir)
        report = CycleReport(
            triggered=True, reason=reason, proposal=proposal,
            proposal_dir=proposal_dir, dry_run=dry_run,
        )
        if dry_run:
            return report

        verify = verify_proposal(proposal, store, index, embedder, settings, llm)
        report.verify = verify
        # 验证结果与提案同目录留档（无论通过与否，人工可追溯）
        atomic_write_text(
            proposal_dir / "verdict.json",
            json.dumps(verify.to_dict(), ensure_ascii=False, indent=2),
        )
        if not verify.passed:
            return report  # 提案留在 review_queue/evolution/<ts>/ 交人工，不改记忆层

        report.apply = apply_proposal(
            proposal, store, index, embedder, settings, verify_report=verify, now=now
        )
        save_last_run_at(settings.data_dir, now)
        return report
    finally:
        index.close()
