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
- **改 schema 必须同步改测试**：`agent_memory/models.py`、`agent_memory/config.py` 的字段或校验规则变更时，必须同步更新 `tests/` 中对应测试，且 `uv run pytest` 全绿才算完成。
- **fail-closed**：配置非法、校验失败、证据缺失时直接报错，不做静默降级；宁可拒绝服务也不产出不可信结果。
- **fail-closed ≠ fail-lost**：写入路径的内容永不因故障丢失——对话原文先归档 `data/raw` 再蒸馏；评价门拒绝可 `force_review=true` 转人工复核。报错必须区分确定性失败（原样重试无效，给出命中原因）与临时性失败（可重试）。
- **subagent 写入纪律**：记忆库对 subagent 只读——它的结论随结果回传主 agent，由主 agent 策展沉淀；subagent 直接落库只会带进任务局部噪音并加剧对账冲突。三层落地：13 个写类 tool（8 个基础 + v0.2 的 archive_sync / wm_refresh / episode_pack / confirm_resolve / forget_request）的 description 带"仅限主 agent 调用"约束（agent 中立，随工具走）；`SKILL.md` 第七节给主 agent 派活时附进 subagent prompt 的标准约束语；宿主工具面硬闸（kimi-code 是 `agents/coder.md` 覆盖文件，`override: true` + `disallowedTools` 摘掉写类工具，由 `scripts/install_kimi_code.sh` 安装）。例外：常驻命名 agent 用 `agent:<名字>` scope。不做服务端按调用方降级——主/subagent 共用同一 MCP 连接，服务端无从区分。
- **代码更新后默认重启 HTTP 常驻服务**：改了 `agent_memory/` 下任何代码，默认动作就是重启 `AgentMemoryHttpServer` 计划任务，否则 8765 端口跑的还是旧代码。步骤：`netstat -ano | findstr 8765` 查 LISTENING 的 PID → 管理员终端 `taskkill /PID <pid> /F`（注意：`schtasks /end` 只杀 cmd 包装进程，python 孙进程会成孤儿残留继续占端口；S4U 会话的进程普通 shell 无权 kill，必须提权）→ `schtasks /run /tn AgentMemoryHttpServer` → 确认 8765 重新 LISTENING。
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
- **内部评测集 MemCompass**（`evals/memcompass/`，D6）：覆盖公开评测集测不到的治理类能力。

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

**任何修改都不能影响原有能力。** 验证集全量不劣、单元测试全绿是提交的前提；改到 `agent_memory/` 的，还要按「工程约定」里的步骤重启常驻服务。

**准入与设计纪律：** ≥2 个独立来源同向不劣、至少一个显著领先（框架 §6.2 L2），任何一桶显著变差即否决；L3 按框架 §6.2a 的门槛（G1–G6 与各能力主指标、护栏）判定，门槛在读数之前写定、只由用户修改；读路径出现 LLM 调用或默认关闭的机制 = 设计违规。每一轮的失分分析、补的指标与题、改动和读数，记进 `docs/research/memory-v1-design.md`。

**跑评测时：** 一次只跑一件；每个评测进程都带内存看门狗（空闲内存低于 1.5 GB 或系统提交余量低于 3 GB 就结束整棵进程树）；不用 `timeout` 包 `uv run`（杀不到孙进程），中止后按进程树清理并确认无残留；出问题先汇报、再处理，看门狗因内存停下的不自动重跑。

## 架构地图与当前状态（M9 完成）

### 数据流

写入：`memory_add`（单条 content 走 脱敏→评价门→对账；conversation_json 走 归档→蒸馏→评价门→对账；distilled_json 走宿主蒸馏——候选照常过 校验/脱敏/评价门/对账，无服务端 LLM 也能用）/ `memory_session_end`（pending todo veto → 归档 → 联合蒸馏（工作记忆快照作参考上下文）→ 清理 done todo）。读取：`memory_search` / `memory_context` / 对外 Search 契约全部走**单一读路径** `MemoryService.recall`（memory-v1，`long_term/retrieve/recall.py`）：记忆命中与原文命中按证据位置归并成证据束、按会话内相邻命中行聚成片段、原话在前、只渲染带原话没有的信息的注解（取代史/失效/出处/事件日期），零 LLM 调用；机制说明见 `docs/design/memory-v1-mechanism.md`；会话开头由宿主 hook 拉 `/wm_blocks` 自动注入工作记忆（M9）。整理：`agent-memory evolve`（触发→整合提案→三档验证→快照晋升→修剪，提案只写 review_queue/evolution/，绝不直接改记忆层）。

### 分层与包结构

- 长期记忆 `long_term/`：store（Markdown 层 + SQLite 派生索引，sqlite-vec 1024 维 + FTS5 trigram，可全量重建；原文归档索引 `raw_index.db` 另有词级 FTS5 表 `raw_words`，英文 BM25 与朴素 RAG 同口径、中文由 trigram 兜底）、ingest（redact 脱敏 / distill 蒸馏 / gate 评价门 / reconcile 对账 / propagate 变更传播 / review_queue 复核队列）、retrieve（embedder bge-m3 / hybrid 稠密+稀疏→RRF→置信度×时间衰减 / **recall 单一读路径与证据束** / inject `<recalled_memories>` 渲染 / resident 常驻画像）、evolve（trigger / consolidate / verify / apply / cycle）、adapters/langgraph（BaseStore + 17 个 ReAct tool，业务收敛在 MemoryService 薄包装，无 LLM 时 save_memory 无近邻直接 ADD、有近邻进入人工复核）。
- 工作记忆 `working/`：操作层草稿，写入只过脱敏，不过评价门、不做对账、不进索引（TODO 天然是祈使句）；turn_watermark 记"更新到第几轮"，`stale_wm` = current_turn > watermark。
- 短期记忆 `short_term/`：transcript 适配层，不新建文件，解析宿主原生日志（kimi-code wire.jsonl 等，`detect_adapter` 按文件名自动识别，识别不了 fail-closed）。
- 服务入口 `server/`：mcp_server.py（stdio，25 个 tool，15 个基础 + v0.2 的 10 个，业务收敛在 MemoryService，测试不走传输直接调）、http_server.py（streamable-http 常驻，默认只绑 127.0.0.1:8765，叠加 /SKILL.md、/bootstrap、/wm_blocks 静态路由；LLM 缺失不阻止启动）。/wm_blocks（M9）是纯读 GET 路由：?scopes=a,b,c 返回各 scope 的非空工作记忆渲染块，供会话开头 hook 免 MCP 握手拉取。
- 评估 `evals/`：datasets（layer1 基础回忆 20 / layer2 多会话 20 / layer3 隐藏关联 12 / prefix 前缀回归 9）+ runners（recall_eval / e2e_eval / prefix_regression / metrics）。e2e_eval 支持 --baseline 配对模式、--jobs 用例级并发、LLM 磁盘缓存默认开（--no-cache 关）；prefix_regression 无 key 整体跳过，门槛 0.8。另有 `evals/memcompass/`：MemCompass 能力画像评测套件的冻结副本（8 个子集 373 条用例 + runner + 校验/体检工具，自带 build 与 naive_rag 运行时依赖，可独立运行），编写源头在 `docs/research/benchmark-suite/`。

### 关键行为语义（改代码前必须知道）

- **评价门（gate.py）**：通过/拒绝/待复核三桶。注入特征任何 memory_type 都拦，原则是"拦指挥模型行为，不拦提及名词"——提示词外泄要动词 + 系统提示词共现（含把字句），裸关键词会误伤文件名/术语引用；开头祈使只拦非 procedural（操作约定天然是祈使句）；low 置信度进复核队列。拒绝原因带命中片段，注明"确定性拦截、原样重试无效"。
- **蒸馏（distill.py）硬规则**：不提炼注入指令；用户明确立的协作约定沉淀为 procedural；状态/安排的变更与撤销必须沉淀；只沉淀用户明确确认过的内容（assistant 单方面建议不沉淀）。反丢弃式防御：id 过 `normalize_entry_id`、非法 confidence 降 medium、detail 超长截断，仍非法进 review_queue，仅脱敏后 <10 字符才真正丢弃。宿主蒸馏（M9）：`get_distill_protocol()` 暴露 prompt/schema 给无 API key 的订阅制 agent，宿主蒸馏产物经 `build_entries_from_distilled()` 走与服务端蒸馏完全相同的 规范化→脱敏 路径（`n_turns=None` 时 evidence_turns 不夹上界），门在服务端、不信任蒸馏来源。
- **对账（reconcile.py）**：两阶段执行——第一阶段并发"找近邻 + LLM 判决策"（LLM 调用是耗时大头，MCP 客户端超时不可配，写路径必须自己够快；并发阶段只读，embedder 加锁、index 连接访问经 RLock 串行化），第二阶段按原顺序串行落库与传播；同批候选彼此不可见（同批重复可能都 ADD，由后续对账/进化收敛）。LLM 判 ADD/UPDATE/DELETE/NOOP；UPDATE 继承 version+1 且 supersedes 指旧 id；冲突不强行收敛、进复核队列。无 LLM 降级（M9，`llm=None`）：无近邻直接 ADD，有近邻一律进复核队列（关系判断必须靠 LLM，fail-safe 不猜）；当前 LangGraph 注册工具也经 MemoryService 执行；适配器中保留的旧规则 helper 不代表注册工具的默认行为。UPDATE/DELETE 落库后接变更传播（propagate.py）：三分类 INVALIDATED 删除留审计（data/logs/propagation.jsonl）/ NEEDS_REVISION 进队列 / UNAFFECTED；判定失败一律进队列不错删；只传播一跳不级联。
- **复核队列（review_queue.py）**：文件名为内容哈希（幂等键，重复排队覆盖同一文件）；读侧 list/load/delete 白名单校验防路径穿越；raw_record 类待办不能直接入库。复核门：`review_gate` = off/ask/strict（默认 ask），积压时 memory_search 按档处置，ask 等 `acknowledge_pending=true` 放行。`memory_add` 返回 `pending_review` 待复核明细，调用方必须向用户报告。
- **scope 纪律**：global / repo:<项目名> / agent:<名字>；`normalize_scope` 读写同口径归一化（`repo:llm_wiki` → `repo:llm-wiki`），归一化后仍非法当场报错，绝不静默返回空；memory_add 缺省回落 global 并附 scope_reminder。
- **LLM（llm.py）**：缺 key 构造即报错；JSON 解析失败重试 1 次后 fail-closed；超时/重试上限配置化（`llm_timeout_seconds` 默认 300 / `llm_max_retries` 默认 2，`AGENT_MEMORY_LLM_TIMEOUT_SECONDS` / `AGENT_MEMORY_LLM_MAX_RETRIES` 覆盖）；可选磁盘缓存 key=sha256(model+kind+system+user)，临时文件+os.replace 并发安全。
- **记忆条目（models.py）**：content 原子句 + 可选 detail（≤800 字符完整段落）+ `event_date`（事件实际发生日期，蒸馏按会话日期换算，content 里原说法保留、日期括号附后）+ `cues`（线索词，只进 `index_text`、永不渲染）；索引用 content+detail+cues，渲染只用 content；`retrieval_count` 只在 `track_retrieval=True` 的检索命中时 +1（写路径近邻检索不计数）。
- **进化闭环（evolve/）**：三档验证（boundary/retention/safety）各自一票否决，LLMError 按不通过 fail-closed；apply 只执行 merge/invalidate/downgrade/archive，conflict/revise 留人工；晋升前快照 `data/snapshots/`，`rollback` 可恢复；离线复核用语义独立的 `judge_validity()`（默认成立，明确证据才判失效，不复用 judge_propagation——前者回答"这条本身还成立吗"，后者回答"撤销牵连谁"）。

### 运维（Windows 计划任务常驻）

- 计划任务 `AgentMemoryHttpServer`：AtLogOn 触发，S4U 后台模式（无窗口）；启动脚本 `scripts/start_http_server.cmd` 为本机部署脚本（含个人路径，未随仓库发布；**必须纯 ASCII**——cmd.exe 按 GBK 读 .cmd，UTF-8 中文注释会乱码并破坏解析），从仓库外的 `.env` 读 `DEEPSEEK_API_KEY` 映射为 `AGENT_MEMORY_LLM_API_KEY`（重试不重读 .env，改 key 需整体重启）；异常退出等 60s 重试最多 3 次，3 连败写 `data/state/http_server_FAILED.txt` 交人工；日志 `data/logs/http_server.log`。
- 注册脚本 `scripts/register_task_s4u.ps1` 同为本机部署脚本（未随仓库发布），需提权运行且**必须纯 ASCII**（PowerShell 5.1 按 ANSI 读无 BOM 的 .ps1）。
- 强制记忆更新 hook：`scripts/memory_turn_hook.py`（kimi-code Stop hook，每 N 轮以退出码 2 拦截本轮结束并注入蒸馏指令；启动时 stdout/stderr 重配 UTF-8 防 GBK 乱码；fail-open；已注册进用户级 `~/.kimi-code/config.toml`；`AGENT_MEMORY_HOOK_DEBUG=1` 时把每次 Stop 事件 payload 落盘 `data/logs/hook_debug.jsonl`，用于实测 Stop 是否对 subagent 轮次触发）。
- 会话开头工作记忆注入 hook（M9）：`scripts/memory_session_context_hook.py`（宿主中立，kimi-code 挂 UserPromptSubmit；每个 session 首条用户消息时向 `/wm_blocks` 拉 global + repo:<当前目录名> + agent:<宿主名> 三个 scope 的工作记忆块写 stdout 注入上下文；状态文件 `data/state/wm_injected_sessions.json` 去重；fail-open；`AGENT_MEMORY_AGENT_NAME` 改宿主名、`AGENT_MEMORY_WM_HOOK=off` 关闭）。已注册进用户级 `~/.kimi-code/config.toml`。
- kimi-code 一键安装：`scripts/install_kimi_code.sh` 做四件事——合并 mcp.json、装 Skill、装 `agents/coder.md` subagent 覆盖文件（记忆写类工具硬闸；已存在先备份 .bak）、注册会话开头工作记忆注入 hook（幂等）。

### 里程碑速览与验收基线

- M0 数据模型与配置；M1 记忆内核（store/index/embed/hybrid/redact/CLI）；M2 蒸馏写路径 + MCP server；M3 LangGraph 适配 + Skill + 前缀回归；M4 进化闭环 + layer3 + 真实验收（layer3 有记忆 91.67% vs baseline 0%，McNemar p=0.0010；缺陷复盘见 `docs/history/defect-postmortem.md` 缺陷 3/4）；M5 人工复核交互 + hook；M6 HTTP 常驻服务 + scope 纪律；M7 三层记忆 + memory_context 统一接口；M8 写入失败语义（归档兜底、force_review 转复核、报错含命中片段、LLM 超时/重试配置化）；M9 宿主蒸馏协议（memory_distill_prompt + distilled_json 模式 + 对账无 LLM 降级，顺带修复无 LLM 时单条写入有近邻崩溃的 bug）+ 会话开头工作记忆自动注入（/wm_blocks 路由 + 宿主中立 hook）。
- v0.2（2026-09-14，框架文档 §9 的优化顺序）：P02 归档先脱敏 + 持续归档（`memory_archive_sync`）；P03 原文可检索（`long_term/store/raw_index.py`，派生的 `data/raw_index.db`，可由 data/raw 重建；`memory_archive_search/read`；`memory_search` 命中"只有要点/核验不一致"的记忆时附 `<raw_evidence>`）；P27/P13 完整度自评与回读核验（`MemoryEntry.completeness/verify_flag`，蒸馏自评 + `annotate_completeness` 对照原文，`MarkdownStore.patch_meta` 只改注解不动 version/last_verified）；P24–P26 主动浮现（`long_term/retrieve/surface.py`：线索扩展 + 一跳扩散 + 精确率优先的记忆副手，`memory_surface`、HTTP `POST /surface`、`scripts/memory_surface_hook.py`）；P29 待确认队列（`agent_memory/confirmations.py`，`data/confirmations/`）；P07/P08 工作记忆 constraints / open_questions / subtasks + 服务端整理（`working/refresh.py`，`memory_wm_refresh`）；P05/P06 事件边界情节卡片（`long_term/ingest/episode.py`，`memory_episode_pack`）；P19 双时态（`valid_from/valid_to/history`，对账 UPDATE 时旧版本折进 history，蒸馏带会话日期）；P15 来源类型（第三方/工具来源的说法降为 low 进复核）；K12 遗忘请求（`long_term/ingest/forget.py`，蒸馏识别 + `memory_forget_request`；D1 有条件例外：只擦指定片段为占位符，审计 `data/logs/forget_audit.jsonl` 只记元数据）。新增能力集中在 `server/service_v2.py`（MemoryService 的混入类）；MCP tool 由 15 个增至 25 个。v0.2 字段全部可选，老数据与老渲染输出不变。
- memory-v1（2026-09-21 起，分支 `feat/memory-v1`）：读取期 LLM 合成机制（v0.4 的 P34/P36b/P37、P23 启发式回退、P35 交错）全部移除，代之以单一读路径 `recall`；写入期蒸馏加 `event_date`/`cues`，可计数实例逐条沉淀不写总数。设计推导与逐轮实测：`docs/research/memory-v1-design.md`；机制说明：`docs/design/memory-v1-mechanism.md`。
- 当前测试基线：`uv run pytest -q` 850 passed（新增 `tests/test_recall.py`、`tests/test_public_hygiene.py`），`uv run ruff check .` 干净（`docs/research/**` 只放宽行长）。
- 版本与变更记录：版本号在 `pyproject.toml`、`agent_memory/__init__.py` 的 `__version__`、`docs/CHANGELOG.md` 顶部条目三处一致（卫生测试核对）；当前 v0.3.2（memory-v1）。行为有变化的改动在 CHANGELOG 顶部条目加一行。
- 评测：MemCompass v0.3（8 个子集 373 条用例，用户已整体签核）。编写源头 `docs/research/benchmark-suite/`（构造脚本 `build/`、数据卡、核验台、设计文档），**冻结副本已按用户授权迁入 `evals/memcompass/`（D6 可信根，agent 不得修改；更新由用户在源头改规格、重新生成、重新核验后用 `tools/migrate_to_evals.py` 执行）**。正式报告 `results/2026-09-16-v03-report.md`（含对照组、消融、成本与用例体检；§8 成熟度按框架 §6.2 定级）。

### data/ 目录

运行时数据目录，gitignored：`data/raw`（原始证据，只追加）、`data/memory`（Markdown 记忆层）、`data/working`（工作记忆操作层，不归 D1 三层）、`data/review_queue`（含 evolution/ 提案子目录）、`data/snapshots`、`data/state`、`data/logs`。评测数据：`data/dev/`（自建验证集）、`data/external/<来源>/`（外部测试集原始文件与抽样），运行结果在 `data/logs/aml_selftest/<run-id>/`。
