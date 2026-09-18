# MemCompass：agent 记忆能力画像评测套件

> 版本 0.3.0-draft（2026-09-15）｜状态：数据、校验、runner 均可运行；用户已默认通过全部用例（见 `datasets/verification.yaml`）｜全部为合成数据
> 依据：`../agent-memory-capability-framework.md` v0.4.1；v0.1 设计稿（`suite-design.md`）。名称 MemCompass 为暂定，公开前需要查重。

## 1. 一句话

用"**能力画像**"评测 agent 的记忆系统：公开集已覆盖好的部分（事实召回、最新状态、多跳、个性化）只引用；公开集覆盖不到的 8 个方向新建子集；机制价值用消融证明；4 个子集与 AML 的 Add/Search 协议兼容。

## 2. v0.2 相对 v0.1 的变化

| 方面 | v0.1（2026-09-13） | v0.2（2026-09-14） |
|---|---|---|
| 规模 | 36 条种子 | **317 条用例 / 418 个探针**（8 个子集） |
| 构造 | agent 手写示例 | 各子集有构造脚本 `build/build_<子集>.py`：紧凑规格 + 程序化金标 + 统一骨架；草稿 24 条由 `build/migrate_drafts.py` 机械迁移 |
| 金标 | 手写 | at 由双时态时间线**重放器**计算；ts 由操作序列**重放器**计算；fg / mp 由代码生成**全库唯一的硬标记**；pf 槽位由代码随机生成；星期与天数由代码算 |
| 难度与现实性 | 记忆库里只有目标记忆 | 每条 pr / ca 用例混入 6 条背景记忆与背景会话（自建池 `build/pools.py`），部分混入近领域干扰记忆；at 每条插 2 个填充会话 |
| 公平性设计 | — | pr：正负例成**最小对比对**（同 group 同切分），类比迁移 5 条各配"同领域无共同原理"负例；at 计划类含"已完成"对照（防一律弃答）；mp 攻击 80% 以上配表层同形的良性对照（算误拦） |
| 校验 | V01–V24 | 新增 V25–V31（背景记忆不能当目标、填充泄漏扫描、ca 关键细节在"记忆 / 原文"的位置与类型一致、多轮标签单调、星期核对、ts 六字段、xa 目标完整）、数据集级 D01–D06（比例与平衡）、包级 P02/P03/P04/P05 |
| 执行 | 只有 pr / ca 两个专项的草稿 runner | 统一 runner `runners/`：8 个子集、S / E / Q 三种模式、5 类对照组，逐题结果流式落盘，报告带 Wilson 区间与配对 McNemar |

**v0.3（2026-09-15）**：按用户的规则"区分不出版本差异的子集说明太简单，修订后重测"，对 v0.2 正式运行里两版都满分、或攻击两版都挡住的子集加难，共新增 40 条（357 条）；v0.3 第一轮正式运行后 ts 仍区分不出（答题器可见的最近 40 条消息里就有完整状态），再加 16 条让状态落到可见窗口之外，并修正 v0.2 并行场景的金标缺陷（共 373 条）。构造脚本 `build/` 此前被仓库的通用 `build/` 忽略规则挡在 git 外，已补入库；全部重新生成与已发布文件逐字节一致。
- 修订模型：Claude，与答题器（DeepSeek）和评委（Kimi K3）都不同源（用户要求）。
- 各子集的具体改动见各数据卡末尾的"v0.3 修订"一节；`build/mcb.py::pin_splits` 保证重新生成时已有用例的切分不变（317 条一条未动）。
- 评委改为 Kimi K3（Kimi Code CLI，走用户会员），与 qwen3.8-max 的一致性：问答 98%（κ 0.93）、主动回忆 96%（κ 0.92）、完整性对齐 85%（κ 0.69）。

## 3. 套件一览（v0.3）

| 轨道 | 子集 | 能力 | 用例 / 探针 | 类型分布 | 切分 dev/test/held-out |
|---|---|---|---|---|---|
| B+S | `mc-proactive-recall` | K8、K7 | 60 / 80 | 应浮现 20（类比 5）、间接线索 10、不应浮现 20、时间陷阱 10；多轮 32% | 11 / 32 / 17 |
| B+Q | `mc-completeness-alignment` | K9、K10、K3 | 64 / 20 | 8 类各 8 条（v0.3 新增长规则表 rule_table_update）；20 道细节保留问答 | 12 / 33 / 19 |
| Q | `mc-asof-temporal` | K5 | 45 / 130 | as-of 12、追溯更正 10、事件 vs 记录 8、计划 8（含已完成对照 3）、有效期 7 | 10 / 23 / 12 |
| Q+S | `mc-forget-request` | K12 | 30 / 70 | 基本 10、间接 8、按作用域 6、忘后重告 6 | 7 / 15 / 8 |
| Q+B | `mc-memory-poisoning` | K12 | 58 / 58 | 嵌入指令 10、低可信 8、延迟触发 5、无标记指令 3、反复传言 3、看似合理的有害做法 3、良性对照 26 | 11 / 32 / 15 |
| B+S | `mc-task-state` | K2 | 46 / 46 | 更新 7、打断 6、切换 5、跨会话 7、并行 5；v0.3 新增连改两次 + 长填充 6、三会话改值与答复 5、三任务并行 + 填充 5 | 10 / 23 / 13 |
| B | `mc-present-fidelity` | K1 | 46 / 46 | 逐字细节 9、约束存活 9、工具输出 6、改动文件 3、不可答 3；v0.3 新增密集细节 6、中途改值 6、中途交代 4（三次压缩） | 10 / 23 / 13 |
| B+S | `mc-cross-agent` | K13 | 24 / 24 | 跨宿主迁移 7、作用域隔离 7、身份解析 4、并发写入 6 | 6 / 12 / 6 |

v1.0 目标规模仍以各数据卡为准（合计约 1,300 条）；v0.2 的规模已足够给出方向与区间，但 test 切分上多数子集 n < 50，区间较宽，报告时如实标注。

## 4. 文件索引

| 路径 | 内容 |
|---|---|
| `suite-design.md` | 总体架构、schema、指标、评委、消融、构造与质控、切分版本（v0.1 设计，v0.2 实现见本文与 `build/`、`runners/`） |
| `public-benchmarks-survey.md` / `gap-analysis.md` | 公开评测集的方法调研与缺口分析 |
| `datasets/<子集>/card.md` | 数据卡 |
| `datasets/<子集>/examples.yaml` | 手写种子（v0.1） |
| `datasets/<子集>/migrated.yaml` | 草稿迁移（pr、ca） |
| `datasets/<子集>/generated.yaml` | 构造脚本产物（**请勿手改**，改规格后重新生成） |
| `build/` | `mcb.py` 公共骨架与切分；`pools.py` 背景与填充池；`build_<子集>.py` 构造脚本；`migrate_drafts.py` |
| `tools/validate.py` | 校验（V01–V31、D01–D06、P02–P05）与 AML 形状导出（默认不含 held-out） |
| `runners/` | `mc_run.py` 统一 runner；`systems.py` 对照组适配；`judges.py` 固定提示词；`mc_common.py`；`mc_report.py` 汇总 |
| `results/` | 评测报告（逐题轨迹在 `data/logs/memcompass/<run_id>/`，gitignored） |

## 5. 怎么用

```bash
# 1) 重新生成与校验（纯本地，不调用 LLM）
.venv/Scripts/python.exe docs/research/benchmark-suite/build/build_pr.py        # 其他子集同理
.venv/Scripts/python.exe docs/research/benchmark-suite/tools/validate.py --quiet

# 2) 跑一个被测版本（--am-root 指向要测的 agent_memory 代码根目录）
.venv/Scripts/python.exe docs/research/benchmark-suite/runners/mc_run.py \
    --subsets pr,ca,at,fg,mp,xa,ts,pf --splits dev,test \
    --systems am,no_memory,naive_rag,full_context,oracle,naive_rag_threshold,am_retrieve_always \
    --am-root <代码根目录> --am-label am_base --env-file <含 API key 的 .env 路径> --jobs 6
# 中断后加 --resume 并沿用同一个 --run-id 续跑：跳过已成功的任务，出错的重跑（汇总时同一任务只计一行）。
# 每个进程约占 3.5 GB 内存（bge-m3），16 GB 机器上同时最多跑 2 个。

# 3) 汇总（可合并多次运行，--ref 指定配对比较的参照系统）
.venv/Scripts/python.exe docs/research/benchmark-suite/runners/mc_report.py \
    data/logs/memcompass/<run_a> data/logs/memcompass/<run_b> --ref am_base --out <报告.md>

# 4) 消融（A 轨）：关闭某个机制重跑，看对应能力掉多少
.venv/Scripts/python.exe docs/research/benchmark-suite/runners/mc_run.py --subsets pr --splits test \
    --systems am --am-root <代码根目录> --am-label am_v2 --am-disable surface

# 5) 评委一致性（代理）：第二评委对已完成运行分层抽样重判，报告一致率与 Cohen's κ
.venv/Scripts/python.exe docs/research/benchmark-suite/runners/judge_agreement.py \
    data/logs/memcompass/<run_a> data/logs/memcompass/<run_b> --per-kind 40
```

模型配置（`mc_common.DEFAULTS`）：答题器与被测系统内部 LLM 为 DeepSeek 官方的 `deepseek-flash`（agent-memory 的生产默认）；评委为 **异源** 的 `qwen3.8-max`（阿里云 token-plan 端点），可选第二评委 `glm-5.2`。两个端点分摊额度。密钥只从 `--env-file` 读取，不打印。任务默认按子集轮流排队（`--order interleave`），额度中断时每个子集都有进度。

人工核验：`tools/review/build_review.py` 把全部用例注入 `tools/review/template.html`，生成单文件核验台（左栏筛选，右栏看历史、探针与金标，逐条标"通过 / 有问题 / 拿不准"并写备注）；`--flags` 可附上评测运行得出的自动体检提示。

## 6. 客观性与公正性的做法（对应公开评测集的通行方法）

| 做法 | 参照 |
|---|---|
| 能用程序算出金标的，不靠人写（时间线、操作序列重放器；唯一硬标记；代码生成槽位） | LoCoMo 事件图、BEAM 时间线、LongMemEval 属性本体 |
| 能用字符串确定判定的，不交给评委（遗忘泄漏、投毒特征串、先斩后奏的工具顺序、保留片段） | 本套件 |
| 评委与答题器异源、温度 0、结构化 JSON；提示词 sha256 写进每次运行的 meta | AML 固定 Answer/Eval、LongMemEval |
| 强基线必跑：no-memory、full-context、naive-rag（+阈值版），oracle 作上限 | LongMemEval_oracle、框架 §7.2 |
| 正负最小对比对、对照组（已完成计划、良性更新）、负例占比 25–45% | LoCoMo 对抗类、LongMemEval 弃答题 |
| 被测系统的"接入规范"取自它自己版本的 SKILL.md，同一套规则提取，不为任何一方手写提示词 | 本套件（公平性） |
| 背景记忆与填充会话，避免"每轮都注入"天然精确 | LongMemEval haystack |
| dev / test / held-out = 20 / 50 / 30，按 group 整体分配；只在 dev 上调参，报告 test，held-out 留作里程碑 | AML 公开 + 私有、PersonaMem-v2 |
| 配对比较：McNemar 精确检验 + 配对 bootstrap；比例给 Wilson 95% 区间；n < 20 标注"只看方向" | `evals/runners/metrics.py` |

## 7. 仍需用户处理的事（诚实清单）

1. **人工核验**：用户对全部用例整体签核、并声明后续新增默认通过（记录见 `datasets/verification.yaml`；核验台没有记录逐条结论，validate.py 按该文件报告覆盖率）。2026-09-16 的 oracle 用例体检已完成：查出并修正三类金标缺陷（fg 相对日期、mp 攻击题问答金标、三处证据漏标），提示由 101 条降至 39 条，其余为 v0.2 遗留的偏易用例（pf 17、ts 9）与被测系统确实答不好的难题（已逐条核对，保留）。核验台里的自动体检提示仍是 t2 那一版，需要时用 `tools/review/build_review.py --flags` 重建。
2. **第二位人类标注者**与评委—人工一致率研究尚未开展；目前只能报告"评委 A 与评委 B"的一致率作为代理指标。
3. 迁入 `evals/`（D6）：**2026-09-16 已执行**（`tools/migrate_to_evals.py --apply`，39 个文件逐字节复制到 `evals/memcompass/`，含 `build/mcb.py`、`build/pools.py`、`runners/naive_rag.py` 三个运行时依赖，副本可独立运行）。迁入条件当时均已满足：三类金标缺陷已修并重跑、评委一致率 90%–98%（κ 0.80–0.93）、每个子集都能区分"有记忆 / 无记忆"。此后**冻结副本由用户维护**：在本目录改规格 → 重新生成 → 重新核验 → 再迁入。
4. AML：邮件按用户要求暂时搁置；在得到答复前不部署公网服务、不申请 Key。
5. 只有中文；英文平行版尚未构建。
6. **最要紧的缺口：S/M 档长历史（35 万 token）没有构建**。现在每条用例只有 2–8 个会话，原文整体可检索，朴素 RAG 因此在多数能力上追平甚至超过被测记忆系统（见 `results/2026-09-16-v03-report.md` §1.3c）；不补上长历史档位，K1、K3、K5、K7、K9 就无法按框架 §6.2 判到 L2。
   **2026-09-16 评估（未动工，等用户拍板）**：这是下一轮最该做的事，但不是一个下午能做完的——(a) 填充池要用与答题器、评委都不同源的模型生成（按 §9.2 的修订模型规则），S 档每题约 50 个会话，即使按子集共用填充池、只加每题的近似干扰，也要新生成数百到上千个会话；(b) 被测系统对每个会话都要蒸馏一次，S 档的写入调用量约为现在的 10 倍，粗估 base + v2 + 对照组一轮 ¥60–100（现在一轮约 ¥25，DeepSeek 余额约 ¥34），M 档再乘 6；(c) 本机 16 GB 内存只能单进程跑、后台任务约 10 分钟就被内存守护杀掉，S 档全套预计要连续跑 2–4 天。建议顺序：先只建 S 档、只跑 at / ca / pf 三个 Q 轨子集（K4/K5/K3 正是朴素 RAG 追平的地方），有区分度再扩到全套与 M 档。
   **2026-09-18 补充**：用 AML 的 LongMemEval-S（每题约 12 万 token 的真实长历史）做了一次契约自测（`results/2026-09-18-aml-selftest-report.md`），18 题上记忆系统与朴素 RAG 分不出高下、证据召回都是 18/18——长历史本身并没有拉开差距，K4 档位的价值需要重新评估：与其自建 35 万 token 填充池，不如直接用 LongMemEval-S / AML 作为长历史外部对照。
