# 数据卡：mc-proactive-recall（主动联想与线索唤起，K7、K8）

> **v0.2（2026-09-14）**：60 条 / 80 个触发探针（种子 6 + 草稿迁移 10 + 新增 44）；构造脚本 `build/build_pr.py`；正负最小对比对、类比迁移 5 条（占应浮现 25%）、多轮 32%、每条混入 6 条背景记忆。全部未经人工核验。数据文件：examples.yaml（种子）+ migrated.yaml（如有）+ generated.yaml。

> 版本 0.1.0-draft（2026-09-13）｜状态：草稿，待用户审核｜**由 `../../../eval-drafts/k7-k8-proactive-recall/` 演进而来**，迁移方式见 §13。

| 项目 | 内容 |
|---|---|
| 子集名 / 前缀 | `mc-proactive-recall` / `pr` |
| 测什么 | 用户没问到记忆时，能否在对的时刻让相关记忆浮现（K8）；线索和原文字面不一致时能否唤起完整情节（K7）；不相关时是否保持沉默 |
| 能力 | 主：K8、K7；辅：K6、K5、K12（隐私类负例） |
| AML 叶子 | 无直接对应；拟议扩展 N1"线索触发的主动回忆"、N2"无关时保持沉默" |
| 轨道 | S 系统轨（浮现接口注入了什么）+ B 行为轨（答题器最终回复）+ Q 问答轨（**需要新契约 proactive-v0**，见 `../../suite-design.md` §6.2） |
| 种子 / 目标规模 | 5 条新种子 + 12 条草稿遗留用例 / 240 个触发 |
| 切分 | dev 48 / test 120 / held-out 72 |
| 主指标 | **F0.5**〔已定：精确率优先〕 |
| 许可建议 | CC BY 4.0 |

## 1. 动机

- **需求**：R5"自己意识到现在该回忆了"、R6"联想"、R8"线索唤起并还原完整"。三者都是用户的核心诉求（★）。
- **现有 layer3 替代不了**〔实测〕：它的评委判的是检索块里有没有目标事实，不看回答；提问本身带提示；没有负例（框架 §7.8）。
- **公开评测的空白**：
  - LongMemEval、LoCoMo、BEAM、PersonaMem 的提问都是显式的，问题本身就是检索线索；
  - ProAgentBench 测"何时介入"的精确率和召回率，但触发源是屏幕行为，不是记忆；
  - π-Bench 有隐性意图和跨会话连续，但没有"不应联想"的负例，也不按精确率优先计分（`../../gap-analysis.md` G4）。

## 2. 类型、子类型与比例

总体比例沿用草稿的 2 : 1 : 2 : 1。**另要求 ≥30% 的用例是多轮触发**（`earliest_turn` > 1）。

| 类型 | 子类型 | 说明 |
|---|---|---|
| `should_surface`（2） | deadline_conflict、pitfall_reuse、data_caveat、late_cue_multi_turn、★`analogy_transfer` | 应当在第 `earliest_turn`–`max_turn` 轮之间浮现 |
| `indirect_cue`（1） | alias、alias_two_hop、paraphrase、situational | 触发和证据的字面重合度低（按 §5 的规则核验） |
| `should_not_surface`（2） | lexical_overlap、same_name_other_entity、resolved、off_task、privacy_sensitive | 保持沉默；`privacy_sensitive` 同时计入隐私披露率 |
| `time_trap`（1） | superseded、expired | 只能浮现最新状态，或者说明"已变更" |

★`analogy_transfer`（学习迁移、类比；2026-09-13 审阅时补充）：
- **定义**：新话题与已学内容共享同一个原理或技巧，但两者没有共同的实体，线索只落在概念层面。例如"今天问 word2vec 负采样的 3/4 次方"对应"昨天学的 GloVe 权重函数也用了 3/4"。
- **来由**：这正是用户最初描述的"这和你昨天学的……很像"。在 2026-09-13 的冒烟中，这类用例（草稿 pr-01）在所有带记忆的对照组里全部失败，即使记忆已经注入上下文也一样（见 `../../../eval-drafts/runner-draft/smoke-results-2026-09-13.md`）。
- **构造要求**：每个版本中，`analogy_transfer` 至少占 `should_surface` 的 20%；`cue_overlap` 必须是 low；gold 的 key_details 必须包含"共同原理"这一条。
- **配对负例**：每条都要配一个"表面同领域、但没有共同原理"的负例，归入 `off_task` 子类型。

## 3. 任务格式（在草稿字段之上新增的部分用 ★ 标出）

| 字段 | 含义 |
|---|---|
| `history.preloaded_memories[]` | 草稿中 `sessions[].memories` 的新位置；增加 `source_session`、★`sensitivity` |
| `probes[].kind: trigger` | 触发探针；多轮时每一轮各是一个探针（`at.trigger_turn`） |
| `probes[].contract` | Q 问答轨使用的答题契约，本子集为 `proactive-v0` |
| `gold.labels.should_surface / target_memory_ids / key_details / stale_details / max_turn` | 与草稿一致 |
| ★`gold.labels.earliest_turn` | 早于这一轮浮现，算作无依据臆测（记 FP） |
| ★`gold.labels.tolerated_mentions` | 容许的中性提及：不计 TP 也不计 FP，只计入打扰感 |
| ★`subtype` | 子类型，用于分桶报告 |

## 4. 示例

`examples.yaml` 共 5 条：pr-0001（域名到期，迁移自草稿 pr-02）、pr-0002（别名两跳）、pr-0003（已解决的风险，负例）、pr-0004（隐私敏感，负例）、pr-0005（第 2 轮才出现线索）。完整展开两条：

**示例 A：pr-0002 `indirect_cue / alias_two_hop`**

| 部分 | 内容 |
|---|---|
| 会话 s1（08-12） | "采购部李工那边的供应商对账接口，每晚 23:00 到次日 01:00 维护，这段时间调用会返回 503。" |
| 会话 s2（08-20） | "以后我说'老李那边'，指的就是采购部李工那个组。" |
| 触发（09-09） | "今天是 2026-09-09。今晚想把跟老李那边的数据全量同步一遍，帮我定个半夜 23:30 跑的定时任务。" |
| 金标 | 应浮现；目标记忆为两条；关键细节：老李那边 = 李工的对账接口、23:00–01:00 维护会 503、应改到 01:00 之后 |
| 陷阱 | 照 23:30 配置，不提维护窗口 |

**示例 B：pr-0005 `should_surface / late_cue_multi_turn`**

| 部分 | 内容 |
|---|---|
| 会话 s1（09-02） | "billing 库的 orders 表有个触发器：单次批量写入超过 1000 行会被拒绝，要分批。" |
| 第 1 轮 | "帮我搭个数据迁移脚本的框架……" → 金标：**不应浮现**（此时还没有线索） |
| 第 2 轮 | "目标库是 billing，要写 orders 表，大概 20 万行。" → 金标：应浮现（earliest_turn = max_turn = 2），关键细节：单批 ≤1000 行、至少 200 批 |

## 5. 构造流程

| 步骤 | 做法 | 参照 |
|---|---|---|
| ① 正负配对 | 每写一个正例，就配一个共用关键词、实体或时间词的负例（最小对比对） | LoCoMo 的对抗类问题；LongMemEval 从其他类型改写出 30 道弃答题 |
| ② 线索重合度分桶 | 用 BM25 和嵌入相似度计算触发与证据的重合度，分为 low / medium / high 三档；`indirect_cue` 必须是 low | PersonaMem 按 `distance_to_ref_in_tokens` 分桶 |
| ③ 间接性核验 | `indirect_cue` 用例中，naive-rag 的 top-5 检索不到目标的比例应 ≥60%。**不做**针对基线的对抗过滤，以免让比较偏向或偏离某个基线：只核验、不筛除，结果单独报告 | —（避免对抗过滤带来的偏差） |
| ④ 多轮构造 | 在第 `earliest_turn` 轮才出现关键线索；之前的轮次是中性铺垫 | ProAgentBench 的时机预测 |
| ⑤ LLM 扩写 + 人工核验 | 同 `../mc-present-fidelity/card.md` §5 的第 ③ 和第 ⑦ 步 | — |
| ⑥ 真实数据变体（可选，私有） | 从用户的会话中找"事后才发现应该早提醒"的片段，用户标注（框架 §7.9） | ProAgentBench |

## 6. 标注指南（要点）

1. **什么算"浮现"**：回复（或注入块）中出现了**用户这次请求里没有提供**、来自记忆的信息。
2. **判定规则**：
   - TP：正例上浮现，而且相关、不过时；
   - FP：负例上浮现（`tolerated_mentions` 除外），或者正例上浮现了过时、无关的内容，或者早于 `earliest_turn` 浮现；
   - FN：正例上没有在 `max_turn` 之前正确浮现。
3. **唤起完整度**：只在 TP 上计算，等于 `key_details` 的覆盖比例。
4. `privacy_sensitive` 负例中，只要披露或暗示了敏感内容，就同时记一次隐私披露。

## 7. 质量控制

| 检查 | 门槛 |
|---|---|
| 负例占比 | 1/3 左右（25%–45%，脚本 D01） |
| 多轮用例占比 | ≥ 30% |
| 间接性核验 | 见 §5 第 ③ 步 |
| 评委对齐（浮现与否、是否过时） | 与人工一致率 ≥ 85%；κ ≥ 0.7 |
| 打扰感（1–5 分） | 人工抽检 20%，与评委的 Spearman ρ ≥ 0.6，否则不作为正式指标 |

## 8. 指标

| 指标 | 定义 |
|---|---|
| 浮现精确率 P | TP / (TP + FP) |
| 主动召回率 R | TP / 正例数 |
| **F0.5（主指标）** | 1.25·P·R / (0.25·P + R) |
| 过时浮现率 | time_trap 与 resolved 中，把 stale_details 当现状浮现的比例 |
| 唤起完整度 | TP 上 key_details 的平均覆盖率 |
| 线索间接召回率 | indirect_cue 类单独计算的 R |
| 过早浮现率（新增） | 早于 earliest_turn 浮现的比例 |
| 隐私披露率（新增） | privacy_sensitive 中披露的比例 |
| 打扰感 | 评委打 1–5 分，重点看负例 |
| 成本 | 每轮注入的 token 数、浮现判定的延迟 |

## 9. 基线

no-memory / full-context / naive-rag（每轮用触发消息检索，无条件注入 top-k）/ naive-rag+阈值（阈值在 dev 集上确定）/ agent-memory-current（没有主动浮现机制，S 系统轨上等于永远不注入；另测"每轮调用 memory_context"作为参照）/ 未来的 P24、P26 机制。以上沿用 `../../../eval-drafts/baseline-naive-rag/design.md`。

## 10. 难度控制

线索重合度 low / medium / high；推理跳数 1 / 2；时间距离 1 / 7 / 30 天以上；干扰记忆数 0 / 5 / 50（填充的长期记忆）；多轮位置 1 / 2 / 4。

## 11. 许可证建议

CC BY 4.0。

## 12. 已知局限

- "该不该插话"有主观成分，负例的金标需要用户重点审核。
- Q 问答轨依赖新的答题契约，这对 AML 来说是一次评分方式的变更，要按新契约版本处理（AML README 贡献条款第 4 条）。
- 单一的模拟用户不会对插话作出反应，测不出"插话之后对话被带偏"。

## 13. 从 eval-drafts 迁移

| 草稿字段 | 新位置 | 规则 |
|---|---|---|
| `id: pr-01` | `id: pr-00NN`，`meta.legacy_id: pr-01` | 按草稿顺序编号；本文件示例中 pr-02 → pr-0001 |
| `suite: proactive_recall` | `subset: mc-proactive-recall` | 固定映射 |
| `type` | `type` | 不变 |
| `sessions[].date`（日期） | `history.sessions[].date` | 保留日期；缺时间时按 10:00 +08:00 处理 |
| `sessions[].turns` | `history.sessions[].messages` | 改名；内容不变 |
| `sessions[].memories` | `history.preloaded_memories`，加 `source_session` | 从会话里提出来 |
| `trigger.date` + `trigger.user_message` | `probes[0]`：`reference_time` 取 trigger.date 的 10:00；Q 轨的 `query` 前面加"今天是 {date}。" | B 轨可以不加日期前缀，由 harness 传入日期 |
| `trigger.context` | `behavior.trigger.context` | — |
| `gold.*` | `probes[0].gold.labels.*` | 原样搬过去；补 `earliest_turn: 1`、`tolerated_mentions: []` |
| `rubric.essential / pitfalls` | `behavior.rubric`，同时写入 `probes[0].gold.nuggets / pitfalls` | — |

12 条草稿遗留用例全部可以无损迁移（草稿的字段是新 schema 的子集）。迁移后运行 `tools/validate.py`。
