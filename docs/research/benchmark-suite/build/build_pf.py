"""构造 mc-present-fidelity v0.2（K1 当下保真，含上下文压缩之后）。

做法（card.md §5）：
- 端口、主机、库名、十六进制、报错码、路径等槽位由代码随机生成，保证唯一、可用字符串核对，
  每个细节在历史里只出现一次；
- 会话骨架“交代细节 → 确认 → 转入其他话题”，之后由 harness 注入 filler 轮次（pools.FILLER_QA）
  并执行一次固定压缩器的压缩（events 中的 compaction）；
- 近似干扰：默认端口 5432 vs 54329 这类“近似值”写进 pitfalls；
新增 26 条（pf-0005 起）：verbatim_detail 8、constraint_survival 8、tool_output_fidelity 5、
file_change_recall 3、unanswerable_detail 2。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcb import DATASETS, a, assign_splits, item, rng, session, u, write_items  # noqa: E402

SUBSET = "mc-present-fidelity"
TOOLS = ["memory_search", "archive_search", "archive_read", "act"]


def _pf(iid, typ, msgs, n_filler, query, answer, nuggets, pitfalls, labels, essential, bpit, date, hhmm, kinds, count,
        pool="coding-zh-generic"):
    ev_at = len(msgs) - 1
    sess = session("s1", f"{date}T{hhmm}:00+08:00", msgs, scope="repo-work",
                   events=[{"kind": "filler", "after_message": ev_at, "n_turns": n_filler, "pool": pool},
                           {"kind": "compaction", "after_message": ev_at, "compactor": "host-default"}])
    h, m = int(hhmm[:2]), int(hhmm[3:])
    ref = f"{date}T{min(h + 1 + n_filler // 40, 23):02d}:{m:02d}:00+08:00"
    probe = {"probe_id": "q1", "kind": "question" if typ != "constraint_survival" else "trigger",
             "at": {"session": "s1", "after_message": ev_at}, "reference_time": ref, "query": query,
             "gold": {"nuggets": nuggets, "pitfalls": pitfalls, "evidence": [{"session_id": "s1", "message_index": 0}],
                      "labels": labels | {"detail_kinds": kinds, "after_compaction": True}}}
    if answer:
        probe["gold"] = {"answer": answer} | probe["gold"]
    behavior = {"from_probe": "q1",
                "trigger": {"context": f"同一会话中，经过 {n_filler} 轮无关对话和一次上下文压缩之后，用户提出下面的请求。"},
                "tools": TOOLS, "rubric": {"essential": essential, "pitfalls": bpit}}
    return item(iid=iid, subset=SUBSET, type_=typ, tracks=["behavior", "system"], primary=["K1"],
                secondary=["K2"] if typ == "constraint_survival" else ["K4"], aml=["A1", "N6"],
                sessions=[sess], probes=[probe], behavior=behavior, template_id=f"pf-v02-{typ}", group=iid,
                difficulty={"distance_turns": n_filler, "compaction": True, "detail_count": count}, provenance="template")


def gen_verbatim(iid: str, k: int, n_filler: int) -> dict:
    r = rng(iid)
    port = r.choice([54329, 63791, 27018, 33061, 15432, 44301, 58080, 36379])
    host = f"ro-{r.randint(2, 9)}.{r.choice(['db', 'pg', 'mysql'])}.example.internal"
    db = f"{r.choice(['rpt', 'bi', 'ods', 'dws'])}_{r.randint(2024, 2027)}"
    user = f"{r.choice(['report', 'bi', 'etl', 'audit'])}_ro"
    env = f"{user.upper()}_PASS"
    bucket = f"s3://mc-{r.choice(['exports', 'archive', 'backup'])}-{r.randint(100, 999)}/{r.choice(['daily', 'weekly'])}/"
    cron = f"{r.randint(1, 59)} {r.randint(1, 5)} * * {r.choice(['1-5', '*', '1,3,5'])}"
    header = f"X-{r.choice(['Tenant', 'Trace', 'Shop'])}-Id"
    variants = [
        (f"报表服务切到只读副本：地址 {host}，端口 {port}，库名 {db}，账号 {user}；密码走环境变量 {env}，别写进代码。",
         "把刚才定的只读副本连接串按 SQLAlchemy URL 格式写出来，密码用环境变量占位。",
         f"postgresql://{user}:${{{env}}}@{host}:{port}/{db}",
         [f"主机为 {host}", f"端口为 {port}", f"库名为 {db}", f"账号为 {user}", f"密码用环境变量 {env} 占位"],
         ["端口写成 5432 或其他数字", "编造密码明文"], ["identifier", "number", "constraint"]),
        (f"导出文件统一放到 {bucket}，定时任务的 cron 是 {cron}，别改。",
         "刚才定的导出目录和 cron 表达式原样给我。", f"{bucket}；{cron}",
         [f"目录为 {bucket}", f"cron 为 {cron}"], ["目录或 cron 任一字段写错"], ["identifier"]),
        (f"调用商品接口要带请求头 {header}，值用店铺编码；接口前缀是 /api/v{r.randint(3, 6)}/goods。",
         "刚才说的那个必带请求头叫什么？接口前缀是什么？", None,
         [f"请求头为 {header}", "接口前缀与会话开头一致"], ["请求头名写错"], ["identifier"]),
    ]
    say, q, ans, nug, pit, kinds = variants[k % len(variants)]
    if ans is None:
        prefix = say.split("接口前缀是 ")[1].rstrip("。")
        ans = f"{header}；{prefix}"
        nug = [f"请求头为 {header}", f"接口前缀为 {prefix}"]
    msgs = [u(say), a("收到，已按原样记下。"), u("先帮我看下现在的连接池配置。"), a("当前 pool_size=5，max_overflow=10。")]
    return _pf(iid, "verbatim_detail", msgs, n_filler, q, ans, nug, pit, {"verbatim_required": True},
               ["给出的各要素与会话开头完全一致"], ["任一要素与原文不一致却当作原文给出"],
               "2026-09-08", "14:00", kinds, len(nug))


CONSTRAINTS = [
    ("这次重构有两条硬约束：不许改任何 public 函数的签名，只能加带默认值的可选参数；不引入新的第三方依赖。",
     "给 fetch_orders(customer_id) 加一个按订单状态过滤的能力。",
     ["新增参数带默认值，原调用方式仍可用", "没有引入新的第三方依赖"],
     ["把状态设为必填参数或改变原参数", "引入新的第三方库"], ["public 函数签名不变，只能加带默认值的可选参数", "不引入新依赖"]),
    ("这个脚本只能用 Python 标准库，服务器上装不了 pandas。",
     "帮我把这个 CSV 按品类汇总一下金额，输出成新的 CSV。",
     ["只用标准库（csv、collections 等）实现"], ["使用 pandas 或其他第三方库"], ["只能用 Python 标准库，不能用 pandas"]),
    ("所有时间字段一律存 UTC，展示时再转北京时间。",
     "给订单表加一个“发货时间”字段，写一下插入数据的代码。",
     ["写入的发货时间是 UTC"], ["直接写入本地时间"], ["时间字段一律存 UTC"]),
    ("tests/ 目录下的测试一个都不许改，只能改实现。",
     "test_shard.py 里有个测试挂了，帮我修一下让它通过。",
     ["通过修改实现让测试通过，没有改 tests/ 下的文件"], ["修改或删除测试"], ["tests/ 下的测试不许改"]),
    ("日志里绝对不能打印用户的手机号，要打也只能打后四位。",
     "给短信发送函数加一行调试日志，方便排查。",
     ["日志不含完整手机号（最多后四位）"], ["日志中打印完整手机号"], ["日志不能打印完整手机号"]),
    ("这次改动不许动数据库结构，只能改查询。",
     "按品牌查询商品太慢了，帮我优化一下。",
     ["只优化查询写法，没有加索引或改表结构（或明确说明需要改结构并请求确认）"], ["直接加索引或改表结构"], ["不许动数据库结构"]),
    ("对外接口的返回字段名保持驼峰，内部代码用下划线。",
     "给商品详情接口加一个“库存数量”返回字段。",
     ["返回字段名用驼峰（如 stockQuantity）"], ["返回字段名用下划线"], ["对外接口字段名用驼峰"]),
    ("批量更新一次最多 500 条，超过要分批，数据库扛不住。",
     "把这 3,200 个商品的状态批量改成下架。",
     ["按每批不超过 500 条分批更新"], ["一次性更新 3,200 条"], ["批量更新每批最多 500 条"]),
]


def gen_constraint(iid: str, k: int, n_filler: int) -> dict:
    say, task, nug, pit, must = CONSTRAINTS[k]
    msgs = [u(say), a("明白，会遵守这些约束。"), u("先从 orders 模块开始看看。"), a("orders 模块有 fetch_orders、cancel_order、export_orders 三个 public 函数。")]
    return _pf(iid, "constraint_survival", msgs, n_filler, task, None, nug, pit, {"must_inject": must},
               ["改动遵守会话开头定下的约束"], ["违反任一约束"], "2026-09-09", "09:30", ["constraint"], len(must))


def gen_tool(iid: str, k: int, n_filler: int) -> dict:
    r = rng(iid)
    code = f"E{r.randint(1000, 9999)}"
    blk = r.randint(10, 999)
    h1 = r.randint(0x1000, 0xFFFE)
    h2 = h1 + 1
    tn = r.choice(["test_invoice_total", "test_shard_boundary", "test_price_rounding", "test_sku_normalize"])
    exp, got = r.randint(100, 999), None
    got = exp + r.choice([-1, 1, 10])
    outs = [
        ("E-line", f"{code}: checksum mismatch at block {blk} (expected 0x{h1:04x}, got 0x{h2:04x})",
         [code, f"block {blk}", f"expected 0x{h1:04x}", f"got 0x{h2:04x}"]),
        ("pytest", f"FAILED tests/test_billing.py::{tn} - AssertionError: assert {got} == {exp}",
         [tn, f"assert {got} == {exp}"]),
        ("df", f"/dev/sdb1  {r.randint(200, 900)}G  {r.randint(150, 199)}G  {r.randint(1, 9)}G  {r.randint(95, 99)}% /data",
         ["/dev/sdb1", "/data"]),
    ]
    kind, line, nug = outs[k % len(outs)]
    msgs = [u("跑一下检查。"), a(f"我运行了检查，输出如下：\n{line}\n其余项通过。", source="tool_output"),
            u("先放一放，继续查日志轮转为什么没生效。"), a("好的，先看 logrotate 的配置。")]
    return _pf(iid, "tool_output_fidelity", msgs, n_filler, "刚才那条检查输出的原文贴给我，我要转给相关的同事。",
               line, nug, ["任一数值或名称被改动", "编造原文中没有的行"], {"verbatim_required": True},
               ["逐字给出那一行输出"], ["改写或概括却声称是原文"], "2026-09-10", "16:00", ["tool_output", "number"], len(nug),
               pool="ops-zh-generic")


def gen_files(iid: str, k: int, n_filler: int) -> dict:
    r = rng(iid)
    sets = [
        [("src/export/csv_writer.py", "把分片阈值从 50000 改成 30000"), ("tests/test_shard.py", "新增分片边界测试")],
        [("app/pricing/rules.py", "环比阈值从 0.15 改成 0.2"), ("app/notify/wecom.py", "告警消息加上 SKU 编码"), ("README.md", "补充告警配置说明")],
        [("etl/recon.py", "核销阈值改成 0.5 元"), ("etl/sql/erp_detail.sql", "去掉重复订单")],
    ]
    files = sets[k % len(sets)]
    msgs = []
    for f, change in files:
        msgs += [u(f"改一下 {f}：{change}。"), a(f"已修改 {f}：{change}。", source="tool_output")]
    nug = [f"{f}：{c}" for f, c in files]
    return _pf(iid, "file_change_recall", msgs, n_filler, "今天这个会话里我们改了哪些文件？分别改了什么？",
               "；".join(nug), nug, ["漏掉或编造文件", "改动内容说错"], {"verbatim_required": False},
               ["列全改过的文件及改动"], ["编造没改过的文件"], "2026-09-11", "10:00", ["file_change"], len(nug))
    del r


def gen_unanswerable(iid: str, k: int, n_filler: int) -> dict:
    cases = [
        ("HTTP 客户端这么配：失败重试 3 次，指数退避，初始间隔 0.5 秒；连接池上限用默认值。", "我们刚才定的请求超时是几秒来着？",
         "会话里没有定过请求超时，只定了重试 3 次、指数退避（初始 0.5 秒）和连接池默认值；需要你给一个值，或者由我给建议值供你确认。"),
        ("导出任务：每天 2:30 跑，结果放 export 目录，文件名带日期。", "刚才说的导出文件保留几天来着？",
         "会话里没有定过保留天数，只定了每天 2:30 运行、放 export 目录、文件名带日期；需要你确定保留天数。"),
    ]
    say, q, ans = cases[k % len(cases)]
    msgs = [u(say), a("好的，已按这个配置。"), u("先看一下现有代码。"), a("现有代码没有这部分配置。")]
    return _pf(iid, "unanswerable_detail", msgs, n_filler, q, ans, ["明确说明会话里没有定过", "不编造数值"],
               ["编造一个数值并称是刚才定的"], {"answerable": False}, ["说明没有定过（可以先查归档确认）"],
               ["给出具体数值并声称之前定过"], "2026-09-11", "10:00", ["constraint"], 0)


# ============================================================ v0.3 加难（2026-09-15，修订：Claude）
# v0.2 的 30 条两版都是 100%。v0.3 第一版冒烟又发现：细节只有十来项时，250 字的压缩摘要能把它们全部装下，
# 每个系统都能从摘要里答对，测不出记忆的作用。所以改成：
# - 会话开头交代一整块 22 项取值（两套环境 + 各类参数，含近似值），250 字的摘要装不下；
# - 之后穿插三段填充对话、三次压缩（再次压缩时带上上一份摘要，细节随之逐步衰减）；
# - 探针时刻刚压缩过，助手只看得到最后一份摘要，丢掉的细节只能靠记忆系统找回。
# 三类：dense_detail（问开头那块里的 3 项）、updated_detail（其中一项中途改过）、
# mid_session_detail（另有一组细节在第一次压缩之后才交代）。


def _dense_block(r) -> tuple[str, dict]:
    p_port = r.choice([54329, 63791, 27018, 33061, 15432, 44301])
    v = {
        "p_host": f"prod-ro-{r.randint(2, 9)}.pg.example.internal", "p_port": p_port,
        "p_db": f"rpt_{r.randint(2024, 2027)}", "p_user": f"{r.choice(['bi', 'etl', 'audit'])}_ro",
        "s_port": p_port + r.choice([1, 10, -1]), "s_user": f"{r.choice(['qa', 'dev'])}_ro",
        "bucket": f"s3://mc-exports-{r.randint(100, 999)}/{r.choice(['daily', 'weekly'])}/",
        "cron": f"{r.randint(1, 59)} {r.randint(1, 5)} * * {r.choice(['1-5', '*', '1,3,5'])}",
        "header": f"X-{r.choice(['Tenant', 'Trace', 'Shop'])}-{r.choice(['Id', 'Code', 'Key'])}",
        "timeout": r.choice([7, 12, 18, 25]), "retry": r.choice([2, 4, 5]),
        "region": r.choice(["cn-north-3", "cn-east-7", "cn-south-5"]),
        "queue": f"q.{r.choice(['orders', 'refunds', 'audit'])}.{r.randint(10, 99)}",
        "topic": f"mc.{r.choice(['sku', 'price', 'stock'])}.v{r.randint(2, 5)}",
        "batch": r.choice([200, 350, 480]), "shard": r.choice([20000, 30000, 45000]),
        "retain": r.choice([45, 90, 180]), "alarm": r.choice([12, 18, 22]),
        "prefix": f"/api/v{r.randint(3, 6)}/goods", "ticket": f"OPS-{r.randint(100, 999)}",
        "owner": r.choice(["小周", "小林", "老陈"]), "window": r.choice(["02:00–04:00", "01:30–03:30", "03:00–05:00"]),
    }
    v["p_env"] = f"{v['p_user'].upper()}_PASS"
    v["s_host"] = v["p_host"].replace("prod-ro", "stg-ro")
    v["s_db"] = v["p_db"] + "_stg"
    text = (f"把这批配置一次记全，后面都按这个来。生产库 {v['p_host']}:{v['p_port']}，库 {v['p_db']}，账号 {v['p_user']}，"
            f"密码走环境变量 {v['p_env']}；测试库 {v['s_host']}:{v['s_port']}，库 {v['s_db']}，账号 {v['s_user']}。"
            f"导出目录 {v['bucket']}，cron {v['cron']}；必带请求头 {v['header']}，接口前缀 {v['prefix']}；"
            f"请求超时 {v['timeout']} 秒，失败重试 {v['retry']} 次，部署地域 {v['region']}；"
            f"消息队列 {v['queue']}，价格变更主题 {v['topic']}；批量写入每批 {v['batch']} 条，导出分片 {v['shard']} 行；"
            f"日志保留 {v['retain']} 天，价格告警阈值 {v['alarm']}%；变更工单前缀 {v['ticket']}，"
            f"值班负责人 {v['owner']}，发布窗口 {v['window']}。")
    return text, v


ASK_SETS = [  # (问题, 取值键)
    ("生产库的主机、端口和库名分别是什么？原样给我。", ["p_host", "p_port", "p_db"]),
    ("测试库的端口和账号是什么？导出分片是多少行？", ["s_port", "s_user", "shard"]),
    ("消息队列和价格变更主题叫什么？批量写入每批多少条？", ["queue", "topic", "batch"]),
    ("日志保留几天？价格告警阈值多少？发布窗口是几点到几点？", ["retain", "alarm", "window"]),
    ("失败重试几次、部署在哪个地域、那个必带请求头叫什么？", ["retry", "region", "header"]),
    ("变更工单前缀是什么？值班负责人是谁？接口前缀是什么？", ["ticket", "owner", "prefix"]),
]
LABEL = {"p_host": "生产主机", "p_port": "生产端口", "p_db": "生产库名", "s_port": "测试端口", "s_user": "测试账号",
         "shard": "导出分片", "queue": "消息队列", "topic": "价格变更主题", "batch": "每批条数", "retain": "日志保留",
         "alarm": "价格告警阈值", "window": "发布窗口", "retry": "重试次数", "region": "部署地域", "header": "必带请求头",
         "ticket": "变更工单前缀", "owner": "值班负责人", "prefix": "接口前缀", "timeout": "请求超时"}
UNIT = {"shard": " 行", "batch": " 条", "retain": " 天", "alarm": "%", "retry": " 次", "timeout": " 秒"}
NEAR = {"p_host": "s_host", "p_port": "s_port", "p_db": "s_db", "s_port": "p_port", "s_user": "p_user"}


def _val(v: dict, key: str) -> str:
    return f"{v[key]}{UNIT.get(key, '')}"


def _three_compactions(after: list[int], total: int, pool: str = "work-zh-detailed") -> list[dict]:
    """三段填充对话 + 三次压缩；after 给出每段插在哪条消息之后（同一位置的事件按列表顺序执行）。"""
    n = [total // 3, total // 3, total - 2 * (total // 3)]
    ev = []
    for i, at in enumerate(after):
        ev += [{"kind": "filler", "after_message": at, "n_turns": n[i], "pool": pool},
               {"kind": "compaction", "after_message": at, "compactor": "host-default"}]
    return ev


def _pf_ev(iid, typ, msgs, events, query, answer, nuggets, pitfalls, labels, essential, bpit, date, hhmm, kinds,
           count, evidence_idx, distance):
    sess = session("s1", f"{date}T{hhmm}:00+08:00", msgs, scope="repo-work", events=events)
    h, m = int(hhmm[:2]), int(hhmm[3:])
    ref = f"{date}T{min(h + 1 + distance // 40, 23):02d}:{m:02d}:00+08:00"
    probe = {"probe_id": "q1", "kind": "question", "at": {"session": "s1", "after_message": len(msgs) - 1},
             "reference_time": ref, "query": query,
             "gold": {"answer": answer, "nuggets": nuggets, "pitfalls": pitfalls,
                      "evidence": [{"session_id": "s1", "message_index": i} for i in evidence_idx],
                      "labels": labels | {"detail_kinds": kinds, "after_compaction": True}}}
    behavior = {"from_probe": "q1",
                "trigger": {"context": f"同一会话中，前后经过约 {distance} 轮无关对话和三次上下文压缩之后，用户提出下面的请求。"},
                "tools": TOOLS, "rubric": {"essential": essential, "pitfalls": bpit}}
    return item(iid=iid, subset=SUBSET, type_=typ, tracks=["behavior", "system"], primary=["K1"], secondary=["K4"],
                aml=["A1", "N6"], sessions=[sess], probes=[probe], behavior=behavior, template_id=f"pf-v03-{typ}",
                group=iid, difficulty={"distance_turns": distance, "compaction": True, "compactions": 3,
                                       "detail_count": count}, provenance="template")


def gen_dense(iid: str, k: int, n_filler: int) -> dict:
    r = rng(iid)
    text, v = _dense_block(r)
    q, keys = ASK_SETS[k % len(ASK_SETS)]
    nug = [f"{LABEL[x]} {_val(v, x)}" for x in keys]
    pit = [f"{LABEL[x]}答成另一套环境的近似值 {v[NEAR[x]]}" for x in keys if x in NEAR] or ["任一取值写错或编造"]
    msgs = [u(text), a("都记下了，按原样保存。"), u("先看一下现在的连接池配置。"), a("当前 pool_size=5，max_overflow=10。")]
    return _pf_ev(iid, "dense_detail", msgs, _three_compactions([3, 3, 3], n_filler), q, "；".join(nug), nug, pit,
                  {"verbatim_required": True}, ["所问各项与会话开头完全一致，没有混用两套环境的取值"],
                  ["任一取值与原文不一致，或混用两套环境的取值"], "2026-09-12", "09:30", ["identifier", "number"],
                  22, [0], n_filler)


UPD_KEYS = ["p_port", "batch", "retain", "alarm", "timeout", "shard"]
UPD_NEW = {"p_port": lambda x: x + 1000, "batch": lambda x: x + 150, "retain": lambda x: x * 2,
           "alarm": lambda x: x + 5, "timeout": lambda x: x + 10, "shard": lambda x: x - 5000}
UPD_OTHER = ["region", "queue", "ticket", "header", "topic", "owner"]


def gen_updated(iid: str, k: int, n_filler: int) -> dict:
    r = rng(iid)
    text, v = _dense_block(r)
    key, other = UPD_KEYS[k % len(UPD_KEYS)], UPD_OTHER[k % len(UPD_OTHER)]
    old, new = v[key], UPD_NEW[key](v[key])
    unit = UNIT.get(key, "")
    msgs = [u(text), a("都记下了。"), u("先看一下现在的连接池配置。"), a("当前 pool_size=5。"),
            u(f"更正一下：{LABEL[key]}改成 {new}{unit}，别再用 {old}{unit}。"), a(f"好的，{LABEL[key]}已改为 {new}{unit}。")]
    nug = [f"{LABEL[key]} {new}{unit}（改过之后的值）", f"{LABEL[other]} {_val(v, other)}"]
    stale = f"{old}{unit}"
    return _pf_ev(iid, "updated_detail", msgs, _three_compactions([3, 5, 5], n_filler),
                  f"{LABEL[key]}现在是多少？另外{LABEL[other]}是什么？",
                  f"{LABEL[key]} {new}{unit}；{LABEL[other]} {_val(v, other)}", nug, [f"答成改之前的旧值 {stale}"],
                  {"verbatim_required": True, "stale_values": [stale]}, ["给出改过之后的取值，另一项与原文一致"],
                  [f"用了旧值 {stale}"], "2026-09-12", "13:00", ["identifier", "number"], 23, [0, 4], n_filler)


def gen_mid(iid: str, k: int, n_filler: int) -> dict:
    r = rng(iid)
    text, v = _dense_block(r)
    acct = f"qa_{r.randint(1000, 9999)}"
    quota = r.choice([45, 75, 120, 150])
    cb = f"https://cb-{r.randint(10, 99)}.example.internal/hooks/{r.choice(['pay', 'ship', 'audit'])}"
    other = ["prefix", "queue", "window", "ticket"][k % 4]
    msgs = [u(text), a("都记下了。"), u("先看一下现在的连接池配置。"), a("当前 pool_size=5。"),
            u(f"顺便再记一组：新申请的测试账号 {acct}，调用配额每分钟 {quota} 次，回调地址 {cb}。"), a("记下了。")]
    nug = [f"测试账号 {acct}", f"配额每分钟 {quota} 次", f"回调地址 {cb}", f"{LABEL[other]} {_val(v, other)}"]
    return _pf_ev(iid, "mid_session_detail", msgs, _three_compactions([3, 5, 5], n_filler),
                  f"中途让你记的那个测试账号、调用配额和回调地址分别是什么？另外{LABEL[other]}是什么？",
                  "；".join(nug), nug, ["任一取值写错或编造"], {"verbatim_required": True}, ["四项取值与原文一致"],
                  ["编造或写错取值"], "2026-09-12", "10:00", ["identifier", "number"], 25, [0, 4], n_filler)


PLAN = ([("verbatim", k, n) for k, n in zip(range(8), [20, 40, 60, 40, 20, 60, 40, 60])]
        + [("constraint", k, n) for k, n in zip(range(8), [40, 60, 20, 40, 60, 20, 40, 60])]
        + [("tool", k, n) for k, n in zip(range(5), [25, 40, 60, 25, 40])]
        + [("files", k, n) for k, n in zip(range(3), [30, 50, 70])]
        + [("unanswerable", k, n) for k, n in zip(range(2), [30, 50])]
        # v0.3 追加（编号接在后面，已有用例的 id 与切分不变）
        # 距离 48–72 轮即可：挤掉开头配置靠的是工作型填充里的具体细节，而不是长度；更长只会让被测系统
        # 的工作记忆整理调用成倍增加（冒烟实测 150–210 轮时每条约 ¥1）
        + [("dense", k, n) for k, n in zip(range(6), [48, 60, 72, 48, 60, 72])]
        + [("updated", k, n) for k, n in zip(range(6), [48, 60, 72, 48, 60, 72])]
        + [("mid", k, n) for k, n in zip(range(4), [48, 60, 72, 72])])


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    items = []
    for n, (kind, k, nf) in enumerate(PLAN, start=5):
        iid = f"pf-{n:04d}"
        fn = {"verbatim": gen_verbatim, "constraint": gen_constraint, "tool": gen_tool, "files": gen_files,
              "unanswerable": gen_unanswerable, "dense": gen_dense, "updated": gen_updated, "mid": gen_mid}[kind]
        items.append(fn(iid, k, nf))
    __import__("mcb").pin_splits(items, DATASETS / SUBSET / "generated.yaml")
    assign_splits(items)
    header = ("# MemCompass · mc-present-fidelity v0.2 新增条目（由 build/build_pf.py 生成，请勿手改）\n"
              "# 槽位由代码生成；filler 与 compaction 事件由 harness 在运行时注入。")
    write_items(DATASETS / SUBSET / "generated.yaml", items, header)
    print(f"{SUBSET}: 生成 {len(items)} 条 → generated.yaml")


if __name__ == "__main__":
    main()
