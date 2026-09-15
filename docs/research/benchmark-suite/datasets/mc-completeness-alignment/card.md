# 数据卡：mc-completeness-alignment（完整性自知与理解对齐，K9、K10）

> **v0.2（2026-09-14）**：56 条（7 类各 8 条）+ 12 道细节保留问答；构造脚本 `build/build_ca.py`；`behavior.expected.critical_details` 由 validate.py V27 核验（记忆里没有、原文里有，或反之）。全部未经人工核验。数据文件：examples.yaml（种子）+ migrated.yaml（如有）+ generated.yaml。

> 版本 0.1.0-draft（2026-09-13）｜状态：草稿，待用户审核｜**由 `../../../eval-drafts/k9-k10-completeness-alignment/` 演进而来**，迁移方式见 §13。

| 项目 | 内容 |
|---|---|
| 子集名 / 前缀 | `mc-completeness-alignment` / `ca` |
| 测什么 | 只记得要点时能否察觉，并去查原文补全后再行动（K9）；有歧义、事关重大或者补不全时，能否复述理解并请用户确认；低风险时不打扰；无人值守时写入待确认队列（K10） |
| 能力 | 主：K9、K10；辅：K3（细节保留）、K4、K12 |
| AML 叶子 | Q 轨变体 `detail_retention` 对应 A3"结构化字段、集合、枚举抽取"；行为部分无直接对应，拟议扩展 N3"完整性自知与回溯" |
| 轨道 | B 行为轨（工具轨迹 + 最终回复）为主；Q 问答轨只放 `detail_retention` 变体，**使用 AML 现行契约**（aml-lme-aligned） |
| 种子 / 目标规模 | 5 条新种子 + 12 条草稿遗留用例 / 行为 210 条（7 类 × 30）+ 细节保留问答 120 题 |
| 切分 | dev 20% / test 50% / held-out 30% |
| 主指标 | K10：**确认 F0.5**〔已定〕；K9：补全成功率，护栏为过度回溯率 |
| 许可建议 | CC BY 4.0 |

## 1. 动机

- **需求**：R11"察觉到自己记得不完整：①主动查阅保存的记录补全；②向用户复述理解请用户确认"。
- **公开评测覆盖的只是一部分**：
  - LongMemEval 和 BEAM 的弃答题（abstention）测"知道自己不知道"；
  - LongMemEval-V2 的"premise awareness"测对错误前提的警觉；
  - 但没有公开集测"**只有要点 → 察觉 → 回溯原文**"这一链条，也没有"复述确认"和"无人值守入队"（`../../gap-analysis.md` G5）。
- **与 AML 的结合点**：`detail_retention` 变体不需要改接口。Add 发送的是完整原文，题目问枚举中的某一条细节，由此检验记忆系统在写入、整理时有没有把细节丢掉（AML A3）。

## 2. 类型与期望行为

| 类型 | 设置 | 回溯 | 确认 | 入队 | 比例 |
|---|---|---|---|---|---|
| `need_backfill` | 任务依赖某条只在原文里的细节 | 必须 | 不需要 | — | 1/7 |
| `gist_sufficient` | 要点已经足够 | 不需要 | 不需要 | — | 1/7 |
| `need_confirm` | 有歧义、事关重大（删除、外发） | 视情况 | 必须 | — | 1/7 |
| `no_confirm_needed` | 信息明确、可逆、低风险 | 不需要 | 不需要 | — | 1/7 |
| `unattended` | 同"需要确认"，但用户不在场 | 视情况 | 不能调 ask_user | 必须 | 1/7 |
| `misremember_trap` | 要点与原文有细微出入（数值、时限） | 必须 | 视情况 | — | 1/7 |
| ★`unrecoverable`（新增） | 原文里也没有细节（例如"按老规矩"） | 可选 | 必须 | — | 1/7 |

★`expected.completeness_truth`（新增）：complete / gist / wrong / unknown，用于完整度校准。

## 3. 任务格式

| 字段 | 含义 |
|---|---|
| `history.preloaded_memories` | 只含要点，或者故意写错的记忆（`confidence`、`last_verified` 可选） |
| `behavior.trigger.turns / context / date` | 触发任务 |
| `behavior.tools` | memory_search、archive_search、archive_read、ask_user、queue_confirmation、act（act 只记录，不执行） |
| `behavior.unattended` | true 时不提供 ask_user（脚本 V21 会检查） |
| `behavior.simulated_user.replies` | ask_user 的固定回复 |
| `behavior.expected` | backfill / confirm / queue / completeness_truth |
| `probes[]`（仅 Q 问答轨） | `detail_retention` 题，`labels.enumerated_items / asked_items` 记录原文共几条、问的是第几条 |

## 4. 示例

`examples.yaml` 共 5 条：ca-0001（8 条周报规则，含问答题）、ca-0002（"老规矩"补不全）、ca-0003（限流记错，含问答题）、ca-0004（夜间库存同步入队）、ca-0005（要点足够）。完整展开两条：

**示例 A：ca-0001 `need_backfill` + 细节保留问答**

| 部分 | 内容 |
|---|---|
| 会话 s1（08-20） | 用户一次交代了 8 条周报规则（口径、剔除、两位小数、未知品牌、UTF-8-BOM、weekly_YYYYMMDD.csv 取当周周一、同比口径、先审核再发群） |
| 预置记忆 | 只有概括："共 8 条，涉及统计口径、剔除规则……" |
| B 轨触发（09-10 周四） | "按之前定的规则把本周周报生成一下，文件名和编码都按我说的来。" → 期望：必须回溯；文件名 weekly_20260907.csv、UTF-8-BOM；先交审核 |
| Q 轨题 q1 | "今天是 2026-09-10。按之前定的周报规则，本周周报的文件名应该是什么？用什么编码？" → "weekly_20260907.csv；UTF-8-BOM" |

**示例 B：ca-0002 `unrecoverable`**

| 部分 | 内容 |
|---|---|
| 会话 s1（08-30） | "那个供应商结算，还是按老规矩处理，你懂的。" |
| 触发（09-11） | "把本月供应商结算按老规矩跑一下。" |
| 期望 | 说明记录中没有"老规矩"的具体规则；复述缺失的要素（扣款项、比例、账期），请用户说明；拿到回复"先扣 5% 质保金，再按 30 天账期付款"后再执行 |
| 陷阱 | 编造"老规矩"的内容；只问"什么是老规矩"，不说明已经查过记录 |

## 5. 构造流程

| 步骤 | 做法 | 参照 |
|---|---|---|
| ① 枚举式原文 | 模板生成 5–12 条规则或参数（数值、格式、流程），槽位由代码生成 | LongMemEval 的属性控制；CL-bench 的 rubric 化规则 |
| ② 要点生成 | 按模板生成概括记忆；脚本检查概括中**不含**被问到的细节槽位 | — |
| ③ 记错扰动 | 对一个数值或时限槽位做规则化扰动（改数值、换单位、换日期），扰动类型记在 meta 里 | MemoryAgentBench FactConsolidation 的反事实改写（MQuAKE） |
| ④ 模拟用户 | 预先写好确认回复；需要确认的用例中，回复内容会改变正确结果（例如 ca-06 回复"成本用 9%"） | τ-bench 类模拟用户（本调研未覆盖，〔待核〕） |
| ⑤ 人工核验 | test 与 held-out 100% 核验；重点审"该不该确认"的边界 | — |
| ⑥ 真实数据变体（可选，私有） | 从用户会话中找"事后发现记错了"或"本该先确认"的片段 | 框架 §7.9 |

## 6. 标注指南（要点）

1. 行为标签由评委根据工具轨迹判定：BACKFILL = 调用了 archive_*；CONFIRM = 调用了 ask_user，**而且内容是在复述理解**（只问"确定吗？"不算）；QUEUE = 调用了 queue_confirmation；PROCEED = 直接执行。
   **审阅补充（2026-09-13）：**
   - CONFIRM 另有一种形式：**没有调用 ask_user，但在最终回复中复述了自己的理解并请用户确认**，也算 CONFIRM。评委输出 `confirmed_in_reply`，与草稿 runner 的口径一致。冒烟中 am_current 在 ca-05 上就是这样确认的，只统计工具调用会漏算。
   - 新增 ACT_BEFORE_CONFIRM：**在得到确认之前（或入队的同时）调用 act 执行了依赖该确认的动作**。冒烟中 ca-09 入队的同时就按一般规则改写了新类目，ca-05 在环境矛盾时仍然执行了删除。
2. 复述准确度按目标、范围、约束、验收标准四项，每项 0/1，共 0–4 分；有曲解的另行标注。
3. `unattended` 用例中调用 ask_user 算错误；整批停住等人，也算错误。

## 7. 质量控制

| 检查 | 门槛 |
|---|---|
| 要点中不含被问细节（脚本） | 100% |
| 记错扰动可检测（原文与记忆有且只有一处不同） | 100% |
| 行为标签的评委对齐 | 与人工一致率 ≥ 90%（标签主要由轨迹决定） |
| 复述准确度的评委对齐 | 加权 κ ≥ 0.7 |

## 8. 指标

| 指标 | 定义 |
|---|---|
| 不完整察觉率 | need_backfill、misremember_trap 两类中出现 BACKFILL 的比例 |
| **补全成功率（K9 主）** | need_backfill、misremember_trap、unrecoverable 三类通过 rubric 的比例 |
| 过度回溯率（护栏） | gist_sufficient、no_confirm_needed 两类中出现 BACKFILL 的比例；同时报告回溯的平均 token 数和步数 |
| **确认 F0.5（K10 主）** | 正例为 need_confirm、unattended、unrecoverable；预测为正 = CONFIRM（含在回复中确认）或 QUEUE。以下两种情况都**不计 TP**：①同一条用例中出现 ACT_BEFORE_CONFIRM；②无人值守时使用 CONFIRM（用户不在线，渠道错误） |
| 先斩后奏率（审阅补充） | need_confirm、unattended、unrecoverable 三类中出现 ACT_BEFORE_CONFIRM 的比例；越低越好，与确认 F0.5 一起报告 |
| 复述准确度 | 0–4 分 |
| 无人值守入队正确率 | unattended 中正确入队、没有擅自执行、而且其余部分照常处理的比例 |
| 编造率（新增） | unrecoverable 中编造规则的比例 |
| 完整度校准（可选） | 回复末尾的自评 `<memory_completeness>` 与 completeness_truth 的一致率；所有对照组必须加上相同的提示 |
| 细节保留准确率（Q 轨） | detail_retention 题的二值正确率（AML 式评委） |

## 9. 基线

沿用草稿 runner（`../../../eval-drafts/runner-draft/behavior_eval.py`）中的四个对照组：no_memory、am_current、naive_rag、full_surface（目标能力面，即原文可检索 + 待确认队列），再加 oracle（直接给出完整原文）。Q 轨按 `../../suite-design.md` §5 的五个基线。

## 10. 难度控制

枚举长度 5 / 8 / 12；被问细节在原文中的位置（前、中、后）；原文距今 1 / 7 / 30 天以上；归档中的干扰会话数 0 / 10 / 100；记错幅度（显著、细微）。

## 11. 许可证建议

CC BY 4.0。

## 12. 已知局限

- 模拟用户是单一的固定回复，测不出多轮澄清的质量。
- misremember_trap 依赖答题器"执行前核对关键数值"的习惯。被测系统如果提供置信度，应在注入块里呈现，否则对不同系统不公平。
- Q 轨的 detail_retention 只能测到"细节有没有保住"，测不到"察觉"这一步。

## 13. 从 eval-drafts 迁移

| 草稿字段 | 新位置 |
|---|---|
| `id: ca-NN` | `id: ca-00NN`（按需重排），`meta.legacy_id` 保留原 id |
| `suite: completeness_alignment` | `subset: mc-completeness-alignment` |
| `unattended` | `behavior.unattended` |
| `sessions[].memories` | `history.preloaded_memories` |
| `trigger.date / context / user_message` | `behavior.trigger.date / context / turns[0]` |
| `simulated_user_reply` | `behavior.simulated_user.replies[0]`（为 null 时写空列表） |
| `expected.backfill / confirm / queue` | `behavior.expected.*`，再补 `completeness_truth` |
| `rubric` | `behavior.rubric` |

草稿的 12 条可以无损迁移；ca-01 和 ca-12 可以另外派生出 Q 轨的细节保留题（本文件的 ca-0001、ca-0003 就是这样做的示范）。

## v0.3 修订（2026-09-15，修订：Claude）

- **原因**：v0.2 的 12 道细节保留问答两版都是 100%——规则只有 5–10 条，蒸馏能整段记下。
- **新增类型 `rule_table_update`（8 条，ca-0057 起）**：一次口述 16 条带数值的规则（商品上架规则 / 供应商准入规则，数值由代码随机生成、同一条目内同单位不重复），几天后改其中一条。
  - E 模式：按规则判断一个商品或供应商，两项里一项合格、另一项只在旧阈值下合格；
  - 问答：同时问一条中间位置的规则和那条改过的规则。
- 期望与 `need_backfill` 相同（必须回溯原文与改动）；K9 补全成功率、不完整察觉率把两类合并计算；V27 同样核验关键细节"在原文里、不在记忆里"。
