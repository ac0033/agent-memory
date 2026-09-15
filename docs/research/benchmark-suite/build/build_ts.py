"""构造 mc-task-state v0.2（K2 任务状态掌握）。

做法（card.md §5）：每个场景先写成结构化操作序列，由重放器计算任意时刻的六字段金标状态
（goal / constraints / done / next_steps / open_questions / key_vars），再把每个操作套模板渲染成对话。
过时条目（stale_items）= 被改掉的旧约束、旧取值；串线条目（contamination_items）= 其他任务的特征词。
新增 26 个场景（ts-0005 起）：state_after_update 6、interrupt_resume 5、task_switch_return 5、
cross_session_resume 6、parallel_tasks_isolation 4。

v0.3（2026-09-15，修订者 Claude）：v0.2 正式运行里两版都接近满分——同会话场景很短，答题器看到的最近 40 条消息
里就有完整状态，不靠记忆也能答。新增 16 个场景（ts-0031 起），让状态落到可见窗口之外：state_long_churn 6
（约束连改两次后插入 26 组填充）、cross_session_churn 5（三个会话里改值两次、未决问题被答复）、parallel3_long 5
（三个任务交错推进后插入填充）。同时修正 v0.2 并行场景漏掉"用户给出约束"那句的缺陷（金标约束在对话里没出现过）。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcb import DATASETS, a, assign_splits, item, session, u, write_items  # noqa: E402
from pools import filler_turns  # noqa: E402

SUBSET = "mc-task-state"

CIRCLED = "①②③④⑤⑥"

# 任务模板：goal、steps、初始约束 {键: 文本}、变量、可更新的约束 (键, 新文本, 原因, 旧值关键词)、未决问题、特征词
TASKS = {
    "export": dict(goal="订单导出支持 CSV 下载，导出异步执行", steps=["导出任务队列", "CSV 分片", "下载接口", "补测试"],
                   cons={"rows": "单文件不超过 5 万行，超过则分片", "async": "导出异步，不阻塞请求"},
                   upd=("rows", "单文件不超过 3 万行，超过则分片", "运营说 Excel 打开 5 万行太卡", "5 万行"),
                   var=("shard_rows", "30000"), done_note="（复用 dramatiq）", q="导出文件保留几天", q_owner="运营",
                   words=["CSV", "分片", "dramatiq"]),
    "pricewatch": dict(goal="写价格监控告警脚本", steps=["拉取价格", "计算环比", "阈值告警", "企业微信通知"],
                       cons={"thr": "价格环比波动超过 15% 告警", "quiet": "告警只发工作时间"},
                       upd=("thr", "价格环比波动超过 20% 告警", "15% 太敏感，误报多", "15%"),
                       var=("channel", "企业微信"), done_note="（按 SKU 维度）", q="节假日要不要静默告警", q_owner="张经理",
                       words=["环比", "告警", "企业微信"]),
    "titles": dict(goal="电线电缆类目商品标题批量标准化", steps=["抽样 20 条确认", "写规则引擎", "全量处理", "人工抽检"],
                   cons={"brand": "不改品牌字段", "len": "标题不超过 60 个字"},
                   upd=("len", "标题不超过 50 个字", "平台新规收紧了", "60 个字"),
                   var=("category", "电线电缆"), done_note="（用户已确认样例）", q="型号缺失的商品怎么处理", q_owner="张经理",
                   words=["标题", "规则引擎", "电线电缆"]),
    "qualification": dict(goal="完成供应商资质年审", steps=["导出供应商清单", "核对证书有效期", "发补件通知", "写汇总报告"],
                          cons={"days": "证书剩余有效期不足 60 天的要提醒", "scope": "只审在合作的供应商"},
                          upd=("days", "证书剩余有效期不足 90 天的要提醒", "法务要求留足续证时间", "60 天"),
                          var=("suppliers", "45"), done_note="（共 45 家）", q="已停止合作的供应商要不要一起审", q_owner="法务",
                          words=["资质", "证书", "补件"]),
    "dashboard": dict(goal="数据看板改版上线", steps=["确认指标口径", "改 SQL", "改前端图表", "验收"],
                      cons={"caliber": "销售额口径用含税金额", "deadline": "9 月 12 日前上线"},
                      upd=("caliber", "销售额口径用不含税金额", "财务要求统一不含税", "含税金额"),
                      var=("release_date", "9 月 12 日"), done_note="（口径文档已归档）", q="旧看板保留多久", q_owner="业务方",
                      words=["看板", "口径", "前端图表"]),
    "recon": dict(goal="写月度对账脚本", steps=["拉平台明细", "拉 ERP 明细", "按订单号对齐", "输出差异单"],
                  cons={"writeoff": "差异小于 0.5 元直接核销", "month": "只处理 8 月数据"},
                  upd=("writeoff", "差异小于 1 元直接核销", "财务主管调整了核销口径", "0.5 元"),
                  var=("month", "2026-08"), done_note="（按订单号去重后）", q="跨月退款怎么归属", q_owner="财务",
                  words=["对账", "ERP", "差异单"]),
    "study": dict(goal="两周内学完 CS224N 第 3–4 讲并完成作业 1", steps=["看第 3 讲", "做作业 1", "读 word2vec 论文", "写学习笔记"],
                  cons={"time": "每天最多学 2 小时", "lang": "笔记用中文写"},
                  upd=("time", "每天最多学 1.5 小时", "最近加班多", "2 小时"),
                  var=("hw_due", "9 月 20 日"), done_note="（笔记已记）", q="要不要先补线性代数", q_owner="自己",
                  words=["CS224N", "作业 1", "word2vec"]),
    "returns": dict(goal="做退货原因分类模型", steps=["整理标注数据", "训练基线模型", "离线评估", "上线灰度"],
                    cons={"data": "只用内部数据，不上传外部平台", "classes": "分 12 类"},
                    upd=("classes", "分 15 类", "业务新增了 3 个退货原因", "12 类"),
                    var=("baseline", "TF-IDF + 逻辑回归"), done_note="（共 8,400 条）", q="“其他”类要不要拆分", q_owner="客服主管",
                    words=["退货", "分类", "逻辑回归"]),
    "weekly": dict(goal="周报生成自动化", steps=["自动取数", "生成图表", "写结论", "发给用户审核"],
                   cons={"review": "先发用户审核，再发群", "time": "每周五 17:00 前生成"},
                   upd=("time", "每周五 15:00 前生成", "张经理要提前看", "17:00"),
                   var=("send_to", "张经理"), done_note="（SQL 已固化）", q="同比口径用自然周还是财务周", q_owner="张经理",
                   words=["周报", "图表", "审核"]),
}


class Replayer:
    """按操作序列计算六字段状态。"""

    def __init__(self, key: str):
        t = TASKS[key]
        self.key = key
        self.t = t
        self.state = {"goal": t["goal"], "constraints": dict(t["cons"]), "done": [], "next_steps": list(t["steps"]),
                      "open_questions": [], "key_vars": {}}
        self.stale: list[str] = []

    def complete(self):
        step = self.state["next_steps"].pop(0)
        idx = len(self.state["done"])
        note = self.t["done_note"] if idx == 0 else ""
        self.state["done"].append(f"{CIRCLED[idx]}{step}{note}")
        return idx, step

    def update(self):
        k, new, reason, old_kw = self.t["upd"]
        self.state["constraints"][k] = new
        self.stale.append(f"把“{old_kw}”当作当前约束")
        return new, reason

    def set_var(self):
        k, v = self.t["var"]
        self.state["key_vars"][k] = v
        return k, v

    def open_q(self):
        self.state["open_questions"].append(f"{self.t['q']}（待问{self.t['q_owner']}）")

    def update2(self):
        """v0.3：同一约束第二次改值；联动的关键变量一起改。两个旧值都进 stale。"""
        k, new, reason, old_kw, var = UPD2[self.key]
        self.state["constraints"][k] = new
        self.stale.append(f"把“{old_kw}”当作当前约束")
        applied = None
        if var and var[0] in self.state["key_vars"]:
            self.stale.append(f"把{var[2]}说成 {self.state['key_vars'][var[0]]}")
            self.state["key_vars"][var[0]] = var[1]
            applied = var
        return new, reason, applied

    def resolve_q(self):
        """v0.3：未决问题得到答复——从 open_questions 移除，答复成为约束。"""
        q = f"{self.t['q']}（待问{self.t['q_owner']}）"
        self.state["open_questions"] = [x for x in self.state["open_questions"] if x != q]
        self.state["constraints"]["ans"] = ANS[self.key]
        self.stale.append(f"把“{self.t['q']}”说成还没定")
        return ANS[self.key]

    def gold(self) -> dict:
        s = copy.deepcopy(self.state)
        s["constraints"] = list(s["constraints"].values())
        s["next_steps"] = [f"{CIRCLED[len(s['done']) + i]}{x}" for i, x in enumerate(s["next_steps"])]
        return s


def start_msgs(r: Replayer, tid: str | None = None) -> list[dict]:
    t = r.t
    cons = "；".join(t["cons"].values())
    plan = "".join(f"{CIRCLED[i]}{s}；" for i, s in enumerate(t["steps"])).rstrip("；")
    return [u(f"今天的任务：{t['goal']}。约束：{cons}。", task_id=tid), a(f"计划：{plan}。先做①。", task_id=tid)]


def complete_msgs(r: Replayer, tid: str | None = None) -> list[dict]:
    idx, step = r.complete()
    note = r.t["done_note"] if idx == 0 else ""
    nxt = r.state["next_steps"][0] if r.state["next_steps"] else None
    tail = f"下一步做{CIRCLED[idx + 1]}{nxt}。" if nxt else "全部步骤完成。"
    return [u(f"{CIRCLED[idx]}做完了{note}。", task_id=tid), a(f"好的，{CIRCLED[idx]}{step}完成。{tail}", task_id=tid)]


def update_msgs(r: Replayer, tid: str | None = None) -> list[dict]:
    new, reason = r.update()
    return [u(f"改一下：{new}，{reason}。", task_id=tid), a(f"已更新：{new}。", task_id=tid)]


def var_msgs(r: Replayer, tid: str | None = None) -> list[dict]:
    k, v = r.set_var()
    label = {"shard_rows": "分片阈值", "channel": "告警通道", "category": "处理类目", "suppliers": "要审的供应商数",
             "release_date": "上线日期", "month": "对账月份", "hw_due": "作业截止日期", "baseline": "基线模型",
             "send_to": "审核人"}[k]
    return [u(f"{label}定为 {v}。", task_id=tid), a(f"记录：{label} {v}。", task_id=tid)]


def openq_msgs(r: Replayer, tid: str | None = None) -> list[dict]:
    r.open_q()
    return [u(f"还有个事没定：{r.t['q']}，我回头问问{r.t['q_owner']}。", task_id=tid),
            a(f"好的，{r.t['q']}待定。", task_id=tid)]


def plan_msgs(r: Replayer, tid: str) -> list[dict]:
    """并行场景里各任务的开场：用户给出约束，助手给出计划（v0.2 只留了计划那句，金标约束在对话里没出现过）。"""
    cons = "；".join(r.t["cons"].values())
    plan = "".join(f"{CIRCLED[i]}{s}；" for i, s in enumerate(r.t["steps"])).rstrip("；")
    return [u(f"{tid} 的约束：{cons}。", task_id=tid), a(f"{tid} 的计划：{plan}。先做①。", task_id=tid)]


def update2_msgs(r: Replayer, tid: str | None = None) -> list[dict]:
    new, reason, var = r.update2()
    extra = f"；{var[2]}也改成 {var[1]}" if var else ""
    return [u(f"再改一下：{new}，{reason}{extra}。", task_id=tid), a(f"已更新：{new}{extra}。", task_id=tid)]


def resolve_msgs(r: Replayer, tid: str | None = None) -> list[dict]:
    ans = r.resolve_q()
    return [u(f"{r.t['q_owner']}回复了：{ans}。", task_id=tid), a(f"好的，{r.t['q']}已定：{ans}。", task_id=tid)]


def contamination(key: str) -> list[str]:
    return list(TASKS[key]["words"])


# ---------------------------------------------------------------- 各类型场景


def scene_update(iid: str, key: str, cross_day: bool) -> dict:
    r = Replayer(key)
    msgs = start_msgs(r) + complete_msgs(r) + update_msgs(r) + var_msgs(r) + openq_msgs(r)
    events = [{"kind": "session_end", "after_message": len(msgs) - 1}] if cross_day else []
    sessions = [session("s1", "2026-09-08T15:00:00+08:00", msgs, scope="repo-work", events=events)]
    at = {"new_session": True} if cross_day else {"session": "s1", "after_message": len(msgs) - 1}
    query = f"{r.t['goal'].split('，')[0]}这件事现在是什么情况？约束有哪些，为什么这么定？还有什么没定的？"
    return dict(sessions=sessions, gold=r.gold(), stale=r.stale, contam=[], at=at, query=query,
                ref="2026-09-09T10:00:00+08:00" if cross_day else "2026-09-08T16:30:00+08:00",
                context="第二天新会话。" if cross_day else "同一会话中。", type="state_after_update",
                diff={"updates": 1, "sessions_gap": 1 if cross_day else 0})


def scene_interrupt(iid: str, key: str, n_pairs: int) -> dict:
    r = Replayer(key)
    msgs = start_msgs(r) + complete_msgs(r) + var_msgs(r) + complete_msgs(r)
    filler = filler_turns(iid, n_pairs)
    all_msgs = msgs + filler
    sessions = [session("s1", "2026-09-10T09:00:00+08:00", all_msgs, scope="repo-work",
                        events=[{"kind": "interrupt", "after_message": len(all_msgs) - 1, "n_turns": n_pairs}])]
    return dict(sessions=sessions, gold=r.gold(), stale=["把已完成的步骤说成未完成"],
                contam=[x["content"][:6] for x in filler[::2][:2]],
                at={"session": "s1", "after_message": len(all_msgs) - 1},
                query="好，回到刚才的事，我们做到哪一步了？接下来做什么？", ref="2026-09-10T11:00:00+08:00",
                context=f"中间插入了 {n_pairs} 组与任务无关的技术问答。", type="interrupt_resume",
                diff={"interrupt_pairs": n_pairs})


def scene_switch(iid: str, key: str, other: str) -> dict:
    r = Replayer(key)
    o = Replayer(other)
    msgs = start_msgs(r, "A") + complete_msgs(r, "A") + update_msgs(r, "A")
    msgs += [u(f"先放一放，插个急活：{o.t['goal']}。", task_id="B"), a("好，先处理这个。", task_id="B")]
    msgs += start_msgs(o, "B")[1:] + complete_msgs(o, "B") + complete_msgs(o, "B")
    msgs += [u("急活先到这。", task_id="B"), a("好的。", task_id="B")]
    sessions = [session("s1", "2026-09-11T14:00:00+08:00", msgs, scope="repo-work",
                        events=[{"kind": "task_switch", "after_message": len(msgs) - 1}])]
    return dict(sessions=sessions, gold=r.gold(), stale=r.stale, contam=contamination(other),
                at={"session": "s1", "after_message": len(msgs) - 1},
                query=f"好，回到刚才的“{r.t['goal'].split('，')[0]}”，现在进度和约束是什么？下一步做什么？",
                ref="2026-09-11T16:00:00+08:00", context="中间切去处理了另一个急活。", type="task_switch_return",
                diff={"switch": True, "updates": 1})


def scene_cross_session(iid: str, key: str, gap_days: int) -> dict:
    r = Replayer(key)
    s1 = start_msgs(r) + complete_msgs(r) + var_msgs(r)
    s2 = [u("接着上次的活。"), a("好的。")] + update_msgs(r) + openq_msgs(r) + [u("今天先到这。"), a("好的，下次继续。")]
    sessions = [
        session("s1", "2026-09-07T10:00:00+08:00", s1, scope="repo-work", events=[{"kind": "session_end", "after_message": len(s1) - 1}]),
        session("s2", f"2026-09-{7 + gap_days:02d}T10:00:00+08:00", s2, scope="repo-work",
                events=[{"kind": "session_end", "after_message": len(s2) - 1}]),
    ]
    return dict(sessions=sessions, gold=r.gold(), stale=r.stale + ["把①说成未完成"], contam=[],
                at={"new_session": True}, query="接着之前的活。先说说现在进度怎么样、有哪些约束、下一步做什么。",
                ref=f"2026-09-{8 + gap_days:02d}T09:30:00+08:00", context="隔天新开的会话，这是第一条用户消息。",
                type="cross_session_resume", diff={"sessions": 2, "gap_days": gap_days, "updates": 1})


def scene_parallel(iid: str, key: str, other: str, ask_first: bool) -> dict:
    r = Replayer(key)
    o = Replayer(other)
    msgs = [u(f"今天并行两件事。A：{r.t['goal']}；B：{o.t['goal']}。"), a("好的，A、B 分开跟踪。")]
    for part in (plan_msgs(r, "A"), plan_msgs(o, "B"), complete_msgs(r, "A"), complete_msgs(o, "B"),
                 update_msgs(o, "B"), complete_msgs(r, "A"), openq_msgs(o, "B")):
        msgs += part
    target, tgt_other, label = (r, o, "A") if ask_first else (o, r, "B")
    sessions = [session("s1", "2026-09-10T14:00:00+08:00", msgs, scope="repo-work")]
    return dict(sessions=sessions, gold=target.gold(), stale=target.stale, contam=contamination(tgt_other.key),
                at={"session": "s1", "after_message": len(msgs) - 1}, query=f"{label} 那件事现在什么情况？",
                ref="2026-09-10T16:00:00+08:00", context="同一会话里交错推进两个任务。", type="parallel_tasks_isolation",
                diff={"parallel_tasks": 2, "interleaved": True})


PLAN = (
    [("update", k, dict(cross_day=i % 2 == 0)) for i, k in enumerate(["pricewatch", "titles", "qualification", "dashboard", "recon", "study"])]
    + [("interrupt", k, dict(n_pairs=n)) for k, n in [("returns", 5), ("weekly", 10), ("export", 20), ("recon", 8), ("titles", 15)]]
    + [("switch", k, dict(other=o)) for k, o in [("dashboard", "recon"), ("study", "weekly"), ("qualification", "titles"),
                                                  ("returns", "pricewatch"), ("weekly", "export")]]
    + [("cross", k, dict(gap_days=g)) for k, g in [("pricewatch", 1), ("returns", 2), ("titles", 1), ("recon", 3), ("dashboard", 1), ("study", 2)]]
    + [("parallel", k, dict(other=o, ask_first=f)) for k, o, f in [("pricewatch", "returns", True), ("qualification", "weekly", False),
                                                                    ("export", "study", True), ("recon", "titles", False)]]
)


# ---------------------------------------------------------------- v0.3 加难（2026-09-15）

# 第二次改值：(约束键, 新文本, 原因, 上一个值的关键词, 联动变量 (键, 新值, 称呼) 或 None)
UPD2 = {
    "export": ("rows", "单文件不超过 2 万行，超过则分片", "数据库导出时 IO 压力太大", "3 万行", ("shard_rows", "20000", "分片阈值")),
    "pricewatch": ("thr", "价格环比波动超过 25% 告警", "20% 还是误报多", "20%", None),
    "titles": ("len", "标题不超过 45 个字", "移动端展示又收紧了", "50 个字", None),
    "qualification": ("days", "证书剩余有效期不足 120 天的要提醒", "续证周期普遍要 3 个月", "90 天", None),
    "recon": ("writeoff", "差异小于 0.8 元直接核销", "审计要求收紧一点", "1 元", None),
    "study": ("time", "每天最多学 1 小时", "下周出差", "1.5 小时", None),
    "returns": ("classes", "分 14 类", "有两个原因合并了", "15 类", None),
    "weekly": ("time", "每周五 14:00 前生成", "张经理周五下午要开会", "15:00", None),
}
# 未决问题的答复（成为新约束）
ANS = {
    "export": "导出文件保留 7 天后自动删除",
    "pricewatch": "节假日静默告警，只记日志",
    "titles": "型号缺失的商品先跳过，单独出清单",
    "qualification": "已停止合作的供应商不审",
    "recon": "跨月退款归属到退款发生的月份",
    "study": "先不补线性代数，边学边查",
    "returns": "“其他”类暂不拆分",
    "weekly": "同比口径用财务周",
}
FILL_CHURN = 26  # 组；26 组 = 52 条消息，多于答题器可见的最近 40 条
FILL_PARALLEL = 22


def _filler(after: int, pairs: int, pool: str) -> dict:
    return {"kind": "filler", "after_message": after, "n_turns": 2 * pairs, "pool": pool}


def scene_churn(iid: str, key: str, pool: str) -> dict:
    r = Replayer(key)
    msgs = (start_msgs(r) + complete_msgs(r) + update_msgs(r) + var_msgs(r) + complete_msgs(r)
            + update2_msgs(r) + openq_msgs(r))
    last = len(msgs) - 1
    sessions = [session("s1", "2026-09-14T09:30:00+08:00", msgs, scope="repo-work",
                        events=[_filler(last, FILL_CHURN, pool)])]
    return dict(sessions=sessions, gold=r.gold(), stale=r.stale + ["把已完成的步骤说成未完成"], contam=[],
                at={"session": "s1", "after_message": last},
                query=f"回到“{r.t['goal'].split('，')[0]}”：现在做到哪了？约束是什么（按最新的说）？还有什么没定？",
                ref="2026-09-14T15:00:00+08:00", context=f"约束连改两次之后，中间又聊了 {FILL_CHURN} 组别的事。",
                type="state_long_churn", diff={"updates": 2, "filler_pairs": FILL_CHURN, "pool": pool})


def scene_cross_churn(iid: str, key: str, gap_days: int) -> dict:
    r = Replayer(key)
    s1 = start_msgs(r) + complete_msgs(r) + var_msgs(r) + openq_msgs(r) + [u("今天先到这。"), a("好的。")]
    s2 = [u("接着上次的活。"), a("好的。")] + update_msgs(r) + complete_msgs(r) + resolve_msgs(r)
    s2_fill_at = len(s2) - 1
    s2 += [u("先这样。"), a("好的，下次继续。")]
    s3 = [u("继续。"), a("好的。")] + update2_msgs(r) + [u("今天就改了这个，别的下次再说。"), a("好的。")]
    d1, d2 = 7, 7 + gap_days
    d3 = d2 + 1
    sessions = [
        session("s1", f"2026-09-{d1:02d}T10:00:00+08:00", s1, scope="repo-work",
                events=[{"kind": "session_end", "after_message": len(s1) - 1}]),
        session("s2", f"2026-09-{d2:02d}T10:00:00+08:00", s2, scope="repo-work",
                events=[_filler(s2_fill_at, 12, "coding-zh-generic"), {"kind": "session_end", "after_message": len(s2) - 1}]),
        session("s3", f"2026-09-{d3:02d}T10:00:00+08:00", s3, scope="repo-work",
                events=[{"kind": "session_end", "after_message": len(s3) - 1}]),
    ]
    return dict(sessions=sessions, gold=r.gold(), stale=r.stale + ["把①说成未完成"], contam=[],
                at={"new_session": True},
                query="接着之前的活。现在进度怎么样？约束有哪些（以最新的为准）？还有什么没定的？下一步做什么？",
                ref=f"2026-09-{d3 + 1:02d}T09:30:00+08:00", context="隔天新开的会话，这是第一条用户消息。",
                type="cross_session_churn", diff={"sessions": 3, "updates": 2, "resolved_questions": 1, "gap_days": gap_days})


def scene_parallel3(iid: str, key: str, others: tuple[str, str], target: str) -> dict:
    rs = {"A": Replayer(key), "B": Replayer(others[0]), "C": Replayer(others[1])}
    A, B, C = rs["A"], rs["B"], rs["C"]
    msgs = [u("今天三件事并行。" + "；".join(f"{t}：{r.t['goal']}" for t, r in rs.items()) + "。"),
            a("好的，A、B、C 分开跟踪。")]
    for part in (plan_msgs(A, "A"), plan_msgs(B, "B"), plan_msgs(C, "C"), complete_msgs(A, "A"), complete_msgs(C, "C"),
                 update_msgs(B, "B"), complete_msgs(B, "B"), var_msgs(A, "A"), update_msgs(C, "C"), openq_msgs(B, "B"),
                 complete_msgs(A, "A"), openq_msgs(C, "C")):
        msgs += part
    last = len(msgs) - 1
    sessions = [session("s1", "2026-09-14T14:00:00+08:00", msgs, scope="repo-work",
                        events=[_filler(last, FILL_PARALLEL, "coding-zh-generic")])]
    tgt = rs[target]
    contam = [w for t, r in rs.items() if t != target for w in contamination(r.key)]
    return dict(sessions=sessions, gold=tgt.gold(), stale=tgt.stale, contam=contam,
                at={"session": "s1", "after_message": last}, query=f"{target} 那件事现在什么情况？约束、进度、没定的事都说一下。",
                ref="2026-09-14T17:00:00+08:00", context=f"同一会话里交错推进三个任务，之后又聊了 {FILL_PARALLEL} 组别的事。",
                type="parallel3_long", diff={"parallel_tasks": 3, "interleaved": True, "filler_pairs": FILL_PARALLEL})


PLAN3 = (
    [("churn", k, dict(pool=p)) for k, p in [("export", "coding-zh-generic"), ("pricewatch", "work-zh-detailed"),
                                             ("titles", "coding-zh-generic"), ("qualification", "work-zh-detailed"),
                                             ("recon", "coding-zh-generic"), ("weekly", "work-zh-detailed")]]
    + [("crosschurn", k, dict(gap_days=g)) for k, g in [("study", 1), ("returns", 2), ("export", 1), ("weekly", 3), ("recon", 2)]]
    + [("parallel3", k, dict(others=o, target=t)) for k, o, t in [("titles", ("recon", "study"), "A"),
                                                                 ("qualification", ("pricewatch", "returns"), "B"),
                                                                 ("weekly", ("export", "titles"), "C"),
                                                                 ("recon", ("study", "pricewatch"), "A"),
                                                                 ("returns", ("qualification", "export"), "B")]]
)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    items = []
    for n, (kind, key, kw) in enumerate(PLAN + PLAN3, start=5):
        iid = f"ts-{n:04d}"
        fn = {"update": scene_update, "interrupt": scene_interrupt, "switch": scene_switch,
              "cross": scene_cross_session, "parallel": scene_parallel, "churn": scene_churn,
              "crosschurn": scene_cross_churn, "parallel3": scene_parallel3}[kind]
        g = fn(iid, key, **kw)
        probe = {"probe_id": "q1", "kind": "state_query", "at": g["at"], "reference_time": g["ref"], "query": g["query"],
                 "gold": {"labels": {"task_id": key, "state": g["gold"], "stale_items": g["stale"],
                                     "contamination_items": g["contam"]}}}
        must = [g["gold"]["goal"]] + g["gold"]["done"][-1:] + g["gold"]["next_steps"][:1]
        behavior = {"from_probe": "q1", "trigger": {"context": g["context"]},
                    "tools": ["state_read", "memory_search", "archive_search", "archive_read"],
                    "rubric": {"essential": [f"准确说出：{x}" for x in must if x],
                               "pitfalls": ["报出过时条目当作现状"] + (["混入其他任务的内容"] if g["contam"] else [])}}
        items.append(item(iid=iid, subset=SUBSET, type_=g["type"], tracks=["behavior", "system"], primary=["K2"],
                          secondary=["K5"] if kind in {"update", "cross", "churn", "crosschurn"} else ["K1"], aml=["D1", "N7"],
                          sessions=g["sessions"], probes=[probe], behavior=behavior,
                          template_id=f"ts-{'v03' if n >= 5 + len(PLAN) else 'v02'}-{kind}",
                          group=iid, difficulty=g["diff"], provenance="template"))
    __import__("mcb").pin_splits(items, DATASETS / SUBSET / "generated.yaml")
    assign_splits(items)
    header = ("# MemCompass · mc-task-state v0.2 / v0.3 新增场景（由 build/build_ts.py 从操作序列生成，请勿手改）\n"
              "# 金标状态由重放器计算；stale_items 为被改掉的旧取值，contamination_items 为其他任务的特征词；\n"
              "# filler 事件由 harness 在运行时注入。")
    write_items(DATASETS / SUBSET / "generated.yaml", items, header)
    print(f"{SUBSET}: 生成 {len(items)} 个场景 → generated.yaml")


if __name__ == "__main__":
    main()
