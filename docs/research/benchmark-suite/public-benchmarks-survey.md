# 公开记忆评测集的设计方法调研

> 版本 v0.1（2026-09-13）｜配套：`gap-analysis.md`（缺口）、`suite-design.md`（本套件如何借鉴）
> 可信度标注：〔官方〕读取了官方仓库、官网、HF 数据卡或官方前端代码；〔论文〕读取了 arXiv 摘要或 HTML 全文；〔推断〕作者推断；〔待核〕尚未核实。
> 读取方式：GitHub 内容通过代理 curl 读取 raw 文件与 API；arXiv 用 WebFetch 读摘要页或 HTML 页；AML 官网为单页应用，读取的是 HTML 与 `static/app.js?v=full-contract-20260826a`。本轮**没有**下载任何受限数据。

## 1. 总览

| 评测集 | 任务格式 | 规模 | 构造方式 | 质量控制 | 指标 / 评委 | 数据许可 | 可信度 |
|---|---|---|---|---|---|---|---|
| **AML**（平台） | Add 写入历史 → Search 取证 → 平台固定答题器 → 固定评分 | 文本轨 10 余个数据集、1,500+ 历史、约 5,000 题 | 汇集公开集并统一口径；另有私有题 | 版本化契约、公榜审核、复现核验 | 各数据集原生评分，归一到 0–100 | 仓库无 LICENSE；上游数据按各自许可 | 〔官方〕 |
| LongMemEval | 带时间戳的多会话聊天历史 + 1 个问题 | 500 题、7 类；S 约 115k token（约 40 会话），M 约 500 会话 | 164 个属性本体 → LLM 出题 → 人工筛改（约 5% 成品率）；自聊生成证据会话，约 70% 人工编辑；填充会话混入 | 人工核验证据；GPT-4o 评委与人工一致率 >97% | 问答正确率（LLM 评委）+ 会话级召回 | MIT | 〔论文〕〔官方〕 |
| LongMemEval-V2 | 历史轨迹 → 记忆系统产出证据 → 问答 | 451 题；最多 500 条轨迹、1.15 亿 token | 人工整理问题 | 〔待核〕 | 正确率；报告延迟 | CC BY 4.0 | 〔论文〕 |
| LoCoMo | 两人多会话对话；问答、事件摘要、多模态对话生成 | 10 段对话，每段约 300 轮、约 9k token，最多 35 个会话 | 人设 + 时间事件图 → LLM 智能体对话 → 人工核验编辑 | 人工核对与事件图的一致性 | 问答用 F1 等 | CC BY-NC 4.0 | 〔论文〕〔官方〕 |
| LoCoMo-Refined | 同 LoCoMo | 修订 337 条问答 | AI 筛查 + 5 名标注者审校 | 300 条人工对齐样本：评委一致率从 43.67% 升到 86.33% | 更严格的 LLM 评委（Qwen3-14B），严格时间粒度 | CC BY-NC 4.0 | 〔官方〕 |
| BEAM | 超长多领域对话 + 探针问题 | 100 段对话（128K/500K/1M/10M）、2,000 道已验证问题、10 种能力 | 种子 → 会话计划（含时间线）→ 角色扮演生成；GPT-4.1-mini 出题 → 人工验证，每段对话每种能力选 2 题 | 人工评对话质量（4.53/4.57/4.64，满分 5） | 按要点（nugget）打 0/0.5/1 分；事件排序用 Kendall τ-b | 代码 MIT；数据 CC BY-SA 4.0 | 〔论文〕〔官方〕 |
| PersonaMem (v1) | 人设多会话历史 + 情境化的选择题 | 180+ 人设、最多 60 个会话、7 种查询类型；32k/128k/1M 三档 | 基于 PersonaHub 生成人设与会话 | 难度元数据（到最近偏好的 token 距离等） | 选择题正确率 | MIT（HF 卡） | 〔论文〕〔官方〕 |
| PersonaMem-v2 | 隐式偏好的长历史 + 选择题和生成题 | 1,000 人设、26,100 条偏好、325 个话题；benchmark_text 5,000 题 | GPT-5 生成 + 多道质量过滤；官方声明"无法人工核验每条" | 按人设不重叠切分 benchmark / train / val | 选择题正确率 + 生成题 LLM 评委 | CC BY 4.0（HF 卡） | 〔官方〕 |
| MemoryAgentBench | 分块增量喂入（记忆构建阶段）→ 提问 | 2,071 题；4 种能力 | 改造已有长上下文数据集 + 新建 EventQA（全自动流程）与 FactConsolidation（MQuAKE 反事实编辑对，新事实排在后面） | 〔待核〕（附录讨论评委有效性） | 正确率、子串精确匹配、F1、Recall@5；部分题用 LLM 评委 | MIT（HF） | 〔论文〕〔官方〕 |
| MemBench | 事实记忆 / 反思记忆 × 参与 / 观察两种场景 | 0–10k 与 100k 两档 | 分类数据 + 加噪扩展长度（每单位约 1k token） | 〔待核〕 | 有效性、效率、容量 | MIT（README 徽章） | 〔论文〕〔官方〕 |
| MemoryArena | 相互依赖的多会话智能体任务 | 购物 150、团队出行 270、渐进搜索 256、数学 40、物理 20 | 人工编写并核验（形式推理由博士级专家整理） | 人工核验唯一解与依赖关系 | 成功率 SR、进度分 PS、软进度分 sPS、分深度成功率 SR@k | 〔待核〕 | 〔论文〕 |
| EvoMemBench | 记忆范围（单回合内 / 跨回合）× 内容（知识 / 执行）四种设定 | 复用 MAB、BFCL、CL-Bench、xbench、WebWalkerQA、ALFWorld | 改写已有集（例如把显式实体换成隐式指代） | 统一骨干模型 DeepSeek-V3.2 | 正确率、成功率、进度分、token 用量 | 〔待核〕 | 〔论文〕 |
| ProAgentBench | 真实屏幕活动流 → 预测何时介入、介入什么 | 28,000+ 事件、500+ 小时 | 志愿者一个月的真实数据；三道隐私处理（VLM 筛查 → 本人审阅 → 规则过滤） | 按时间切分，用户之间隔离 | 时机：正确率、精确率、召回率、F1（"精确率反映打扰成本"）；内容：意图正确率、语义相似度 | 仅限研究，禁止重识别、商用和监控 | 〔论文〕 |
| π-Bench / StreamMemBench | 隐性意图的多轮任务 / 流式观察 → 后续协助 | 100 个任务、5 个人设 / 〔待核〕 | 〔待核〕 | 〔待核〕 | StreamMemBench：证据召回、证据首次使用、反馈吸收、后续复用 | 〔待核〕/ CC BY 4.0 | 〔论文〕 |
| MINJA（攻击） | 只通过查询交互向 agent 记忆注入恶意记录 | 〔待核〕 | 桥接步骤 + 渐进缩短提示 | — | 注入与攻击成功率〔推断〕 | — | 〔论文〕 |
| CL-bench（AML 成员） | 系统提示 + 上下文（新知识）+ 任务 + rubric | 1,899 个任务，每个上下文平均 63.2 条 rubric；Life 版 405 个任务 | 领域专家标注，每个上下文约 20 小时 | 专家 | 严格二值（满足全部 rubric 才算解决）；GPT-5.1 评委 | 〔待核〕（GitHub 显示 NOASSERTION） | 〔官方〕 |

## 2. AML（Agent Memory Leaderboard）

### 2.1 能力分类〔官方：前端代码 `CAPABILITY` 与 `LEAF_CAPABILITY_DETAILS`〕

README 只列出 7 个一级能力；网站前端代码中还有 **24 个叶子能力**：

| 一级 | 叶子 |
|---|---|
| A 显式事实召回 | A1 单点事实、属性与来源回忆；A2 实体、角色、说话人、行动与归因；A3 结构化字段、集合、枚举抽取 |
| B 关系与多跳组合推理 | B1 跨片段、跨会话多证据聚合；B2 因果链、路径与中间步骤恢复；B3 证据集合与路径支持的有效性验证；B4 受控的开放域与常识补全 |
| C 时间与事件序列 | C1 日期、相对时间与间隔；C2 事件顺序与阶段排序；C3 事件与立场的轨迹变化 |
| D 记忆治理 | D1 新值覆盖与当前状态；D2 矛盾检测与冲突消解；D3 删除、遗忘与抑制；F1 摘要与压缩、长历史全局综合（挂在 D 下） |
| E 个性化与关怀 | E1 偏好回忆与个性化遵循；E2 刻板与反刻板偏好的鲁棒性；E3 个人背景、健康与心理关怀 |
| G 上下文学习、规则与流程执行 | G1 领域知识推理；G2 规则系统应用；G3 程序与工作流执行；G4 经验数据发现与模拟；G5 指令与格式约束遵循 |
| H 认识论安全与隐私 | H1 拒答、未知与证据不足；H2 敏感信息最小披露与隐私 |
| 编程轨 | Debug Memory、Development Memory（数据集未公开）〔官方 README〕 |

### 2.2 评测契约（仓库 `data/*/pipeline.py`）〔官方〕

仓库**不含**数据，只公开每个数据集的"答题 + 评分"脚本。模型与密钥通过 `api_config.py` 从环境变量读取：`ANSWER_API_BASE/KEY/MODEL`、`JUDGE_API_BASE/KEY/MODEL/VERSION`。

| 数据集 | 契约文件 | 答题 | 评分 | 输入记录的字段 |
|---|---|---|---|---|
| LongMemEval-S / Refined | `data/longmemeval-s/pipeline.py` | 开放式模板：只用给定记忆；相对时间换算成日期；冲突时优先最新；答案尽量短 | 二值 CORRECT/WRONG：包含 + 不矛盾；**严格时间粒度**；列表题多出条目判错 | `id`、`question`、`gold_answer`（或 golden_answer / reference_answer / correct_answer）、`retrieved_context` 或 `speaker_1_memories` 等 |
| LoCoMo-Refined | `data/locomo-refined/pipeline.py` | 与 LongMemEval 相同（注释称"exactly the same"） | 同上 | 同上 |
| BEAM | `data/beam/pipeline.py`（固定上游 commit 3e12035…） | RAG 答题模板 | 每个 rubric 要点打 0/0.5/1 分，取平均；事件排序用 LLM 对齐后算 Kendall τ-b 和 F1 | `rubric_nuggets`、`question_type` |
| PersonaMem v1 / v2 | `data/personamem/pipeline_v1.py`、`pipeline_v2.py` | 官方选择题模板；v2 另有生成题 | v1 按官方规则抽取选项；v2 用选择题 + 正负两种窄评委打分（boxed score） | `question`、`all_options`、`correct_answer`、`incorrect_answers` |
| CL-bench | `data/clbench/pipeline.py` | 检索式模板 | rubric 评委 → **Strict**（全部满足）与 **Rubric Coverage**（满足比例） | `rubrics`、`qa_type`、`options` |
| ScriptMem | `data/scriptmem/pipeline.py` | 选择题模板 | 选项和排序的字母匹配 | `qa_id`、`dataset`（angry / enemy / friends / man_earth）；上游来源未公开 |

### 2.3 Add/Search 接入契约（`/docs`、`/api-guide`）〔官方〕

| 项目 | 规定 |
|---|---|
| Add 请求 | `request_id`、`messages[{role, content, timestamp?}]`（role 只能是 user / assistant；timestamp 为 Unix 毫秒）、`user_id`、`session_id`；**不发送** metadata、app_id、agent_id、async_mode |
| 分段 | 一个来源会话默认调用一次 Add；超过 20 条消息或 2,000 词时，在最近的完整消息或句子边界分段 |
| Add 响应 | 同步：写入完成、立即可检索后才返回 200，并原样回显三个 id；不接受 202 或 task id |
| Search 请求 | `query`（数据集原文，不改写）、`options?`、`user_id`、`top_k`（正式评测固定为 **100**）；不发送 filters、rerank、keyword_search |
| Search 响应 | `{"data":[{id, content, score?, created_at?}]}`，按相关性排序；平台只读这几个字段 |
| 隔离 | user_id 是唯一的检索隔离字段，禁止跨 user_id 返回记忆 |
| 参评规则 | Search 不能直接生成答案；不能跨样本共享状态；Add/Search 内部使用的模型必须是 **gpt-4o-mini**（Full 评测的前置清单） |
| 模式与配额 | smoke 每小时 1 次（仅私有）；full 每 3 个月 1 次（审核后可发布）。评测页界面还有 light、scriptmem 两种模式 |
| 公开与私有 | smoke 用固定小子集；正式排名综合公开集与私有集的结果，私有题和答案不对参评方开放（赛事页 FAQ） |
| 公榜准入 | 统一的答题模型、评测契约、**pipeline code hash、dataset bundle hash、题量记录**必须完整，并与当前发布基线一致 |
| 数据处理 | 参评方只能为本次任务使用评测数据，任务结束后 30 天内删除 |

### 2.4 其他观察

- 仓库本身**没有 LICENSE 文件**（GitHub API 返回 license 为 null）〔官方〕。
- 编程轨的规模两处说法不一致：README 写"12 个仓库、150 个基础任务、1,290 个历史任务"，赛事页写"344 个基础任务、2,987 个相关任务（强相关 555 / 弱相关 514 / 无关 1,918）"〔官方，两处不一致〕。
- 贡献机制见 `aml-contribution.md`。

## 3. LongMemEval

| 维度 | 内容 |
|---|---|
| 任务格式 | 每道题配一份按时间排序的聊天历史（`haystack_sessions`、`haystack_dates`），外加问题和问题日期；`answer_session_ids` 标出证据会话，可以算会话级召回〔官方 README〕 |
| 能力与题型 | 5 种能力：信息抽取、多会话推理、知识更新、时间推理、弃答。6 种题型：single-session-user / assistant / preference、temporal-reasoning、knowledge-update、multi-session；id 以 `_abs` 结尾的是弃答题（30 道，由其他类型改写为错误前提）〔官方〕〔论文〕 |
| 构造 | 164 个用户属性，分 5 大类；LLM 按背景生成候选问题，人工筛选重写（每类约 1,000 道候选，保留约 50 道，约 5%）；Llama 3 70B 自聊生成证据会话，约 70% 经人工编辑（确认包含证据、分散证据位置、改成口语）；填充会话 25% 取自 ShareGPT、25% 取自 UltraChat、50% 为模拟会话；时间戳随机分配在 2023 年 5 月，证据会话用预定时间锚点〔论文〕 |
| 规模控制 | Oracle（只含证据会话）/ S（约 115k token，约 40 个会话）/ M（约 500 个会话）；可以用脚本重新采样历史（`min/max_n_haystack_filler`）〔官方〕 |
| 质量控制 | 人工核验证据会话；GPT-4o 评委与人工专家的一致率 >97%；2025-09 发布 cleaned 版，删除了会干扰答案正确性的噪声会话〔论文〕〔官方〕 |
| 指标 | 问答正确率（LLM 评委）；会话级召回；论文把设计空间拆成四个控制点做消融（CP1 值的粒度、CP2 键的扩展、CP3 时间感知的查询扩展、CP4 读取策略）〔论文〕 |
| 切分与许可 | 没有 train/test 切分，全部是评测集；MIT（仓库与 HF）〔官方〕 |
| 来源 | https://arxiv.org/abs/2410.10813 ；https://github.com/xiaowu0162/LongMemEval ；https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned |

**LongMemEval-V2**〔论文〕：面向"有经验的同事"，评估 5 种能力（静态状态回忆、动态状态跟踪、工作流知识、环境坑点、前提意识）；451 题，历史轨迹最多 500 条、1.15 亿 token；CC BY 4.0。关键发现：coding agent 直接查原始轨迹（AgentRunbook-C）平均 72.5%，高于在笔记上做 RAG（48.5%）。https://arxiv.org/abs/2605.12493

## 4. LoCoMo 与 LoCoMo-Refined

| 维度 | LoCoMo | LoCoMo-Refined |
|---|---|---|
| 任务格式 | 两位说话人的多会话对话（带会话时间戳、图片及其描述）；问答（`category`、`evidence` 标出对话 id）、事件摘要（人工标注）、多模态对话生成〔官方 README〕 | 同一批对话；问答字段改为可接受答案列表；统一的词汇指标与 LLM 评委脚本〔官方〕 |
| 构造 | 人设 + 时间事件图 → LLM 智能体生成对话（可以分享图片）→ 人工核验编辑，确保与事件图一致〔论文〕 | AI 辅助筛查 + 5 名标注者审校，修订 337 条样本（措辞歧义、主宾颠倒、时间与原对话不一致）〔官方〕 |
| 规模 | 10 段对话；每段约 300 轮、约 9k token，最多 35 个会话〔论文〕〔官方〕 | 同左 |
| 质量控制 | 人工编辑 | 300 条人工对齐样本：原评委一致率 43.67%，新评委 86.33%〔官方〕 |
| 指标 / 评委 | 问答 F1 等；问答类别的具体划分〔待核〕 | 官方评委 Qwen3-14B；原则是"答案覆盖所需信息、不附加没有依据的内容、保持严格时间粒度"〔官方〕 |
| 许可 | CC BY-NC 4.0（LICENSE.txt）〔官方〕 | CC BY-NC 4.0（README 徽章）〔官方〕 |
| 来源 | https://arxiv.org/abs/2402.17753 ；https://github.com/snap-research/locomo | https://github.com/mem-eval-suite/LoCoMo_refined |

**启示**：评委本身需要"修订 + 人工对齐研究"。宽松评委会给"话题相近但细节错误"的答案记分；AML 采用了这种严格评委。

## 5. BEAM（Beyond a Million Tokens）

| 维度 | 内容 |
|---|---|
| 任务格式 | 超长对话 + 探针问题；覆盖通用、编程、数学等领域〔官方〕 |
| 10 种能力 | 弃答、矛盾消解、事件排序、信息抽取、指令遵循、知识更新、多跳推理、偏好遵循、摘要、时间推理〔官方〕〔论文〕 |
| 构造 | 种子（领域、主题、子话题、叙事、MBTI 用户画像、关系图、**明确的时间线**）→ N 个子计划 → 按批生成用户发言 → 两个 LLM 扮演用户和助手，配追问检测模块；10M 档用"顺序扩展"和"分层分解"两种策略〔论文〕 |
| 出题与核验 | GPT-4.1-mini 按能力从计划要点中选材出题，给出候选答案和出处；**人工**逐条选出有效题目、修正小的不一致；每段对话每种能力选 2 题，共 20 题〔论文〕 |
| 规模 | 100 段对话、2,000 题；128K / 500K / 1M / 10M 四档〔论文〕〔官方〕 |
| 指标 / 评委 | 要点（nugget）是"原子的、自足的判据"，LLM 评委对每个要点打 0 / 0.5 / 1 分，取平均；事件排序用 LLM 等价检测对齐后算 Kendall τ-b〔论文〕；AML 的契约照此实现〔官方〕 |
| 质量 | 人工评对话的连贯性 4.53、真实性 4.57、复杂度 4.64（满分 5）；标注者间一致性在附录 B.2〔待核〕 |
| 许可 | 代码 MIT；数据 CC BY-SA 4.0〔官方〕 |
| 来源 | https://arxiv.org/abs/2510.27246 ；https://github.com/mohammadtavakoli78/BEAM |

## 6. PersonaMem 与 PersonaMem-v2

| 维度 | v1 | v2 |
|---|---|---|
| 任务格式 | 情境化的用户查询，选择最适合当前用户画像的回答（选择题）；7 种查询类型，例如回忆用户分享过的事实、承认最新偏好、按当前偏好主动推荐〔官方 HF 卡〕 | 隐式透露的偏好 + 选择题和开放生成题〔官方〕 |
| 构造 | 基于 PersonaHub 自动生成人设与多会话对话；有静态属性和动态偏好〔官方〕 | GPT-5 生成全部人设、偏好、对话片段和问答，经多道质量与安全过滤；目标答案避开敏感信息；**官方声明无法人工核验每一条**〔官方〕 |
| 规模 | 180+ 人设、最多 60 个会话；32k / 128k / 1M 三档〔论文〕〔官方〕 | 1,000 人设、26,100 条偏好、325 个对话话题；历史长度 32k / 128k〔官方〕 |
| 难度元数据 | `distance_to_ref_in_tokens`、`num_irrelevant_tokens`、`distance_to_ref_proportion_in_context`〔官方〕 | 偏好类别标签 |
| 切分 | 按上下文长度分三个 split | benchmark_text 5,000 / train_text 18,500 / val_text 2,600，**按 persona_id 互不重叠**；多模态另有一套〔官方〕 |
| 指标 | 选择题正确率；前沿模型约 50%〔论文〕 | 前沿模型 37–48%；智能体记忆框架用 2k token 记忆达到 55%〔官方〕 |
| 许可 | MIT（HF 卡、仓库）〔官方〕 | CC BY 4.0（HF 卡）〔官方〕 |
| 来源 | https://arxiv.org/abs/2504.14225 ；https://huggingface.co/datasets/bowen-upenn/PersonaMem | https://arxiv.org/abs/2512.06688 ；https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2 |

## 7. MemoryAgentBench

| 维度 | 内容 |
|---|---|
| 任务格式 | 统一成"块 c1…cn + 问题 q1…qm + 答案"：先逐块喂入并提示记住，再提问；块大小 512（检索类任务、LongMemEval、选择性遗忘）或 4,096 token〔论文〕 |
| 4 种能力 | 精确检索、测试时学习、长程理解、冲突消解（选择性遗忘）〔论文〕〔官方〕 |
| 构造 | 改造已有数据集；新建 EventQA（读小说后从候选中选出正确事件，全自动流程）和 FactConsolidation（MQuAKE 的真实 / 反事实编辑对，新事实排在后面，按序号越大越新消解冲突），上下文 6K–262K〔论文〕 |
| 指标 | 各任务用正确率、子串精确匹配、F1、Recall@5；LongMemEval 和 InfBench 摘要题用 GPT-4o 评委〔官方 README〕 |
| 规模 | 2,071 题〔论文〕 |
| 主要发现 | 多跳冲突消解所有方法最高只有 7%；Mem0 的记忆构建时间是 BM25 的 20,000 倍；块大小、top-k 的扫描〔官方 HF 卡〕 |
| 许可 | MIT（HF 与 GitHub）〔官方〕；上游 MQuAKE 等数据的许可〔待核〕 |
| 来源 | https://arxiv.org/abs/2507.05257 ；https://github.com/HUST-AI-HYZ/MemoryAgentBench ；https://huggingface.co/datasets/ai-hyz/MemoryAgentBench |

## 8. MemBench

事实记忆与反思记忆两个层级；参与（第一人称）与观察（第三人称）两种场景；从有效性、效率、容量三方面度量〔论文〕。数据提供 0–10k 与 100k 两档，可以用 `makenoise.py` 加噪扩展（每单位约 1k token）；MIT〔官方 README〕；ACL 2025 Findings。规模与标注质控〔待核〕。https://arxiv.org/abs/2506.21605 ；https://github.com/import-myself/Membench

## 9. MemoryArena

| 维度 | 内容 |
|---|---|
| 任务格式 | 子任务之间显式依赖的多会话智能体任务：记忆系统提供 Retrieval（给定查询返回相关记忆）和 Update（把完成的子任务并入记忆）两个抽象函数，每个子任务行动前注入检索结果〔论文〕 |
| 环境与规模 | 打包网购 150 个任务（每个 6 个子任务）、团队出行规划 270 个（5–9 个）、渐进式网页搜索 256 个（2–16 个）、数学形式推理 40 个、物理 20 个〔论文〕 |
| 构造与质控 | 人工核验兼容链与干扰项；出行任务保证唯一有效解；搜索任务核验分解正确、只依赖前序子查询的信息；形式推理由博士级专家把论文主张分解成有序的中间命题〔论文〕 |
| 指标 | 成功率 SR、进度分 PS、软进度分 sPS（按满足约束的比例）、分深度成功率 SR@k〔论文〕 |
| 基线 | 长上下文 agent、Letta、Mem0、Mem0-g、ReasoningBank、BM25、嵌入检索、MemoRAG、GraphRAG；平均 SR 只有 0.02–0.23；同时报告延迟〔论文〕 |
| 许可 | 数据许可〔待核〕 |
| 来源 | https://arxiv.org/abs/2602.16313 |

## 10. EvoMemBench

两条轴（记忆范围：单回合内 / 跨回合；记忆内容：知识 / 执行）交叉成四种设定。单回合内知识复用 MemoryAgentBench（2,800 条）；单回合内执行改写 BFCL 多轮长上下文，把显式实体、参数换成隐式指代（4 个领域各 200 条）；跨回合知识复用 CL-Bench（120 个上下文、884 条）；跨回合执行复用 BFCL、xbench-DeepSearch、WebWalkerQA、ALFWorld。统一骨干模型 DeepSeek-V3.2；指标为正确率、成功率、进度分和 **token 用量**；比较了 15 种记忆方法；上下文预算扫描显示，记忆在预算紧时收益最大，128K 时有的方法不如无记忆〔论文〕。许可〔待核〕。https://arxiv.org/abs/2605.18421

## 11. ProAgentBench（主动性）

| 维度 | 内容 |
|---|---|
| 任务格式 | 分层分解：①时机预测（**何时**介入）；②协助内容生成（介入**什么**）〔论文〕 |
| 数据 | 志愿者一个月的真实使用数据（LifeTrace 每秒一张截图 + 应用日志）；28,000+ 事件，其中 LLM 相关事件 7,222 个；500+ 小时〔论文〕 |
| 隐私 | Qwen3-VL-Plus 识别敏感信息 → 志愿者本人逐图决定保留、打码或删除 → 规则过滤与永久删除；参与者可以随时暂停、退出、要求删除〔论文〕 |
| 金标 | 事件级自动标注（VLM 分类交互类型并抽取对话）〔论文〕 |
| 指标 | 时机：正确率、精确率、召回率、F1，论文明确"**精确率衡量打扰成本，召回率衡量需求覆盖**"；内容：意图正确率、嵌入语义相似度〔论文〕 |
| 切分 | 按时间切分，用户之间隔离〔论文〕 |
| 许可 | 仅限研究；禁止重识别、商业用途和任何形式的用户监控或画像〔论文〕 |
| 来源 | https://arxiv.org/abs/2602.04482 |

**补充**〔论文，只读了摘要〕：π-Bench（https://arxiv.org/abs/2605.14678）有 100 个多轮任务、5 个人设，含隐性意图、任务间依赖、跨会话连续；StreamMemBench（https://arxiv.org/abs/2606.14571）在 EgoLife 视频流上构造两步任务，度量证据召回、首次使用、反馈吸收、后续复用，CC BY 4.0。两者都**没有**报告"误报 / 打扰"类的精确率指标（按摘要判断）。

## 12. 记忆安全：MINJA

MINJA〔论文〕：攻击者不能直接写入记忆，只通过正常查询交互注入恶意记录；手法是用"桥接步骤"把受害查询连到恶意推理，并逐步缩短指示提示，让记录更容易在之后被检索到。它是**攻击**评估，不是标准化的防御基准。https://arxiv.org/abs/2503.03704 。AgentPoison 等其他工作〔待核：本轮未读取〕。

## 13. CL-bench（AML 成员）

每个实例包含系统提示、任务、含新知识的上下文和 rubric，全部由领域专家标注（每个上下文平均 63.2 条 rubric，约 20 小时专家投入）；1,899 个任务、4 大类 18 子类；评分为二值严格判定（满足全部 rubric 才算解决），默认评委 GPT-5.1；Life 版 405 个任务、5,348 条 rubric；排行榜通过 PR 提交评分文件〔官方 README〕。https://github.com/Tencent-Hunyuan/CL-bench

## 14. 数据集文档规范

| 规范 | 要点 | 本套件如何采用 | 可信度 |
|---|---|---|---|
| Datasheets for Datasets（Gebru 等） | 每个数据集附一份"数据表"，回答动机、组成、采集过程、预处理与标注、用途、分发、维护七组问题 | 每张 `card.md` 覆盖这七组问题：动机 → §1；组成 → §2–§4；采集 → §5；标注 → §6；用途与局限 → §12；分发 → §11；维护 → `suite-design.md` §11 | 〔论文：摘要确认了动机、组成、采集过程、推荐用途；其余章节名依通行版本，全文〔待核〕〕 https://arxiv.org/abs/1803.09010 |
| Data Cards（Pushkarna 等） | 数据集关键事实的结构化摘要；强调透明、有目的、以人为中心；作者在真实场景部署了 20 多张 | 卡片开头的"事实表"和按主题分节 | 〔论文〕 https://arxiv.org/abs/2204.01075 ；OFTEn 框架与"31 个主题"的细节〔待核〕 |
| HF 数据卡元数据 | README 顶部的 YAML 声明 license、configs 与 splits、size_categories、task_categories | `MANIFEST.yaml` 采用同样的字段 | 〔官方：在 LongMemEval、PersonaMem、MemoryAgentBench 的 HF 卡中看到〕 |
| Canary 串 | 在数据文件中放一个唯一的 GUID，便于日后检测是否被训练语料收录 | 所有文件带 `MEMCOMPASS-CANARY-…` | 〔推断：BIG-bench 等的通行做法，出处〔待核〕〕 |

## 15. 可复用的设计做法

| 做法 | 出处 | 本套件用在哪里 |
|---|---|---|
| 属性、事件驱动的**程序化金标** | LongMemEval 属性本体；LoCoMo 时间事件图；BEAM 会话计划与时间线 | mc-task-state、mc-asof-temporal 的重放器金标 |
| LLM 生成 + **人工筛选编辑** | LongMemEval（约 5% 成品率，约 70% 编辑）；BEAM 人工验证；LoCoMo 人工编辑 | 所有子集：test 与 held-out 100% 人工核验 |
| 填充会话与**规模档** | LongMemEval Oracle / S / M；MemBench 加噪；PersonaMem 32k / 128k / 1M | 自建填充池；S / M 两档（考虑 AML top_k=100） |
| 证据指针 | LongMemEval `answer_session_ids`；LoCoMo `evidence` | `gold.evidence`（可以算检索召回） |
| **弃答题由其他题型改写** | LongMemEval 30 道 `_abs` 题 | pf unanswerable、at plan_unconfirmed、fg direct |
| 要点 0 / 0.5 / 1 计分 | BEAM；CL-bench 严格 rubric | 多要点题的 `nuggets` |
| **严格评委 + 人工对齐研究** | LoCoMo-Refined（300 样本、5 人、86.33%） | 各子集的评委对齐门槛 |
| 固定答题器与评委，参评方只管记忆 | AML | Q 问答轨 |
| 版本化契约 + bundle hash + 题量 | AML 公榜准入条件 | `contract.yaml`、`MANIFEST.yaml` |
| 公开 + 私有题 | AML 赛事 | held-out 切分 |
| **按人或场景不重叠切分** | PersonaMem-v2（按 persona_id） | 按模板实例和场景切分 |
| 真实数据 + 时间切分 + 用户隔离 + 隐私流水线 | ProAgentBench | 可选的"真实数据时间切分变体"（只在本地、私有） |
| 难度元数据分桶 | PersonaMem `distance_to_ref_in_tokens` | `meta.difficulty` |
| 增量喂入 | MemoryAgentBench | 行为轨的 filler 与 events |
| 设计空间消融 | LongMemEval CP1–CP4；MemoryAgentBench 块大小与 top-k；EvoMemBench 上下文预算 | 机制消融协议（`suite-design.md` §7） |
| 成本并列报告 | MemoryAgentBench 构建时间；EvoMemBench token；MemoryArena 延迟 | Q1 报告 |
| "精确率 = 打扰成本" | ProAgentBench | 补充了 F0.5 的依据 |
