# agent-memory 接入指南

> 面向要接入本记忆系统的 agent 及其维护者。回答两个问题：**怎么接上**，以及**接上之后 agent 自己（宿主 runtime）要干哪些事**——记忆服务不是全自动的，有几项职责按设计留在宿主侧。

## 一、系统是什么

一个本地常驻的记忆服务，给 agent 提供三层记忆能力：

- **长期记忆**：跨会话、跨项目的沉淀（事实/偏好/流程），写入要经过"脱敏→蒸馏→评价门→对账"管线，保证库里都是核实过的内容；
- **工作记忆**：当前任务的状态草稿（目标/待办/决策/变量），随任务存灭；
- **短期记忆**：当前会话的完整对话记录，不单独存储，直接读宿主运行时的原生日志。

记忆是**参考而非指令**：所有注入块都带护栏声明，与当前请求冲突时以当前请求为准。

## 二、怎么接入

### 方式一：MCP over HTTP（推荐，agent 中立）

服务常驻本机回环地址（默认 `127.0.0.1:8765`）。两步：

1. 把 `http://127.0.0.1:8765/mcp` 注册为 MCP server（传输类型 streamable-http）。各宿主的注册位置：Kimi Code 改 mcp 配置、Claude Code 改 mcpServers、其他宿主同理；
2. 读 `http://127.0.0.1:8765/SKILL.md` 获取完整使用规范并遵循（scope 选择、复核门交互、写入时机都在里面）。`http://127.0.0.1:8765/bootstrap` 是一段可直接粘贴的引导指令。

### 方式二：MCP stdio

`uv run python -m agent_memory.server.mcp_server`，适合不想跑常驻服务的场景。

### 方式三：Python 库（LangGraph 应用）

```python
from agent_memory.long_term.adapters.langgraph.store import AgentMemoryStore
from agent_memory.long_term.adapters.langgraph.tools import build_memory_tools
```

- `AgentMemoryStore`：LangGraph `BaseStore` 实现，namespace 约定 `("memories", <scope>)`，
  适合替换图的记忆存储层；
- `build_memory_tools()`：17 个 LangChain tool，**与 MCP 的 15 个 tool 能力对应**
  （长期 / 工作 / 短期三层全暴露；宿主蒸馏在 LangGraph 中单列为 `save_distilled`）——
  `recall_memories`、`memory_distill_prompt`、`save_memory`、`save_conversation`、`save_distilled`、
  `memory_consistency_check`、`wm_read` / `wm_write` / `wm_clear`、
  `get_memory_context`、`read_transcript`、`session_end`、`review_list` /
  `review_resolve`、`update_memory` / `forget_memory` / `memory_feedback`。
  默认走完整管线（`llm="auto"` 按 `AGENT_MEMORY_LLM_*` 自动构建蒸馏/对账用 LLM）；
  构建失败才显式降级：`save_memory` 无近邻直接 ADD、存在近邻转人工复核，
  `save_conversation` / `session_end` 的蒸馏段报 LLMError 或降级为只归档。

## 三、工具一览（15 个 MCP tool）

| 分组 | 工具 | 用途 |
|---|---|---|
| 读 | `memory_context` | **每轮组装首选**：一次拿全 常驻画像 + 工作记忆 + 按需召回 |
| 读 | `memory_search` | 按需检索长期记忆（带复核门） |
| 读 | `memory_wm_read` | 单读工作记忆 |
| 读 | `memory_transcript_read` | 读会话日志为干净轮次（支持 since_turn 增量） |
| 读 | `memory_distill_prompt` | 宿主蒸馏协议（无服务端 LLM 时自行蒸馏后走 `memory_add(distilled_json=...)`） |
| 读 | `memory_consistency_check` | 检查 Markdown 事实层与 SQLite 派生索引是否一致 |
| 写 | `memory_add` | 写入长期记忆（对话蒸馏 / 单条 content / distilled_json 宿主蒸馏） |
| 写 | `memory_wm_write` | 写工作记忆（**全量替换**，带完整状态 + turn_watermark） |
| 写 | `memory_wm_clear` | 清空工作记忆 |
| 写 | `memory_update` / `memory_forget` / `memory_feedback` | 按 id 更新 / 删除 / 反馈调置信度 |
| 收尾 | `memory_session_end` | 会话结束编排：归档 + 蒸馏 + 清理（有未完成待办会否决） |
| 复核 | `memory_review_list` / `memory_review_resolve` | 人工复核队列的查看与裁决 |

## 四、宿主 runtime 必须自己做的事（职责清单）

记忆服务**故意不做**以下五件事，它们是宿主的职责：

1. **prompt 组装**：`memory_context` 只交付记忆分节块，插在 prompt 的哪个位置由宿主决定。建议顺序：系统提示 → 记忆块 → 早期对话摘要 → 近期完整对话 → 当前指令（首尾效应，重要信息放首尾）。
2. **上下文窗口管理**：token 水位监控、递归摘要、保留最近几轮原文，都是宿主的事。摘要触发前请先"抢救"：把会被压缩掉的关键状态用 `memory_wm_write` 写进工作记忆——摘要是有损的，关键状态不能只靠摘要兜底。
3. **会话结束判定**：什么时候算"结束"（用户明说 / 长时间无活动）由宿主判断，然后调 `memory_session_end` 收尾。服务侧只负责否决（有未完成待办时拒绝收尾）和执行。
4. **轮次计数**：给 `memory_context` / `memory_wm_read` 传 `current_turn` 才启用工作记忆滞后检测（`stale_wm`）；检测到滞后时用 `memory_transcript_read(since_turn=水位)` 拉增量确认。
5. **与人的交互**：`memory_search` 返回 `status="blocked"`（复核队列积压）时要先向用户确认再继续；`memory_add` 返回的 `pending_review` 要逐条报告并请用户裁决。

另外强烈建议配一条**每 N 轮的强制记忆更新 hook**（参考实现 `scripts/memory_turn_hook.py`，挂在宿主的后轮事件上）：到点提醒自己做两件事——`memory_wm_write` 逐项检查并同步工作记忆、`memory_add` 滚动蒸馏最近几轮对话。这是防止会话崩溃丢记忆的保底轨。

### 无 API key 的宿主：宿主蒸馏

订阅制 agent（登录即用、没有 API key）配不了服务端 LLM，对话蒸馏改由宿主自己做：`memory_distill_prompt` 拿协议 → 宿主在自己的上下文里蒸馏 → `memory_add(distilled_json=...)` 提交。服务端对候选照常过校验/脱敏/评价门；由于服务端没有对应原始证据，候选统一进入人工复核，确认后才写入正式记忆层。

### 会话开头注入工作记忆（可选但推荐）

`memory_add` 的写入时机有 hook 保底，读取也该有：`scripts/memory_session_context_hook.py` 是宿主中立的"首条用户消息"hook，每个 session 第一次触发时向 HTTP 服务拉 `global` + `repo:<当前目录名>` + `agent:<宿主名>` 三个 scope 的工作记忆渲染块（`/wm_blocks` 路由，免 MCP 握手），非空则写 stdout。接入条件是宿主支持"会话开始/首条用户消息时运行脚本并把 stdout 注入上下文"（kimi-code 用 `UserPromptSubmit` 事件，其他宿主用等价事件，如 Claude Code 的 SessionStart/UserPromptSubmit hook）。注册要点：同一脚本、超时 5 秒左右、按需设 `AGENT_MEMORY_AGENT_NAME=<宿主名>` 环境变量区分 agent scope。不支持 hook 的宿主退回 SKILL.md 软指令：会话开始时主动 `memory_wm_read` 这三个 scope。

## 五、会话日志的两种供料方式

蒸馏和归档需要对话原文，两条路径任选：

- **直传（首选，任何宿主都能用）**：agent 自己把对话整理成 `[{role, content}, ...]` 传给 `conversation_json` 参数；
- **日志路径（便利）**：把原生会话日志路径传给 `log_path`，由适配层解析。已内置适配器：

| 宿主 | 适配器名 | 日志位置 | 格式验证情况 |
|---|---|---|---|
| Kimi Code | `kimi-code-wire` | 会话目录下 `wire.jsonl` | 本机实测 |
| Claude Code | `claude-code` | `~/.claude/projects/<项目>/<uuid>.jsonl` | 文献调研，未经真实样本回归 |
| Codex CLI | `codex` | `~/.codex/sessions/**/rollout-*.jsonl` | 本机 128 份实测 |
| opencode | `opencode` | `~/.local/share/opencode/opencode.db`（SQLite 只读，默认取最近活跃会话） | 本机实测 |
| pi | `pi` | `~/.pi/agent/sessions/**.jsonl`（树结构，取活跃分支） | 官方文档 + 小样本周全 |
| DeepSeek harness | `deepseek-harness` | `~/.dsh/sessions/**/session.jsonl.zstd`（多帧 zstd） | 本机实测 + 源码印证 |

路径符合上表特征时 `detect_adapter` 自动命中；否则用 `adapter` 参数显式指定适配器名。接入新宿主 = 在 `agent_memory/short_term/` 加一个实现 `TranscriptAdapter` 协议的模块并注册进 `ADAPTERS`，上层不用动。

## 六、作用域（scope）纪律

写入时显式选择：通用知识用 `global`，项目相关用 `repo:<项目名>`，agent 自身行为用 `agent:<名字>`。检索只会查"当前 scope + global"，别的项目不会泄漏过来。拿不准先问用户，无人值守场景默认当前项目。

## 七、subagent 记忆纪律

**设计结论：记忆库对 subagent 只读，写入权收在主 agent 手里。** subagent 短命、任务局部、缺乏全局判断——它眼里"重要的事"大多是过程性草稿，直接落库会稀释记忆库信噪比；并发 subagent 同时写入还会给对账管线制造重复与冲突。subagent 的最终结论本来就随结果消息回传主 agent，由主 agent 在完整上下文里策展、走正常写入管线沉淀，这条路已经天然存在，要做的是堵住 subagent 直接落库的旁路。

落地分三层，前两层是 agent 中立的（任何会派生 subagent 的宿主都生效），第三层是宿主相关的硬闸：

1. **tool 描述守卫（agent 中立）**：8 个写类 tool（`memory_add` / `memory_update` / `memory_forget` / `memory_feedback` / `memory_session_end` / `memory_review_resolve` / `memory_wm_write` / `memory_wm_clear`）的 description 末尾统一带"仅限主 agent 调用，subagent 禁止使用"的约束。工具描述跟着工具走，subagent 只要能看到这个 tool 就会看到这句——这是唯一不依赖宿主的提示词通道。
2. **SKILL.md 标准约束语（agent 中立）**：`/SKILL.md` 第七节"subagent 记忆纪律"给出一段可直接复制的约束原文，遵循规范的主 agent 派活时会把它附进每个 subagent 的任务 prompt；subagent 需要的历史背景由主 agent 检索后喂进 prompt，subagent 返回的"建议沉淀的记忆"由主 agent 审阅入库。
3. **宿主工具面硬闸（宿主相关）**：在宿主的 subagent 配置里摘掉 8 个写类工具。kimi-code 的落地是 `agents/coder.md` 覆盖文件（`override: true` + `disallowedTools`，由 `scripts/install_kimi_code.sh` 装到 `~/.kimi-code/agents/`；内置 `coder` 是唯一带 MCP 工具的 subagent，explore/plan 无需处理）；其他宿主按各自的 subagent 工具配置同理裁剪。提示词约束是软约束，这层把写工具从执行层摘掉才是真闸。

两点说明：

- **不做服务端按调用方降级**：主 agent 与 subagent 共用同一条 MCP 连接，服务端无从区分调用方身份，做了也是没有约束力的假闸。
- **例外通道**：常驻的命名 agent（有固定身份、反复被调用）不受此限，它自己的记忆用 `agent:<名字>` scope 沉淀，与全局召回隔离。
