# AGENTS.md — agent-memory 仓库协作规范

本仓库是"本地长期记忆基础设施"项目，agent 中立，支持插件 / MCP / Skill / LangGraph 库四种接入方式。任何人或 LLM agent 在本仓库工作都必须遵守本文件。只是通过插件或 MCP 使用记忆服务（而不是开发本仓库）的 agent，读 `plugins/agent-memory/skills/agent-memory/SKILL.md` 与 `docs/agent-integration.md`，不受本文件约束。

## 三条架构红线（不可违反）

- **D1 数据三层分离**：`data/raw` 原始证据只追加不改写；`data/memory` 是 Markdown 记忆层（唯一事实来源）；`data/index.db` 是可重建派生索引，绝不手改。
- **D2 写入过门**：原始对话不直接入库，必须经脱敏→蒸馏→对账；蒸馏绝不提炼指令性内容。
- **D6 可信根**：`evals/`（MemCompass 运行副本除外）、rubric、发布门槛、审计日志禁止 agent 自行修改。

## 可信根清单（D6 的枚举，禁止 agent 自修改）

- `evals/`（评估数据集与 runner；`evals/memcompass/` 除外，见下一条）、rubric、发布门槛（含 `config.py` 中 `evolve_*` 阈值与 `long_term/evolve/verify.py` 的三档判定逻辑）；
- 不在清单内：`evals/memcompass/`。它是 `docs/research/benchmark-suite/` 的运行副本：改动先在编写源头做，有需要时 agent 直接用 `docs/research/benchmark-suite/tools/migrate_to_evals.py --apply --force --note "<改了什么>"` 同步（逐字节一致，README 更新记录自动追加一行），不在副本里单独改；改评分口径的同步，改前改后的读数不直接比较；
- 审计日志：`data/logs/evolution_audit.jsonl`、`data/logs/propagation.jsonl`——只追加，禁止改写或删除既有记录；
- `data/snapshots/`（整理晋升前的记忆层快照）——回滚依据，禁止改写。

## 工程约定

- **环境管理**：一律用 uv（`uv sync` 装环境、`uv run pytest` 跑测试、`uv run ruff check .` 跑 lint）；不安装系统级 Python 之外的任何东西。宿主用的 `agent-memory` 命令来自 `uv tool install --editable .` 装的独立环境，不占用仓库的 `.venv`；若后台进程是从仓库 `.venv` 启动的（如 `uv run agent-memory daemon serve`），它会锁住 `.venv`，这时 `uv run` 加 `--no-sync`，要 `uv sync` 先停掉它。
- **改 schema 必须同步改测试**：`agent_memory/models.py`、`agent_memory/config.py` 的字段或校验规则变更时，必须同步更新 `tests/` 中对应测试，且 `uv run pytest` 全绿才算完成。
- **fail-closed**：配置非法、校验失败、证据缺失时直接报错，不做静默降级；宁可拒绝服务也不产出不可信结果。
- **fail-closed ≠ fail-lost**：写入路径的内容永不因故障丢失——对话原文先归档 `data/raw` 再蒸馏；评价门拒绝可 `force_review=true` 转人工复核。报错必须区分确定性失败（原样重试无效，给出命中原因）与临时性失败（可重试）。
- **subagent 写入纪律**：记忆库对 subagent 只读——它的结论随结果回传主 agent，由主 agent 策展沉淀；subagent 直接落库只会带进任务局部噪音并加剧对账冲突。三层落地：13 个写类 tool（add / update / forget / feedback / session_end / review_resolve / wm_write / wm_clear / archive_sync / wm_refresh / episode_pack / confirm_resolve / forget_request，均带 `memory_` 前缀）的 description 带"仅限主 agent 调用"约束（agent 中立，随工具走）；`SKILL.md` 第七节给主 agent 派活时附进 subagent prompt 的标准约束语；宿主工具面硬闸（宿主支持按 subagent 裁剪工具时，在其配置里摘掉写类工具）。例外：常驻命名 agent 用 `agent:<名字>` scope。不做服务端按调用方降级——主/subagent 共用同一 MCP 连接，服务端无从区分。
- **代码更新后后台进程要换成新代码**：后台进程按包内代码指纹自动对齐——改了 `agent_memory/` 下的代码，下一次工具调用会先把旧进程停掉重启（前提是宿主用的是可编辑安装，见「环境管理」）。要立即生效或确认状态：`agent-memory daemon stop` / `agent-memory daemon status`。
- **评测纪律**：见下一节「评测驱动改进的工作流程」，改机制之前必须按它走。

## 评测驱动改进的工作流程（必须遵守）

**核心原则：从能力出发，不从题目出发。** 失分要先归到能力框架（`docs/research/agent-memory-capability-framework.md` 的 K1–K13、Q1–Q3）与它的评价标准，再围绕这项能力改机制。盯着某一道失分题去调规则、改提示词、加特判，就是过拟合，一律不做——即使改完那道题能过。

**三类数据，三种用途，不混用：**

- **外部测试集**（LoCoMo、PersonaMem、LongMemEval 等）：默认全部当 test，只做验收；每个来源的首测读数原样记录，不因后续迭代覆盖。能拆出 dev 的来源（如 PersonaMem 的 dev 历史），dev 的使用受以下限制：
  1. **看失分形态只看一次**：首测失分后，dev 只用来查看失分形态，看完即止；不在 dev 上调参、定阈值或试改动。
  2. **复现与调优只在自建验证集上做**：把看到的失分形态搬进自建验证集（`data/dev/`）复现，改机制、验证、定阈值都在自建验证集上完成。
  3. **dev 此后只用于复测**：复测结果不佳时，回到自建验证集继续调优，不回 dev 找新线索。唯一例外是自建验证集没能覆盖 dev 上的全部失分形态——这种情况应当避免；真遇到了，只能再次调整自建验证集，不能改为直接在 dev 上调。
  4. **复测最多两次**：只保存最后一次有效复测的数据。第二次复测比第一次**更好或持平**时，保留第二次的数据、丢弃第一次的；**更差**时，放弃第二轮改动，代码回滚到第一次复测时的状态，只保留第一次复测的数据。
  5. **报告分开写**：首测读数与诊断后复测读数分列，注明该来源已参与诊断；计算"≥2 个独立来源"时，至少一个来源从未用于诊断。
- **自建验证集**（`data/dev/`，`docs/research/benchmark-suite/runners/dev_check.py`）：按能力维度出题的集成测试，可以反复用。
- **内部评测集 MemCompass**（编写源头 `docs/research/benchmark-suite/`，运行副本 `evals/memcompass/`，按需同步）：覆盖公开评测集测不到的治理类能力。

**一个板块（题型、子集或能力维度）明显低于要求、且原因已经明确时，先解决它，再继续评测**，不要等全部测完再一起改。按顺序：

1. **能力维度与评价标准是否涵盖这类失分？** 把失分归到某项能力，再看框架里该能力的指标能不能度量这种失分形态。没涵盖（例如指标太粗、没有分层、缺少配对的护栏指标）→ 先在框架里补指标（写进 §8a 这类补充节，不改已定内容），再往下走。
2. **验证集是否覆盖？** 涵盖了说明是能力本身不够。再看验证集有没有这类题——验证集通过而测试集失分，多半是验证集覆盖不全。补题（或调整已有题），直到**用现有代码能在验证集上复现这类失分**；复现不了就说明还没找准形态，回到失分样本再看，不要带着猜测去改机制。补题的同时补**护栏题**：同一能力的反面（比如补"该拒答"就要补"换了说法但确实能答"），防止改动把原有能力带坏。
3. **针对这项能力改机制。** 没有思路时先查论文或开源项目，借鉴思路、不照抄。机制要能推广到这一类情况，而不是只对上那几道题；阈值只在验证集或 dev 切分上定。
4. **在验证集上验证**：新补的题通过，**所有原有的桶都不劣**，护栏题零错误弃答。任何一桶变差就否决，回到第 3 步。
5. **在测试集上只复测这一板块**：用同一份已缓存的样本（写入与对照组走缓存，只重算受影响的部分），确认提升且不回退。
6. **继续评测其余板块。**

**判断"是否明显低于要求"时要排除噪声：**

- 版本对比或配对对比出现差距时，先确认是不是**单次采样被缓存固化**：同一份输入不走缓存重复采样（N≥5），看决定是否稳定。采样不稳定时，差距多半是运气；这时要处理的是"不稳定"这项能力本身，而不是那几道题。
- 开放式题靠 LLM 评委判分，同义回答可能得到不同判定。不挑单题重判；按原判计分，报告里注明。
- 评测框架本身的缺陷（例如答题模板缺提问日期）要和记忆系统的能力分开：两边同样受影响的，修评测口径、两边同条件重测，并在报告里写明。

**任何修改都不能影响原有能力。** 验证集全量不劣、单元测试全绿是提交的前提；改到 `agent_memory/` 的，用 `agent-memory daemon status` 确认后台进程已换成新代码（`stale` 表示还没重启）。

**准入与设计纪律：** ≥2 个独立来源同向不劣、至少一个显著领先（框架 §6.2 L2），任何一桶显著变差即否决；L3 按框架 §6.2a 的门槛（G1–G6 与各能力主指标、护栏）判定，门槛在读数之前写定、只由用户修改；读路径出现 LLM 调用或默认关闭的机制 = 设计违规。每一轮的失分分析、补的指标与题、改动和读数，记进 `docs/research/memory-v1-design.md`。

**跑评测时：** 一次只跑一件；每个评测进程都带内存看门狗（空闲内存低于 1.5 GB 或系统提交余量低于 3 GB 就结束整棵进程树）；不用 `timeout` 包 `uv run`（杀不到孙进程），中止后按进程树清理并确认无残留；出问题先汇报、再处理，看门狗因内存停下的不自动重跑。

## 架构地图与当前状态

### 数据流

写入：`memory_add`（单条 content 走 脱敏→评价门→对账；conversation_json 走 归档→蒸馏→评价门→对账；distilled_json 走宿主蒸馏——候选照常过 校验/脱敏/评价门/对账，无服务端 LLM 也能用）/ `memory_session_end`（pending todo veto → 归档 → 联合蒸馏（工作记忆快照作参考上下文）→ 清理 done todo）。读取：`memory_search` / `memory_context` / 对外 Search 契约全部走**单一读路径** `MemoryService.recall`（memory-v1，`long_term/retrieve/recall.py`）：记忆命中与原文命中按证据位置归并成证据束、按会话内相邻命中行聚成片段、原话在前、只渲染带原话没有的信息的注解（取代史/失效/出处/事件日期），零 LLM 调用；机制说明见 `docs/design/memory-v1-mechanism.md`；会话开头由宿主 hook（`agent-memory hook wm-inject`）直接读工作记忆文件注入。整理：`agent-memory evolve`（触发→整合提案→三档验证→快照晋升→修剪，提案只写 review_queue/evolution/，绝不直接改记忆层）。

### 分层与包结构

- 长期记忆 `long_term/`：store（Markdown 层 + SQLite 派生索引，sqlite-vec 1024 维 + FTS5 trigram，可全量重建；原文归档索引 `raw_index.db` 另有词级 FTS5 表 `raw_words`，英文 BM25 与朴素 RAG 同口径、中文由 trigram 兜底）、ingest（redact 脱敏 / distill 蒸馏 / gate 评价门 / reconcile 对账 / propagate 变更传播 / review_queue 复核队列）、retrieve（embedder bge-m3 / hybrid 稠密+稀疏→RRF→置信度×时间衰减 / **recall 单一读路径与证据束** / inject `<recalled_memories>` 渲染 / resident 常驻画像）、evolve（trigger / consolidate / verify / apply / cycle）、adapters/langgraph（BaseStore + 17 个 ReAct tool，业务收敛在 MemoryService 薄包装，无 LLM 时 save_memory 无近邻直接 ADD、有近邻进入人工复核）。
- 工作记忆 `working/`：操作层草稿，写入只过脱敏，不过评价门、不做对账、不进索引（TODO 天然是祈使句）；turn_watermark 记"更新到第几轮"，`stale_wm` = current_turn > watermark。
- 短期记忆 `short_term/`：transcript 适配层，不新建文件，解析宿主原生会话日志（每种宿主一个适配器，`detect_adapter` 按路径自动识别，识别不了 fail-closed）。
- 扩展能力模块（接口集中在 `server/service_v2.py`，MemoryService 的混入类；新增字段全部可选，老数据与老渲染不变）：原文归档与检索 `long_term/store/raw_index.py`（派生 `data/raw_index.db`，可由 data/raw 重建）；完整度自评与回读核验（`MemoryEntry.completeness/verify_flag`，`MarkdownStore.patch_meta` 只改注解不动 version/last_verified）；主动浮现 `long_term/retrieve/surface.py`（线索扩展 + 一跳扩散 + 精确率优先的记忆副手，HTTP `POST /surface`、`agent-memory hook surface`）；待确认队列 `agent_memory/confirmations.py`（`data/confirmations/`）；工作记忆整理 `working/refresh.py`；情节卡片 `long_term/ingest/episode.py`；双时态 `valid_from/valid_to/history`（对账 UPDATE 时旧版本折进 history）；来源类型（第三方/工具来源的说法降为 low 进复核）；遗忘请求 `long_term/ingest/forget.py`（D1 有条件例外：只擦指定片段为占位符，审计 `data/logs/forget_audit.jsonl` 只记元数据）。
- 服务入口 `server/`：mcp_server.py（25 个 tool 的定义与 MemoryService 业务层，测试不走传输直接调；`main` 是进程内 stdio 模式，即 `agent-memory mcp --direct`）、stdio_proxy.py（`agent-memory mcp`：工具清单取本地定义，调用转给后台进程，连接失败重新拉起后重试一次）、http_server.py（后台进程本体：无状态 streamable-http，只绑 127.0.0.1:8765，叠加 /health、/surface、/wm_blocks、/SKILL.md、/bootstrap；空闲 `daemon_idle_minutes` 分钟自动退出；LLM 缺失不阻止启动）、daemon.py（生命周期：探活、拉起、停止、代码指纹对齐；端口被非本服务占用时 fail-closed 报错，不杀别人的进程）。
- 宿主 hook `hooks.py`（经 `agent-memory hook wm-inject|surface|turn` 调用）：stdin 按 UTF-8 字节读事件 JSON，全部 fail-open；wm-inject 直接读文件、不依赖后台进程，surface 发现后台进程没在跑只负责拉起、本次放行，turn 纯本地计数且跳过 `stop_hook_active` 的轮次。
- 插件 `plugins/agent-memory/`：`.claude-plugin/plugin.json`（版本与包一致，卫生测试核对）、`.mcp.json`、`hooks/hooks.json`、`skills/agent-memory/SKILL.md`（Skill 唯一源头，打包时复制进包内供 /SKILL.md 读取）；仓库根 `.claude-plugin/marketplace.json` 是插件市场清单。
- 评估 `evals/`：datasets（layer1 基础回忆 20 / layer2 多会话 20 / layer3 隐藏关联 12 / prefix 前缀回归 9）+ runners（recall_eval / e2e_eval / prefix_regression / metrics）。e2e_eval 支持 --baseline 配对模式、--jobs 用例级并发、LLM 磁盘缓存默认开（--no-cache 关）；prefix_regression 无 key 整体跳过，门槛 0.8。另有 `evals/memcompass/`：MemCompass 能力画像评测套件的运行副本（8 个子集 373 条用例 + runner + 校验/体检工具，自带 build 与 naive_rag 运行时依赖，可独立运行），编写源头在 `docs/research/benchmark-suite/`。

### 关键行为语义（改代码前必须知道）

- **评价门（gate.py）**：通过/拒绝/待复核三桶。注入特征任何 memory_type 都拦，原则是"拦指挥模型行为，不拦提及名词"——提示词外泄要动词 + 系统提示词共现（含把字句），裸关键词会误伤文件名/术语引用；开头祈使只拦非 procedural（操作约定天然是祈使句）；low 置信度进复核队列。拒绝原因带命中片段，注明"确定性拦截、原样重试无效"。
- **蒸馏（distill.py）硬规则**：不提炼注入指令；用户明确立的协作约定沉淀为 procedural；状态/安排的变更与撤销必须沉淀；只沉淀用户明确确认过的内容（assistant 单方面建议不沉淀）；可计数的实例逐条沉淀、不写总数。反丢弃式防御：id 过 `normalize_entry_id`、非法 confidence 降 medium、detail 超长截断，仍非法进 review_queue，仅脱敏后 <10 字符才真正丢弃。宿主蒸馏：`get_distill_protocol()` 暴露 prompt/schema 给无 API key 的订阅制 agent，宿主蒸馏产物经 `build_entries_from_distilled()` 走与服务端蒸馏完全相同的 规范化→脱敏 路径（`n_turns=None` 时 evidence_turns 不夹上界），门在服务端、不信任蒸馏来源。
- **对账（reconcile.py）**：两阶段执行——第一阶段并发找近邻，有近邻的候选按批（`DECIDE_BATCH`=8）合并成一次"LLM 判决策"、各批并发，批判定失败或漏判的逐条补判（LLM 调用是耗时大头，MCP 客户端超时不可配，写路径必须自己够快；并发阶段只读，embedder 加锁、index 连接访问经 RLock 串行化），第二阶段按原顺序串行落库与传播；同批候选彼此不可见（同批重复可能都 ADD，由后续对账/进化收敛）。LLM 判 ADD/UPDATE/DELETE/NOOP；UPDATE 继承 version+1 且 supersedes 指旧 id；冲突不强行收敛、进复核队列。无 LLM 降级（`llm=None`）：无近邻直接 ADD，有近邻一律进复核队列（关系判断必须靠 LLM，fail-safe 不猜）；当前 LangGraph 注册工具也经 MemoryService 执行；适配器中保留的旧规则 helper 不代表注册工具的默认行为。UPDATE/DELETE 落库后接变更传播（propagate.py）：三分类 INVALIDATED 删除留审计（data/logs/propagation.jsonl）/ NEEDS_REVISION 进队列 / UNAFFECTED；判定失败一律进队列不错删；只传播一跳不级联。
- **复核队列（review_queue.py）**：文件名为内容哈希（幂等键，重复排队覆盖同一文件）；读侧 list/load/delete 白名单校验防路径穿越；raw_record 类待办不能直接入库。复核门：`review_gate` = off/ask/strict（默认 ask），积压时 memory_search 按档处置，ask 等 `acknowledge_pending=true` 放行。`memory_add` 返回 `pending_review` 待复核明细，调用方必须向用户报告。
- **scope 纪律**：global / repo:<项目名> / agent:<名字>；`normalize_scope` 读写同口径归一化（`repo:llm_wiki` → `repo:llm-wiki`），归一化后仍非法当场报错，绝不静默返回空；memory_add 缺省回落 global 并附 scope_reminder。
- **LLM（llm.py）**：缺 key 构造即报错；JSON 解析失败重试 1 次后 fail-closed；超时/重试上限配置化（`llm_timeout_seconds` 默认 300 / `llm_max_retries` 默认 2，`AGENT_MEMORY_LLM_TIMEOUT_SECONDS` / `AGENT_MEMORY_LLM_MAX_RETRIES` 覆盖）；可选磁盘缓存 key=sha256(model+kind+system+user)，临时文件+os.replace 并发安全。
- **记忆条目（models.py）**：content 原子句 + 可选 detail（≤800 字符完整段落）+ `event_date`（事件实际发生日期，蒸馏按会话日期换算，content 里原说法保留、日期括号附后）+ `cues`（线索词，只进 `index_text`、永不渲染）；索引用 content+detail+cues，渲染只用 content；`retrieval_count` 只在 `track_retrieval=True` 的检索命中时 +1（写路径近邻检索不计数）。
- **进化闭环（evolve/）**：三档验证（boundary/retention/safety）各自一票否决，LLMError 按不通过 fail-closed；apply 只执行 merge/invalidate/downgrade/archive，conflict/revise 留人工；晋升前快照 `data/snapshots/`，`rollback` 可恢复；离线复核用语义独立的 `judge_validity()`（默认成立，明确证据才判失效，不复用 judge_propagation——前者回答"这条本身还成立吗"，后者回答"撤销牵连谁"）。

### 运维（按需启动的后台进程）

- 配置：`~/.agent-memory/config.env`（`AGENT_MEMORY_CONFIG` 改路径），`KEY=VALUE`，环境变量优先；格式非法报出文件与行号（fail-closed），hook 遇到配置错误则放行。后台进程与 hook 不继承交互 shell 的环境，长期配置（数据目录、LLM key）必须写进这个文件。
- 拉起：Windows 上经 WMI（`Win32_Process.Create`）创建 pythonw 进程，脱离宿主的进程树与作业对象，会话结束后仍存活；WMI 进程拿不到调用方环境，相关变量写进一次性文件 `state/daemon_env.json`，后台进程读完即删。启动在 `state/daemon.lock` 下串行化。
- 日志 `<数据目录>/logs/daemon.log`；状态以 `agent-memory daemon status` 为准（running / stale / stopped / foreign）。
- 调试：`AGENT_MEMORY_HOOK_DEBUG=1` 时每个 hook 把事件 payload 追加到 `logs/hook_debug_<名字>.jsonl`。

### 版本、基线与评测

- 历史交付见 `docs/history/milestones.md`（M0–M9）与 `docs/CHANGELOG.md`；现行读写机制说明 `docs/design/memory-v1-mechanism.md`，设计推导与逐轮实测 `docs/research/memory-v1-design.md`。
- 当前测试基线：`uv run pytest -q` 875 passed + 1 skipped，`uv run ruff check .` 干净（`docs/research/**` 只放宽行长）。
- 版本与变更记录：版本号在 `pyproject.toml`、`agent_memory/__init__.py` 的 `__version__`、`docs/CHANGELOG.md` 顶部条目三处一致（卫生测试核对，插件清单的版本也须一致）；当前 v0.4.0。行为有变化的改动在 CHANGELOG 顶部条目加一行。
- 评测：MemCompass v0.3（8 个子集 373 条用例，用户已整体签核）。编写源头 `docs/research/benchmark-suite/`（构造脚本 `build/`、数据卡、核验台、设计文档），运行副本 `evals/memcompass/`（同步规则见「可信根清单」）。正式报告 `results/2026-09-16-v03-report.md`。

### data/ 目录

运行时数据目录，gitignored：`data/raw`（原始证据，只追加）、`data/memory`（Markdown 记忆层）、`data/working`（工作记忆操作层，不归 D1 三层）、`data/review_queue`（含 evolution/ 提案子目录）、`data/snapshots`、`data/state`、`data/logs`。评测数据：`data/dev/`（自建验证集）、`data/external/<来源>/`（外部测试集原始文件与抽样），运行结果在 `data/logs/aml_selftest/<run-id>/`。
