# MemCompass 评测结果

本目录只放汇总报告；逐题轨迹（results.jsonl）与运行元数据（meta.json：代码版本、模型、提示词 sha256、任务数、错误数）在 `data/logs/memcompass/<run_id>/`（gitignored，本地保留）。

## 报告口径（事先固定，结果出来后不改）

1. **对比对象**：基线 `am_base` = agent-memory HEAD `bb1c19f`（git worktree）；优化版 `am_v2` = 工作区（v0.2 代码，运行时冻结）。其余对照组 no_memory / full_context / oracle / naive_rag（+阈值版）/ am_base_ra（基线的"每轮检索都注入"）只在基线运行中跑一次，因为它们不依赖 agent-memory 的版本。
2. **切分**：dev 用于调试与调参，**test 是主报告切分**；held-out 不参与本轮任何运行，留作里程碑评测。
   **2026-09-14 变更**：第一轮 dev+test 运行（答题器 `deepseek-flash`，官方 API）在跑到约 3% 时因 DeepSeek 账户余额耗尽（HTTP 402）中断，已作废（`full-base`、`full-v2` 等目录只作冒烟记录）；正式运行改为**只跑 test 切分**、答题器与被测系统内部 LLM 统一换成 token-plan 端点上的 `deepseek-v4-flash-0731`，基线与优化版用同一模型从头重跑（`t-base`、`t-v2`、`t-abl-*`）。为控制成本，对照组去掉了 oracle 与 am_base_ra。
   **2026-09-14 第二次中断**：14:32 左右 token-plan 的周额度耗尽（`insufficient_quota`，2026-09-21 00:43 UTC 重置）。评委 qwen3.8-max、第二评委 glm-5.2 与答题器都在这个额度上，`t-base`、`t-v2` 此后的任务全部报错，已停止；消融未跑。只有 mc-proactive-recall 的部分 test 用例在额度耗尽前完成，据此写了**中期报告**（只含 pr）。有效行的筛选规则写在中期报告 §2；原始 results.jsonl 保留不改。
   **补跑方案**（额度重置或用户充值后）：用新的 run id（`t2-base`、`t2-v2`、`t2-abl-*`）从头跑同样的命令——成功过的 LLM 调用都在磁盘缓存里，会原样重放、不耗额度；失败的调用没有缓存，会重新请求。同时最多 2 个进程（每个约 3.5 GB 内存）。命令见 `../README.md` §5，完成后用 `runners/make_report.py` 生成正式报告。
   **2026-09-14 第三次变更（正式运行）**：用户确认 token-plan 已恢复、DeepSeek 官方可用。正式运行恢复原协议：答题器与被测系统内部 LLM 为 DeepSeek 官方 `deepseek-flash`，评委 qwen3.8-max 走 token-plan（两端分摊额度）；`t2-base`、`t2-v2` 先只跑两版 agent-memory（最关键的前后对比先跑完），对照组（no_memory、full_context、naive_rag、阈值版、oracle、am_base_ra）单独一次运行 `t2-ctrl`，再跑消融。中期报告的 `t-*` 运行用的是另一个答题器模型，只作为中期记录，不与正式结果合并。
3. **统计**：比例给 Wilson 95% 区间；与 `am_base` 的配对比较用 McNemar 精确检验 + 配对 bootstrap 95% 区间；n < 20 标注"只看方向"。每个设置 1 个种子（成本所限），种子方差未估计。
4. **评委**：qwen3.8-max（与答题器 DeepSeek 异源），温度 0；评委之间的一致性用 glm-5.2 抽样重判（`runners/judge_agreement.py`），不替代人工一致性研究。
5. **消融**（A 轨）：在 test 切分上关闭单个机制重跑（`mc_run.py --am-disable <能力>`），看对应能力的主指标变化。
6. **诚实披露**：冒烟阶段修正了若干评测执行器的缺陷（死锁、能力探测、会话 id、作用域口径、评委对"回溯原文"和"软泄漏"的判定口径），修正对两个版本同样生效；优化版有一处渲染改动（追溯更正提示）是看到一条 test 用例失败后加的，属于轻微的 test 信息泄漏，已在报告中标注。

## 文件

| 文件 | 内容 |
|---|---|
| `2026-09-14-v02-report.md` | **正式报告**：8 个子集的两版配对比较、能力画像、两项消融、六个案例、局限；对照组、oracle 体检与评委一致性待评委额度恢复后补 |
| `2026-09-18-aml-selftest-report.md` | **AML 契约自测**：按 Agent Memory Leaderboard 的 Add/Search 契约在 LongMemEval-S（公开题分层抽样 18 题、每题约 48 个会话）上测 v0.2.2 对朴素 RAG，两评委；结论：问答正确率分不出高下（14/18 对 12/18 与 13/18 对 15/18），证据召回两者 18/18；含参赛判断与运行事故 |
| `2026-09-14-v02-interim-report.md` | 中期报告（已被正式报告取代）：只含 pr 子集的部分用例，答题器模型不同，数字不与正式结果合并 |
