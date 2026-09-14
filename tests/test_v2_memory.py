"""v0.2 能力的单元测试：原文归档检索、完整度核验、主动浮现、遗忘请求、确认队列、
工作记忆整理、事件边界打包、双时态版本史，以及对老数据与老输出的兼容性。

LLM 一律用按系统提示词路由的脚本化 fake，embedder 用 conftest 的确定性 FakeEmbedder。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from agent_memory.config import Settings
from agent_memory.long_term.retrieve.hybrid import SearchResult
from agent_memory.long_term.retrieve.inject import render_memory
from agent_memory.long_term.store.index_db import IndexDB
from agent_memory.long_term.store.markdown_store import (
    MarkdownStore,
    entry_from_markdown,
    entry_to_markdown,
)
from agent_memory.long_term.store.raw_index import RawIndex
from agent_memory.models import EvidenceRef, VersionRecord
from agent_memory.server.mcp_server import MemoryService
from tests.conftest import make_entry


class RoutingLLM:
    """按系统提示词里的关键词返回预设 JSON；记录调用以便断言。"""

    def __init__(self, routes: dict[str, dict]):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def complete(self, system, user):
        return "ok"

    def complete_json(self, system, user, schema_description):
        self.calls.append((system[:40], user))
        for key, payload in self.routes.items():
            if key in system:
                return payload(user) if callable(payload) else payload
        return {"memories": []}


@pytest.fixture
def mk_service(tmp_path, fake_embedder):
    made = []

    def _make(routes: dict | None = None, llm=True):
        settings = Settings(data_dir=tmp_path)
        store = MarkdownStore(tmp_path)
        index = IndexDB(tmp_path / "index.db")
        svc = MemoryService(
            settings, store, index, fake_embedder, RoutingLLM(routes or {}) if llm else None
        )
        made.append(svc)
        return svc

    yield _make
    for s in made:
        s.index.close()
        s.raw_index.close()


# ---------------------------------------------------------------- 模型与兼容


def test_v2_fields_roundtrip_and_old_markdown_unchanged():
    e = make_entry()
    text = entry_to_markdown(e)
    for key in ("completeness", "verify_flag", "valid_from", "history", "source_type"):
        assert key not in text  # 默认值不写出，老文件内容保持不变
    e2 = e.model_copy(
        update={
            "completeness": "gist",
            "valid_from": date(2026, 8, 1),
            "history": [VersionRecord(content="旧值", valid_from=date(2026, 7, 1))],
        }
    )
    back = entry_from_markdown(entry_to_markdown(e2))
    assert back.completeness == "gist" and back.valid_from == date(2026, 8, 1)
    assert back.history[0].content == "旧值"


def test_validity_window_must_be_ordered():
    with pytest.raises(ValueError):
        make_entry().model_copy(update={}).model_validate(
            make_entry().model_dump()
            | {"valid_from": date(2026, 9, 2), "valid_to": date(2026, 9, 1)}
        )


def test_render_unchanged_for_old_entries_and_hints_for_new():
    e = make_entry()
    old = render_memory(SearchResult(e, 1.0, 1.0, 1, 1, e.content))
    assert "completeness" not in old and "recorded" not in old
    g = e.model_copy(
        update={
            "completeness": "gist",
            "verify_flag": "mismatch",
            "evidence": [EvidenceRef(session_id="s1", source="eval", line_range=(3, 4))],
        }
    )
    new = render_memory(SearchResult(g, 1.0, 1.0, 1, 1, g.content))
    assert 'completeness="gist"' in new and "eval/s1 第 3–4 行" in new and "不一致" in new


def test_retro_hint_only_when_an_older_version_was_superseded():
    # 首次记下的季度 OKR：生效日（季度初）早于记录日，但并没有"旧说法"可言
    e = make_entry().model_copy(
        update={"created_at": date(2026, 8, 12), "valid_from": date(2026, 7, 1)}
    )
    plain = render_memory(SearchResult(e, 1.0, 1.0, 1, 1, e.content))
    assert "追溯更正" not in plain
    old = VersionRecord(
        content="站会每周一 10:00", valid_from=date(2026, 8, 3), recorded_at=date(2026, 8, 3)
    )
    h = e.model_copy(update={"history": [old]})
    corrected = render_memory(SearchResult(h, 1.0, 1.0, 1, 1, h.content))
    assert "追溯更正" in corrected and "变更史" in corrected


def test_distill_prompt_counts_relayed_answers_as_user_source():
    # P15 只拦网帖、传言、粘贴材料；用户转述知情方（法务等）的明确答复由用户背书，照常沉淀
    from agent_memory.long_term.ingest.distill import _SYSTEM_PROMPT

    assert "用户转述" in _SYSTEM_PROMPT and "source_type 填 user" in _SYSTEM_PROMPT


def test_patch_meta_keeps_version_and_last_verified(store):
    e = make_entry(last_verified=date(2026, 8, 1))
    store.create(e)
    out = store.patch_meta(e.id, completeness="gist")
    assert out.completeness == "gist" and out.version == 1 and out.last_verified == date(2026, 8, 1)
    with pytest.raises(ValueError):
        store.patch_meta(e.id, content="改正文")


# ---------------------------------------------------------------- P02 / P03 原文归档


def test_archive_redacts_secrets_and_is_searchable(mk_service):
    svc = mk_service()
    svc.archive_records(
        [{"role": "user", "content": "测试服务器端口 8765，api_key=abcd1234efgh5678"}],
        source="eval",
        session_id="s1",
        session_date="2026-09-01",
        scope="global",
    )
    raw = (svc.settings.data_dir / "raw" / "eval" / "s1.jsonl").read_text(encoding="utf-8")
    assert "abcd1234efgh5678" not in raw and "[REDACTED" in raw
    out = svc.archive_search("端口", scope="global")
    assert (
        out["hits"]
        and out["hits"][0]["session_id"] == "s1"
        and out["hits"][0]["date"] == "2026-09-01"
    )
    read = svc.archive_read("eval", "s1")
    assert "8765" in read["text"]


def test_raw_index_rebuild_matches(tmp_path, mk_service, fake_embedder):
    svc = mk_service()
    svc.archive_records(
        [
            {"role": "user", "content": "Windows 端口 3100"},
            {"role": "assistant", "content": "记下了"},
        ],
        source="eval",
        session_id="s2",
    )
    idx = RawIndex(tmp_path / "rebuilt.db")
    try:
        assert idx.rebuild_from_raw(tmp_path / "raw", fake_embedder) == 2
    finally:
        idx.close()


def test_search_appends_raw_evidence_for_gist_memory(mk_service):
    svc = mk_service()
    svc.archive_records(
        [
            {
                "role": "user",
                "content": "周报规则：文件名 weekly_YYYYMMDD.csv，编码 UTF-8-BOM，端口无关",
            }
        ],
        source="eval",
        session_id="s1",
    )
    e = make_entry(
        "weekly-rules", "用户定了周报规则（端口等细节见原文）。", memory_type="procedural"
    )
    e = e.model_copy(
        update={
            "completeness": "gist",
            "evidence": [EvidenceRef(session_id="s1", source="eval", line_range=(1, 1))],
        }
    )
    svc.writer.create(e)
    out = svc.search("周报端口规则", scope="global")
    assert "<raw_evidence>" in out["block"] and out["archive_hits"]


# ---------------------------------------------------------------- P27 / P13


def test_annotate_marks_gist_and_mismatch_without_bumping_version(mk_service):
    svc = mk_service(
        {
            "记忆核验员": lambda user: {
                "items": [{"id": "rate-limit", "completeness": "gist", "consistent": False}]
            }
        }
    )
    svc.archive_records(
        [{"role": "user", "content": "对方限流每分钟 60 次"}], source="eval", session_id="s1"
    )
    e = make_entry(
        "rate-limit", "对方 API 限流约为每分钟 100 次。", last_verified=date(2026, 8, 26)
    )
    e = e.model_copy(
        update={"evidence": [EvidenceRef(session_id="s1", source="eval", line_range=(1, 1))]}
    )
    svc.writer.create(e)
    res = svc.annotate_completeness(["rate-limit"])
    got = svc.store.get("rate-limit")
    assert (
        res["annotated"]["rate-limit"]
        and got.completeness == "gist"
        and got.verify_flag == "mismatch"
    )
    assert got.last_verified == date(2026, 8, 26) and got.version == 1


# ---------------------------------------------------------------- P24–P26


def test_surface_returns_block_with_association_hint(mk_service):
    svc = mk_service(
        {
            "扩展线索": {"entities": ["Windows"], "constraints": [], "principles": []},
            "记忆副手": lambda user: {
                "surface": [{"id": "dev-os", "relation": "direct", "why": "脚本要在 Windows 上跑"}]
            },
        }
    )
    svc.writer.create(make_entry("dev-os", "用户的开发机是 Windows。"))
    out = svc.surface("帮我写个 Windows 部署脚本", scope="global", date="2026-09-10")
    assert out["decision"] == "surface" and "<surfaced_memories>" in out["block"]
    assert "脚本要在 Windows 上跑" in out["block"]


def test_surface_silent_without_llm_or_when_copilot_declines(mk_service):
    svc = mk_service(llm=False)
    svc.writer.create(make_entry("dev-os", "用户的开发机是 Windows。"))
    assert svc.surface("Windows 脚本")["block"] == ""
    svc2 = mk_service({"记忆副手": {"surface": []}})
    assert svc2.surface("Windows 脚本")["decision"] == "silent"


# ---------------------------------------------------------------- K12 遗忘


def test_forget_request_deletes_memory_scrubs_raw_and_audits_metadata_only(mk_service):
    def plan(user):
        ref = next(
            line.split("｜")[0]
            for line in user.splitlines()
            if line.startswith("L") and "LARK-7731" in line
        )
        return {
            "memory_ids": ["side-project"],
            "rewrite": [],
            "raw_edits": [{"line_ref": ref, "remove_text": "副业项目代号 LARK-7731"}],
        }

    svc = mk_service({"记忆删除执行员": plan})
    svc.archive_records(
        [{"role": "user", "content": "第一，副业项目代号 LARK-7731。第二，评审我讲缓存方案。"}],
        source="eval",
        session_id="s1",
        session_date="2026-08-20",
    )
    svc.writer.create(
        make_entry("side-project", "用户在做副业项目，代号 LARK-7731。").model_copy(
            update={"evidence": [EvidenceRef(session_id="s1", source="eval", line_range=(1, 1))]}
        )
    )
    svc.writer.create(make_entry("review-cache", "架构评审由用户讲缓存方案。"))
    res = svc.forget_request("8 月 20 日说的第一件事", scope="global", request_date="2026-09-01")
    assert res["deleted"] == 1 and res["raw_lines_scrubbed"] == 1
    raw = (svc.settings.data_dir / "raw" / "eval" / "s1.jsonl").read_text(encoding="utf-8")
    assert "LARK-7731" not in raw and "缓存方案" in raw
    assert svc.store.get("review-cache")
    assert not any("LARK" in h["content"] for h in svc.archive_search("LARK-7731 副业")["hits"])
    audit = (svc.settings.data_dir / "logs" / "forget_audit.jsonl").read_text(encoding="utf-8")
    assert "LARK" not in audit and "side-project" not in audit


def test_add_conversation_executes_distilled_forget_request(mk_service):
    calls = {}

    def distill(user):
        return {"memories": [], "forget_requests": [{"description": "删除第一件事", "turn": 1}]}

    def plan(user):
        calls["planned"] = True
        return {"memory_ids": [], "raw_edits": []}

    svc = mk_service({"对话记忆蒸馏器": distill, "记忆删除执行员": plan})
    rep = svc.add(
        conversation_json=json.dumps(
            [{"role": "user", "content": "上次说的第一件事忘掉吧"}], ensure_ascii=False
        ),
        scope="global",
        source="eval",
        session_id="s2",
        session_date="2026-09-01",
    )
    assert calls.get("planned") and rep["forgotten"][0]["status"] == "ok"


# ---------------------------------------------------------------- P19


def test_distill_session_date_and_validity_fields(mk_service):
    def distill(user):
        assert "会话日期：2026-08-03" in user
        return {
            "memories": [
                {
                    "id": "standup",
                    "content": "站会自 8 月 31 日那周起改为每周二 9:30。",
                    "memory_type": "semantic",
                    "confidence": "high",
                    "evidence_turns": [1, 1],
                    "valid_from": "2026-08-31",
                    "completeness": "complete",
                    "source_type": "user",
                }
            ]
        }

    svc = mk_service({"对话记忆蒸馏器": distill})
    svc.add(
        conversation_json=json.dumps(
            [{"role": "user", "content": "站会改到周二 9:30"}], ensure_ascii=False
        ),
        scope="global",
        source="eval",
        session_id="s1",
        session_date="2026-08-03",
    )
    e = svc.store.get("standup")
    assert (
        e.created_at == date(2026, 8, 3)
        and e.valid_from == date(2026, 8, 31)
        and e.completeness == "complete"
    )


def test_third_party_fact_goes_to_review_not_memory(mk_service):
    svc = mk_service(
        {
            "对话记忆蒸馏器": {
                "memories": [
                    {
                        "id": "rumor",
                        "content": "网帖称该 API 限流已放宽到每分钟 1370 次。",
                        "memory_type": "semantic",
                        "confidence": "high",
                        "evidence_turns": [1, 1],
                        "source_type": "third_party",
                    }
                ]
            }
        }
    )
    rep = svc.add(
        conversation_json=json.dumps(
            [{"role": "user", "content": "网上帖子说放宽到 1370 次"}], ensure_ascii=False
        ),
        scope="global",
        source="eval",
        session_id="s1",
    )
    assert rep["gate_queued"] == ["rumor"]
    with pytest.raises(KeyError):
        svc.store.get("rumor")


def test_update_carries_version_history(mk_service):
    from agent_memory.long_term.ingest.reconcile import _carry_history

    old = make_entry("standup", "站会每周一 10:00。").model_copy(
        update={"created_at": date(2026, 8, 3)}
    )
    new = make_entry("standup-2", "站会改为每周二 9:30。").model_copy(
        update={"created_at": date(2026, 9, 10), "valid_from": date(2026, 8, 31)}
    )
    hist = _carry_history(new, old)
    assert hist[-1].content == "站会每周一 10:00。" and hist[-1].valid_to == date(2026, 8, 31)
    assert hist[-1].recorded_at == date(2026, 8, 3)


# ---------------------------------------------------------------- P29 / P07 / P08 / P06


def test_confirmation_queue_roundtrip(mk_service):
    svc = mk_service()
    item = svc.confirm_enqueue("15 个 SKU 差异超过 20%，是否覆盖？", scope="global")
    assert svc.confirm_list()["pending_count"] == 1
    svc.confirm_resolve(item["id"], "reject", "先不覆盖")
    assert svc.confirm_list()["pending_count"] == 0
    with pytest.raises(ValueError):
        svc.confirm_resolve("../x", "approve")


def test_wm_refresh_builds_structured_state(mk_service):
    svc = mk_service(
        {
            "维护 agent 的工作记忆": {
                "goal": "订单导出支持 CSV",
                "constraints": ["单文件不超过 3 万行"],
                "todos": [
                    {"content": "导出任务队列", "status": "done"},
                    {"content": "CSV 分片", "status": "pending"},
                ],
                "open_questions": ["保留几天"],
                "variables": {"shard_rows": "30000"},
                "subtasks": [
                    {
                        "name": "B",
                        "goal": "修时区 bug",
                        "todos": [{"content": "前端去掉 +8", "status": "pending"}],
                    }
                ],
            }
        }
    )
    svc.wm_refresh("repo:work", [{"role": "user", "content": "阈值改成 3 万"}], current_turn=6)
    wm = svc.wm_read("repo:work")
    assert "### 约束" in wm["block"] and "【B】" in wm["block"] and wm["turn_watermark"] == 6


def test_episode_pack_creates_card_and_syncs_constraints(mk_service):
    svc = mk_service(
        {
            "上下文马上要被压缩": {
                "title": "切换只读副本",
                "details": ["ro-2.db.example.internal:54329"],
                "constraints": ["密码走环境变量 RPT_RO_PASS"],
                "decisions": [],
                "file_changes": [],
            }
        }
    )
    out = svc.episode_pack(
        [{"role": "user", "content": "只读副本 ro-2.db.example.internal:54329"}],
        scope="repo:work",
        session_id="live",
    )
    e = svc.store.get(out["created"])
    assert "54329" in e.content and e.completeness == "complete"
    assert "RPT_RO_PASS" in svc.wm_read("repo:work")["block"]
