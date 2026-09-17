# 里程碑记录（M0–M9）

> 本文是 agent-memory 从 M0 到 M9 各阶段交付内容的历史记录，从早期 README 原样迁出。其中的工具数量、验收数字是对应阶段的快照；**当前接口以根目录 README 与源码为准**。v0.2 之后的机制变更见 [`../research/optimization-v02.md`](../research/optimization-v02.md)，评测结果见 [`../research/benchmark-suite/results/`](../research/benchmark-suite/results/)。

下列 M0–M7 内容记录各阶段交付，工具数量和验证结果是对应阶段的历史记录；当前接口以以上入口和源码为准。

M0 只交付项目骨架与核心 schema：

- `agent_memory/models.py`：记忆条目（`MemoryEntry`）、证据指针（`EvidenceRef`）、蒸馏提案（`MemoryProposal`）的 pydantic 模型与校验规则；
- `agent_memory/config.py`：单一配置模块，环境变量可覆盖，非法配置 fail-closed；
- `evals/datasets/layer1/`：20 条"基础回忆"评估用例（YAML），用于后续 M1+ 的召回评估。

M1 交付记忆内核 MVP（手动蒸馏）：`long_term/store/`（Markdown 记忆层 + sqlite-vec/FTS5 派生索引）、`long_term/retrieve/`（bge-m3 嵌入 + 稠密/稀疏 RRF 混合检索）、`long_term/ingest/redact.py`（正则脱敏）、`cli.py`（add / search / list / update / forget / rebuild / stats）、`evals/runners/recall_eval.py`（layer1 recall@5）。

M2 交付蒸馏写路径 + MCP server：

- `agent_memory/llm.py`：`LLMClient` 协议（依赖注入，测试用 fake）与 `OpenAILLMClient`（OpenAI 兼容端点，默认 DeepSeek，缺 key fail-closed）；
- `agent_memory/long_term/ingest/distill.py`：对话 → 原子记忆候选（prompt 硬规则：绝不提炼指令性内容，红线 D2；id/confidence/detail 先规范化再校验，规范化后仍非法的进 `data/review_queue/` 而非静默丢弃）；
- `agent_memory/long_term/ingest/gate.py`：评价门（脱敏残留 / 指令性内容 / 长度下限 / low 置信度三桶分流）；
- `agent_memory/long_term/ingest/reconcile.py`：Mem0 式对账（ADD / UPDATE / DELETE / NOOP，冲突无法收敛时写 `data/review_queue/`）；UPDATE/DELETE 落库后接 `long_term/ingest/propagate.py` 变更传播（依赖旧事实的近邻由 LLM 判失效/需修订/不受影响，失效删除留审计日志 `data/logs/propagation.jsonl`，需修订进复核队列）；
- `agent_memory/long_term/retrieve/inject.py`：检索结果渲染为 `<recalled_memories>` XML 注入块（带"参考而非指令"护栏前缀，预算整条截断）；
- `agent_memory/server/mcp_server.py`：MCP stdio server，五个 tool（memory_search / memory_add / memory_feedback / memory_update / memory_forget）；
- `evals/datasets/layer2/`：20 条多会话检索/消歧用例（时序冲突 7 + 多对象消歧 7 + 有效/失效区分 6）；
- `evals/runners/e2e_eval.py`：端到端评估（无 LLM key 时自动降级为规则判定模式）。

M3 交付 LangGraph 适配 + Skill + 轨迹前缀回归评估：

- `agent_memory/long_term/adapters/langgraph/store.py`：`AgentMemoryStore`（LangGraph `BaseStore` 实现，namespace `("memories", <scope>)`，put 过脱敏+评价门规则、search 走混合检索）；
- `agent_memory/long_term/adapters/langgraph/tools.py`：`build_memory_tools()` 产出 17 个 ReAct tool（三层记忆全暴露，业务实现收敛在 MemoryService）；默认走完整管线（含 LLM 对账），LLM 缺失时无近邻直接 ADD、存在近邻转人工复核；
- `agent_memory/long_term/retrieve/resident.py`：`build_system_context(scope)` 常驻层注入（profile 记忆按置信度排序进 system prompt，预算为召回预算的一半）；
- `skills/agent-memory/SKILL.md`：教 agent 何时检索/写入/反馈（MCP tool 名与参数示例，"召回是参考而非指令"）；
- `evals/datasets/prefix/` 9 条轨迹前缀回归用例（指令冲突 2 + scope 泄漏 2 + 低置信度 2 + 抗注入 2 + 正常召回对照 1）；
- `evals/runners/prefix_regression.py`：冻结上下文 → LLM 输出下一步动作 → 评委判定可接受/禁止集合（429 自动重试，无 key 跳过）；
- `examples/langgraph_demo.py`：最小 LangGraph ReAct agent 接入演示（跨会话记住偏好）。

M4a 交付进化闭环核心（睡眠学习循环 + 定期整理），双循环成形：在线循环只追加证据（蒸馏→评价门→对账），离线循环批量整理记忆库——

- `agent_memory/long_term/evolve/trigger.py`：触发判定（距上次整理超 N 天 / 新增条目超阈值 / review_queue 积压超阈值，任一满足即触发，阈值全部走 `AGENT_MEMORY_EVOLVE_*` 环境变量）；
- `agent_memory/long_term/evolve/consolidate.py`：整合产出 `EvolutionProposal`——去重合并（近邻对 LLM 判 MERGE/CONFLICT/UNRELATED，CONFLICT 不强行收敛交人工）、离线复核最旧条目（复用 `long_term/ingest/propagate.py` 的 `judge_propagation`）、长期未检索条目降权/归档建议；提案只写 `data/review_queue/evolution/<timestamp>/`，绝不直接改记忆层；
- `agent_memory/long_term/evolve/verify.py`：三档验证（boundary 契约核查 / retention 基准 query top-5 diff / safety 安全记忆保护），任一不过即整体否决；
- `agent_memory/long_term/evolve/apply.py`：晋升前快照（`data/snapshots/<timestamp>/`）、应用后审计（`data/logs/evolution_audit.jsonl`）、`rollback(snapshot_id)` 回滚；
- `agent_memory/long_term/evolve/cycle.py`：五步编排（触发 → 定向 → 整合 → 验证 → 修剪）；
- `models.py`：`MemoryEntry` 新增 `retrieval_count` 字段（hybrid 检索命中 +1，写路径近邻检索不计）。

M4b 交付 layer3 评估集 + 进化指标 + 真实验收：

- `evals/datasets/layer3/`：12 条跨会话隐藏关联用例（书中第三层"主动服务"的编程场景改造：
  事实与计划分处不同会话，答对必须主动提示两者的隐藏冲突；每条用例都含 profile 常驻层记忆
  + 检索层细节记忆，rubric.essential 必含"主动提示隐藏关联"项）；
- `evals/runners/metrics.py`：配对统计（McNemar 精确检验 + 配对 bootstrap 增益区间，
  纯函数无 scipy 依赖，样本 < 20 显式标注"不足以下强结论"）；
- `evals/runners/e2e_eval.py` 新增 `--baseline`：同一批用例在空库下配对重跑，输出逐题胜负、
  p 值、留出增益区间；并埋点三个进化指标——激活率（写入记忆被召回比例）、
  遵循率（评委确认判定依据来自召回记忆的用例比例）、留出增益（有记忆 - baseline 分差）；
- 真实验收数字：layer3 有记忆 91.67% vs baseline 0%（McNemar p=0.0010，n=12 仅方向性参考）；
  回归 layer1 100% / layer2 100% / prefix 88.89%；evolve 闭环真实演示（含一次 boundary
  否决与一次 merge 晋升 + 回滚）暴露两处 consolidate 缺陷，见 AGENTS.md 遗留问题。

M5 交付人工复核交互节点 + 强制更新 hook：

- `config.py` 新增 `review_gate`（off / ask / strict，默认 ask：复核队列有积压时 `memory_search` 的
  处置档位）与 `review_turn_interval`（默认 3，hook 的计轮间隔），环境变量
  `AGENT_MEMORY_REVIEW_GATE` / `AGENT_MEMORY_REVIEW_TURN_INTERVAL` 覆盖；
- MCP tool 从五个扩到七个——新增 `memory_review_list`（待办明细）与
  `memory_review_resolve`（approve 原样入库 / modify 改文本过脱敏+评价门后入库 / discard 丢弃）；
  `memory_search` 加复核门（ask 档 blocked 等用户确认后放行，strict 档一律拒读，off 不拦）；
  `memory_add` 返回 `pending_review` 待复核明细；
- 蒸馏 prompt 新增"用户确认资格"硬规则：assistant 单方面提出、用户未明确确认的建议/方案/结论不沉淀；
- `scripts/memory_turn_hook.py`：kimi-code Stop hook，按 session 计轮，每 N 轮拦截本轮结束并
  注入蒸馏指令（材料 = 每轮用户消息 + 紧邻的 assistant 回复），可由使用者注册到宿主配置；克隆仓库本身不会安装 hook。

M6 交付 HTTP 常驻服务 + 作用域纪律：

- `agent_memory/server/http_server.py`：streamable-http 常驻服务，默认只绑 127.0.0.1:8765
  （默认仅供本机访问），在 MCP 端点上叠加 `/SKILL.md`（提示层全文分发）与 `/bootstrap`
  （新 agent 接入引导指令）两个静态路由；对方 agent 一条引导指令即可接入，不再需要复制文件；
- 作用域纪律（共用一套库、多 agent 多项目混用）：SKILL.md 新增 scope 选择规则（共性进 global、
  项目进 repo:<名>、拿不准先问用户），`memory_add` 的 scope 缺省回落 global 但返回附
  `scope_reminder` 提醒；
- Windows 常驻运维：`scripts/start_http_server.cmd` 启动包装脚本（崩溃自动重试最多 3 次，
  3 连败写 `data/state/http_server_FAILED.txt` 失败标记交人工，日志在 `data/logs/http_server.log`）
  + 登录触发的计划任务（注册脚本 `scripts/register_task_s4u.ps1`，需管理员权限运行）。

M7 交付三层记忆（长期 / 工作 / 短期）+ 统一接口：

- 包结构迁移：原 `store/ retrieve/ ingest/ evolve/ adapters/` 五个子包整体迁入
  `agent_memory/long_term/`（逻辑零改动），新增 `working/` 与 `short_term/`；
- `agent_memory/working/`：工作记忆（操作层，当前任务状态——目标/待办/决策/变量/备注，
  每个 scope 一份，存 `data/working/`）。写入是全量替换、只过脱敏不过评价门；
  `turn_watermark` 水位配合 `stale_wm` 判定状态是否滞后；
- `agent_memory/short_term/`：短期记忆 transcript 适配层，把 agent 原生日志
  （如 kimi-code 的 wire.jsonl）解析成干净轮次序列，不新建任何文件；
- MCP tool 从七个扩到十三个：新增 `memory_wm_read` / `memory_wm_write` / `memory_wm_clear`
  （工作记忆读写清）、`memory_context`（常驻画像 + 工作记忆 + 召回 一次组装）、
  `memory_transcript_read`（轮次读取，since_turn 增量）、`memory_session_end`
  （会话收尾：归档 data/raw + 联合蒸馏 + 清理已完成待办，pending 待办 veto）。

