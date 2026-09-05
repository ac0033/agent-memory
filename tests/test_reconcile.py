"""reconcile.py 测试：ADD / UPDATE / DELETE / NOOP 四分支 + 冲突进复核队列。

近邻构造依赖 conftest.FakeEmbedder 的关键词向量：共享关键词 → cosine 距离 0
（近邻），不含共同关键词 → 正交（距离 1.0，非近邻）。
"""

import pytest
import yaml

from agent_memory.llm import LLMError
from agent_memory.long_term.ingest.reconcile import reconcile
from agent_memory.long_term.store.markdown_store import MarkdownStore


class DecideLLM:
    """按候选 id 返回脚本化决策的 fake LLM。"""

    def __init__(self, decisions: dict[str, dict] | None = None, error: bool = False):
        self.decisions = decisions or {}
        self.error = error

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, user: str, schema_description: str) -> dict:
        if self.error:
            raise LLMError("模拟 LLM 输出连续无法解析")
        for cid, decision in self.decisions.items():
            if f"id: {cid}" in user:
                return decision
        return {"action": "ADD", "target_id": None, "reason": "默认"}


@pytest.fixture
def components(tmp_path, fake_embedder):
    from agent_memory.config import Settings

    store = MarkdownStore(tmp_path)
    index_path = tmp_path / "index.db"
    from agent_memory.long_term.store.index_db import IndexDB

    index = IndexDB(index_path)
    settings = Settings(data_dir=tmp_path)
    yield store, index, fake_embedder, settings
    index.close()


def _seed(store, index, embedder, entry):
    store.create(entry)
    index.upsert(entry, embedder.embed_texts([entry.content])[0])


def _reconcile(candidates, components, llm):
    store, index, embedder, settings = components
    return reconcile(candidates, store, index, llm, embedder=embedder, settings=settings)


def test_add_when_no_neighbors(entry_factory, components):
    candidate = entry_factory(entry_id="new-one", content="本项目 dev server 端口固定 8765。")
    report = _reconcile([candidate], components, DecideLLM())
    assert report.counts()["add"] == 1
    store, _, _, _ = components
    assert store.get("new-one").content == candidate.content


def test_add_when_neighbors_too_far(entry_factory, components):
    store, index, embedder, _ = components
    _seed(
        store, index, embedder,
        entry_factory(entry_id="uv-entry", content="用户偏好用 uv 管理环境。"),
    )
    # 不含共同关键词 → 向量正交，距离 1.0 低于近邻阈值
    candidate = entry_factory(entry_id="new-one", content="本项目 dev server 端口固定 8765。")
    report = _reconcile([candidate], components, DecideLLM())
    assert report.counts()["add"] == 1
    assert store.get("uv-entry") is not None  # 远邻不受影响


def test_direct_add_id_conflict_is_queued_and_batch_continues(
    entry_factory, components
):
    store, index, embedder, _ = components
    original = entry_factory(
        entry_id="occupied-id",
        scope="repo:other",
        content="另一作用域中的既有事实。",
    )
    _seed(store, index, embedder, original)
    conflict = entry_factory(
        entry_id="occupied-id",
        scope="global",
        content="候选不能覆盖同 ID 的既有事实。",
    )
    later = entry_factory(
        entry_id="later-candidate",
        scope="global",
        content="本项目 dev server 端口固定 8765。",
    )

    report = _reconcile([conflict, later], components, DecideLLM())

    assert report.added == ["later-candidate"]
    assert [entry.id for entry, _reason in report.queued] == ["occupied-id"]
    assert len(report.queued_files) == 1
    assert store.get("occupied-id") == original
    assert store.get("later-candidate") == later


def test_llm_add_id_conflict_is_queued_and_batch_continues(
    entry_factory, components
):
    store, index, embedder, _ = components
    original = entry_factory(
        entry_id="occupied-id",
        scope="repo:other",
        content="另一作用域中的既有事实。",
    )
    neighbor = entry_factory(
        entry_id="uv-neighbor",
        scope="global",
        content="既有项目使用 uv 管理环境。",
    )
    _seed(store, index, embedder, original)
    _seed(store, index, embedder, neighbor)
    conflict = entry_factory(
        entry_id="occupied-id",
        scope="global",
        content="新候选也提到 uv，但不能覆盖占用的 ID。",
    )
    later = entry_factory(
        entry_id="later-candidate",
        scope="global",
        content="用户机器时区为 UTC+8。",
    )
    llm = DecideLLM({
        "occupied-id": {"action": "ADD", "target_id": None, "reason": "独立事实"}
    })

    report = _reconcile([conflict, later], components, llm)

    assert report.added == ["later-candidate"]
    assert [entry.id for entry, _reason in report.queued] == ["occupied-id"]
    assert len(report.queued_files) == 1
    assert store.get("occupied-id") == original
    assert store.get("later-candidate") == later


def test_update_inherits_version_and_supersedes(entry_factory, components):
    store, index, embedder, _ = components
    old = entry_factory(
        entry_id="port-old", content="本项目 dev server 端口固定 8765。", version=3
    )
    _seed(store, index, embedder, old)
    candidate = entry_factory(
        entry_id="port-new", content="本项目 dev server 端口已改为 8766（8765 被占用）。"
    )
    llm = DecideLLM({"port-new": {"action": "UPDATE", "target_id": "port-old", "reason": "修正"}})
    report = _reconcile([candidate], components, llm)
    assert report.counts()["update"] == 1
    assert report.updated == [("port-new", "port-old")]
    with pytest.raises(KeyError):
        store.get("port-old")  # 旧条目已删除
    new = store.get("port-new")
    assert new.version == 4  # 继承旧 version +1
    assert new.supersedes == "port-old"


def test_delete_removes_old_without_adding(entry_factory, components):
    store, index, embedder, _ = components
    _seed(
        store, index, embedder,
        entry_factory(entry_id="port-old", content="本项目 dev server 端口固定 8765。"),
    )
    candidate = entry_factory(
        entry_id="port-gone", content="本项目 dev server 端口约定已废弃，不再有固定端口。"
    )
    llm = DecideLLM(
        {"port-gone": {"action": "DELETE", "target_id": "port-old", "add_new": False}}
    )
    report = _reconcile([candidate], components, llm)
    assert report.counts()["delete"] == 1
    with pytest.raises(KeyError):
        store.get("port-old")
    with pytest.raises(KeyError):
        store.get("port-gone")  # add_new=False，新条目不入库


def test_noop_only_refreshes_last_verified(entry_factory, components):
    from datetime import date

    store, index, embedder, _ = components
    old = entry_factory(entry_id="port-old", content="本项目 dev server 端口固定 8765。")
    _seed(store, index, embedder, old)
    candidate = entry_factory(
        entry_id="port-dup", content="本项目 dev server 端口固定 8765。"  # 与旧条目同义
    )
    llm = DecideLLM({"port-dup": {"action": "NOOP", "target_id": "port-old", "reason": "重复"}})
    report = _reconcile([candidate], components, llm)
    assert report.counts()["noop"] == 1
    with pytest.raises(KeyError):
        store.get("port-dup")  # 候选不入库
    refreshed = store.get("port-old")
    assert refreshed.last_verified == date.today()


def test_conflict_goes_to_review_queue(entry_factory, components):
    store, index, embedder, _ = components
    _seed(
        store, index, embedder,
        entry_factory(entry_id="port-old", content="本项目 dev server 端口固定 8765。"),
    )
    candidate = entry_factory(
        entry_id="port-conflict", content="本项目 dev server 端口固定 9999（另一会话记录）。"
    )
    llm = DecideLLM(
        {"port-conflict": {"action": "CONFLICT", "target_id": None, "reason": "两条矛盾各有证据"}}
    )
    report = _reconcile([candidate], components, llm)
    assert report.counts()["queued"] == 1
    assert store.get("port-old") is not None  # 冲突不强行收敛，旧条目保持
    with pytest.raises(KeyError):
        store.get("port-conflict")
    assert len(report.queued_files) == 1
    payload = yaml.safe_load(report.queued_files[0].read_text(encoding="utf-8"))
    assert payload["entry"]["id"] == "port-conflict"
    assert "对账" in payload["reason"]


def test_llm_error_goes_to_review_queue(entry_factory, components):
    store, index, embedder, _ = components
    _seed(
        store, index, embedder,
        entry_factory(entry_id="port-old", content="本项目 dev server 端口固定 8765。"),
    )
    candidate = entry_factory(entry_id="port-new", content="本项目 dev server 端口改为 8766。")
    report = _reconcile([candidate], components, DecideLLM(error=True))
    assert report.counts()["queued"] == 1
    assert "LLM 决策不可用" in report.queued[0][1]


def test_unknown_target_id_goes_to_review_queue(entry_factory, components):
    store, index, embedder, _ = components
    _seed(
        store, index, embedder,
        entry_factory(entry_id="port-old", content="本项目 dev server 端口固定 8765。"),
    )
    candidate = entry_factory(entry_id="port-new", content="本项目 dev server 端口改为 8766。")
    llm = DecideLLM(
        {"port-new": {"action": "UPDATE", "target_id": "not-a-neighbor", "reason": "幻觉目标"}}
    )
    report = _reconcile([candidate], components, llm)
    assert report.counts()["queued"] == 1
    assert "不在近邻集合" in report.queued[0][1]
    assert store.get("port-old") is not None  # 未被执行


def test_update_triggers_propagation_to_dependents(entry_factory, components):
    """通病 B：UPDATE 落库后反向传播，依赖旧事实的 procedural 近邻被判失效删除。"""
    store, index, embedder, _ = components
    _seed(
        store, index, embedder,
        entry_factory(entry_id="port-old", content="本项目 dev server 端口固定 8765。"),
    )
    _seed(
        store, index, embedder,
        entry_factory(
            entry_id="check-port-first",
            content="排查启动失败时先看 dev server 端口 8765 是否被占用。",
            memory_type="procedural",
        ),
    )
    candidate = entry_factory(entry_id="port-new", content="本项目 dev server 端口已改为 8766。")
    llm = DecideLLM(
        {
            "port-new": {"action": "UPDATE", "target_id": "port-old", "reason": "端口变更"},
            # DecideLLM 按 "id: X" 匹配，传播 prompt 里的近邻 id 也会命中
            "check-port-first": {"verdict": "INVALIDATED", "reason": "前提已失效"},
        }
    )
    report = _reconcile([candidate], components, llm)
    assert report.counts()["update"] == 1
    assert len(report.propagation) == 1
    assert report.propagation[0].invalidated == ["check-port-first"]
    with pytest.raises(KeyError):
        store.get("check-port-first")  # 依赖旧事实的 procedural 已被传播删除
    assert store.get("port-new") is not None  # 变更本身正常落库


def test_decide_calls_run_concurrently(entry_factory, components):
    """两阶段结构：第一阶段的 LLM 决策并发执行（写路径耗时大头，不能串行等）。

    用记录线程 id 的 fake LLM 验证确实跑了多个线程；落库结果与串行版一致。
    """
    import threading

    store, index, embedder, _ = components
    _seed(
        store, index, embedder,
        entry_factory(entry_id="port-old", content="本项目 dev server 端口固定 8765。"),
    )

    thread_ids: set[int] = set()

    class ThreadRecordingLLM:
        def complete(self, system: str, user: str) -> str:
            raise NotImplementedError

        def complete_json(self, system: str, user: str, schema_description: str) -> dict:
            thread_ids.add(threading.get_ident())
            return {"action": "ADD", "target_id": None, "reason": "并发验证"}

    candidates = [
        entry_factory(entry_id=f"port-note-{i}", content=f"端口相关的第 {i} 条补充说明。")
        for i in range(4)
    ]
    report = _reconcile(candidates, components, ThreadRecordingLLM())
    assert report.counts()["add"] == 4
    assert len(thread_ids) > 1  # 串行执行时全部调用都在同一线程


# ---- M9：无 LLM 规则降级（llm=None）----


def test_no_llm_add_when_no_neighbors(entry_factory, components):
    """无 LLM 降级：无近邻的候选直接 ADD（纯规则，不需要 LLM）。"""
    candidate = entry_factory(entry_id="new-one", content="本项目 dev server 端口固定 8765。")
    report = _reconcile([candidate], components, None)
    assert report.counts()["add"] == 1
    store, _, _, _ = components
    assert store.get("new-one").content == candidate.content


def test_no_llm_neighbors_queued_not_crash(entry_factory, components):
    """无 LLM 降级：有近邻时不猜关系，进复核队列（fail-safe）。

    同时是 _add_direct 崩溃 bug 的回归测试：旧实现会对 None 调 complete_json
    抛 AttributeError。
    """
    store, index, embedder, _ = components
    _seed(
        store, index, embedder,
        entry_factory(entry_id="uv-entry", content="用户偏好用 uv 管理环境。"),
    )
    # 共享关键词 "uv" → cosine 距离 0，是近邻
    candidate = entry_factory(entry_id="uv-new", content="用户确认用 uv 跑测试。")
    report = _reconcile([candidate], components, None)
    assert report.counts()["add"] == 0
    assert len(report.queued) == 1
    assert "未配置 LLM" in report.queued[0][1]
    assert report.queued_files  # 进复核队列文件，不丢
    with pytest.raises(KeyError):
        store.get("uv-new")  # 未落库
    assert store.get("uv-entry") is not None  # 既有条目不受影响
