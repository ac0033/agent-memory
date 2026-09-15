"""把 eval-drafts v0.1 的 24 条草稿用例迁移为 memcompass/item@0.1（suite-design §4.5 的映射表）。

已在 examples.yaml 中手工迁移过的草稿（pr-01、pr-02、ca-01、ca-03、ca-12）跳过；
其余 19 条按映射表机械迁移，写入各子集的 migrated.yaml：
- pr-03 … pr-12 → pr-0007 … pr-0016
- ca-02、ca-04 … ca-11 → ca-0006 … ca-0014

迁移时补齐 v0.2 的约定字段：earliest_turn、tolerated_mentions、completeness_truth、
6 条背景记忆（与新增条目同一口径）。草稿的文字内容不改动（只有 ca 的
simulated_user_reply 从单值变成列表）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcb import (  # noqa: E402
    DATASETS,
    ROOT,
    a,
    assign_splits,
    item,
    mem,
    ref_time,
    session,
    today_prefix,
    u,
    write_items,
)
from pools import background, background_date  # noqa: E402

DRAFTS = ROOT.parent / "eval-drafts"
SKIP = {"pr-01", "pr-02", "ca-01", "ca-03", "ca-12"}

PR_MAP = {f"pr-{i:02d}": f"pr-{i + 4:04d}" for i in range(3, 13)}
CA_MAP = {"ca-02": "ca-0006", **{f"ca-{i:02d}": f"ca-{i + 3:04d}" for i in range(4, 12)}}

PR_SUBTYPE = {
    "pr-03": "data_caveat", "pr-04": "pitfall_reuse", "pr-05": "situational", "pr-06": "alias",
    "pr-07": "off_task", "pr-08": "off_task", "pr-09": "lexical_overlap", "pr-10": "same_name_other_entity",
    "pr-11": "superseded", "pr-12": "superseded",
}
CA_TRUTH = {
    "need_backfill": "gist", "gist_sufficient": "complete", "need_confirm": "gist",
    "no_confirm_needed": "complete", "unattended": "gist", "misremember_trap": "wrong",
}


def _load(name: str) -> list[dict]:
    path = DRAFTS / name / "cases.yaml"
    return [x for x in yaml.safe_load_all(path.read_text(encoding="utf-8")) if x]


def _sessions_and_mems(case: dict) -> tuple[list[dict], list[dict]]:
    sessions, mems = [], []
    for s in case["sessions"]:
        sessions.append(session(str(s["session_id"]), str(s["date"]),
                                [u(t["content"].strip()) if t["role"] == "user" else a(t["content"].strip())
                                 for t in s["turns"]]))
        for m in s.get("memories") or []:
            extra = {}
            if m.get("confidence"):
                extra["confidence"] = m["confidence"]
            if m.get("last_verified"):
                extra["last_verified"] = str(m["last_verified"])
            if m.get("supersedes"):
                extra["supersedes"] = m["supersedes"]
            mems.append(mem(m["id"], str(s["session_id"]), m["content"], m["memory_type"], **extra))
    return sessions, mems


def migrate_pr(case: dict) -> dict:
    new_id = PR_MAP[case["id"]]
    sessions, mems = _sessions_and_mems(case)
    bg_s, bg_m = background(new_id, date=background_date(sessions))
    g = case["gold"]
    date = str(case["trigger"]["date"])
    positive = bool(g["should_surface"])
    labels = {
        "should_surface": positive,
        "target_memory_ids": g.get("target_memory_ids") or [],
        "key_details": g.get("key_details") or [],
        "stale_details": g.get("stale_details") or [],
        # v0.3：同名不同人的负例里，只为消歧而简短提醒"记录里另有同名的另一位"不算浮现（2026-09-15）
        "tolerated_mentions": (["简短提醒记录里另有同名的另一位（只为消歧，不带入其内容）"]
                               if PR_SUBTYPE.get(case["id"]) == "same_name_other_entity" else []),
        "earliest_turn": 1,
        "max_turn": int(g.get("max_turn") or 1),
    }
    probe = {
        "probe_id": "q1", "kind": "trigger", "contract": "proactive-v0", "at": {"new_session": True},
        "reference_time": ref_time(date),
        "query": today_prefix(date) + case["trigger"]["user_message"].strip(),
        "gold": {"nuggets": case["rubric"]["essential"], "pitfalls": case["rubric"]["pitfalls"], "labels": labels},
    }
    return item(
        iid=new_id, subset="mc-proactive-recall", type_=case["type"], subtype=PR_SUBTYPE[case["id"]],
        tracks=["behavior", "system", "qa"],
        primary=["K8", "K7"] if positive else ["K8"], secondary=["K5"] if case["type"] == "time_trap" else [],
        aml=["N1"] if positive else ["N2"],
        sessions=[bg_s] + sessions, memories=bg_m + mems, probes=[probe],
        behavior={"from_probe": "q1", "tools": ["memory_search", "act"], "rubric": case["rubric"]},
        template_id=f"pr-draft-{case['type']}", group=new_id, provenance="migrated-draft",
        legacy_id=case["id"], notes=case.get("description"),
    )


def migrate_ca(case: dict) -> dict:
    new_id = CA_MAP[case["id"]]
    sessions, mems = _sessions_and_mems(case)
    bg_s, bg_m = background(new_id, date=background_date(sessions))
    trig = case["trigger"]
    unattended = bool(case.get("unattended"))
    tools = ["memory_search", "archive_search", "archive_read"]
    tools += ["queue_confirmation"] if unattended else ["ask_user"]
    tools += ["act"]
    trigger = {"date": str(trig["date"])}
    if trig.get("context"):
        trigger["context"] = trig["context"]
    trigger["turns"] = [u(trig["user_message"].strip())]
    reply = case.get("simulated_user_reply")
    expected = dict(case["expected"])
    expected["completeness_truth"] = CA_TRUTH[case["type"]]
    return item(
        iid=new_id, subset="mc-completeness-alignment", type_=case["type"],
        tracks=["behavior"],
        primary=["K10"] if case["type"] in {"need_confirm", "no_confirm_needed", "unattended"} else ["K9"],
        secondary=["K9"] if case["type"] in {"need_confirm", "unattended"} else ["K4"],
        aml=["N3"],
        sessions=[bg_s] + sessions, memories=bg_m + mems, probes=[],
        behavior={"trigger": trigger, "tools": tools, "unattended": unattended,
                  "simulated_user": {"replies": [reply] if reply else []},
                  "expected": expected, "rubric": case["rubric"]},
        template_id=f"ca-draft-{case['type']}", group=new_id, provenance="migrated-draft",
        legacy_id=case["id"], notes=case.get("description"),
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    pr = [migrate_pr(c) for c in _load("k7-k8-proactive-recall") if c["id"] not in SKIP]
    ca = [migrate_ca(c) for c in _load("k9-k10-completeness-alignment") if c["id"] not in SKIP]
    assign_splits(pr)
    assign_splits(ca)
    head = ("# MemCompass v0.2 · 由 build/migrate_drafts.py 从 eval-drafts v0.1 机械迁移（请勿手改）。\n"
            "# 文字内容与草稿一致；补齐了 earliest_turn / tolerated_mentions / completeness_truth 与 6 条背景记忆。")
    write_items(DATASETS / "mc-proactive-recall" / "migrated.yaml", pr, head)
    write_items(DATASETS / "mc-completeness-alignment" / "migrated.yaml", ca, head)
    print(f"迁移 pr {len(pr)} 条、ca {len(ca)} 条")


if __name__ == "__main__":
    main()
