# 给 AML 的咨询邮件草稿（由用户本人发送）

> 收件人：contactus@agentmemoryleaderboard.ai（官网公布的联系邮箱）
> 说明：本草稿**只由用户本人审阅、修改和发送**，agent 不代发。方括号 `[ ]` 中的内容请替换或删除。
> 目的：落实 `aml-contribution.md` §1.2 中影响路线选择的待核问题（Q1–Q6、Q8、Q9）。
> 建议：先发中文版；如果需要，可以附上英文版。邮件中**不要附带任何数据文件**，AML 明确要求数据只能通过官网表单提交。

---

## 中文版

**主题**：关于向 AML 提交补充评测集（Benchmark Contribution）的几个问题

您好，

我是 [姓名]，[个人开发者 / 所在组织]。我们正在设计一套面向 agent 长期记忆的补充评测集（暂名 MemCompass），打算在 AML 第二期前后，通过官网的 Benchmark Contribution 表单提交提案。它主要补充现有公开评测集较少覆盖的维度：

- "截至某时"的时态问答与双时态（事件发生时间和记录时间）；
- 可验证的遗忘请求（泄漏与误删）；
- 写入阶段的记忆投毒防御；
- 枚举型细节保留；
- 以及一个需要新答题契约的"主动联想"子集（用户没有提问时，系统能否在恰当的时候想起相关记忆，同时在不相关时保持沉默）。

在准备提案包之前，想请教以下问题：

1. **API Key**：提案表单要求填写 Leaderboard Platform API Key。如果只贡献评测集，不以参评系统身份参赛，能否获得这个 Key？如果必须先参评，走"学术 · 代码"路线（平台部署、不签发 Eval Key）的团队应该怎样提案？
2. **文件格式**：`benchmark_file` 的大小上限是多少？题目行是否需要与现有契约一致（例如 question、answer、question_type、会话与时间戳字段）？是否有推荐的 schema？
3. **语言**：是否接受中文数据集？是否要求提供英文平行版本？
4. **新契约与新赛道**："主动联想"子集需要一个新的答题和评分提示（Add/Search 接口不变）。平台是否接受附带新契约的提案？另外，是否考虑设立"行为轨"（在 agent 实际执行任务的过程中评估记忆）这类新赛道？
5. **审核周期**：提案审核大约需要多长时间？如果希望纳入第二期或后续的 League，最晚什么时候提交？
6. **私有数据**：如果选择"仅作为私有评测数据"，贡献方自己能否继续使用、发布同一批数据的公开部分（dev / test）？held-out 部分的保密责任怎样划分？
7. **证据要求**：是否要求贡献方提供基线结果或区分度分析？对最低题量有没有要求？
8. **社区贡献计划**：参赛说明中提到的"提交 3 组有挑战性的测试样本"，格式和提交渠道是什么？是否也通过同一张表单提交？

感谢！期待您的回复。

[姓名]
[联系方式（可选）]

---

## English version

**Subject**: Questions about submitting a complementary benchmark to AML

Hello,

I'm [Name], [independent developer / organization]. We are designing a complementary benchmark for long-term agent memory (working name: MemCompass) and plan to submit a proposal through the Benchmark Contribution form around AML's second cycle. It targets dimensions that existing public benchmarks cover less:

- as-of temporal questions with bitemporal records (event time vs. record time);
- verifiable forget requests (leakage and over-deletion);
- write-time memory-poisoning defense;
- detail retention in enumerated instructions;
- a "proactive recall" subset that needs a new answer contract (whether a system surfaces relevant memories at the right moment without being asked, and stays silent when they are irrelevant).

Before preparing the package, we'd like to ask:

1. **API key**: The proposal form requires a Leaderboard Platform API Key. Can a benchmark contributor that does not submit a memory system obtain one? If participation is required, how should teams on the "academic, code-based" route (platform-deployed, no Eval Key issued) submit proposals?
2. **File format**: What is the size limit for `benchmark_file`? Should question rows follow the existing contracts (question, answer, question_type, session and timestamp fields)? Is there a recommended schema?
3. **Language**: Are Chinese-language datasets accepted? Is an English parallel version required?
4. **New contracts and tracks**: The proactive-recall subset needs a new answer and judging prompt (the Add/Search interface is unchanged). Would proposals that include a new contract be considered? Is a behavior track (evaluating memory while an agent performs tasks) something AML might add?
5. **Review timeline**: How long does proposal review take, and what is the latest date to be considered for cycle 2 or a future League?
6. **Private data**: If we choose "private evaluation data only", may we still use and publish the public portions (dev/test) ourselves? How is confidentiality of the held-out split handled?
7. **Evidence**: Do you expect baseline results or a discriminability analysis, and is there a minimum size?
8. **Community contribution program**: What are the format and submission channel for the "3 challenging test samples" program? Is it the same form?

Thank you, and we look forward to your reply.

[Name]
[Contact (optional)]
