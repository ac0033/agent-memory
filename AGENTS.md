# AGENTS.md — agent-memory 仓库协作规范

本仓库是"本地长期记忆基础设施"项目，agent 中立，支持 LangGraph 库接入 / MCP / Skill 三种接入方式。任何人或 LLM agent 在本仓库工作都必须遵守本文件。

## 三条架构红线（不可违反）

- **D1 数据三层分离**：`data/raw` 原始证据只追加不改写；`data/memory` 是 Markdown 记忆层（唯一事实来源）；`data/index.db` 是可重建派生索引，绝不手改。
- **D2 写入过门**：原始对话不直接入库，必须经脱敏→蒸馏→对账；蒸馏绝不提炼指令性内容。
- **D6 可信根**：`evals/`、rubric、发布门槛、审计日志禁止 agent 自行修改。

## 可信根清单（D6 的枚举，禁止 agent 自修改）

- `evals/`（评估数据集与 runner）、rubric、发布门槛（含 `config.py` 中 `evolve_*` 阈值与 `long_term/evolve/verify.py` 的三档判定逻辑）；
- 审计日志：`data/logs/evolution_audit.jsonl`、`data/logs/propagation.jsonl`——只追加，禁止改写或删除既有记录；
- `data/snapshots/`（整理晋升前的记忆层快照）——回滚依据，禁止改写。

## 工程约定

- **环境管理**：一律用 uv（`uv sync` 装环境、`uv run pytest` 跑测试、`uv run ruff check .` 跑 lint）；不安装系统级 Python 之外的任何东西。
- **改 schema 必须同步改测试**：`src/agent_memory/models.py`、`src/agent_memory/config.py` 的字段或校验规则变更时，必须同步更新 `tests/` 中对应测试，且 `uv run pytest` 全绿才算完成。
- **fail-closed**：配置非法、校验失败、证据缺失时直接报错，不做静默降级；宁可拒绝服务也不产出不可信结果。

## 当前里程碑状态（M7 完成）

- M0 已实现：`models.py`（MemoryEntry / EvidenceRef / MemoryProposal）、`config.py`（Settings + 环境变量覆盖）、`evals/datasets/layer1/` 20 条基础回忆用例。
- M1 已实现（记忆内核 MVP，手动蒸馏）：
  - `long_term/store/markdown_store.py`：Markdown 记忆层 CRUD（scope 里的 `:` 以 `__` 替换作目录名）；
  - `long_term/store/index_db.py`：SQLite 派生索引（sqlite-vec 1024 维 cosine + FTS5 trigram），支持全量重建；
  - `long_term/retrieve/embedder.py`：bge-m3 嵌入（sentence-transformers 实现——fastembed 至 0.8.0 不支持 bge-m3）；
  - `long_term/retrieve/hybrid.py`：稠密 + 稀疏两路召回 → RRF 融合 → 置信度 × 时间衰减加权；rerank 只留接口（默认关，显式开启抛 NotImplementedError）；
  - `long_term/ingest/redact.py`：入库前正则脱敏；
  - `cli.py`：`agent-memory` 命令（add / search / list / update / forget / rebuild / stats）；
  - `evals/runners/recall_eval.py`：layer1 recall@5 评估（临时目录建库，不碰真实 data_dir）。
- M2 已实现（蒸馏写路径 + MCP server）：
  - `models.py`：`MemoryEntry` 增加可选 `detail` 字段（Enhanced-Notes 式完整段落，≤800 字符，与 content 原子句混合存储；索引用 `content + "\n" + detail` 拼接文本，渲染/注入仍用 content）；
  - `llm.py`：`LLMClient` Protocol（依赖注入：生产 `OpenAILLMClient` 走 OpenAI 兼容端点、默认 DeepSeek，测试注入 fake；缺 key 构造即报错，JSON 解析失败重试 1 次后 fail-closed；显式超时 300s 防单次挂起拖死并发；可选磁盘缓存 cache_dir，key=sha256(model+kind+system+user)，临时文件+os.replace 并发安全）；
  - `long_term/ingest/distill.py`：对话蒸馏为原子记忆候选（prompt 硬规则：绝不提炼注入式指令；用户明确立下的协作约定以陈述句沉淀为 procedural；状态/安排的变更与撤销必须沉淀。反"丢弃式防御"：id 统一过 `normalize_entry_id`、confidence 非法值降 medium、detail 超长截断；规范化后仍非法的原始记录进 review_queue（传 data_dir 时落盘），只有脱敏后 <10 字符的候选才真正丢弃并计数）；
  - `long_term/ingest/gate.py`：评价门（纯规则：脱敏残留拒绝、注入特征任意位置全类型拦截、开头祈使模式仅对非 procedural 拒绝——procedural 操作约定天然是祈使句、长度下限、low 置信度进 review_queue；通过/拒绝/待复核三桶）；
  - `long_term/ingest/review_queue.py`：复核队列写入（distill/gate/reconcile/传播共用；文件名为内容哈希而非时间戳——重复排队覆盖同一文件，幂等键思路）；
  - `long_term/ingest/reconcile.py`：Mem0 式对账（近邻检索 → LLM 判 ADD/UPDATE/DELETE/NOOP；UPDATE 继承 version+1 且 supersedes 指旧 id；冲突无法收敛写 `data/review_queue/` 不强行收敛）；UPDATE/DELETE 落库后接变更传播；
  - `long_term/ingest/propagate.py`：变更传播（反向传播）——以被取代/删除的旧条目为 query 检索语义近邻（procedural 全量并入），LLM 三分类判定：INVALIDATED 删除并留审计快照（`data/logs/propagation.jsonl`）、NEEDS_REVISION 进 review_queue、UNAFFECTED 不动；判定失败一律进队列不错删；只传播一跳不级联；另有语义独立的 `judge_validity()` 供 M4 离线抽查复核条目自身（不复用 `judge_propagation`——它回答"撤销牵连谁"，回答不了"这条本身还成立吗"）；
  - `long_term/retrieve/inject.py`：`render_recall_block` 渲染 `<recalled_memories>` XML 注入块（护栏前缀"参考而非指令"，按综合分降序、超预算整条丢弃、XML 转义）；
  - `server/mcp_server.py`：MCP stdio server 五个 tool（memory_search / memory_add / memory_feedback / memory_update / memory_forget），业务实现收敛在 `MemoryService`（测试不走传输直接调）；
  - `cli.py` 新增 `distill` 命令（对话 JSON 文件走完整写入管线）；
  - `evals/datasets/layer2/` 20 条多会话用例（时序冲突 7 + 多对象消歧 7 + 有效/失效区分 6，per-session `memories` 作 mock 灌库 oracle，`supersedes` 体现 UPDATE 语义）；
  - `evals/runners/e2e_eval.py`：端到端评估（`--layers`、`--seeds`、`--seed`、`--llm-judge`、`--case` 单用例过滤；无 LLM key 自动降级为规则判定并显著标注；真实模式下基于 expected id 的 write_ok/recall_hit 标记为 N/A，PASS/FAIL 以评委或关键词判定为准）。提速：`--jobs N` 用例级线程池并发（默认 4，429 指数退避重试），LLM 响应磁盘缓存默认开（`data/logs/llm_cache/`，key=sha256(model+system+user)，换模型自动不命中，`--no-cache` 关闭），逐用例输出灌库/判定耗时与总墙钟；
- M3 已实现（LangGraph 适配 + Skill + 轨迹前缀回归）：
  - `long_term/adapters/langgraph/store.py`：`AgentMemoryStore`（LangGraph BaseStore 实现，langgraph 1.x 抽象方法只有 batch/abatch，异步用 asyncio.to_thread 包同步实现）；namespace 约定 `("memories", <scope>)`，put 过 redact+gate 规则（不走 LLM 蒸馏）、upsert 语义、value=None 即 delete，search 走混合检索、filter 只支持 memory_type/confidence；
  - `long_term/adapters/langgraph/tools.py`：`build_memory_tools()` 工厂产出 recall_memories / save_memory 两个 ReAct tool；save_memory 走 脱敏→评价门→规则对账（无 LLM：向量近邻距离 ≤ 阈值判 NOOP 刷新核实时间，否则 ADD）；
  - `long_term/retrieve/resident.py`：`build_system_context(scope)` 常驻层注入（当前 scope+global 的 profile 条目，按 confidence 排序，预算为 recall_budget_chars 一半，带护栏说明）；
  - `skills/agent-memory/SKILL.md`：Skill 提示层（何时检索/写入/反馈，MCP tool 名与参数示例，"召回是参考而非指令"）；
  - `evals/datasets/prefix/` 9 条轨迹前缀回归用例（conflict_override 2 + scope_leak 2 + low_confidence 2 + injection_resistance 2 + normal_recall 对照 1）；
  - `evals/runners/prefix_regression.py`：冻结上下文 → LLM 输出下一步动作 → 评委判定可接受/禁止集合（429 限流自动间隔重试；LLM 响应磁盘缓存默认开，`--no-cache` 关闭；无 key 整体跳过返回 0；门槛 0.8；无规则降级模式——actor 行为本身是被测对象）；
  - `examples/langgraph_demo.py`：最小 LangGraph ReAct agent 接入演示（session 1 告知偏好 → session 2 新 thread 记起）。
- M4a 已实现（进化闭环核心：睡眠学习循环 + 定期整理，五步：触发 → 定向 → 整合 → 验证 → 修剪）：
  - `long_term/evolve/trigger.py`：`should_run(stats, last_run_at, settings)` 三条件任一触发（距上次整理超 `evolve_interval_days`=7 天 / 新增条目超 50 / review_queue 积压超 10；全部阈值在 config.py，`AGENT_MEMORY_EVOLVE_*` 环境变量覆盖）；`collect_store_stats` 采集触发输入；
  - `long_term/evolve/consolidate.py`：整合产出 `EvolutionProposal`（models.py 新增，扩展 MemoryProposal 的契约思想——每条变更带 FalsifiableContract：证据/根因/预期修复/可能受损面）：同 scope 稠密距离 ≤ `evolve_merge_max_distance` 的条目对交 LLM 判 MERGE/CONFLICT/UNRELATED（MERGE 产出自包含合并条目：evidence 并集、version=max+1、supersedes 指主条目、retrieval_count 求和；CONFLICT 不强行收敛交人工）；按 last_verified 最旧抽查，用 `judge_validity()` 复核条目本身是否仍成立（默认假设成立，明确证据——被新条目取代/与现存条目矛盾/时间条件已过期——才判 invalidate，证据不足降级 revise 交人工；不复用传播判定，见 postmortem 缺陷 3）；retrieval_count==0 且创建超 stale_days 的条目建议 downgrade（非 low 降一档）/ archive（low）；LLM 合并产出先 normalize_entry_id 规范化再校验，仍非法降级为 revise 交人工（postmortem 启示 5）。**提案只写 `data/review_queue/evolution/<timestamp>/`，绝不直接改记忆层**；
  - `long_term/evolve/verify.py`：三档验证各自一票否决（不可平均分绕过）——boundary（LLM 核查提案是否覆盖其契约声称修复的问题场景；变更分两类评估：自动应用类 merge/invalidate/downgrade/archive 核查契约达成，转人工类 conflict/revise 只核查标记准确性与证据充分性、标记准确即通过，"矛盾未解决"不是否决理由；LLMError 按不通过 fail-closed）、retention（提案应用到临时副本，提案未涉及条目不得掉出基准 query 的 top5）、safety（自动应用的变更不得触碰 content 含"安全/safety"的记忆；conflict/revise 是人工裁决项不算触碰）；
  - `long_term/evolve/apply.py`：晋升前快照 `data/snapshots/<timestamp>/memory`；apply 只执行 merge/invalidate/downgrade/archive（conflict/revise 跳过）；应用后追加审计 `data/logs/evolution_audit.jsonl`（提案 id、三档结果、应用时间、快照路径）；`rollback(snapshot_id)` 从快照恢复并重建索引（回滚前现场挪 pre_rollback_memory 保留）；verify 未通过时 apply 直接 raise（fail-closed）；
  - `long_term/evolve/cycle.py`：`run_evolution_cycle(settings, llm, ...)` 编排五步；验证结果（无论通过与否）写提案同目录 `verdict.json`；否决的提案留在 review_queue/evolution/ 交人工且不改记忆层、不刷新水位；运行水位记 `data/state/evolution_state.json`；
  - `models.py`：`MemoryEntry` 新增 `retrieval_count: int = 0`（hybrid 检索 `track_retrieval=True` 命中时 +1，CLI search 与 MCP memory_search 已接上；写路径近邻检索不计数）；`MarkdownStore.increment_retrieval_count` 只改计数不动 version/last_verified；
  - `cli.py` 新增 `evolve [--dry-run] [--scope]`（dry-run 只到提案为止，打印提案摘要）。
- 空包占位（后续里程碑实现）：无（evolve/ 已由 M4a 实现）。
- M4b 已实现（layer3 评估集 + 进化指标 + 真实验收）：
  - `evals/datasets/layer3/`：12 条跨会话隐藏关联用例（category=hidden_association，事实与计划分处不同会话，答对必须主动提示隐藏冲突；每条含 ≥1 条 profile 常驻层记忆 + ≥1 条检索层细节记忆，rubric.essential 必含"主动提示"项；结构校验见 tests/test_eval_datasets.py）；
  - `evals/runners/metrics.py`：配对统计纯函数（McNemar 精确检验 = 二项双侧，配对 bootstrap 95% CI，样本 < 20 标 small_sample 只作方向性参考）；
  - `evals/runners/e2e_eval.py`：`--baseline` 配对模式（同批用例空库重跑，逐题胜负 + p 值 + 留出增益区间，样本不足不并入退出码）；layer3 的判定上下文 = build_system_context 常驻画像块 + 召回块（双层配合）；检索带 track_retrieval=True 以支持激活率埋点；评委 schema 新增 used_memories（遵循率埋点）；LAYER_THRESHOLDS 加 layer3 0.8；
  - 真实验收（DeepSeek 实跑，2026-08-20）：layer3 有记忆 91.67%（11/12，layer3-02 评委判 essential 未覆盖）vs baseline 0%，McNemar p=0.0010，留出增益 +91.67% CI [+75%, +100%]（n=12 仅方向性）；激活率 100%（单用例库条目少、top5 几乎全覆盖，指标解释力有限）、遵循率 100%（n=12）；回归 layer1 100% / layer2 100% / prefix 88.89%（prefix-08 抗注入未过，门槛 80% 达标）；
  - evolve 闭环真实演示：混合问题库（近似重复对 + 冲突对 + 过期条目）跑 `agent-memory evolve` → 提案 5 条变更 → boundary 否决 → 未改记忆层、提案留 review_queue/evolution/；纯重复对场景 → merge 提案三档全过 → 晋升（快照 + 审计日志）→ `rollback(snapshot_id)` 恢复两条原始条目（回滚前现场留 pre_rollback_memory）。2026-08-20 修复缺陷 3/4 后复跑：混合库（正常旧条目 + 冲突对 + 重复对）→ conflict + merge 提案三档全过 → merge 晋升、conflict 留人工、正常旧条目不再被误判失效；纯重复对 merge → 晋升 → rollback 恢复正常。
- M4b 真实演示暴露的既有缺陷（~~未修~~ → 已于 2026-08-20 修复，复盘见 `docs/m2-defect-postmortem.md` 缺陷 3/4）：
  1. ~~consolidate 的离线复核把 `judge_propagation(old=entry, new=None, neighbor=entry)` 指向条目自身，prompt 告诉 LLM"旧事实已被删除"，导致被抽查条目恒判 INVALIDATED（正常记忆"仓库使用 Python 3.12"也被建议删除）~~ ——已改为语义独立的 `judge_validity()`（默认成立、明确证据才判失效、证据不足交人工）；
  2. ~~boundary 档会因为提案含 conflict 人工裁决项（"矛盾未在提案中解决"）而否决整份提案，含 conflict 的提案可能永远无法自动晋升~~ ——boundary prompt 已明确两类变更的评估标准：自动应用类核查契约达成，conflict/revise 转人工类只核查标记准确性与证据充分性，标记准确即通过。
- M5 已实现（人工复核交互节点 + 强制更新 hook）：
  - `config.py` 新增 `review_gate`（off/ask/strict，默认 ask；复核队列积压时 memory_search 的处置档位）与 `review_turn_interval`（默认 3，hook 计轮间隔），`AGENT_MEMORY_REVIEW_GATE` / `AGENT_MEMORY_REVIEW_TURN_INTERVAL` 环境变量覆盖；
  - `long_term/ingest/review_queue.py` 补读侧：`list_review_queue`（只列直接子级 *.yaml，按 queued_at 升序，损坏文件标 unreadable 不静默跳过）、`load_review_item` / `delete_review_item`（文件名白名单校验，拒路径穿越，fail-closed）；
  - `server/mcp_server.py`：tool 从五个扩到七个——新增 `memory_review_list`（待办明细）与 `memory_review_resolve`（approve 原样入库 / modify 改文本过脱敏+评价门后入库 / discard 丢弃；人工裁决刷新 last_verified、裁决成功删队列文件；raw_record 类待办不可直接入库）；`memory_search` 加复核门（ask 档 blocked 等用户确认后 `acknowledge_pending=true` 放行，strict 档一律拒读，off 不拦；返回统一带 status / pending_review_count）；`memory_add` 返回新增 `pending_review` 待复核明细（蒸馏非法产出 + 规则门低置信度 + 对账排队三类）；`memory_add` 的 `conversation_json` 兼容数组输入（收到 list 自动 json.dumps 序列化——LLM 调工具把数组当原生对象传出是常见错误；其他类型报 ValueError 并提示正确格式，非法 JSON 字符串经 parse_conversation_json 包装为带格式提示的 ValueError）；
  - `long_term/ingest/distill.py` 蒸馏 prompt 新增第 6 条硬规则"用户确认资格"：assistant 单方面提出、用户未明确确认的建议/方案/结论不沉淀（原第 6 条顺延为第 7 条）；
  - `scripts/memory_turn_hook.py`：kimi-code Stop hook，按 session 计轮（`<data_dir>/state/turn_counter.json`），每 N 轮以退出码 2 拦截本轮结束并注入蒸馏指令（材料 = 每轮用户消息 + 紧邻的 assistant 回复；指令含确认资格与 pending_review 报告要求）；启动时把 stdout/stderr 重配为 UTF-8（Windows 上管道捕获默认走 GBK 区域编码，宿主按 UTF-8 读取，不重配中文指令会乱码）；按宿主惯例 fail-open；已注册进用户级 `~/.kimi-code/config.toml`（对所有项目会话生效）；
  - `skills/agent-memory/SKILL.md` 新增"人工复核交互"一节：节点一（写入后报告 pending_review 并请用户裁决）、节点二（复核门 blocked 时 ask/strict 两档的处理流程）、hook 指令的响应方式。
- M6 已实现（HTTP 常驻服务 + 作用域纪律）：
  - `server/http_server.py`：streamable-http 常驻服务（`uv run python -m agent_memory.server.http_server`），默认只绑 127.0.0.1（`http_host`/`http_port` 配置项，`AGENT_MEMORY_HTTP_*` 覆盖，回环地址天然免鉴权）；用 MCPServer.custom_route 叠加 `/SKILL.md`（提示层全文分发）与 `/bootstrap`（新 agent 接入引导指令）两个静态路由；LLM 缺失不阻止启动（与 stdio 入口一致）；
  - 作用域纪律（共用一套库、多 agent 多项目混用）：SKILL.md 新增 scope 选择规则（共性进 global、项目进 repo:<名>、拿不准先问用户、无人值守默认当前项目）；`memory_add` 的 scope 缺省回落 global 但返回附 `scope_reminder` 提醒；scope 读写同口径归一化（`models.py` 的 `normalize_scope`：去空白转小写、slug 非法字符段折叠为连字符，`repo:llm_wiki` → `repo:llm-wiki`），memory_search 与 memory_add 都过它；归一化后仍非法的 scope 当场报错 fail-closed，绝不静默返回空结果（MemoryEntry 校验器保持严格不变，归一化只做在服务入口）；
  - 接入方式：对方 agent 只需一条引导指令（注册 http://127.0.0.1:8765/mcp + 读取 /SKILL.md），不再需要复制文件；
  - `tests/test_http_server.py`：TestClient 直连 Starlette 应用（base_url 须用真实监听地址——MCP 传输层校验 Host 头防 DNS rebinding），覆盖静态路由 + initialize/tools/list/tools/call 全握手 + 复核门跨 HTTP 生效。
- M6 运维（Windows 计划任务常驻）：
  - `scripts/start_http_server.cmd`：启动包装脚本（**必须纯 ASCII**——cmd.exe 按 GBK 读 .cmd，UTF-8 中文注释会乱码并破坏解析）；启动时从 `D:\4_Projects\.env` 读 `DEEPSEEK_API_KEY` 映射为 `AGENT_MEMORY_LLM_API_KEY`（PowerShell 解析，兼容等号两侧空格与 CRLF 行尾）；env 设置块在 `:retry` 标签之前，重试不会重读 .env，改 key 需整体重启任务；重试逻辑内置在脚本里而非依赖计划任务的 restart-on-failure（后者对手动启动的任务不可靠）：异常退出等 RETRY_SECONDS（默认 60，ping trick 代替 timeout——无控制台时 timeout 报错）后拉起，最多 3 次；3 连败写 `data/state/http_server_FAILED.txt` 失败标记交人工；每次尝试开头清标记；日志在 `data/logs/http_server.log`；
  - 计划任务 `AgentMemoryHttpServer`：登录时触发（AtLogOn，**S4U 后台模式**——完全无窗口；S4U 注册需管理员权限，注册脚本 `scripts/register_task_s4u.ps1` 需提权运行且**必须纯 ASCII**——PowerShell 5.1 按 ANSI 读无 BOM 的 .ps1，非 ASCII 会损坏解析），失败重试设置仅作兜底（真实重试在脚本内）；
  - 实测验证：3 连败→写 flag 放弃、崩溃→自动重试→服务恢复且 flag 自动清除，两条路径均端到端通过；运维注意：`schtasks /end` 只杀 cmd 包装进程，python 孙进程会成孤儿残留并继续占用端口（S4U 会话的进程普通 shell 无权 taskkill，需管理员终端 `taskkill /PID <pid> /F` 后再 `schtasks /run`）。
- M7 已实现（三层记忆：长期 / 工作 / 短期 + 统一接口）：
  - 包结构迁移：原 `store/ retrieve/ ingest/ evolve/ adapters/` 五个子包整体迁入 `src/agent_memory/long_term/`（逻辑零改动，import 全仓库更新）；新增 `working/`（工作记忆）与 `short_term/`（transcript 适配层）；`config.py`/`llm.py`/`models.py`/`server/`/`cli.py` 位置不变；
  - M7a 工作记忆（操作层，当前任务状态）：`working/models.py`（`WorkingMemory`/`TodoItem`：goal、decisions、variables、todos(pending|done)、notes、turn_watermark、version、updated_at）；`working/store.py`（存 `data/working/<scope目录名>.md`，frontmatter 全量 dump 是唯一事实来源、正文仅供人翻看，全量替换语义、version 自增，scope 目录名映射复用长期记忆层的 scope_to_dirname）；`working/render.py`（注入块渲染，护栏行"参考而非指令"写死在渲染层，预算 `working_memory_budget_chars` 默认 1000、`AGENT_MEMORY_WORKING_MEMORY_BUDGET_CHARS` 覆盖，超预算整条丢弃）。关键取舍：工作记忆是操作层草稿——写入只过脱敏，不过评价门、不做对账（TODO 天然是祈使句，过不了评价门），不进向量索引、不进进化循环；`turn_watermark` 记"本份状态已更新到第几轮"，`is_stale` 判定 current_turn > watermark 即可能滞后；
  - M7b 短期记忆（transcript 适配层，不新建任何文件，载体是宿主原生日志）：`short_term/adapter.py`——`Turn`（turn_index/role/tool_name/content/ts）、`TranscriptAdapter` Protocol、`KimiCodeWireAdapter`（解析 kimi-code wire.jsonl：`context.append_message` 取 user 且归"即将到来的那一轮"——用户消息本身不带 turnId；`loop_event` 的 `content.part` type=text 聚合为 assistant、think 跳过；`tool.call`/`tool.result` 按 toolCallId 配对，output 截断 2000 字符；坏行跳过，文件不存在 fail-closed）、`ADAPTERS` 注册表、`detect_adapter`（文件名 wire.jsonl 自动命中，识别不了要求显式指定，不瞎猜）；
  - `server/mcp_server.py`：tool 从七个扩到十三个——`memory_wm_read`（读 + 渲染块 + stale_wm 判定）/ `memory_wm_write`（全量替换非合并，todos 兼容纯字符串列表按 pending）/ `memory_wm_clear`（幂等，不存在返回 already empty）/ `memory_context`（统一组装：常驻画像块 → 工作记忆块 → 召回块，复核门 blocked 原样透出，返回 {status, block, sections, stale_wm, pending_review_count}）/ `memory_transcript_read`（干净轮次序列，`since_turn` 增量语义=只返回水位之后的轮次）/ `memory_session_end`（会话收尾编排，顺序固定：pending todo veto（force=true 放行）→ 归档 `data/raw/<source>/<session_id>.jsonl` 只追加不改写 → 联合蒸馏（工作记忆快照渲染后作 extra_context 注入 distill user prompt，蒸馏七条硬规则未动；log_path 路径剔除 tool 轮次与空内容轮）→ 清理 done todo（pending 保留，全空时 wm_hint 提示可 wm_clear、不自动清）；llm 缺失降级 `archived_only` 且不动工作记忆）；
  - 与框架文档的对齐结论：记忆类型 / 作用域 / 冲突更新语义与框架兼容保留；框架要求的归档缺口已由 session_end 补齐；写入时机 = 滚动蒸馏（hook 保底防崩溃丢失）+ session_end（标准收尾：归档 + 联合蒸馏 + 清理）双轨；框架术语"项目（作用域）"对应实现的 `repo:<slug>`；
  - 验收：`uv run ruff check .` 干净；`uv run pytest -q` 535 passed。
- `data/` 是运行时数据目录，gitignored，结构为 `data/raw`、`data/memory`、`data/working`（工作记忆操作层，不归 D1 三层）、`data/review_queue`（含 `evolution/` 提案子目录）、`data/snapshots`、`data/state`、`data/logs`。
