"""构造 mc-asof-temporal v0.2（K5 双时态与"截至某时"）。

做法（card.md §5）：先由代码生成双时态时间线，再由重放器计算金标，最后套自然语言模板：
- 每个"事实"是一串版本：值、有效起点 valid_from、记录时刻 recorded_at（就是说出这句话的会话日期）；
  普通变更 valid_from = recorded_at；追溯更正 valid_from < recorded_at。
- 有效时间轴（valid）：取"最终掌握的全部记录"里有效区间覆盖 t 的版本；
- 记录时间轴（record）：只用 recorded_at ≤ t 的记录，再取当时认为有效的版本；
- 计划类：有计划、有/没有完成记录；有完成记录的是对照（防止"一律答无法确认"刷分）；
- 有效期类：临时规则的窗口 [from, to]，问窗口内、窗口后与持续天数（含首尾）。
每条用例另插 2 个填充会话（pools.FILLER_QA），并包含一道"现在是什么"的对照题。
星期、间隔都由代码计算；问的日期与变更边界至少相隔 2 天，避免边界歧义。

本子集只走 Q 问答轨（AML 兼容：Add 按会话写入，题面自带"今天是 YYYY-MM-DD"）。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcb import (  # noqa: E402
    DATASETS,
    a,
    assign_splits,
    d,
    iso,
    item,
    md,
    ref_time,
    rng,
    session,
    today_prefix,
    u,
    weekday_cn,
    write_items,
)
from pools import filler_turns  # noqa: E402

SUBSET = "mc-asof-temporal"

# ---------------------------------------------------------------- 事实域（主语 + 取值序列）

FACTS = [
    ("项目锁定的 pydantic 版本系列", ["1.10.x", "2.8.x", "2.9.x", "2.10.x"]),
    ("项目锁定的 pandas 版本系列", ["1.5.x", "2.1.x", "2.2.x"]),
    ("周例会的时间", ["每周一 10:00", "每周二 9:30", "每周三 14:00", "每周四 16:00"]),
    ("供应商资质年审的负责人", ["小周", "王工", "小林", "老赵"]),
    ("价格监控接口的超时时间", ["30 秒", "60 秒", "90 秒"]),
    ("商品审核的抽检比例", ["5%", "8%", "10%", "12%"]),
    ("默认运费", ["8 元", "10 元", "12 元"]),
    ("周会用的会议室", ["3 楼 301", "5 楼 502", "7 楼大会议室"]),
    ("订单服务的 CPU 告警阈值", ["80%", "85%", "90%"]),
    ("批量写入的每批条数", ["500 条", "1000 条", "2000 条"]),
    ("月报的提交截止日", ["每月 3 日", "每月 5 日", "每月 7 日"]),
    ("电缆类目标准化的负责人", ["小陈", "王工", "小周"]),
]

EVENTS = [
    # (等待中的说法, 获悉后的说法模板, 事件名, 实际状态描述)
    ("供应商锦程五金的合同续签了吗？我这边还不知道，等法务消息。",
     "法务回复了：锦程五金的合同其实 {ev} 就续签好了，有效期到 {end}。",
     "锦程五金的合同续签", "已续签"),
    ("华东仓那批电缆到了没有？仓库还没给我回话。",
     "仓库说那批电缆其实 {ev} 就到仓了，今天才补登记。",
     "华东仓那批电缆到仓", "已到仓"),
    ("报表导出乱码的 bug 修了吗？开发还没回复。",
     "开发说导出乱码的 bug 其实 {ev} 就修好上线了，今天才告诉我。",
     "导出乱码 bug 的修复上线", "已修复上线"),
    ("恒川电气的 3C 证书年检过了吗？还在等他们的消息。",
     "恒川电气回复了：3C 证书其实 {ev} 就年检通过了，有效期到 {end}。",
     "恒川电气 3C 证书年检", "已通过年检"),
]

PLANS = [("数据库迁移", "凌晨", "王工"), ("机房断电演练", "晚上", "小周"), ("供应商现场审核", "上午", "小林"),
         ("价格系统升级", "晚上", "老赵"), ("生产证书续期", "下午", "小陈"), ("主数据平台切换", "凌晨", "王工"),
         ("审核规则 v3 上线", "上午", "小周")]

RULES = [
    # (规则陈述, 当前题, 当前答案（过期）, 窗口内题, 窗口内答案)
    ("封版，这期间所有代码合并都要两个人审批", "现在合并代码还需要两个人审批吗？",
     "不需要按封版规则两人审批：该规则只适用于 {f}–{t} 的封版期，已经过期（除非之后有新规定）。",
     "{m} 那天合并代码需要几个人审批？", "两个人（当时在封版期内）。"),
    ("平台大促，调价要提前 48 小时报备", "现在调价还需要提前 48 小时报备吗？",
     "不需要：提前 48 小时报备只适用于 {f}–{t} 的大促期，已经结束。",
     "{m} 那天调价需要提前多久报备？", "提前 48 小时（当时在大促期内）。"),
    ("大仓盘点，不能做出库调拨", "现在大仓能出库调拨吗？",
     "可以：不能出库调拨只适用于 {f}–{t} 的盘点期，已经结束（除非之后有新安排）。",
     "{m} 那天大仓能出库调拨吗？", "不能（当时在盘点期内）。"),
    ("测试库只读，不能写数据", "现在能往测试库写数据吗？",
     "可以：只读限制只在 {f}–{t} 期间有效，已经结束。",
     "{m} 那天能往测试库写数据吗？", "不能（当时测试库只读）。"),
    ("客服系统灰度，只对华东区开放", "现在客服系统还只对华东区开放吗？",
     "不是：只对华东区开放是 {f}–{t} 的灰度期安排，已经结束（之后的范围以新通知为准）。",
     "{m} 那天客服系统对哪些区域开放？", "只对华东区开放（当时在灰度期内）。"),
    ("审核加严，驳回理由必须附截图", "现在驳回理由还必须附截图吗？",
     "不是必须：附截图的要求只适用于 {f}–{t} 的加严期，已经结束。",
     "{m} 那天驳回理由需要附截图吗？", "需要（当时在加严期内）。"),
]


def pad(v: str) -> str:
    """中文与数字、字母之间加空格（中文排版惯例）。"""
    return (" " if v[:1].isascii() else "") + v + (" " if v[-1:].isascii() else "")


def dd(x: dt.date, n: int) -> dt.date:
    return x + dt.timedelta(days=n)


def mdw(x: dt.date) -> str:
    return f"{md(x)}（{weekday_cn(x)}）"


# ---------------------------------------------------------------- 重放器


class Timeline:
    """一个事实的双时态版本表。versions: [(value, valid_from, recorded_at)]。"""

    def __init__(self):
        self.versions: list[tuple[str, dt.date, dt.date]] = []

    def add(self, value: str, valid_from: dt.date, recorded_at: dt.date):
        self.versions.append((value, valid_from, recorded_at))

    def _known(self, record_cutoff: dt.date | None):
        vs = [v for v in self.versions if record_cutoff is None or v[2] <= record_cutoff]
        # 同一有效起点以后记录的为准；按有效起点排序
        vs.sort(key=lambda v: (v[1], v[2]))
        return vs

    def valid_at(self, t: dt.date, record_cutoff: dt.date | None = None) -> str | None:
        cur = None
        for value, vf, _ in self._known(record_cutoff):
            if vf <= t:
                cur = value
        return cur


# ---------------------------------------------------------------- 各类型生成器


def _filler_sessions(iid: str, dates: list[dt.date], start_idx: int) -> list[dict]:
    return [session(f"f{i + start_idx}", iso(x), filler_turns(f"{iid}:{i}", 2)) for i, x in enumerate(dates)]


def _q(pid, date, text, answer, nuggets, pitfalls, evidence, labels):
    return {"probe_id": pid, "kind": "question", "at": {"after_session": "__last__"},
            "reference_time": ref_time(iso(date)), "query": today_prefix(iso(date)) + text,
            "gold": {"answer": answer, "nuggets": nuggets, "pitfalls": pitfalls, "evidence": evidence, "labels": labels}}


def gen_version_chain(iid: str, k: int, retro: bool) -> dict:
    r = rng(iid)
    subj, values = FACTS[k % len(FACTS)]
    n_versions = r.choice([2, 3]) if not retro else 2
    vals = values[: n_versions + (1 if retro else 0)]
    t0 = dd(d("2026-07-20"), r.randint(0, 10))
    tl = Timeline()
    sessions: list[dict] = []
    # 第 1 版
    sessions.append(session("s1", iso(t0), [u(f"{subj}定为{pad(vals[0])}。".replace(" 。", "。")), a(f"记下了：{subj}为{pad(vals[0])}。".replace(" 。", "。"))]))
    tl.add(vals[0], t0, t0)
    cur = t0
    for i in range(1, n_versions):
        cur = dd(cur, r.randint(12, 20))
        sessions.append(session(f"s{i + 1}", iso(cur), [u(f"{subj}改成{pad(vals[i])}了。"), a(f"已更新：{subj}改为{pad(vals[i])}。".replace(" 。", "。"))]))
        tl.add(vals[i], cur, cur)
    retro_from = None
    if retro:
        rec = dd(cur, r.randint(14, 18))
        retro_from = dd(rec, -r.randint(7, 10))
        v = vals[n_versions]
        sessions.append(session(f"s{n_versions + 1}", iso(rec), [
            u(f"更正一下：{subj}其实从 {md(retro_from)}起就已经改成{pad(v)}了，之前忘了跟你说。"),
            a(f"已更正：自 {md(retro_from)}起，{subj}为{pad(v)}。".replace(" 。", "。")),
        ]))
        tl.add(v, retro_from, rec)
        cur = rec
    last = cur
    ref = dd(last, r.randint(2, 6))
    # 填充会话插在最后一个会话之前
    fill = _filler_sessions(iid, [dd(t0, 3), dd(t0, 9)], 1)
    sessions = [sessions[0]] + fill + sessions[1:]
    probes = []
    # q1：as-of 过去某日（有效时间），选第 1 版有效期中间
    v1_end = tl.versions[1][1]
    t_past = dd(t0, max(2, (v1_end - t0).days // 2))
    ans_past = tl.valid_at(t_past)
    probes.append(_q("q1", ref, f"截至 {iso(t_past)}，{subj}是什么？", f"{ans_past}。", [ans_past],
                     ["答成后来的取值"], [{"session_id": "s1", "message_index": 0}],
                     {"as_of": iso(t_past), "time_axis": "valid"}))
    # q2：当前（对照）
    now_v = tl.valid_at(ref)
    now_sid = sessions[-1]["session_id"]
    probes.append(_q("q2", ref, f"现在{subj}是什么？", f"{now_v}。", [now_v],
                     ["答成被取代的旧值"], [{"session_id": now_sid, "message_index": 0}],
                     {"as_of": iso(ref), "time_axis": "valid"}))
    if retro:
        # q3：追溯区间内某日的实际情况（有效时间，受更正影响）
        t_r = dd(retro_from, 2)
        truth = tl.valid_at(t_r)
        believed = tl.valid_at(t_r, record_cutoff=t_r)
        probes.append(_q("q3", ref, f"{md(t_r)}那天，{subj}实际是什么？",
                         f"{truth}（自 {md(retro_from)}起已经是{pad(truth)}，这是事后更正的）。", [truth],
                         [f"答成{believed}"], [{"session_id": now_sid, "message_index": 0}],
                         {"as_of": iso(t_r), "time_axis": "valid", "retroactive": True}))
        # q4：记录时间轴：当时我们以为是什么
        probes.append(_q("q4", ref, f"按 {iso(t_r)} 那天我们手上的记录，{subj}被认为是什么？",
                         f"{believed}（当时还没收到更正）。", [believed, "当时尚未得知更正"],
                         [f"答成{truth}"], [{"session_id": sessions[-2]["session_id"], "message_index": 0}],
                         {"as_of": iso(t_r), "time_axis": "record"}))
    else:
        # q3：某次变更是哪天定的（记录时间，日粒度）
        v_i, vf_i, rec_i = tl.versions[1]
        probes.append(_q("q3", ref, f"{subj}改成{pad(v_i)}是哪天定下来的？", f"{iso(rec_i)}。", [f"{rec_i.year} 年 {rec_i.month} 月 {rec_i.day} 日"],
                         ["日期答错或只答到月份"], [{"session_id": "s2", "message_index": 0}],
                         {"time_axis": "record", "granularity": "day"}))
    for p in probes:
        p["at"] = {"after_session": sessions[-1]["session_id"]}
    return dict(sessions=sessions, probes=probes, type="retro_correction" if retro else "as_of_past",
                template=f"at-v02-{'retro' if retro else 'chain'}", difficulty={"versions": len(tl.versions), "retroactive": retro,
                                                                                 "filler_sessions": 2})


def gen_event_vs_record(iid: str, k: int) -> dict:
    r = rng(iid)
    wait, told, name, state = EVENTS[k % len(EVENTS)]
    s1d = dd(d("2026-08-20"), r.randint(0, 12))
    ev = dd(s1d, -r.randint(2, 5))
    learned = dd(s1d, r.randint(4, 8))
    end = dd(ev, 364)
    ref = dd(learned, r.randint(1, 4))
    mid = dd(s1d, 1)
    sessions = [session("s1", iso(s1d), [u(wait), a(f"记下了：{name}的情况待确认。")])]
    sessions += _filler_sessions(iid, [dd(s1d, 1), dd(s1d, 2)], 1)
    sessions.append(session("s2", iso(learned), [u(told.format(ev=md(ev), end=iso(end))),
                                                a(f"已更新：{name}实际发生在 {md(ev)}（{md(learned)} 获悉）。")]))
    probes = [
        _q("q1", ref, f"{name}实际是哪天？我们是哪天知道的？",
           f"实际是 {iso(ev)}；我们 {iso(learned)} 才得知。", [f"实际日期 {iso(ev)}", f"得知日期 {iso(learned)}"],
           ["把两个日期弄反"], [{"session_id": "s2", "message_index": 0}], {"time_axis": "both", "granularity": "day"}),
        _q("q2", ref, f"{md(mid)}那天，{name}实际上是什么状态？当时我们掌握的情况又是什么？",
           f"实际上 {md(ev)}就{state}；但 {md(mid)}当时我们还不知道，记录的是“待确认”。",
           [f"{md(mid)}时实际上{state}", f"{md(mid)}当时的记录是待确认"],
           ["说当时尚未发生", "混淆实际状态与当时的认知"],
           [{"session_id": "s1", "message_index": 0}, {"session_id": "s2", "message_index": 0}],
           {"as_of": iso(mid), "time_axis": "both"}),
    ]
    for p in probes:
        p["at"] = {"after_session": "s2"}
    return dict(sessions=sessions, probes=probes, type="event_vs_record_time", template="at-v02-bitemporal-event",
                difficulty={"bitemporal": True, "filler_sessions": 2})


def gen_plan(iid: str, k: int, completed: bool) -> dict:
    r = rng(iid)
    task, when, person = PLANS[k % len(PLANS)]
    s1d = dd(d("2026-08-24"), r.randint(0, 10))
    plan_d = dd(s1d, r.randint(4, 7))
    ref = dd(plan_d, r.randint(3, 6))
    sessions = [session("s1", iso(s1d), [u(f"{task}定在 {mdw(plan_d)}{when}做，{person}执行。"),
                                        a(f"记下了：{task}计划于 {md(plan_d)}{when}进行，由{person}执行。")])]
    sessions += _filler_sessions(iid, [dd(s1d, 1), dd(s1d, 2)], 1)
    if completed:
        done_d = plan_d
        sessions.append(session("s2", iso(dd(plan_d, 1)), [u(f"{task}已经在 {md(done_d)}{when}做完了，{person}说一切正常。"),
                                                           a(f"已记录：{task}于 {md(done_d)}完成。")]))
        probes = [_q("q1", ref, f"{task}做完了吗？", f"做完了：{iso(done_d)} {when}由{person}完成，一切正常。",
                     ["已完成", f"{iso(done_d)} 完成"], ["说无法确认或尚未进行"], [{"session_id": "s2", "message_index": 0}],
                     {"answer_kind": "completed", "time_axis": "valid"})]
    else:
        probes = [_q("q1", ref, f"{task}做完了吗？",
                     f"无法确认。记录只显示计划在 {iso(plan_d)} {when}由{person}执行，之后没有完成与否的记录，建议向{person}确认。",
                     [f"计划日期 {iso(plan_d)}，由{person}执行", "没有完成情况的记录，无法确认"],
                     ["断言已经完成", "断言尚未进行"], [{"session_id": "s1", "message_index": 0}],
                     {"answer_kind": "unknown_with_plan", "time_axis": "valid"})]
    # 对照题：计划本身定在哪天（星期由代码算）
    probes.append(_q("q2", ref, f"{task}原本计划在哪天？星期几？", f"{iso(plan_d)}，{weekday_cn(plan_d)}。",
                     [iso(plan_d), weekday_cn(plan_d)], ["日期或星期答错"], [{"session_id": "s1", "message_index": 0}],
                     {"time_axis": "valid", "granularity": "day"}))
    last = sessions[-1]["session_id"]
    for p in probes:
        p["at"] = {"after_session": last}
    return dict(sessions=sessions, probes=probes, type="plan_unconfirmed", template=f"at-v02-plan-{'done' if completed else 'open'}",
                difficulty={"requires_abstention": not completed, "filler_sessions": 2})


def gen_expired(iid: str, k: int, extended: bool) -> dict:
    r = rng(iid)
    rule, q_now, a_now, q_in, a_in = RULES[k % len(RULES)]
    s1d = dd(d("2026-08-20"), r.randint(0, 8))
    f = dd(s1d, r.randint(3, 6))
    t = dd(f, r.randint(8, 14))
    sessions = [session("s1", iso(s1d), [u(f"{md(f)}到{md(t)}{rule}。"), a(f"记下了：{md(f)}–{md(t)}{rule}。")])]
    sessions += _filler_sessions(iid, [dd(s1d, 1), dd(s1d, 2)], 1)
    t_final = t
    if extended:
        t_final = dd(t, r.randint(3, 5))
        sessions.append(session("s2", iso(dd(t, -2)), [u(f"刚才说的那个期限延长了，改到{md(t_final)}结束。"),
                                                       a(f"已更新：结束日期延长到 {md(t_final)}。")]))
    ref = dd(t_final, r.randint(4, 8))
    mid = dd(f, (t - f).days // 2)
    # 证据指向说出规则的会话（延长时再加上 s2 的延期消息）；sessions[-1] 在未延长时是填充会话，
    # oracle 会因此只拿到无关问答（v0.3 体检：at-0040/0042/0044 的 oracle 全部答"无法确定"）
    ev_rule = [{"session_id": "s1", "message_index": 0}] + ([{"session_id": "s2", "message_index": 0}] if extended else [])
    probes = [
        _q("q1", ref, q_now, a_now.format(f=md(f), t=md(t_final)), ["该规则已过期", f"只在 {md(f)}–{md(t_final)} 有效"],
           ["回答现在仍然适用"], ev_rule,
           {"as_of": iso(ref), "time_axis": "valid"}),
        _q("q2", ref, q_in.format(m=md(mid)), a_in, [a_in.split("（")[0]], ["答成不适用"],
           [{"session_id": "s1", "message_index": 0}], {"as_of": iso(mid), "time_axis": "valid"}),
    ]
    days = (t_final - f).days + 1
    probes.append(_q("q3", ref, "这个期限一共持续了多少天（含首尾两天）？", f"{days} 天。", [f"{days} 天"],
                     [f"答成 {days - 1} 天"], ev_rule,
                     {"time_axis": "valid", "interval": True}))
    if extended:
        t_ext = dd(t, 1)
        probes.append(_q("q4", ref, q_in.format(m=md(t_ext)), a_in.replace("当时在", "延长后仍在"), [a_in.split("（")[0]],
                         ["按原截止日判断为已结束"], [{"session_id": "s2", "message_index": 0}],
                         {"as_of": iso(t_ext), "time_axis": "valid"}))
    last = sessions[-1]["session_id"]
    for p in probes:
        p["at"] = {"after_session": last}
    return dict(sessions=sessions, probes=probes, type="expired_validity", template=f"at-v02-validity{'-ext' if extended else ''}",
                difficulty={"interval_calc": True, "extended": extended, "filler_sessions": 2})


# ---------------------------------------------------------------- 组装

PLAN = (
    [("chain", i) for i in range(11)]
    + [("retro", i) for i in range(9)]
    + [("event", i) for i in range(7)]
    + [("plan", i) for i in range(7)]
    + [("expired", i) for i in range(6)]
)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    items = []
    for n, (kind, k) in enumerate(PLAN, start=6):
        iid = f"at-{n:04d}"
        if kind == "chain":
            g = gen_version_chain(iid, k, retro=False)
        elif kind == "retro":
            g = gen_version_chain(iid, k + 5, retro=True)
        elif kind == "event":
            g = gen_event_vs_record(iid, k)
        elif kind == "plan":
            g = gen_plan(iid, k, completed=(k % 7) in {1, 3, 5})
        else:
            g = gen_expired(iid, k, extended=(k % 2 == 1))
        items.append(item(
            iid=iid, subset=SUBSET, type_=g["type"], tracks=["qa"], primary=["K5"], secondary=["K4"],
            aml=["C1", "C3", "D1", "N5"], sessions=g["sessions"], probes=g["probes"],
            template_id=g["template"], group=iid, difficulty=g["difficulty"], provenance="template",
        ))
    assign_splits(items)
    header = ("# MemCompass · mc-asof-temporal v0.2 新增条目（由 build/build_at.py 从双时态时间线程序化生成，请勿手改）\n"
              "# 金标由重放器计算：有效时间轴取覆盖该日的版本，记录时间轴只用当日及以前的记录。星期与天数由代码计算。")
    write_items(DATASETS / SUBSET / "generated.yaml", items, header)
    n_q = sum(len(it["probes"]) for it in items)
    print(f"{SUBSET}: 生成 {len(items)} 条、{n_q} 道题 → generated.yaml")


if __name__ == "__main__":
    main()
