# 数据卡：mc-cross-agent（跨 agent 连续，K13）

> **v0.2（2026-09-14）**：24 条；构造脚本 `build/build_xa.py`。全部未经人工核验。数据文件：examples.yaml（种子）+ migrated.yaml（如有）+ generated.yaml。

> 版本 0.1.0-draft（2026-09-13）｜状态：草稿，待用户审核｜**只在本地运行**：AML 现行 Add 契约不发送 agent_id 或元数据，无法表达"宿主"。

| 项目 | 内容 |
|---|---|
| 子集名 / 前缀 | `mc-cross-agent` / `xa` |
| 测什么 | 在一个宿主（codex、kimi-code、claude-code……）里学到的，在另一个宿主里能否用上；身份与作用域解析是否正确、互不串线；多个宿主先后写入冲突内容时，结果是否一致 |
| 能力 | 主：K13；辅：K5、K11、K12 |
| AML 叶子 | 无直接对应；拟议扩展 N8"跨 agent 连续" |
| 轨道 | B 行为轨 + S 系统轨（目标宿主开启会话时注入了什么） |
| 种子 / 目标规模 | 4 条 / 60 个场景 |
| 切分 | dev 12 / test 30 / held-out 18 |
| 许可建议 | CC BY 4.0 |

## 1. 动机

- **需求**：R10"Codex、Claude、kimi 等共用同一个'我'"。agent-memory 的读取器覆盖 6 个宿主，但只有 kimi-code 完整接入，Claude Code 没有接入 MCP（框架 §5.3 P33、§8）。
- **公开评测的空白**：所有公开记忆评测都假设只有单一 agent 或单一接口。AML 的隔离边界只有 user_id（`../../public-benchmarks-survey.md` §2），没有"宿主"维度（`../../gap-analysis.md` G9）。

## 2. 类型与比例

| 类型 | 说明 | 比例 |
|---|---|---|
| `cross_host_transfer` | 在宿主 A 学到的规则或事实，在宿主 B 里用上 | 30% |
| `scope_isolation` | 不同项目有相似事实（端口、账号），在 B 项目里不能用 A 项目的 | 30% |
| `identity_resolution` | 各宿主的本地用户名不同，要解析到同一个人 | 15% |
| `concurrent_writers` | 两个宿主先后写入冲突内容，要按时间取最新 | 25% |

## 3. 任务格式

| 字段 | 含义 |
|---|---|
| `history.sessions[].host` | 会话发生在哪个宿主（generic、claude-code、codex、kimi-code、opencode、deepseek-harness、pi） |
| `history.sessions[].scope` | 项目作用域 |
| `gold.labels.target_host / target_scope` | 触发发生在哪个宿主和作用域 |
| `gold.labels.identity_map` | 各宿主的本地身份 |
| `gold.labels.must_inject / must_not_inject` | S 系统轨：目标宿主会话开头的注入块中必须有、不能有的要点 |

**宿主格式渲染**：harness 把统一格式的会话渲染成各宿主的原生日志格式（Claude Code 的 JSONL、Codex 的 rollout 等），再交给被测系统的读取器（P01）。这样还能顺带检验读取适配器。渲染器属于 runner 的一部分（D6），本轮只写需求。

## 4. 示例

`examples.yaml` 共 4 条。完整展开两条：

**示例 A：xa-0002 `scope_isolation`**

| 部分 | 内容 |
|---|---|
| s1（kimi-code，repo-a，09-01） | "repo-a 的开发服务器端口固定用 8100。" |
| s2（codex，repo-b，09-02） | "repo-b 的开发服务器端口固定用 3100。" |
| 触发（claude-code，repo-b，09-10） | "把开发服务器起起来。" |
| 期望 | 用 3100；注入块中不能出现 repo-a 的 8100 |

**示例 B：xa-0004 `concurrent_writers`**

| 部分 | 内容 |
|---|---|
| s1（codex，09-02 10:00） | "默认分支已经从 trunk 改名为 main 了。" |
| s2（kimi-code，09-02 10:05） | "改名那件事撤回了，默认分支还是 trunk。" |
| 触发（claude-code，09-03） | "从默认分支拉一个新分支 feat/export-csv。" |
| 期望 | 基于 trunk；注入块中不能出现"默认分支为 main" |

## 5. 构造流程

| 步骤 | 做法 |
|---|---|
| ① 场景矩阵 | 宿主对（A→B）× 事实类型（规则、配置、身份、偏好）× 作用域关系（同项目、异项目、全局） |
| ② 近似干扰 | 相似事实分布在不同作用域或宿主（同类端口、同类账号） |
| ③ 时间冲突 | 相隔几分钟到几天的冲突写入，覆盖"时钟靠近"的难例 |
| ④ 渲染 | 统一格式 → 各宿主原生日志（需要 runner 支持） |
| ⑤ 人工核验 | 100% 核验作用域归属无歧义 |

## 6. 标注指南（要点）

1. 触发中没有明确说出作用域时，以宿主当前打开的仓库为准（写在 behavior.trigger.context 里）。
2. 使用了其他作用域的事实，记一次串线；这个事实即使只出现在注入块里、没有被答题器采用，也在 S 系统轨上记一次。
3. 冲突写入时，按事件时间取最新；如果两条写入的时间戳相同，用例作废。

## 7. 质量控制

| 检查 | 门槛 |
|---|---|
| 作用域归属双人一致 | 100%（不一致的丢弃） |
| 渲染后的日志能被各宿主的读取器解析 | 100%（runner 冒烟测试） |

## 8. 指标

| 指标 | 定义 | 主/辅 |
|---|---|---|
| **跨宿主迁移成功率** | cross_host_transfer 与 identity_resolution 中，目标事实被正确使用的比例 | **主** |
| **串线率** | 使用或注入了 must_not_inject 内容的比例 | **主**（越低越好） |
| 并发一致性 | concurrent_writers 中取到最新值的比例 | 辅 |
| 注入召回率（S 轨） | must_inject 被注入的比例 | 辅 |
| 成本 | 会话开头注入的 token 数 | 辅（Q1） |

## 9. 基线

no-memory（各宿主各自为政）/ 单宿主记忆（只用目标宿主自己的原生记忆，例如 CLAUDE.md、Codex memories）/ naive-rag（跨宿主汇总原文检索，不分作用域）/ agent-memory-current。

## 10. 难度控制

宿主数 2 / 3 / 4；相似事实数 0 / 2 / 5；冲突写入间隔（分钟、小时、天）；目标作用域是否在触发中明示。

## 11. 许可证建议

CC BY 4.0。

## 12. 已知局限

- 宿主格式是合成渲染的，真实宿主只做抽检。
- 依赖被测系统对多宿主接入的支持程度；未接入的宿主上结果会是 0 分，这本身就是需要记录的现状。
- 无法作为 AML 提案的一部分：它需要平台在 Add 契约里增加 agent 或宿主维度，只能作为"新赛道"建议提出。
