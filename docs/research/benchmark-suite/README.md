# MemCompass：agent 记忆能力画像评测套件

> 版本 0.2.0-draft（2026-09-14）｜状态：**数据、校验、runner 均已可运行；尚无人工核验**｜全部示例为合成数据，待用户审核
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

## 3. 套件一览（v0.2）

| 轨道 | 子集 | 能力 | 用例 / 探针 | 类型分布 | 切分 dev/test/held-out |
|---|---|---|---|---|---|
| B+S | `mc-proactive-recall` | K8、K7 | 60 / 80 | 应浮现 20（类比 5）、间接线索 10、不应浮现 20、时间陷阱 10；多轮 32% | 11 / 32 / 17 |
| B+Q | `mc-completeness-alignment` | K9、K10、K3 | 56 / 12 | 7 类各 8 条；12 道细节保留问答 | 11 / 29 / 16 |
| Q | `mc-asof-temporal` | K5 | 45 / 130 | as-of 12、追溯更正 10、事件 vs 记录 8、计划 8（含已完成对照 3）、有效期 7 | 10 / 23 / 12 |
| Q+S | `mc-forget-request` | K12 | 30 / 70 | 基本 10、间接 8、按作用域 6、忘后重告 6 | 7 / 15 / 8 |
| Q+B | `mc-memory-poisoning` | K12 | 42 / 42 | 嵌入指令 10、低可信 8、延迟触发 5、良性对照 19 | 9 / 23 / 10 |
| B+S | `mc-task-state` | K2 | 30 / 30 | 更新 7、打断 6、切换 5、跨会话 7、并行 5 | 7 / 15 / 8 |
| B | `mc-present-fidelity` | K1 | 30 / 30 | 逐字细节 9、约束存活 9、工具输出 6、改动文件 3、不可答 3 | 7 / 15 / 8 |
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
    --am-root <代码根目录> --am-label am_base --env-file D:/4_Projects/.env --jobs 6
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

1. **人工核验**：2026-09-14 用户对全部 317 条整体签核（记录见 `datasets/verification.yaml`；核验台没有记录逐条结论，validate.py 按该文件报告覆盖率）。正式运行里两版都没通过的 40 条用例已在核验台标出自动体检提示，等 oracle 体检跑完后复核。
2. **第二位人类标注者**与评委—人工一致率研究尚未开展；目前只能报告"评委 A 与评委 B"的一致率作为代理指标。
3. 迁入 `evals/`（D6）：用户已授权"评测集没问题、能真实准确地反映能力后迁入"。`tools/migrate_to_evals.py` 已就绪（逐字节复制到 `evals/memcompass/`，默认 dry run）。暂缓执行的原因：oracle 体检与评委一致性还没做（评委额度耗尽，2026-09-21 重置）；ts 串线判定有测量缺陷；pf 与 ca 问答没有区分度。
4. AML：邮件按用户要求暂时搁置；在得到答复前不部署公网服务、不申请 Key。
5. v0.2 只有中文；英文平行版与 S/M 档长历史（35 万 token）尚未构建。
