# AML 补充评测集贡献机制：调研结论与提案包

> 版本 v0.1（2026-09-13）｜**只做了调研**：没有开 issue 或 PR，没有提交表单，没有发邮件，也没有调用 AML 的任何写接口。读取 AML 接口时只用了公开的 GET 请求（openapi.json、GitHub 公开 API）。
> 可信度：〔官方〕读到了官方仓库、官网 HTML、前端代码 `static/app.js?v=full-contract-20260826a`、`/api/openapi.json`；〔推断〕；〔待核〕。

## 0. 决定与复核（2026-09-13）

- **用户决定**：先咨询、再决定。咨询邮件由用户本人发出（草稿 `aml-inquiry-email-draft.md` 仅本地保留，不随仓库公开）；答复之前不部署公网 Add/Search，也不申请评测 Key。LoCoMo-Refined 不引用。
- **主 agent 复核**：以下三点在官网上逐一核实，均属实。
  - `/api/openapi.json` 中有 `POST /benchmark-proposals`（Submit Benchmark Proposal），但没有公开的请求 schema；
  - 提案页 `/benchmark-contributions` 的 HTML 中有 `<input name="ldbd_key" ... required />`，并且有 `benchmark_file` 字段；
  - `app.js` 中有原文"accepts both public and private"。

## 1. 结论速览

### 1.1 已核实

| # | 结论 | 证据 |
|---|---|---|
| 1 | **AML 接受第三方补充评测集**，公开和私有两种都接受 | README"propose a complementary benchmark with meaningful difficulty and clear provenance"；官网"Benchmark Contribution"页："The platform accepts both public and private benchmark contributions"〔官方〕 |
| 2 | **正式渠道是官网表单**，不是 GitHub PR：页面路径 `/benchmark-contributions`（`/provide-benchmarks` 也可以到达），表单以 multipart 方式 POST 到后端 `POST /benchmark-proposals`（openapi.json 中标为"Submit Benchmark Proposal"） | app.js 中的 `VIEW_ROUTES`、`submitBenchmarkProposal()`；openapi.json〔官方〕 |
| 3 | **GitHub 只用于文档和契约澄清**，而且明令禁止在 issue 或 PR 中提交评测语料、保留题、金标答案、rubric、私有标注 | README"Repository scope""Contributing"〔官方〕 |
| 4 | 表单必填项中有 **Leaderboard Platform API Key**（`ldbd_key`），提案必须关联到"已验证的 Leaderboard 参评身份" | 表单中的 `ldbd_key`（required）；文案"link the proposal to a verified Leaderboard participant"〔官方〕 |
| 5 | 上传文件**必须**是非空的 JSON 或 JSONL（`benchmark_file`，accept 为 .json/.jsonl）；"内部 schema 由人工审核"；完整的包建议包含 README、schema、任务格式、金标政策和一个小的验证切分 | 表单文案〔官方〕 |
| 6 | 许可证只能从 5 项中选：CC BY 4.0 / CC BY-SA 4.0 / Apache-2.0 / 自定义研究许可 / 其他（待审核） | 表单 `license` 下拉框〔官方〕 |
| 7 | 发布权限二选一：允许公开发布（署名）/ 仅作私有评测数据 | 表单 `publication_permission`〔官方〕 |
| 8 | 审核标准有三项：科学价值（覆盖度与区分能力）、数据治理（权利、隐私与授权）、可复现性（schema、标注与协议）。审核流程分三步：范围审核 → 数据审核 → 纳入决策 | 页面文案〔官方〕 |
| 9 | 审核通过的题目"**可能**被纳入未来的评测 League"，具体安排另行公布 | 页面文案〔官方〕 |
| 10 | 任何影响评分的变化都要作为**新的评测契约版本**，不能静默修改已有结果；需要说明改动影响评分还是只影响文档；保留上游署名和许可 | README Contributing 第 2–4 条〔官方〕 |
| 11 | 另有一条轻量路径："**社区贡献计划**"：提交 3 组有挑战性的测试样本并通过审核，即可入选（奖励至少 50 元的 Kimi Token 额度） | 赛事页、参赛说明〔官方〕 |
| 12 | 第二期预计 **2026-09-20** 开放；平台持续接收新系统、新版本和补充基准提案 | README、官网横幅〔官方〕 |
| 13 | 公开仓库目前**没有**任何 benchmark 提案类的 issue 或 PR（共 19 个 issue 或 PR：修 bug、问协议、问归一化公式）；#15、#18、#19 截至 2026-09-13 没有维护者回复 | GitHub API〔官方〕 |

### 1.2 待核

| # | 问题 | 为什么重要 |
|---|---|---|
| Q1 | 没有参评身份的纯贡献者能否拿到 `ldbd_key`？"学术·代码"路线（平台部署）**不签发** Eval Key，那么走这条路线的团队怎样提案？ | 决定我们是否必须先把 agent-memory 作为参评系统登记 |
| Q2 | 上传文件的大小上限、内部 schema 要求、是否需要 AML 兼容的题目行字段（question、gold_answer 等） | 决定提案包的格式 |
| Q3 | 是否接受中文数据集？是否要求英文平行版本？ | 本套件 v0.1 只有中文。AML 规定"query 保持原语言"，由此推断多语言可行〔推断〕 |
| Q4 | 是否接受需要**新答题 / 评分契约**的子集（例如 proactive-v0）？是否接受"Other / proposed track"（行为轨）？ | 决定主动联想能否入包 |
| Q5 | 审核周期多长；能否赶上第二期；"League"何时启动 | 决定时间规划 |
| Q6 | 选"私有评测数据"之后，贡献方自己能否继续使用和发布同一批数据？held-out 的保密责任怎么划分？ | 关系到 held-out 与本地评测的双重用途 |
| Q7 | 署名方式（数据集引用、共同作者）；维护责任（勘误、版本更新） | Datasheet 中"维护"一节 |
| Q8 | 是否要求提交方给出基线结果或区分度证据，最低规模多少 | 决定发起提案前要跑多少实验（有 API 成本） |
| Q9 | 社区计划中"3 组测试样本"的格式和提交渠道（是否也走这张表单） | 可以作为先行的低成本试探 |
| Q10 | 第二期通知的具体内容（官方链接指向微信公众号文章 https://mp.weixin.qq.com/s/SS4itah0sa4R2vcDQ4uevQ ，本机访问触发了"环境异常"验证，没有读到） | 时间节点、规则变化 |
| Q11 | 公榜 0–100 分的归一化公式（issue #18 有人问过，无人回复） | 提案里的区分度分析要与平台口径一致 |
| Q12 | 生成数据所用 LLM 的输出条款，与 CC BY 4.0 是否兼容 | 数据治理审核 |

## 2. 来源与读取情况

| 来源 | URL | 读取方式 | 结果 |
|---|---|---|---|
| README / README_CN | https://github.com/AML-memory/agent-memory-leaderboard | raw（代理） | 已读 |
| 契约文件 | `data/{beam,clbench,locomo-refined,longmemeval-s,personamem,scriptmem}/pipeline*.py` | raw | 已读（摘要见 `public-benchmarks-survey.md` §2.2） |
| `api_config.py`、`requirements.txt` | 同上 | raw | 已读：只有 ANSWER_* 与 JUDGE_* 环境变量；依赖只有 httpx |
| 官网 /docs、/rules、/api-guide、/evaluation | https://agentmemoryleaderboard.ai/docs 等 | HTML（单页应用，各路由内容相同） | 已读：接入规范、模式、配额、公榜准入、贡献表单文案 |
| 前端代码 | https://agentmemoryleaderboard.ai/static/app.js?v=full-contract-20260826a | 下载后本地检索 | 已读：能力叶子、数据集列、表单提交逻辑 |
| OpenAPI | https://agentmemoryleaderboard.ai/api/openapi.json | GET | 已读：`POST /benchmark-proposals`，没有公开请求 schema |
| 赛事页 | https://agentmemoryleaderboard.ai/competition/ | HTML | 已读 |
| 第二期通知 | https://mp.weixin.qq.com/s/SS4itah0sa4R2vcDQ4uevQ | HTML | **没有读到**（触发验证页） |
| GitHub issues / PR / commits | GitHub API | GET | 已读标题与部分正文 |

## 3. 贡献渠道对照

| 渠道 | 适用内容 | 是否适合我们的提案 | 依据 |
|---|---|---|---|
| **官网 Benchmark Contribution 表单** | 补充基准提案（数据文件 + 说明 + 治理条款） | **是，主渠道** | §1.1 第 2 条 |
| 社区贡献计划"3 组测试样本" | 少量有挑战性的样本 | 可以作为先行试探（例如 at、fg、mp 各 1 组），格式〔待核〕 | §1.1 第 11 条 |
| GitHub issue | 澄清已公开的契约、指出文档问题 | 只适合提出契约问题，例如"是否接受 proactive-v0""归一化公式"；**不能**附带数据 | README |
| GitHub PR | 改进文档、修复 pipeline | 不适合提案；将来如果题目被纳入，可能需要 PR 增加一个契约目录（例如 `data/memcompass/pipeline.py`）〔推断〕 | README |
| 邮件 contactus@agentmemoryleaderboard.ai | 报名与评测问题 | 可以用来咨询 §1.2 中的待核问题（**本轮没有联系**） | 官网 |

## 4. 表单字段与我们的提案包

| 表单字段 | 必填 | 我们准备什么 | 对应文件（发布包） |
|---|---|---|---|
| `ldbd_key` | 是 | 需要先取得参评身份（§6 缺口 1） | — |
| `contact_name` / `contact_email` / `organization` | 是 | 由用户填写（可以用个人名义或组织名义，**需要用户决定**） | — |
| `benchmark_name` | 是 | "MemCompass-QA：as-of、遗忘、投毒与细节保留"（暂定） | MANIFEST.yaml |
| `track` | 是 | Textual memory；另在说明里建议一个"Other / proposed track：行为轨" | — |
| `scale` | 否 | 例如"约 450 条用例、约 1,000 题；S 与 M 两档历史" | MANIFEST.yaml |
| `objective` | 是 | 研究问题：公开集只覆盖"取最新"和"被问到时的检索"，本基准测截至某时与双时态、可验证的遗忘（泄漏 + 误删）、写入期投毒防御、枚举细节保留 | README.md、`gap-analysis.md` 精简版 |
| `dataset_context` | 是 | 合成数据；模板 + 程序化金标 + LLM 口语化 + 100% 人工核验；标注一致性；泄漏检查；canary | 各子集 card.md |
| `dataset_content` | 否 | schema 片段 + 2 条样本 | `suite-design.md` §4 精简版 |
| `benchmark_file` | 是 | 验证切分（dev）的 JSONL：AML 形状的题目行 + Add 请求体（可以直接由 `tools/validate.py --export-aml` 生成） | `subsets/*/aml/*.jsonl`（只放 dev） |
| `license` | 是 | **CC BY 4.0**（建议） | LICENSE-DATA |
| `source_url` | 否 | 公开仓库或项目页（如果用户决定公开） | — |
| `publication_permission` | 是 | 建议：dev 与 test 允许公开，held-out 作为私有评测数据（表单只能二选一，需要问清楚能否分开处理，见 Q6） | — |
| `rights_confirmation` | 是 | 确认有权提交、个人信息已处理、来源与许可已如实标注 | 数据卡 §11 |

**提案包目录（建议）**：

```
memcompass-aml-proposal/
├── README.md                  # 研究问题、协议、与 AML 能力体系的映射（含拟议叶子 N4、N5）
├── datacards/                 # at、fg、mp、ca-detail 四张数据卡
├── schema.json                # AML 形状的题目行 + Add 请求体 + 扩展标签
├── validation_split.jsonl     # 上传文件：dev 切分
├── contract/                  # 复用 aml-lme-aligned；遗忘与投毒的确定性检测说明；（可选）proactive-v0 提示词
├── baselines/results.md       # no-memory、full-context、naive-rag、agent-memory，外加 1–2 个开源系统，含 95% 区间
├── judge_alignment/report.md  # 评委与人工的一致率、κ
├── LICENSE-DATA               # CC BY 4.0
└── MAINTENANCE.md             # 维护人、勘误流程、版本策略
```

## 5. 数据、契约与 API 兼容要求

| 要求 | AML 规定 | 我们的做法 |
|---|---|---|
| 接口 | 参评系统只提供 Add 与 Search；平台控制答题和评分 | Q 轨子集只用 Add 与 Search；导出器已经实现（`tools/validate.py`） |
| 消息角色 | 只有 user / assistant | V07 强制检查；第三方内容写进 content，来源标注只在本地保留 |
| 时间 | timestamp 可选，Unix 毫秒 | 由会话日期和序号推出 |
| 分段 | 超过 20 条消息或 2,000 词时自动分段 | 构造时控制在阈值以内（V08 警告） |
| 检索 | query 用原文；正式 top_k=100 | 题面自带"今天是…"（V14）；提供 M 档历史，保证 top_k=100 下仍有区分度 |
| 隔离 | 只按 user_id 隔离 | 一条用例一个 user_id；遗忘与投毒都在同一 user_id 内测 |
| 答题模板 | 平台固定（LongMemEval 对齐版：优先最新、不轻易拒答） | as-of 的 record 轴和遗忘题会受这两条规则影响，要在提案中说明（这恰好是基准要揭示的现象） |
| 评分 | 按题型固定契约 | 默认复用"二值 + 严格时间粒度"与 BEAM 要点分；遗忘与投毒额外建议平台对 Search 返回做字符串检测（需要平台支持，属于**新契约**） |
| 版本化 | 评分变更 = 新契约版本；公榜比较需要 pipeline code hash、dataset bundle hash、题量一致 | `contract.yaml` 记录全部 hash 与题量 |
| 许可 | 从表单的 5 项中选；保留上游署名 | CC BY 4.0；不包含任何 NC 或 SA 的上游内容 |
| 隐私 | 不含未披露的个人信息与密钥 | 全合成；V23 扫描；保留域名 |

## 6. 提案包还缺什么

| # | 缺口 | 需要做什么 | 成本或依赖 |
|---|---|---|---|
| 1 | **Leaderboard API Key** | 先完成评测权限申请（需要 agent-memory 提供公网可达的 Add/Search，或者提交公开 GitHub 仓库由平台部署。后者不签发 Key，见 Q1）；AML 还要求参评系统在 Add/Search 中使用 gpt-4o-mini | 需要用户决定是否让 agent-memory 参评；涉及付费 API 与公网部署 |
| 2 | **规模** | 从 35 条种子扩到约 450 条用例、1,000 题（4 个子集）；AML 现有文本套件约 5,000 题 | LLM 生成费用 + 人工核验工时 |
| 3 | **区分度证据** | 至少 4–5 个系统的基线结果，显示分数分得开、不饱和、与现有数据集相关性不高（证明互补） | 需要 API 预算；本轮禁止调用 |
| 4 | **标注质量** | 第二位人类标注者；报告一致率与 κ；评委与人工的对齐研究（≥100 条每子集） | 需要找人 |
| 5 | **难度与 top_k=100** | 做出 M 档（约 35 万 token）的填充历史，并证明 naive-rag 在 top_k=100 下不会饱和 | 生成填充池 |
| 6 | 英文版本（如果需要，见 Q3） | 本地化，而不是直译 | 翻译与复核 |
| 7 | 生成来源文档 | 记录生成模型、提示词、种子；核对模型输出条款（Q12） | — |
| 8 | 维护承诺 | MAINTENANCE.md：维护人、勘误周期 | 用户决定 |
| 9 | 名称查重 | 确认"MemCompass"没有与已有基准重名 | 检索 |
| 10 | 契约问题的书面确认 | Q2、Q4、Q6（通过邮件或 issue 询问，由用户决定是否以及何时发出） | — |

## 7. 版本化与时间节点

| 日期 | 事件 | 可信度 |
|---|---|---|
| 2026-07-29 | AML 发布，首期开放报名 | 〔官方〕 |
| 2026-07-31 | 仓库唯一一次提交："Publish public evaluation pipelines"（此后只有合并的修复 PR） | 〔官方〕 |
| 2026-08-07 | 首期提交截止 | 〔官方〕 |
| 2026-08-12 | 首期公榜发布 | 〔官方〕 |
| 2026-08-26 | 前端版本号 `full-contract-20260826a`（契约相关的最近一次前端更新） | 〔官方，推断其含义〕 |
| **2026-09-20** | 第二期**预计**开放 | 〔官方，"expected"〕 |
| 未公布 | 提案审核周期；League 启动时间 | 〔待核〕 |

**建议节奏**（供用户决策，不是承诺）：

| 阶段 | 时间（建议） | 内容 |
|---|---|---|
| 0 | 现在到 9/20 | 用户审核本设计；确定 §8 中需要拍板的问题；如有需要，由用户本人邮件咨询 Q1–Q4 |
| 1 | 9/20 之后 2–4 周 | at、fg、mp、ca-detail 扩到约 600 题（S 档）；在本地跑五个基线；做评委对齐 |
| 2 | 之后 3–4 周 | 做出 M 档；第二标注者复核；定稿 v0.9；可以先走社区计划的"3 组样本"试探 |
| 3 | 取得 ldbd_key 之后 | 由用户本人通过官网表单提交提案（dev 切分 + 文档；held-out 选私有） |

## 8. 本轮没有做的事（声明）

- 没有访问 AML 的任何写接口（`POST /benchmark-proposals`、`/evaluation-access-requests` 等），也没有注册账号。
- 没有在 GitHub 上开 issue、PR 或发表评论；没有发送邮件；没有联系任何人。
- 没有下载任何上游评测数据，只读取了 README、许可文件和数据卡的元数据。
