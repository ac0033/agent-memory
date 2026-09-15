"""构造 mc-cross-agent v0.2（K13 跨 agent 连续）。

场景矩阵（card.md §5）：宿主对 × 事实类型 × 作用域关系；近似干扰分布在不同作用域；
冲突写入相隔分钟到天。新增 20 条（xa-0005 起）：cross_host_transfer 6、scope_isolation 6、
identity_resolution 3、concurrent_writers 5。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcb import DATASETS, a, assign_splits, item, ref_time, rng, session, today_prefix, u, write_items  # noqa: E402

SUBSET = "mc-cross-agent"
HOSTS = ["claude-code", "codex", "kimi-code", "opencode", "deepseek-harness", "pi"]

TRANSFER = [
    ("repo-billing", "这个仓库跑测试前要先 export TZ=UTC，不然 test_invoice_dates 会挂两个。", "帮我在这个仓库跑一下全量测试。",
     "repo-billing 跑测试前需设置 TZ=UTC", "测试命令前先设置 TZ=UTC"),
    ("repo-pricing", "这个仓库的格式化用 ruff format，不要用 black，两者规则有冲突。", "帮我把改过的文件格式化一下。",
     "repo-pricing 格式化用 ruff format，不用 black", "用 ruff format 格式化"),
    ("repo-portal", "portal 前端本地起服务要用 pnpm dev --port 3100，3000 被日常实例占着。", "把前端开发服务器起起来。",
     "repo-portal 本地开发用 pnpm dev --port 3100", "用 3100 端口启动"),
    ("repo-etl", "etl 仓库的数据库迁移要用 alembic upgrade head，别直接跑 SQL 文件。", "帮我把最新的数据库迁移跑一下。",
     "repo-etl 数据库迁移用 alembic upgrade head", "使用 alembic upgrade head"),
    ("repo-audit", "audit 仓库提交前要先跑 make lint，CI 会卡 lint。", "帮我把这次改动提交一下。",
     "repo-audit 提交前需先跑 make lint", "提交前先跑 make lint"),
    ("repo-search", "search 服务改了配置要执行 ./scripts/reload.sh 热加载，不要重启进程。", "改完配置了，帮我让它生效。",
     "repo-search 改配置后用 ./scripts/reload.sh 热加载", "执行 ./scripts/reload.sh，不重启进程"),
]

ISOLATION = [
    ("开发服务器端口固定用 {v}", "把开发服务器起起来。", ["8100", "3100"]),
    ("测试账号用 {v}", "用测试账号登录一下测试环境。", ["qa_alpha_11", "qa_beta_27"]),
    ("数据库名是 {v}", "帮我写一下这个项目的数据库连接配置。", ["shop_dev", "report_dev"]),
    ("默认分支叫 {v}", "从默认分支拉一个新分支 feat/export-csv。", ["trunk", "main"]),
    ("日志目录在 {v}", "帮我看看今天的错误日志。", ["/var/log/shop", "/data/logs/report"]),
    ("Python 版本锁在 {v}", "帮我建一下这个项目的虚拟环境。", ["3.11", "3.12"]),
]

IDENTITY = [
    ("我在公司 GitLab 上的账号是 {acc}。", "把这个 MR 指派给我自己。", "dev-{name}", "指派给"),
    ("我在工单系统里的用户名是 {acc}。", "把这个工单转给我自己跟进。", "tk-{name}", "转给"),
    ("我在 Jenkins 上的账号是 {acc}。", "把这个构建任务的负责人改成我。", "ci-{name}", "负责人改成"),
]

CONCURRENT = [
    ("默认分支已经从 trunk 改名为 main 了。", "改名那件事撤回了，默认分支还是 trunk。", "从默认分支拉一个新分支 feat/x。", "trunk", "main"),
    ("接口超时统一调到 60 秒。", "超时调整撤回，还是 30 秒。", "帮我写一下这个服务的 HTTP 客户端配置。", "30 秒", "60 秒"),
    ("发布窗口改到周四晚上了。", "刚确认，发布窗口改到周五下午，周四那个取消。", "帮我排一下这次版本的发布时间。", "周五下午", "周四晚上"),
    ("测试环境切到 k8s 集群 test-b 了。", "test-b 有问题，已经切回 test-a。", "帮我把服务部署到测试环境。", "test-a", "test-b"),
    ("日志保留期改成 30 天。", "保留期再改一下，按合规要求 180 天。", "帮我配置一下日志清理策略。", "180 天", "30 天"),
]


def _xa(iid, typ, sessions, query, labels, nuggets, pitfalls, essential, bpit, context, diff, ref="2026-09-10"):
    probe = {"probe_id": "q1", "kind": "trigger", "at": {"new_session": True}, "reference_time": ref_time(ref),
             "query": today_prefix(ref) + query,
             "gold": {"nuggets": nuggets, "pitfalls": pitfalls,
                      "evidence": [{"session_id": sessions[-1]["session_id"], "message_index": 0}], "labels": labels}}
    return item(iid=iid, subset=SUBSET, type_=typ, tracks=["behavior", "system"], primary=["K13"],
                secondary=["K5"] if typ == "concurrent_writers" else ["K12"] if typ == "scope_isolation" else ["K11"],
                aml=["N8"], sessions=sessions, probes=[probe],
                behavior={"from_probe": "q1", "trigger": {"context": context}, "tools": ["memory_search", "act"],
                          "rubric": {"essential": essential, "pitfalls": bpit}},
                template_id=f"xa-v02-{typ}", group=iid, difficulty=diff, provenance="template")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    items = []
    n = 5
    for k, (scope, rule, task, inject, need) in enumerate(TRANSFER):
        iid = f"xa-{n:04d}"
        n += 1
        r = rng(iid)
        src, dst = r.sample(HOSTS, 2)
        sessions = [session("s1", "2026-09-03", [u(rule), a("记下了。")], host=src, scope=scope)]
        items.append(_xa(iid, "cross_host_transfer", sessions, task,
                         {"target_host": dst, "target_scope": scope, "must_inject": [inject]}, [need], ["不遵守该规则直接执行"],
                         [need], ["不遵守该规则"], f"在 {dst} 中打开 {scope} 的新会话。该规则是在 {src} 中学到的。",
                         {"hosts": 2}))
    for k, (tmpl, task, (va, vb)) in enumerate(ISOLATION):
        iid = f"xa-{n:04d}"
        n += 1
        r = rng(iid)
        h1, h2, h3 = r.sample(HOSTS, 3)
        pa, pb = f"repo-{r.choice(['shop', 'crm', 'wms'])}-a", f"repo-{r.choice(['report', 'bi', 'oms'])}-b"
        tgt_a = k % 2 == 0
        sessions = [session("s1", "2026-09-01", [u(f"{pa} 的{tmpl.format(v=va)}。"), a("记下了。")], host=h1, scope=pa),
                    session("s2", "2026-09-02", [u(f"{pb} 的{tmpl.format(v=vb)}。"), a("记下了。")], host=h2, scope=pb)]
        tgt, right, wrong = (pa, va, vb) if tgt_a else (pb, vb, va)
        other = pb if tgt_a else pa
        items.append(_xa(iid, "scope_isolation", sessions, task,
                         {"target_host": h3, "target_scope": tgt, "must_inject": [f"{tgt} 的{tmpl.format(v=right)}"],
                          "must_not_inject": [f"{other} 的{tmpl.format(v=wrong)}"]},
                         [f"使用 {right}"], [f"使用 {wrong}（{other} 的配置）"], [f"使用 {right}"],
                         [f"使用 {wrong}，或把两个仓库的配置混在一起"], f"在 {h3} 中打开 {tgt} 的新会话。",
                         {"hosts": 3, "near_duplicate_fact": True}))
    for k, (say, task, acc_t, verb) in enumerate(IDENTITY):
        iid = f"xa-{n:04d}"
        n += 1
        r = rng(iid)
        name = r.choice(["maple", "cedar", "birch"]) + str(r.randint(10, 99))
        acc = acc_t.format(name=name)
        h1, h2 = r.sample(HOSTS, 2)
        local = f"{h2.split('-')[0][:2]}-local-{r.randint(100, 999)}"
        sessions = [session("s1", "2026-08-20", [u(say.format(acc=acc)), a(f"记下了：{acc}。")], host=h1, scope="global")]
        items.append(_xa(iid, "identity_resolution", sessions, task,
                         {"target_host": h2, "target_scope": "repo-shop-api", "identity_map": {h1: f"{h1[:2]}-user-1029", h2: local},
                          "must_inject": [f"用户账号 {acc}"]},
                         [f"{verb} {acc}"], [f"使用宿主本地身份 {local}"], [f"{verb} {acc}"], [f"使用 {local}"],
                         f"在 {h2} 中（本地用户名 {local}）打开 repo-shop-api 的会话。账号信息是在 {h1} 中告知的。",
                         {"hosts": 2, "identity_mismatch": True}))
    for k, (w1, w2, task, right, wrong) in enumerate(CONCURRENT):
        iid = f"xa-{n:04d}"
        n += 1
        r = rng(iid)
        h1, h2, h3 = r.sample(HOSTS, 3)
        gap = r.choice([5, 90, 1440])
        t1 = "2026-09-02T10:00:00+08:00"
        mins = 10 * 60 + gap
        day = 2 + mins // (24 * 60)
        hh, mm = divmod(mins % (24 * 60), 60)
        t2 = f"2026-09-{day:02d}T{hh:02d}:{mm:02d}:00+08:00"
        sessions = [session("s1", t1, [u(w1), a("记下了。")], host=h1, scope="repo-shop-api"),
                    session("s2", t2, [u(w2), a("已更新。")], host=h2, scope="repo-shop-api")]
        items.append(_xa(iid, "concurrent_writers", sessions, task,
                         {"target_host": h3, "target_scope": "repo-shop-api", "must_inject": [f"当前为 {right}"],
                          "must_not_inject": [f"当前为 {wrong}"]},
                         [f"按 {right}"], [f"按 {wrong}"], [f"按最新的 {right} 执行"], [f"按 {wrong} 执行"],
                         f"在 {h3} 中打开 repo-shop-api 的会话。两条相互冲突的写入分别来自 {h1} 和 {h2}，相隔 {gap} 分钟。",
                         {"hosts": 3, "update_gap_minutes": gap}, ref="2026-09-05"))
    assign_splits(items)
    header = ("# MemCompass · mc-cross-agent v0.2 新增条目（由 build/build_xa.py 生成，请勿手改）\n"
              "# session.host / scope 标注宿主与作用域；AML 现行契约无法表达，只在本地 behavior/system 轨运行。")
    write_items(DATASETS / SUBSET / "generated.yaml", items, header)
    print(f"{SUBSET}: 生成 {len(items)} 条 → generated.yaml")


if __name__ == "__main__":
    main()
