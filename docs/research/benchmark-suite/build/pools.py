"""MemCompass 自建背景池（v0.2，CC BY 4.0，全部合成）。

两类池：
- PERSONAL_FACTS：与工作任务无关的个人生活事实，作为"背景记忆 + 背景会话"混入
  pr / ca / xa 等子集的每条用例（suite-design §9.3 的填充思路，参照 LongMemEval 的
  haystack）。目的：让记忆库不只有目标记忆，"每轮都注入"的做法无法天然精确，
  检索排序和相关性判断才有区分度。刻意选与各子集触发主题不相交的领域，
  并由 validate.py 的 V25 检查它们不与任何金标关键细节共享槽位。
- FILLER_QA：通用技术问答，用作 pf 的会话内填充、ts 的打断、at 的填充会话。
  不含任何用例槽位（数字、标识符），validate.py 的 V26 会做泄漏扫描。

不使用 ShareGPT、UltraChat 等许可不一的外部语料（suite-design §9.3）。
"""

from __future__ import annotations

from mcb import a, mem, rng, session, u

# (id, 用户原话, 记忆 content)
PERSONAL_FACTS: list[tuple[str, str, str]] = [
    ("bg-run-pace", "我最近在练 5 公里跑，目标配速是 5 分 30 秒。", "用户在练 5 公里跑，目标配速 5 分 30 秒。"),
    ("bg-cat-tuanzi", "我家的猫叫团子，它对鸡肉过敏，买猫粮要避开鸡肉的。", "用户家的猫叫团子，对鸡肉过敏。"),
    ("bg-book-zhishen", "最近在读《置身事内》，读到第三章了。", "用户在读《置身事内》，读到第三章。"),
    ("bg-coffee-bean", "周末我喜欢在家做手冲，最常用的是耶加雪菲的豆子。", "用户周末在家做手冲咖啡，常用耶加雪菲的豆子。"),
    ("bg-keyboard", "我的机械键盘是 87 键的茶轴。", "用户的机械键盘是 87 键茶轴。"),
    ("bg-hometown", "我老家在江西赣州，春节一般回老家过。", "用户老家在江西赣州，春节一般回老家。"),
    ("bg-guitar", "在学吉他，现在还在练 C 大调音阶。", "用户在学吉他，正在练 C 大调音阶。"),
    ("bg-coriander", "我不吃香菜，点外卖记得备注。", "用户不吃香菜。"),
    ("bg-nas", "家里的 NAS 是群晖 DS220+，主要用来备份照片。", "用户家里的 NAS 是群晖 DS220+，用来备份照片。"),
    ("bg-half-marathon", "去年跑过一次半马，成绩 2 小时 05 分。", "用户去年跑过一次半程马拉松，成绩 2 小时 05 分。"),
    ("bg-kindle", "我的 Kindle 里有三百多本书，大部分是历史类的。", "用户的 Kindle 里有三百多本书，主要是历史类。"),
    ("bg-balcony", "阳台上种了薄荷和罗勒，夏天长得很快。", "用户在阳台种了薄荷和罗勒。"),
    ("bg-camera", "我的相机是富士 X-T30，常用 35mm 定焦。", "用户的相机是富士 X-T30，常用 35mm 定焦镜头。"),
    ("bg-civ6", "玩《文明6》的时候我最喜欢用中国文明。", "用户玩《文明6》时最喜欢用中国文明。"),
    ("bg-documentary", "最近在看纪录片《河西走廊》。", "用户最近在看纪录片《河西走廊》。"),
    ("bg-pollen", "我对花粉过敏，春天出门都要戴口罩。", "用户对花粉过敏，春天出门戴口罩。"),
    ("bg-dali-trip", "去年秋天去云南大理玩了一周。", "用户去年秋天在云南大理旅行了一周。"),
    ("bg-oolong", "我平时喝无糖乌龙茶，不喝含糖饮料。", "用户平时喝无糖乌龙茶，不喝含糖饮料。"),
    ("bg-calligraphy", "学过两年书法，主要写楷书。", "用户学过两年书法，主要写楷书。"),
    ("bg-go-rank", "我的围棋水平大概是业余 3 段。", "用户的围棋水平约为业余 3 段。"),
    ("bg-swim", "每周六上午去游泳，一次游 1500 米。", "用户每周六上午游泳，一次 1500 米。"),
    ("bg-bonsai", "办公桌上养了一盆文竹，两周浇一次水。", "用户办公桌上养了一盆文竹，两周浇一次水。"),
    ("bg-podcast", "通勤的时候喜欢听历史类播客。", "用户通勤时喜欢听历史类播客。"),
    ("bg-badminton", "周三晚上和朋友打羽毛球。", "用户每周三晚上和朋友打羽毛球。"),
]


def background_date(sessions: list[dict], default: str = "2026-07-01") -> str:
    """背景会话日期：取 default 与"最早会话前 3 天"中较早的一个，保证会话日期非递减。"""
    import datetime as _dt
    earliest = min(_dt.date.fromisoformat(str(s["date"])[:10]) for s in sessions) if sessions else None
    d0 = _dt.date.fromisoformat(default)
    if earliest and earliest - _dt.timedelta(days=3) < d0:
        d0 = earliest - _dt.timedelta(days=3)
    return d0.isoformat()


def background(item_id: str, n: int = 6, date: str = "2026-07-01", sid: str = "s0") -> tuple[dict, list[dict]]:
    """为一条用例确定性地抽取 n 条背景事实，返回 (背景会话, 背景记忆列表)。

    背景记忆带 role=background，validate.py 据此确认它们不会成为任何探针的目标。
    """
    picks = rng(f"bg:{item_id}").sample(PERSONAL_FACTS, n)
    msgs = []
    mems = []
    for fid, said, content in picks:
        msgs.append(u(said))
        msgs.append(a("好的，记下了。"))
        mems.append(mem(fid, sid, content, "profile" if fid in {"bg-coriander", "bg-oolong", "bg-pollen"} else "episodic",
                        role="background"))
    return session(sid, date, msgs), mems


# 通用技术问答填充（问, 答）。刻意不含具体端口、版本号、编号等槽位型数字。
FILLER_QA: list[tuple[str, str]] = [
    ("Python 里列表推导式和生成器表达式有什么区别？", "列表推导式一次性生成整个列表；生成器表达式按需逐个产出元素，更省内存。"),
    ("git stash 是做什么的？", "把工作区还没提交的改动临时收起来，之后可以用 git stash pop 恢复。"),
    ("为什么 SQL 里 NULL = NULL 不成立？", "NULL 表示未知，未知与未知比较的结果仍是未知，要用 IS NULL 判断。"),
    ("正则里的贪婪和非贪婪匹配怎么区分？", "默认是贪婪匹配，尽量多吃字符；在量词后加问号就变成非贪婪，尽量少吃。"),
    ("HTTP 的幂等是什么意思？", "同一个请求执行一次和执行多次，对服务器状态的影响相同，比如 GET 和 PUT。"),
    ("Docker 镜像和容器是什么关系？", "镜像是只读模板，容器是镜像运行起来的实例，可以有自己的可写层。"),
    ("pandas 的 merge 和 concat 有什么不同？", "merge 按键做类似 SQL 的连接；concat 是沿某个轴把多个表直接拼起来。"),
    ("什么是缓存穿透？", "查询一个根本不存在的数据，缓存里没有、数据库里也没有，每次都打到数据库。"),
    ("单元测试里 mock 是干什么的？", "用假对象替换外部依赖，让测试只关注被测代码本身的逻辑。"),
    ("为什么推荐用虚拟环境管理 Python 依赖？", "不同项目的依赖版本可能冲突，虚拟环境把它们隔离开，互不影响。"),
    ("Linux 里软链接和硬链接的区别？", "软链接是指向路径的快捷方式，原文件删了就失效；硬链接指向同一个 inode。"),
    ("什么情况下该加数据库索引？", "经常用于过滤、排序、连接的列适合加索引；写多读少、区分度很低的列就不划算。"),
    ("JSON 和 YAML 怎么选？", "机器之间交换数据多用 JSON；需要人经常手改的配置文件用 YAML 更友好。"),
    ("什么是闭包？", "函数连同它引用的外部变量一起被保存下来，函数返回后仍能访问那些变量。"),
    ("为什么浮点数 0.1 + 0.2 不等于 0.3？", "二进制无法精确表示 0.1 和 0.2，相加后有微小误差，比较时要用容差。"),
    ("什么是 CI？", "持续集成：每次提交都自动构建和跑测试，尽早发现集成问题。"),
    ("TCP 三次握手是为了什么？", "双方确认彼此的收发能力正常，并同步初始序列号。"),
    ("Excel 里 VLOOKUP 和 XLOOKUP 差在哪？", "XLOOKUP 可以向左查、默认精确匹配、找不到时能返回自定义值，比 VLOOKUP 灵活。"),
    ("什么是乐观锁？", "先不加锁直接改，提交时检查数据是否被别人改过，改过就重试或报错。"),
    ("为什么日志要分级？", "按严重程度区分信息、警告和错误，线上可以只保留需要的级别，排查时更快定位。"),
    ("什么是 RESTful 风格？", "用资源路径表示对象，用 HTTP 方法表示操作，接口风格统一、可预期。"),
    ("Python 的 GIL 会影响什么？", "同一时刻只有一个线程执行 Python 字节码，CPU 密集任务用多线程提速有限。"),
    ("数据仓库和数据库有什么区别？", "数据库面向日常事务读写；数据仓库面向分析，存历史数据、按主题组织。"),
    ("什么是灰度发布？", "新版本先让一小部分用户使用，观察没问题再逐步扩大范围。"),
    ("为什么要做代码评审？", "发现逻辑问题、统一风格、传播知识，也能减少只有一个人懂某段代码的风险。"),
    ("什么是时间复杂度？", "描述算法耗时随输入规模增长的趋势，比如 O(n) 表示线性增长。"),
    ("Markdown 里怎么写表格？", "用竖线分隔列，第二行用短横线分隔表头和内容。"),
    ("什么是死锁？", "两个或多个任务互相等待对方释放资源，谁也无法继续。"),
    ("为什么密码要加盐再哈希？", "同样的密码加不同的盐会得到不同的哈希，防止彩虹表批量破解。"),
    ("VS Code 怎么多光标编辑？", "按住 Alt 点击可以放多个光标，或者选中一个词后按快捷键逐个选中相同的词。"),
    ("什么是 A/B 测试？", "把用户随机分成两组分别用不同方案，比较关键指标，判断哪个方案更好。"),
    ("为什么接口要做分页？", "一次返回全部数据会很慢、占内存，分页能控制每次的数据量。"),
    ("什么是正则化？", "在损失函数里加入对参数大小的惩罚，降低模型过拟合的风险。"),
    ("什么是消息队列？", "生产者把消息放进队列，消费者异步取出处理，用来解耦和削峰。"),
    ("为什么要写 README？", "让别人（包括以后的自己）快速知道项目是什么、怎么装、怎么用。"),
    ("什么是交叉验证？", "把数据分成几份，轮流用其中一份做验证、其余做训练，评估更稳定。"),
    ("CSV 文件里字段本身有逗号怎么办？", "用双引号把整个字段包起来，字段内的双引号写成两个双引号。"),
    ("什么是二分查找？", "在有序数组里每次和中间元素比较，把查找范围缩小一半。"),
    ("为什么要做数据备份的恢复演练？", "只有真正恢复过一次，才知道备份是不是完整可用、恢复要多久。"),
    ("什么是技术债？", "为了赶进度采用的临时方案，后续要花额外成本去偿还和修正。"),
]


# v0.3：带具体取值的"工作型"填充（pf 的 v0.3 用例用）。真实会话里，后面的工作也有大量具体细节，会与开头
# 交代的配置争夺压缩摘要的篇幅；通用问答型填充会被压缩器一句"其余为通用问答"带过，测不出记忆的作用。
# 取值范围与 pf 用例的槽位刻意错开（端口 7000–7999、工单 DEV-、主机与文件带 tmp 前缀），不会与金标串线。
WORK_TEMPLATES: list[tuple[str, str]] = [
    ("把 src/tmp_{w}/client.py 里的连接超时改成 {n} 秒。", "已把 src/tmp_{w}/client.py 的连接超时改成 {n} 秒。"),
    ("看一下 tmp-{w}.example.internal 这台机器的负载。", "tmp-{w}.example.internal 当前 CPU {p}%，内存 {q}%，磁盘 {r}%。"),
    ("PR #{k} 的评审意见处理一下。", "PR #{k} 的 {s} 条意见都处理了，其中 {t} 条改了实现。"),
    ("临时起个调试服务，用 {port} 端口。", "调试服务已在 {port} 端口启动，进程号 {pid}。"),
    ("工单 DEV-{k} 改成处理中，指派给我。", "DEV-{k} 已改为处理中，指派给你。"),
    ("把 feature/tmp-{w} 合到 develop。", "feature/tmp-{w} 已合并到 develop，解决了 {s} 处冲突。"),
    ("跑一下 tests/test_tmp_{w}.py。", "tests/test_tmp_{w}.py 共 {k2} 个用例，{s} 个失败，失败的是断言 {n} == {n2}。"),
    ("缓存 key 前缀先定成 tmp:{w}:v{s}。", "好的，缓存 key 前缀用 tmp:{w}:v{s}。"),
    ("把定时任务 tmp_{w}_sync 的频率改成每 {n} 分钟一次。", "tmp_{w}_sync 已改为每 {n} 分钟执行一次。"),
    ("查一下昨天 tmp-{w} 服务的报错数。", "昨天 tmp-{w} 服务共报错 {k2} 次，其中超时 {s} 次。"),
]
_WORDS = ["alpha", "bravo", "delta", "kilo", "lima", "oscar", "sierra", "tango", "victor", "zulu"]


def work_filler_turns(seed: str, n_pairs: int) -> list[dict]:
    """确定性生成 n_pairs 组带具体取值的工作型填充，返回消息列表。"""
    r = rng(f"work:{seed}")
    out = []
    for _ in range(n_pairs):
        q, ans = r.choice(WORK_TEMPLATES)
        vals = {"w": f"{r.choice(_WORDS)}{r.randint(1, 9)}", "n": r.randint(3, 59), "n2": r.randint(3, 59),
                "p": r.randint(5, 95), "q": r.randint(5, 95), "r": r.randint(5, 95), "k": r.randint(1000, 9999),
                "k2": r.randint(10, 400), "s": r.randint(1, 9), "t": r.randint(0, 5), "port": r.randint(7000, 7999),
                "pid": r.randint(10000, 60000)}
        out.append(u(q.format(**vals)))
        out.append(a(ans.format(**vals)))
    return out


def filler_turns(seed: str, n_pairs: int, pool: str | None = None) -> list[dict]:
    """确定性抽取 n_pairs 组填充（可重复抽样），返回消息列表。pool="work-zh-detailed" 时用工作型填充，
    其余（包括 v0.2 用例里写的 coding-zh-generic / ops-zh-generic）一律用通用问答，行为与 v0.2 相同。"""
    if pool == "work-zh-detailed":
        return work_filler_turns(seed, n_pairs)
    r = rng(f"filler:{seed}")
    out = []
    for _ in range(n_pairs):
        q, ans = r.choice(FILLER_QA)
        out.append(u(q))
        out.append(a(ans))
    return out
