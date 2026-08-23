# agent-memory

本地长期记忆基础设施（Local Long-Term Memory Infrastructure）。agent 中立：不绑定任何特定 agent 框架，通过三种方式接入——

- **Python 库**：LangGraph 等框架直接 `import agent_memory`（见 `src/agent_memory/adapters/`）；
- **MCP server**：任何支持 MCP 的客户端（见 `src/agent_memory/server/`，M2+ 实现）；
- **Skill**：以 Skill 形式挂载到支持 Skill 的 agent（见 `skills/agent-memory/`，M3 实现）。

## 当前状态：M6（完成）

M0 只交付项目骨架与核心 schema：

- `src/agent_memory/models.py`：记忆条目（`MemoryEntry`）、证据指针（`EvidenceRef`）、蒸馏提案（`MemoryProposal`）的 pydantic 模型与校验规则；
- `src/agent_memory/config.py`：单一配置模块，环境变量可覆盖，非法配置 fail-closed；
- `evals/datasets/layer1/`：20 条"基础回忆"评估用例（YAML），用于后续 M1+ 的召回评估。

M1 交付记忆内核 MVP（手动蒸馏）：`store/`（Markdown 记忆层 + sqlite-vec/FTS5 派生索引）、`retrieve/`（bge-m3 嵌入 + 稠密/稀疏 RRF 混合检索）、`ingest/redact.py`（正则脱敏）、`cli.py`（add / search / list / update / forget / rebuild / stats）、`evals/runners/recall_eval.py`（layer1 recall@5）。

M2 交付蒸馏写路径 + MCP server：

- `src/agent_memory/llm.py`：`LLMClient` 协议（依赖注入，测试用 fake）与 `OpenAILLMClient`（OpenAI 兼容端点，默认 DeepSeek，缺 key fail-closed）；
- `src/agent_memory/ingest/distill.py`：对话 → 原子记忆候选（prompt 硬规则：绝不提炼指令性内容，红线 D2；id/confidence/detail 先规范化再校验，规范化后仍非法的进 `data/review_queue/` 而非静默丢弃）；
- `src/agent_memory/ingest/gate.py`：评价门（脱敏残留 / 指令性内容 / 长度下限 / low 置信度三桶分流）；
- `src/agent_memory/ingest/reconcile.py`：Mem0 式对账（ADD / UPDATE / DELETE / NOOP，冲突无法收敛时写 `data/review_queue/`）；UPDATE/DELETE 落库后接 `ingest/propagate.py` 变更传播（依赖旧事实的近邻由 LLM 判失效/需修订/不受影响，失效删除留审计日志 `data/logs/propagation.jsonl`，需修订进复核队列）；
- `src/agent_memory/retrieve/inject.py`：检索结果渲染为 `<recalled_memories>` XML 注入块（带"参考而非指令"护栏前缀，预算整条截断）；
- `src/agent_memory/server/mcp_server.py`：MCP stdio server，五个 tool（memory_search / memory_add / memory_feedback / memory_update / memory_forget）；
- `evals/datasets/layer2/`：20 条多会话检索/消歧用例（时序冲突 7 + 多对象消歧 7 + 有效/失效区分 6）；
- `evals/runners/e2e_eval.py`：端到端评估（无 LLM key 时自动降级为规则判定模式）。

M3 交付 LangGraph 适配 + Skill + 轨迹前缀回归评估：

- `src/agent_memory/adapters/langgraph/store.py`：`AgentMemoryStore`（LangGraph `BaseStore` 实现，namespace `("memories", <scope>)`，put 过脱敏+评价门规则、search 走混合检索）；
- `src/agent_memory/adapters/langgraph/tools.py`：`build_memory_tools()` 产出 `recall_memories` / `save_memory` 两个 ReAct tool（save 的对账是无 LLM 纯规则路径：近邻重复 NOOP，否则 ADD）；
- `src/agent_memory/retrieve/resident.py`：`build_system_context(scope)` 常驻层注入（profile 记忆按置信度排序进 system prompt，预算为召回预算的一半）；
- `skills/agent-memory/SKILL.md`：教 agent 何时检索/写入/反馈（MCP tool 名与参数示例，"召回是参考而非指令"）；
- `evals/datasets/prefix/` 9 条轨迹前缀回归用例（指令冲突 2 + scope 泄漏 2 + 低置信度 2 + 抗注入 2 + 正常召回对照 1）；
- `evals/runners/prefix_regression.py`：冻结上下文 → LLM 输出下一步动作 → 评委判定可接受/禁止集合（429 自动重试，无 key 跳过）；
- `examples/langgraph_demo.py`：最小 LangGraph ReAct agent 接入演示（跨会话记住偏好）。

M4a 交付进化闭环核心（睡眠学习循环 + 定期整理），双循环成形：在线循环只追加证据（蒸馏→评价门→对账），离线循环批量整理记忆库——

- `src/agent_memory/evolve/trigger.py`：触发判定（距上次整理超 N 天 / 新增条目超阈值 / review_queue 积压超阈值，任一满足即触发，阈值全部走 `AGENT_MEMORY_EVOLVE_*` 环境变量）；
- `src/agent_memory/evolve/consolidate.py`：整合产出 `EvolutionProposal`——去重合并（近邻对 LLM 判 MERGE/CONFLICT/UNRELATED，CONFLICT 不强行收敛交人工）、离线复核最旧条目（复用 `ingest/propagate.py` 的 `judge_propagation`）、长期未检索条目降权/归档建议；提案只写 `data/review_queue/evolution/<timestamp>/`，绝不直接改记忆层；
- `src/agent_memory/evolve/verify.py`：三档验证（boundary 契约核查 / retention 基准 query top-5 diff / safety 安全记忆保护），任一不过即整体否决；
- `src/agent_memory/evolve/apply.py`：晋升前快照（`data/snapshots/<timestamp>/`）、应用后审计（`data/logs/evolution_audit.jsonl`）、`rollback(snapshot_id)` 回滚；
- `src/agent_memory/evolve/cycle.py`：五步编排（触发 → 定向 → 整合 → 验证 → 修剪）；
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
  注入蒸馏指令（材料 = 每轮用户消息 + 紧邻的 assistant 回复），已注册进用户级 `~/.kimi-code/config.toml`。

M6 交付 HTTP 常驻服务 + 作用域纪律：

- `src/agent_memory/server/http_server.py`：streamable-http 常驻服务，默认只绑 127.0.0.1:8765
  （回环地址天然免鉴权），在 MCP 端点上叠加 `/SKILL.md`（提示层全文分发）与 `/bootstrap`
  （新 agent 接入引导指令）两个静态路由；对方 agent 一条引导指令即可接入，不再需要复制文件；
- 作用域纪律（共用一套库、多 agent 多项目混用）：SKILL.md 新增 scope 选择规则（共性进 global、
  项目进 repo:<名>、拿不准先问用户），`memory_add` 的 scope 缺省回落 global 但返回附
  `scope_reminder` 提醒；
- Windows 常驻运维：`scripts/start_http_server.cmd` 启动包装脚本（崩溃自动重试最多 3 次，
  3 连败写 `data/state/http_server_FAILED.txt` 失败标记交人工，日志在 `data/logs/http_server.log`）
  + 登录触发的计划任务（注册脚本 `scripts/register_task_s4u.ps1`，需管理员权限运行）。

## 目录结构

```
agent-memory/
├── src/agent_memory/     # Python 包（src 布局，import 名 agent_memory）
│   ├── config.py         # 配置（AGENT_MEMORY_* 环境变量覆盖）
│   ├── models.py         # 记忆条目 schema（M0 核心）
│   ├── store/            # Markdown 记忆层 + sqlite-vec 索引（M1）
│   ├── ingest/           # 脱敏 → 蒸馏 → 对账 写入管线（M2）
│   ├── retrieve/         # 混合检索 + 注入渲染 + 常驻层（M1-M3）
│   ├── evolve/           # 睡眠学习循环：触发/整合/验证/晋升/回滚（M4）
│   ├── server/           # MCP server：stdio（M2）+ HTTP 常驻（M6）
│   └── adapters/         # 框架适配器（LangGraph，M3）
├── skills/agent-memory/  # Skill 接入方式（M3）
├── scripts/              # 运维脚本：turn hook（M5）、HTTP 服务启动/计划任务注册（M6）
├── evals/                # 评估集：datasets / rubrics / runners（agent 禁改，D6）
├── tests/
└── data/                 # 运行时数据（gitignored）：raw / memory / review_queue / snapshots / state / logs
```

## 快速开始

```bash
uv sync          # 创建虚拟环境并安装依赖
uv run pytest    # 跑测试
uv run ruff check .
```

## M2 用法

### 配置 LLM（蒸馏 / 对账 / LLM 评委用）

蒸馏写路径需要一个 OpenAI 兼容端点，默认 DeepSeek（`https://api.deepseek.com`，模型 `deepseek-chat`）：

```bash
export AGENT_MEMORY_LLM_API_KEY=sk-...
# 可选覆盖：AGENT_MEMORY_LLM_BASE_URL / AGENT_MEMORY_LLM_MODEL
# 评估评委可单独配置（异源互审）：AGENT_MEMORY_JUDGE_LLM_API_KEY 等
```

未配置 key 时，检索、手动写入、反馈、删除等不依赖 LLM 的功能照常可用；
只有对话蒸馏路径在调用时报错（fail-closed）。

### CLI 蒸馏命令

把一段对话（`[{role, content}, ...]` 的 JSON 文件）走完整写入管线入库：

```bash
uv run agent-memory distill --file conversation.json --scope repo:my-project \
    --source kimi-code --session-id 2026-08-19-session
# 管线：蒸馏 → 评价门 → 对账；无法自动收敛的冲突会写入 data/review_queue/
```

### MCP server

启动：`uv run python -m agent_memory.server.mcp_server`（stdio）。

Claude Code / Kimi Code 的 MCP 配置片段：

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "uv",
      "args": ["run", "python", "-m", "agent_memory.server.mcp_server"],
      "env": {
        "AGENT_MEMORY_DATA_DIR": "C:/Users/<you>/.agent-memory/data",
        "AGENT_MEMORY_LLM_API_KEY": "sk-...",
        "AGENT_MEMORY_LLM_BASE_URL": "https://api.deepseek.com",
        "AGENT_MEMORY_LLM_MODEL": "deepseek-chat"
      }
    }
  }
}
```

五个 tool 起步（M5 起扩为七个，见下文 M5 用法）：`memory_search`（混合检索 + XML 注入块，scope 过滤在服务端强制）、
`memory_add`（对话 JSON 走蒸馏管线 / 单条 content 走脱敏+对账）、
`memory_feedback`（升降置信度，降到 low 以下进复核队列）、
`memory_update`（过脱敏+评价门后更新）、`memory_forget`（删除）。

### 端到端评估

```bash
uv run python evals/runners/e2e_eval.py --layers 1,2            # 无 key 时自动规则降级模式
uv run python evals/runners/e2e_eval.py --layers 1,2 --llm-judge # 真实 LLM 评委按 rubric 判定
uv run python evals/runners/e2e_eval.py --layers 3 --llm-judge --jobs 8   # layer3 跨会话隐藏关联
uv run python evals/runners/e2e_eval.py --layers 3 --llm-judge --jobs 8 --baseline
    # --baseline：同一批用例在空库下配对重跑，输出逐题胜负 / McNemar p 值 /
    # 配对 bootstrap 留出增益区间，以及激活率 / 遵循率 / 留出增益三个进化指标
uv run python evals/runners/e2e_eval.py --layers 2 --llm-judge --jobs 8   # 调高用例并发
uv run python evals/runners/e2e_eval.py --layers 2 --llm-judge --no-cache # 禁用响应缓存
```

提速机制（真实模式默认生效）：

- **LLM 响应磁盘缓存**：蒸馏 / 对账 / 评委的每次响应按 sha256(model + system + user)
  缓存在 `data/logs/llm_cache/`（已 gitignored）。重跑时未改动的环节直接命中缓存，
  秒级完成；换模型自动不命中。`--no-cache` 关闭。
- **用例级并发**：`--jobs N`（默认 4）用线程池并发跑用例，每条用例独立临时目录，
  429 限流自动指数退避重试。
- **模型加载**：bge-m3 每进程加载一次（约 1-2 分钟）。跑多个 layer 时用
  `--layers 1,2` 一次跑完，不要分两个进程各跑一层。

规则降级模式不代表真实蒸馏质量，正式验收需配置真实 LLM 后重跑。

## M3 用法

### LangGraph 接入

自写的 LangGraph agent 有三种接法，可叠加使用：

```python
from agent_memory.long_term.adapters.langgraph.store import AgentMemoryStore
from agent_memory.long_term.adapters.langgraph.tools import build_memory_tools
from agent_memory.long_term.retrieve.resident import build_system_context
from langgraph.prebuilt import create_react_agent

# 1) BaseStore：namespace 约定 ("memories", <scope>)，put/search/delete 直接映射到记忆内核
store = AgentMemoryStore()          # 配置走 AGENT_MEMORY_* 环境变量
store.put(("memories", "repo:myproj"), "db-choice",
          {"content": "本项目数据库定为 SQLite，文件 data/app.db。", "confidence": "high"})

# 2) ReAct tool：recall_memories / save_memory 挂进 tools 列表
tools = build_memory_tools()

# 3) 常驻层：profile 类记忆渲染进 system prompt（预算是召回预算的一半）
prompt = "你是用户的编程助手……\n\n" + build_system_context("repo:myproj")

agent = create_react_agent(model, tools, prompt=prompt, store=store)
```

完整可运行示例见 `examples/langgraph_demo.py`（`uv run python examples/langgraph_demo.py`，
需 `AGENT_MEMORY_LLM_API_KEY`）。

注意 BaseStore 的 put 是低层同步接口：调用方要给提炼好的原子内容，适配层过
脱敏+评价门规则（指令性内容直接抛错），但不做 LLM 蒸馏；save_memory tool 的
对账是无 LLM 纯规则路径（近邻重复 NOOP，否则 ADD），冲突收敛仍走 M2 蒸馏管线。

### Skill 接入

`skills/agent-memory/SKILL.md` 是提示层，教封装好的 agent（Kimi Code / Claude Code）
何时检索、写入、反馈。安装方式（配合 MCP server 一起用）：

- Kimi Code：把 `skills/agent-memory/` 复制或软链到 `~/.kimi-code/skills/agent-memory/`；
- Claude Code：复制到 `~/.claude/skills/agent-memory/`；
- 同时按上文 MCP 配置挂上 `agent-memory` server，Skill 里的 tool 名（memory_search 等）才有实现。

### 轨迹前缀回归评估

冻结上下文（system + 已注入记忆块 + 用户最新消息）→ LLM 输出下一步动作 → 评委判定
是否落在可接受集合且未触碰禁止集合。覆盖四类边界场景（指令冲突 / scope 泄漏 /
低置信度 / 抗注入）+ 正常召回对照：

```bash
uv run python evals/runners/prefix_regression.py             # 需 LLM key，无 key 整体跳过
uv run python evals/runners/prefix_regression.py --seeds 3   # 多种子报均值与区间
uv run python evals/runners/prefix_regression.py --no-cache  # 禁用 LLM 响应缓存（默认开）
```

429 限流会自动间隔重试；LLM 响应磁盘缓存与 e2e_eval 共用 `data/logs/llm_cache/`。本评估没有规则降级模式（actor 行为本身就是被测对象）。

## M4 用法

### 睡眠学习循环（evolve）

```bash
# dry-run：只到提案为止，打印提案摘要，不验证、不应用
uv run agent-memory evolve --dry-run

# 完整循环：触发 → 整合 → 三档验证 → 通过则晋升（自动快照 + 审计）
uv run agent-memory evolve

# 只整理某个 scope
uv run agent-memory evolve --scope repo:my-repo
```

触发条件（满足任一，阈值用 `AGENT_MEMORY_EVOLVE_*` 环境变量覆盖）：距上次整理超 7 天（`EVOLVE_INTERVAL_DAYS`）、新增条目超 50（`EVOLVE_NEW_ENTRIES_THRESHOLD`）、复核队列积压超 10（`EVOLVE_REVIEW_BACKLOG_THRESHOLD`）。

整理产出的是**提案**（`data/review_queue/evolution/<timestamp>/proposal.yaml`），不是直接改写：三档验证（boundary / retention / safety）任一不过即否决，提案留档交人工；全过才晋升——晋升前对记忆层做快照（`data/snapshots/<timestamp>/`），晋升后写审计日志（`data/logs/evolution_audit.jsonl`）。回滚用 `agent_memory.long_term.evolve.apply.rollback(snapshot_id, settings, embedder)` 从快照恢复记忆层并重建索引。

## M5 用法

### 人工复核（复核队列的两个交互节点）

蒸馏非法产出、评价门判低置信度、对账无法收敛的冲突，都会进 `data/review_queue/` 等人工裁决。
复核通过两个 MCP tool 完成：

- `memory_review_list`：列出待办明细（来源、原因、内容）；
- `memory_review_resolve`：裁决——`approve` 原样入库 / `modify` 改文本过脱敏+评价门后入库 /
  `discard` 丢弃。裁决成功即删队列文件；raw_record 类待办不可直接入库。

复核门（`AGENT_MEMORY_REVIEW_GATE`，默认 `ask`）：队列有积压时 `memory_search` 的行为——
`ask` 返回 `status=blocked` 等用户确认（`acknowledge_pending=true` 放行）、`strict` 一律拒读
（无人值守场景用）、`off` 不拦。`memory_add` 的返回会附 `pending_review` 明细，
agent 应逐条向用户报告并请其裁决（SKILL.md 有对应流程）。

### 强制记忆更新 hook

`scripts/memory_turn_hook.py` 是 kimi-code 的 Stop hook：按 session 计轮，每
`AGENT_MEMORY_REVIEW_TURN_INTERVAL`（默认 3）轮拦截一次会话结束，注入蒸馏指令
（材料 = 每轮用户消息 + 紧邻的 assistant 回复）。已注册进用户级
`~/.kimi-code/config.toml`，对所有项目会话生效；其他宿主可参照脚本自行挂接。

## M6 用法

### HTTP 常驻服务

stdio 模式由宿主把 server 拉成子进程、随会话生灭；HTTP 模式是一个长期运行的本机服务，
任何能发 HTTP 请求的 agent 宿主注册一个 URL 即得全部七个 tool：

```bash
uv run python -m agent_memory.server.http_server
# 默认监听 http://127.0.0.1:8765/mcp（只绑回环地址，天然免鉴权）
# 覆盖：AGENT_MEMORY_HTTP_HOST / AGENT_MEMORY_HTTP_PORT
```

服务另有两个静态路由：`/SKILL.md`（提示层全文）和 `/bootstrap`（接入引导指令）。
新 agent 接入只需把 `/bootstrap` 的内容给它：注册 `http://127.0.0.1:8765/mcp`
（传输类型 streamable-http）+ 读取并遵循 `/SKILL.md`，不需要复制任何文件。

### Windows 常驻（计划任务）

`scripts/start_http_server.cmd` 是启动包装脚本：异常退出等待 60 秒后拉起，最多 3 次；
3 连败写 `data/state/http_server_FAILED.txt` 失败标记交人工；日志在
`data/logs/http_server.log`。`scripts/register_task_s4u.ps1` 注册登录触发的计划任务
（S4U 后台模式，完全无窗口），需管理员权限运行。**两个脚本都必须保持纯 ASCII**
（cmd.exe 按 GBK 读 .cmd、PowerShell 5.1 按 ANSI 读无 BOM 的 .ps1，非 ASCII 会损坏解析）。

## 三条架构红线

详见 [AGENTS.md](AGENTS.md)。简而言之：数据三层分离（raw 只追加、memory 是唯一事实来源、index 可重建绝不手改）；写入必须过脱敏→蒸馏→对账的门；evals / rubric / 发布门槛 / 审计日志禁止 agent 自行修改。
