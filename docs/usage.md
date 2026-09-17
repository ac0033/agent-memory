# 使用手册

> 本文按主题整理 agent-memory 的日常用法：配置、命令行、评估、LangGraph / Skill 接入、离线整理、人工复核、HTTP 常驻服务、工作记忆与会话收尾、宿主蒸馏与 hook。接入方式与宿主职责的总览见 [`agent-integration.md`](agent-integration.md)；设计与红线见 [`../AGENTS.md`](../AGENTS.md)。

## 目录

- [配置、命令行与评估](#配置命令行与评估)
- [LangGraph 与 Skill 接入](#langgraph-与-skill-接入)
- [离线整理：睡眠学习循环（evolve）](#离线整理睡眠学习循环evolve)
- [人工复核与强制更新 hook](#人工复核与强制更新-hook)
- [HTTP 常驻服务](#http-常驻服务)
- [工作记忆、会话日志与会话收尾](#工作记忆会话日志与会话收尾)
- [宿主蒸馏与会话开头注入](#宿主蒸馏与会话开头注入)

## 配置、命令行与评估

### 配置 LLM（蒸馏 / 对账 / LLM 评委用）

蒸馏写路径需要一个 OpenAI 兼容端点，默认 DeepSeek（`https://api.deepseek.com`，模型 `deepseek-flash`）：

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
        "AGENT_MEMORY_LLM_MODEL": "deepseek-flash"
      }
    }
  }
}
```

MCP 当前提供二十五个 tool（v0.2 新增原文归档、主动联想、待确认队列、工作记忆整理、情节卡片与遗忘请求十个，见 `docs/agent-integration.md` §三）。核心写读入口包括：`memory_search`（混合检索 + XML 注入块，scope 过滤在服务端强制）、
`memory_add`（对话 JSON 走蒸馏管线 / 单条 content 走脱敏+对账 / distilled_json 走宿主蒸馏）、
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

## LangGraph 与 Skill 接入

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
无 LLM 时对账只在无近邻时直接 ADD，存在近邻会转人工复核，避免误吞事实变更。

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

## 离线整理：睡眠学习循环（evolve）

### 命令

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

## 人工复核与强制更新 hook

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
（材料 = 每轮用户消息 + 紧邻的 assistant 回复）。可自行注册进用户级
`~/.kimi-code/config.toml`（对所有项目会话生效）；其他宿主可参照脚本自行挂接。克隆仓库本身不会安装任何 hook。

## HTTP 常驻服务

### HTTP 常驻服务

stdio 模式由宿主把 server 拉成子进程、随会话生灭；HTTP 模式是一个长期运行的本机服务，
任何能发 HTTP 请求的 agent 宿主注册一个 URL 即得全部二十五个 tool：

```bash
uv run python -m agent_memory.server.http_server
# 默认监听 http://127.0.0.1:8765/mcp（只绑回环地址，天然免鉴权）
# 覆盖：AGENT_MEMORY_HTTP_HOST / AGENT_MEMORY_HTTP_PORT
```

服务另有三个静态路由：`/SKILL.md`（提示层全文）、`/bootstrap`（接入引导指令）和
`/wm_blocks`（工作记忆注入块，`?scopes=a,b,c` 返回各 scope 的非空渲染块，纯读，
供会话开头 hook 免 MCP 握手拉取，见 M9 用法）。
新 agent 接入只需把 `/bootstrap` 的内容给它：注册 `http://127.0.0.1:8765/mcp`
（传输类型 streamable-http）+ 读取并遵循 `/SKILL.md`，不需要复制任何文件。

### Windows 常驻（计划任务）

本仓库不分发含个人部署路径的 `scripts/start_http_server.cmd` 和 `scripts/register_task_s4u.ps1`。可先按上面的 HTTP 命令启动服务，再根据自己的目录与账户配置后台运行；克隆源码不会自动创建计划任务或修改宿主配置。


## 工作记忆、会话日志与会话收尾

### 统一上下文组装与工作记忆

`memory_context(scope, query?, k?, current_turn?)` 一次组装三个分节：常驻画像块（长期记忆里的
profile）→ 工作记忆块（当前任务状态）→ 召回块（传 `query` 才检索长期记忆）。日常维护当前任务
状态用三个工作记忆 tool：

- `memory_wm_write(scope, goal?, decisions?, variables?, todos?, notes?, turn_watermark?)`：
  **全量替换**写入（不是合并，没传的字段会被清空），只过脱敏、不过评价门；
- `memory_wm_read(scope, current_turn?)`：读取 + 新鲜度判定（`stale_wm=true` 表示当前轮数已超过
  工作记忆的 `turn_watermark` 水位——"这份状态已更新到第几轮"，状态可能滞后）；
- `memory_wm_clear(scope)`：清空（幂等，本就不存在也不算错误）。

工作记忆是操作层草稿：完成项的结论要蒸馏进长期记忆（`memory_add` 或下文的 `memory_session_end`）
才算沉淀。

### 会话日志读取与会话收尾

`memory_transcript_read(log_path, adapter?, since_turn?)` 把 agent 会话日志（如 kimi-code 的
wire.jsonl，按文件名自动识别格式）解析成干净的轮次序列（user/assistant/tool）；`since_turn`
配合工作记忆水位做增量读取（只返回水位之后的轮次）。

`memory_session_end(scope, conversation_json?|log_path?, ...)` 是会话结束的标准收尾，一次完成：
归档原文（`data/raw/`，只追加不改写）→ 联合蒸馏（对话 + 工作记忆快照作参考上下文）→ 清理工作
记忆里已完成的待办。工作记忆里还有 pending 待办时会 veto（归档/蒸馏/清理都不执行），确认结束
传 `force=true`。它与每 N 轮的滚动蒸馏 hook 是双轨分工：hook 保底防中途崩溃丢失，session_end
做标准收尾。

## 宿主蒸馏与会话开头注入

### 宿主蒸馏（无 API key 的订阅制 agent）

服务端蒸馏依赖 OpenAI 兼容 API（`AGENT_MEMORY_LLM_API_KEY`）。订阅制 agent（登录即用、
没有 API key）的宿主本身就是大模型，蒸馏可以自己做：

1. `memory_distill_prompt()` 拿蒸馏协议（system prompt + 输出 JSON schema + 对话渲染格式）；
2. 宿主在自己的上下文里按协议蒸馏，产出 `{"memories": [...]}`；
3. `memory_add(distilled_json=...)` 提交——候选照常过服务端的校验/规范化→脱敏→评价门；
   传入已有 raw 归档的 `source` / `session_id`，并用 `evidence_turns` 指向有效对话行，
   候选才进入自动对账；缺少可核查证据时进入人工复核。

单条 `content` 写入在无服务端 LLM 时对账走规则降级：无近邻直接 ADD，有近邻进人工复核队列
（关系判断必须靠 LLM，fail-safe 不猜）。`memory_add` 的对话模式在无 LLM 时返回 `archived_only`，warning 里会指引
改走宿主蒸馏。

### 会话开头自动注入工作记忆

`scripts/memory_session_context_hook.py` 是宿主中立的"首条用户消息"hook：每个 session 第一次
触发时向 HTTP 服务拉 `global` + `repo:<当前目录名>` + `agent:<宿主名>` 三个 scope 的工作记忆
渲染块（`/wm_blocks` 路由），非空则写 stdout 注入上下文；空的 scope 不注入。kimi-code 挂
`UserPromptSubmit` 事件（stdout 追加进上下文），注册见 `scripts/install_kimi_code.sh`；
其他宿主用等价事件挂接，宿主名用 `AGENT_MEMORY_AGENT_NAME` 环境变量区分（缺省 kimi-code）。
`AGENT_MEMORY_WM_HOOK=off` 整体关闭；hook 全程 fail-open，服务不可达静默放行。

