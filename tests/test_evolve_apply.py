"""apply.py 测试：快照 → 晋升 → 审计日志，以及 rollback 回滚。"""

import json
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace

import pytest

from agent_memory.config import Settings
from agent_memory.long_term.evolve.apply import apply_proposal, rollback
from agent_memory.long_term.evolve.verify import TierResult, VerifyReport
from agent_memory.long_term.store.coordinator import CoordinatedWriteError, MemoryWriter
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import MarkdownStore
from agent_memory.models import (
    EvolutionChange,
    EvolutionProposal,
    FalsifiableContract,
)

NOW = datetime(2026, 8, 20, 12, 0, 0)


def _crash_mid_evolution(data_dir):
    import os

    store = MarkdownStore(data_dir)
    index = IndexDB(data_dir / "index.db")
    original_delete = MemoryWriter.delete

    def crash_on_second(self, entry_id, **kwargs):
        if entry_id == "second":
            os._exit(78)
        return original_delete(self, entry_id, **kwargs)

    MemoryWriter.delete = crash_on_second
    proposal = _proposal([
        EvolutionChange(
            kind="invalidate",
            target_ids=["first", "second"],
            reason="失效",
            contract=_contract(),
        )
    ])
    apply_proposal(
        proposal,
        store,
        index,
        UnitEmbedderForProcess(),
        Settings(data_dir=data_dir),
        verify_report=_passing_verify(),
        now=NOW,
    )


class UnitEmbedderForProcess:
    def embed_texts(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]


@pytest.fixture
def components(tmp_path, fake_embedder):
    store = MarkdownStore(tmp_path)
    index = IndexDB(tmp_path / "index.db")
    settings = Settings(data_dir=tmp_path)
    yield tmp_path, store, index, fake_embedder, settings
    index.close()


def _seed(store, index, embedder, entry):
    store.create(entry)
    index.upsert(entry, embedder.embed_texts([entry.index_text])[0])


def _contract():
    return FalsifiableContract(
        evidence="证据", root_cause="根因", expected_fix="预期修复", blast_radius="受损面"
    )


def _proposal(changes, proposal_id="evolve-20260820-test01"):
    return EvolutionProposal(
        id=proposal_id, changes=changes,
        falsifiable_contract="整体契约", created_by="test", created_at=NOW,
    )


def _passing_verify():
    return VerifyReport(
        boundary=TierResult(True, "ok"),
        retention=TierResult(True, "ok"),
        safety=TierResult(True, "ok"),
    )


def _apply(components, proposal, verify=None):
    _, store, index, embedder, settings = components
    verify = _passing_verify() if verify is None else verify
    return apply_proposal(
        proposal, store, index, embedder, settings, verify_report=verify, now=NOW
    )


class TestApply:
    def test_merge_applied_with_snapshot_and_audit(self, entry_factory, components):
        tmp_path, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="依赖管理用 uv。"))
        merged = entry_factory(entry_id="uv-c", content="包管理与依赖管理都用 uv。")
        proposal = _proposal([
            EvolutionChange(
                kind="merge", target_ids=["uv-a", "uv-b"], merged_entry=merged,
                reason="重复", contract=_contract(),
            ),
            EvolutionChange(
                kind="conflict", target_ids=["uv-a"], reason="人工裁决", contract=_contract(),
            ),
        ])

        report = _apply(components, proposal, verify=_passing_verify())

        # 记忆层：被合并者移除，合并条目入库，索引同步
        assert {e.id for e in store.list()} == {"uv-c"}
        assert index.get_meta("uv-a") is None
        assert index.get_meta("uv-c") is not None
        # conflict 是人工裁决项，跳过
        assert report.skipped == [("conflict", ["uv-a"], "人工裁决项，不自动应用")]
        # 快照：应用前的记忆层完整拷贝
        snapshot_dir = tmp_path / "snapshots" / report.snapshot_id / "memory" / "global"
        assert (snapshot_dir / "uv-a.md").exists()
        assert (snapshot_dir / "uv-b.md").exists()
        # 审计日志：提案 id、三档结果、应用时间、快照路径
        records = [
            json.loads(line)
            for line in (tmp_path / "logs" / "evolution_audit.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert len(records) == 1
        rec = records[0]
        assert rec["proposal_id"] == proposal.id
        assert rec["snapshot_id"] == report.snapshot_id
        assert rec["verify"]["passed"] is True
        assert rec["changes_applied"] == [{"kind": "merge", "target_ids": ["uv-a", "uv-b"]}]

    def test_downgrade_applied(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        proposal = _proposal([
            EvolutionChange(
                kind="downgrade", target_ids=["cold"], new_confidence="medium",
                reason="长期未检索", contract=_contract(),
            )
        ])
        _apply(components, proposal)
        updated = store.get("cold")
        assert updated.confidence == "medium"
        assert updated.version == 2  # store.update 自动 +1
        assert index.get_meta("cold")["confidence"] == "medium"

    def test_invalidate_and_archive_delete(self, entry_factory, components):
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="dead", content="已失效的事实。"))
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        proposal = _proposal([
            EvolutionChange(
                kind="invalidate", target_ids=["dead"], reason="失效", contract=_contract()
            ),
            EvolutionChange(
                kind="archive", target_ids=["cold"], reason="归档", contract=_contract()
            ),
        ])
        _apply(components, proposal)
        assert store.list() == []
        assert index.count() == 0

    def test_failed_verify_refuses_to_apply(self, entry_factory, components):
        """三档未全过的提案 fail-closed 拒绝晋升，记忆层不动。"""
        _, store, index, embedder, _ = components
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        proposal = _proposal([
            EvolutionChange(
                kind="archive", target_ids=["cold"], reason="归档", contract=_contract()
            )
        ])
        failed = VerifyReport(
            boundary=TierResult(False, "驳回"),
            retention=TierResult(True, "ok"),
            safety=TierResult(True, "ok"),
        )
        with pytest.raises(ValueError, match="拒绝晋升"):
            _apply(components, proposal, verify=failed)
        assert store.get("cold").content == "冷条目。"
        assert not (components[0] / "logs" / "evolution_audit.jsonl").exists()

    def test_missing_verify_report_refuses_to_apply(self, entry_factory, components):
        _, store, index, embedder, settings = components
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        proposal = _proposal([
            EvolutionChange(
                kind="archive", target_ids=["cold"], reason="归档", contract=_contract()
            )
        ])
        with pytest.raises(ValueError, match="缺少真实三档验证报告"):
            apply_proposal(proposal, store, index, embedder, settings, verify_report=None)
        assert store.get("cold") is not None

    def test_truthy_fake_verify_report_is_rejected(self, entry_factory, components):
        _, store, index, embedder, settings = components
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        fake = SimpleNamespace(passed=True, to_dict=lambda: {"passed": True})
        with pytest.raises(ValueError, match="真实三档验证报告"):
            apply_proposal(
                _proposal([]), store, index, embedder, settings, verify_report=fake
            )
        assert store.get("cold") is not None

    def test_string_false_in_real_verify_report_is_rejected(
        self, entry_factory, components
    ):
        _, store, index, embedder, settings = components
        _seed(store, index, embedder, entry_factory(entry_id="cold", content="冷条目。"))
        malformed = VerifyReport(
            boundary=TierResult("false", "类型错误"),  # type: ignore[arg-type]
            retention=TierResult(True, "ok"),
            safety=TierResult(True, "ok"),
        )
        with pytest.raises(ValueError, match="未通过三档验证"):
            apply_proposal(
                _proposal([
                    EvolutionChange(
                        kind="archive",
                        target_ids=["cold"],
                        reason="归档",
                        contract=_contract(),
                    )
                ]),
                store,
                index,
                embedder,
                settings,
                verify_report=malformed,
            )
        assert store.get("cold") is not None

    def test_failed_apply_does_not_erase_writer_waiting_on_evolution_lock(
        self, entry_factory, components, monkeypatch
    ):
        _, store, index, embedder, settings = components
        writer = MemoryWriter(store, index, embedder)
        writer.create(entry_factory(entry_id="before", content="进化开始前已有的事实。"))
        pending = None

        def fail_after_writer_starts(*_args, **_kwargs):
            nonlocal pending
            pending = pool.submit(
                writer.create,
                entry_factory(entry_id="concurrent", content="并发写入应在回滚后提交。"),
            )
            time.sleep(0.1)
            assert not pending.done()
            raise OSError("simulated apply failure")

        monkeypatch.setattr(
            "agent_memory.long_term.evolve.apply._apply_changes", fail_after_writer_starts
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(OSError, match="apply failure"):
                apply_proposal(
                    _proposal([]), store, index, embedder, settings,
                    verify_report=_passing_verify(), now=NOW,
                )
            pending.result(timeout=10)
        assert {entry.id for entry in store.list()} == {"before", "concurrent"}

    def test_process_crash_rolls_back_whole_evolution(
        self, tmp_path, entry_factory
    ):
        index = IndexDB(tmp_path / "index.db")
        writer = MemoryWriter(MarkdownStore(tmp_path), index, UnitEmbedderForProcess())
        writer.create(entry_factory(entry_id="first", content="第一条原始事实。"))
        writer.create(entry_factory(entry_id="second", content="第二条原始事实。"))
        index.close()

        process = multiprocessing.get_context("spawn").Process(
            target=_crash_mid_evolution, args=(tmp_path,)
        )
        process.start()
        process.join(20)
        assert process.exitcode == 78

        recovered_index = IndexDB(tmp_path / "index.db")
        recovered = MemoryWriter(
            MarkdownStore(tmp_path), recovered_index, UnitEmbedderForProcess()
        )
        assert {entry.id for entry in recovered.store.list()} == {"first", "second"}
        assert recovered.check_consistency().consistent
        assert not recovered.evolution_journal_path.exists()
        recovered_index.close()

    def test_failed_evolution_recovery_blocks_existing_writer_until_recovered(
        self, tmp_path, entry_factory, fake_embedder, monkeypatch
    ):
        store = MarkdownStore(tmp_path)
        index = IndexDB(tmp_path / "index.db")
        writer = MemoryWriter(store, index, fake_embedder)
        writer.create(entry_factory(entry_id="original", content="必须保留的原始事实。"))
        proposal = _proposal([
            EvolutionChange(
                kind="invalidate",
                target_ids=["original", "missing"],
                reason="注入第二步失败以触发整批回滚",
                contract=_contract(),
            )
        ])
        original_rebuild = index.rebuild_from_markdown

        def fail_rebuild(*_args, **_kwargs):
            raise OSError("simulated recovery rebuild failure")

        monkeypatch.setattr(index, "rebuild_from_markdown", fail_rebuild)
        with pytest.raises(OSError, match="simulated recovery rebuild failure"):
            apply_proposal(
                proposal,
                store,
                index,
                fake_embedder,
                Settings(data_dir=tmp_path),
                verify_report=_passing_verify(),
                now=NOW,
            )
        assert writer.evolution_journal_path.exists()
        assert store.get("original") is not None

        with pytest.raises(
            CoordinatedWriteError, match="进化事务自动恢复失败"
        ):
            writer.create(
                entry_factory(
                    entry_id="accepted-after-failure",
                    content="该事实不得在未恢复事务期间被接受。",
                )
            )
        with pytest.raises(KeyError):
            store.get("accepted-after-failure")

        monkeypatch.setattr(index, "rebuild_from_markdown", original_rebuild)
        writer.create(
            entry_factory(
                entry_id="accepted-after-recovery",
                content="恢复完成后才允许接受该事实。",
            )
        )
        assert {entry.id for entry in store.list()} == {
            "original",
            "accepted-after-recovery",
        }
        assert not writer.evolution_journal_path.exists()
        assert writer.check_consistency().consistent
        index.close()


class TestRollback:
    def test_rollback_restores_memory_and_index(self, entry_factory, components):
        tmp_path, store, index, embedder, settings = components
        _seed(store, index, embedder, entry_factory(entry_id="uv-a", content="包管理器用 uv。"))
        _seed(store, index, embedder, entry_factory(entry_id="uv-b", content="依赖管理用 uv。"))
        merged = entry_factory(entry_id="uv-c", content="包管理与依赖管理都用 uv。")
        proposal = _proposal([
            EvolutionChange(
                kind="merge", target_ids=["uv-a", "uv-b"], merged_entry=merged,
                reason="重复", contract=_contract(),
            )
        ])
        report = _apply(components, proposal)
        index.close()  # 释放 index.db，rollback 重建时另开连接（Windows 文件锁）
        assert {e.id for e in store.list()} == {"uv-c"}

        rollback(report.snapshot_id, settings, embedder)

        assert {e.id for e in store.list()} == {"uv-a", "uv-b"}
        # 索引也重建：merge 产物不在，原条目可检索
        new_index = IndexDB(tmp_path / "index.db")
        try:
            assert new_index.get_meta("uv-c") is None
            assert new_index.get_meta("uv-a") is not None
        finally:
            new_index.close()
        # 回滚前的状态也留了现场（回滚本身可回滚）
        assert (tmp_path / "snapshots" / report.snapshot_id / "pre_rollback_memory"
                / "global" / "uv-c.md").exists()

    def test_rollback_missing_snapshot_fails(self, components):
        _, _, _, embedder, settings = components
        with pytest.raises(FileNotFoundError, match="无法回滚"):
            rollback("20990101T000000", settings, embedder)
