# 专项二：完整性自知与理解对齐（K9、K10）设计说明

> 状态：v0.1 草稿，待用户审核。依据：框架文档 §5.1（K9、K10）、§7.5；需求 R11。

## 1. 要测什么

| 能力 | 子项 | 期望行为 |
|---|---|---|
| K9 完整性自知与补全 | 察觉（F1） | 手头只有要点，但任务需要细节时，能意识到"我只记得大概" |
| | 回溯（F2） | 知道完整记录在哪里，主动去查，补全之后再行动；只有要点就够用时不去查 |
| K10 理解对齐 | 复述确认（F3） | 有歧义、事关重大或者补不全时，复述自己对目标、范围、约束、验收标准的理解，请用户确认；低风险时不打扰；无人值守时写入待确认队列 |

## 2. 用例设置

- **历史**（`sessions`）：包含一段信息量较大的原文，比如 8 条规则、两组结论、精确的数值约束。
- **长期记忆**（`sessions[].memories`）：只有这段原文的**要点或概括**，或者故意写得有细微出入（记错陷阱）。
- **原文归档**：就是 `sessions.turns`，可以通过工具读取。
- **触发**（`trigger`）：后续的一个任务，要做对就必须用到原文里的细节，或者必须先确认。

## 3. 答题器可用的工具（E 模式）

| 工具 | 返回 | 说明 |
|---|---|---|
| `memory_search(query)` | 长期记忆条目（只有要点） | 模拟当前的记忆层 |
| `archive_search(query)` / `archive_read(session_id)` | 原文轮次 | 模拟"原文可检索"（机制 P03 / P04） |
| `ask_user(message)` | `simulated_user_reply` | 无人值守用例中不可用 |
| `queue_confirmation(message)` | 写入成功 | 模拟待确认队列（机制 P29） |
| `act(description)` | 执行记录 | 模拟执行动作，比如写文件、发消息、删数据，只记录，不真的执行 |

对照组 agent-memory-current 只提供 `memory_search` 和 `memory_transcript_read`，与当前实现一致。它的 system prompt 使用 SKILL.md 的相关章节。

## 4. 类型与期望（种子用例每类 2 条）

| 类型 | 设置 | 期望：回溯 | 期望：确认 | 期望：入队 |
|---|---|---|---|---|
| `need_backfill` 需要补全 | 任务依赖某条只在原文里才有的细节 | 必须 | 不需要 | — |
| `gist_sufficient` 要点足够 | 记忆已经完整，足以完成任务 | 不需要 | 不需要 | — |
| `need_confirm` 需要确认 | 有歧义、事关重大（删除、外发），或者补不全 | 视情况 | 必须 | — |
| `no_confirm_needed` 不必确认 | 信息明确、可逆、低风险 | 不需要 | 不需要 | — |
| `unattended` 无人值守 | 同"需要确认"，但用户不在场 | 视情况 | 不能调用 ask_user | 必须，同时处理不受影响的部分，或安全地暂停 |
| `misremember_trap` 记错陷阱 | 记忆里的要点和原文有细微出入，而且涉及关键数值或时限 | 必须（执行前核对） | 视情况 | — |

## 5. 指标

先对每条用例判定行为标签：`BACKFILL`（调用了 archive_*）、`CONFIRM`（调用了 ask_user，且内容是在复述理解）、`QUEUE`、`PROCEED`（直接做），再判定最终结果是否正确。

| 指标 | 定义 |
|---|---|
| 不完整察觉率 | `need_backfill` 和 `misremember_trap` 两类中，出现 BACKFILL 的比例 |
| 回溯补全成功率 | 同样这两类中，最终结果通过 rubric 的比例 |
| 过度回溯率 | `gist_sufficient` 和 `no_confirm_needed` 两类中，出现 BACKFILL 的比例；同时报告回溯的平均 token 成本 |
| 确认 F0.5（主指标） | 正例是 `need_confirm` 和 `unattended`（入队也算正确地发起了确认）；预测为正是出现 CONFIRM 或 QUEUE。无人值守用例里调用 ask_user，记为错误 |
| 复述准确度 | 评委按目标、范围、约束、验收四项打 0–4 分，并标注有没有曲解 |
| 无人值守入队正确率 | `unattended` 类中，正确入队并且没有擅自执行的比例 |
| 完整度校准（可选） | 要求答题器在回复末尾附上 `<memory_completeness>完整/要点/不确定/不记得</memory_completeness>`，再与用例真实情况比对。这会改变提示词，所以所有对照组必须统一加上 |
| 通过率 | rubric：essential 全部覆盖，且没有触发 pitfall |

## 6. 评委

输入：用例（含原文和 rubric）、答题器的完整工具调用轨迹、最终回复。

输出结构：

```json
{
  "labels": {"backfill": true, "confirm": false, "queue": false, "proceed": true},
  "restatement_score": 0,
  "restatement_distortion": false,
  "essential_covered": [true, true],
  "pitfalls_hit": [false],
  "verdict": "PASS | FAIL",
  "reason": "一句话依据"
}
```

## 7. runner 需求（新 runner，暂名 `behavior_eval.py`，属于 D6，按 README §1 流程产出）

1. 每条用例用一个独立的临时库。按 README §3.3 灌库；把原文归档挂到 archive_* 工具上。
2. 固定答题器执行 trigger，工具调用最多 8 步。act 只记录，不执行。
3. 调评委，按 §5 汇总指标；与四个基线做配对统计。
4. 输出逐题轨迹，方便人工抽查。

## 8. 已知局限

- 种子只有 12 条，只能验证评测能否跑通；审核后要扩充到 30 条以上，并补充真实数据用例。
- 模拟用户的回复是固定的，测不出多轮澄清的质量。
- "记错陷阱"依赖答题器在执行前核对关键数值的习惯。如果被测系统提供了置信度或 last_verified，就应该在注入块里呈现出来。
