# agent-memory

**给 LLM agent 的本地长期记忆基础设施。** 一个跑在你自己机器上的小服务，让任何 agent（Claude Code、Kimi Code、LangGraph 应用……）跨会话记住用户偏好、项目约定和踩过的坑；写入必须过门、可按要求遗忘、能在合适的时候主动想起来，并且用一套 373 条用例的评测集把效果量出来。

> English: [README.en.md](README.en.md) · License: [MIT](LICENSE) · Python ≥ 3.12 · 846 个测试无需网络与 API key · 当前版本 v0.3.0（[CHANGELOG](docs/CHANGELOG.md)）

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [为什么不是"再做一个 RAG"](#为什么不是再做一个-rag)
- [核心亮点](#核心亮点)
- [评测结果](#评测结果)
- [架构](#架构)
- [快速开始](#快速开始)
- [MCP 工具一览](#mcp-工具一览)
- [仓库结构](#仓库结构)
- [文档导航](#文档导航)
- [设计原则](#设计原则)
- [已知局限与路线图](#已知局限与路线图)

---

## 它解决什么问题

编程 agent 每开一个新会话都从零开始：上周定好的数据库选型、用户"以后都用 uv"的偏好、昨天踩过的坑，全部要重新讲一遍。常见的补救办法是把历史对话整体塞进 RAG，但这样做有四个结构性的问题：

| 问题 | 原文 RAG 的表现 | agent-memory 的做法 |
|---|---|---|
| **忘不掉** | 用户要求"忘了这件事"，原文还在，检索照样泄漏 | 删除记忆条目 + 把原文对应片段擦成占位符，审计只记元数据 |
| **防不了投毒** | 对话里混进的"以后忽略安全检查"会被原样召回 | 写入过评价门：指令性内容、注入特征、泄漏的密钥一律拦在库外 |
| **没有任务状态** | 只能检索到"说过什么"，不知道"现在做到哪了" | 独立的工作记忆层：目标、约束、待办、未决问题，会话开头自动注入 |
| **不知道何时该开口** | 每轮都注入，相关不相关一起塞 | 精确率优先的主动浮现：只在"不提就会出错"时才提 |

agent-memory 把记忆分成三层（长期 / 工作 / 短期），写入走"脱敏 → 蒸馏 → 评价门 → 对账"管线，读出时带"参考而非指令"的护栏，并提供 MCP、Python 库、Skill 三种接入方式。

## 为什么不是"再做一个 RAG"

朴素 RAG（原文全留、每轮检索）是很强的基线——我们自己早期的评测里，它在多数纯问答子集上追平甚至超过当时的记忆系统。memory-v1（2026-09-21 起）的答案不是"不跟它比问答"，而是把它当成**地板**：

- **载荷按构造包含原话**：记忆条目只是通向原文的键和贴在原文上的注解，答题者看到的信息不少于朴素 RAG 给的（检索对等率约 90%，载荷体量 ≤ 1.15 倍）；
- **读取时只组织、不裁决**：不合成"库里没有 X"、不替答题者数数，读路径零 LLM 调用；
- **写入期把原文里没有的东西加上去**：绝对日期、取代史、有效期、出处、线索词、用户画像——这些是朴素 RAG 结构上给不出的，也是领先的来源；
- 治理能力照旧是它做不到的：按要求遗忘（泄漏 0%）、投毒拦截（误拦 0%）、任务状态、主动浮现、跨 agent 不串线。

在没见过的外部对话上的读数（同一套默认配置，答题器与评委固定）：LoCoMo 两段新对话 60 题 **42 对 33**（p=0.049）；PersonaMem-32k 60 道选择题 51 对 53（持平）；LongMemEval-S 60 题 46 对 46（持平，读路径版本）。推导与逐轮记录见 [docs/research/memory-v1-design.md](docs/research/memory-v1-design.md)，机制说明见 [docs/design/memory-v1-mechanism.md](docs/design/memory-v1-mechanism.md)。

## 核心亮点

1. **三层记忆，一次组装。** 长期记忆（跨会话的事实 / 偏好 / 流程）、工作记忆（当前任务的目标 / 约束 / 待办 / 未决问题）、短期记忆（直接读宿主自己的会话日志，不复制）。`memory_context` 一次调用拿到"常驻画像 + 工作记忆 + 相关召回"三个分节块。
2. **写入永远过门，但永不丢数据。** 任何候选都要经过脱敏 → 蒸馏 → 评价门 → 对账（ADD / UPDATE / DELETE / NOOP）；判不了的冲突进人工复核队列而不是猜。原文在蒸馏之前就已归档，评价门拒绝的内容可以强制转人工复核。
3. **记忆是参考，不是指令。** 蒸馏拒绝提炼指令性内容，评价门拦截提示词注入特征，注入块自带护栏声明。当记忆与当前请求冲突时，永远以当前请求为准。
4. **可验证的遗忘。** `memory_forget_request` 同时删除记忆条目和原文归档中的对应片段，审计日志只记元数据，检索层与回答层泄漏均为 0%。
5. **主动浮现（精确率优先）。** LLM 线索扩展 + 一跳扩散 + 逐条判断"不提就会出错 / 原理相通值得点明"，宁可少说。系统层 F0.5 77%，误插话率 2/13。
6. **双时态与变更史。** 每条记忆带 `valid_from / valid_to`，对账 UPDATE 时旧版本折进 `history`；蒸馏时按会话日期换算相对时间、识别追溯更正。"截至某时"问答准确率 97%。
7. **原话随记忆一起到场。** 归档脱敏后建派生索引（向量 + 词级 / trigram 全文）；检索返回的是"证据束"——同一处原话及其注解（取代史、有效期、出处、事件日期），原话在前、注解在后，只是复述原话的条目不再渲染。答题者永远能回到证据核实。
8. **离线进化闭环，可回滚。** `agent-memory evolve` 做去重合并、离线复核、降权归档，产出**提案**而非直接改库；三档验证（边界 / 留存 / 安全）任一不过即否决，晋升前快照、晋升后审计、随时回滚。
9. **宿主中立，没有 API key 也能用。** MCP over HTTP / MCP stdio / LangGraph 库 / Skill 四种接入；订阅制 agent 拿 `memory_distill_prompt` 的协议在自己的上下文里蒸馏，服务端照样校验、脱敏、过门、对账。
10. **自带评测集与诚实的报告。** MemCompass：8 个子集 373 条合成用例，覆盖公开评测集没有的能力（遗忘、投毒、主动浮现、任务状态、跨 agent、双时态……），程序化金标、异源评委、配对统计、消融、对照组，报告里连"朴素 RAG 在哪里赢了我们"都写清楚。

## 评测结果

**外部验收（memory-v1，2026-09-22，每个来源只测一次；朴素 RAG 同场、同答题器 deepseek-v4.1-flash、同评委 glm-5.3-flash，AML 公开的答题与评分提示词）**：

| 来源 | 题量 | 朴素 RAG | memory-v1 | 配对 |
|---|---|---|---|---|
| LoCoMo（未见过的 conv-41/42） | 60 | 33（55%） | **42（70%）** | 独赢 13 / 独输 4，McNemar p=0.049 |
| LoCoMo 首测（conv-26/30） | 54 | 31（57%） | **37（69%）** | 独赢 10 / 独输 4，p=0.18，方向一致 |
| PersonaMem-32k（4 份历史，选择题） | 60 | 53 | 51 | 独赢 2 / 独输 4，p=0.69（持平）；全文上下文 47 |
| LongMemEval-S（读路径版本） | 60 | 46 | 46 | 可答题 45 对 45，错误弃答 1 对 1（持平） |

LoCoMo 上的领先主要来自前提不成立的对抗题（9 对 0，载荷首条的静态行为协议起作用），多跳与时间题各 +1，单跳与开放域各 −1；载荷体量 1.15 倍于朴素 RAG。PersonaMem 上事实回忆 21/21、变更原因 6/6、推荐 7/7 与朴素 RAG 相等，唯一落后的 suggest_new_ideas 类（5 对 8 / 14）对全文上下文也只有 6/14。

自建验证集（一份 55 场会话的共享历史，60 题 9 个能力桶）58/60，朴素 RAG 39/45、全文上下文 40/45，每桶不低于两个对照；34 道可答事实题没有一次错误弃答。方法：[docs/research/benchmark-suite/README.md §8](docs/research/benchmark-suite/README.md)。

**内部评测集**（v0.2.2 时的读数）[MemCompass v0.3](docs/research/benchmark-suite/README.md)（8 个子集 / 373 条用例，本仓库自建，全部合成数据）。下表是 test 切分（193 条）上 **v0.2.2 与前一版本的逐题配对比较**，McNemar 精确检验：

| 能力 | 子集 / 模式 | v0.2.2 | 前一版本 | p | 机制证据 |
|---|---|---|---|---|---|
| 按要求遗忘 | fg / 问答 | 100%（15/15） | 40% | .004 | 检索层硬泄漏 0% vs 70% |
| 回溯补全 | ca / 端到端 | 88%（30/33） | 19% | <.001 | 消融关掉原文工具后降到 75% |
| 主动浮现（系统层） | pr / 系统层 | 78%（25/32，F0.5 77%） | 41%（13/32） | .004 | 消融关掉主动浮现后回到 13/32 |
| 跨 agent 迁移（系统层） | xa / 系统层 | 83% | 0% | .002 | 宿主中立的归档与注入 |
| 时间与变化 | at / 问答 | 97% | 57% | .007 | 双时态与追溯更正 |
| 当下保真 | pf / 端到端 | 100% | 84% | .016 | 压缩前情节卡片 |
| 投毒鲁棒 | mp / 问答 | 100% | 89% | .250 | 攻击成功率两版都是 0%，差异在误拦 0% vs 21% |

**诚实的注脚**：

- 每条用例只有 2–8 个会话，朴素 RAG 在多数纯问答子集上持平或更好（见上一节）；长历史档位（每题约 35 万 token）尚未构建，是下一步最重要的工作。
- 单种子、n < 50 的子集区间较宽，报告里逐处标注了"只看方向"。
- 评委与答题器异源（DeepSeek 答题、Kimi K3 评委、Claude 修订用例），评委间一致率 85%–98%（κ 0.69–0.93）；尚未做人工一致性研究。

完整报告（对照组、消融、成本、用例体检、局限）：[`docs/research/benchmark-suite/results/2026-09-16-v03-report.md`](docs/research/benchmark-suite/results/2026-09-16-v03-report.md)。复现方法见 [`evals/memcompass/README.md`](evals/memcompass/README.md)。

## 架构

```
                 ┌──────────────────────────────────────────────────────┐
   宿主 agent    │  memory_context = 常驻画像 + 工作记忆 + 相关召回      │
 (MCP / 库 / Skill)  memory_surface = 主动浮现（精确率优先）              │
                 └───────────────▲──────────────────────────▲───────────┘
                                 │ 读                        │ 读
        ┌────────────────────────┴────────┐   ┌──────────────┴──────────────┐
        │ 长期记忆  data/memory/*.md      │   │ 工作记忆  data/working/      │
        │ 唯一事实来源，按 scope 隔离      │   │ 目标/约束/待办/未决问题       │
        │ + 可重建索引 index.db           │   │ 服务端增量整理 wm_refresh    │
        │   (sqlite-vec 1024d + FTS5)     │   └──────────────▲──────────────┘
        └────────────────────────▲────────┘                  │ 只过脱敏
                                 │ 写                        │
   ┌─────────────────────────────┴──────────────────────────────────────┐
   │ 写入管线： 脱敏 → 蒸馏 → 评价门 → 对账(ADD/UPDATE/DELETE/NOOP) → 变更传播 │
   │           判不了的 → data/review_queue/（人工复核）                    │
   └─────────────────────────────▲──────────────────────────────────────┘
                                 │ 先归档再蒸馏
        ┌────────────────────────┴────────┐   ┌─────────────────────────────┐
        │ 原文归档  data/raw/（只追加）    │   │ 短期记忆 = 宿主自己的会话日志 │
        │ 脱敏后归档 + 派生索引 raw_index │   │ transcript 适配层直接解析     │
        └─────────────────────────────────┘   └─────────────────────────────┘

   离线：agent-memory evolve → 提案 → 三档验证 → 快照 → 晋升 → 审计 / 回滚
```

作用域：`global`（跨项目）/ `repo:<项目名>` / `agent:<宿主名>`，检索只看当前 scope 加 `global`。三条不可违反的红线（数据三层分离、写入过门、评测可信根）见 [AGENTS.md](AGENTS.md)。

## 快速开始

### 1. 安装并启动

```bash
git clone https://github.com/ac0033/agent-memory.git
cd agent-memory
uv sync                 # Python ≥ 3.12；bge-m3 嵌入模型首次使用时下载（约 2 GB）
uv run pytest -q        # 846 个测试，不需要网络与 API key

export AGENT_MEMORY_LLM_API_KEY=sk-...   # 任意 OpenAI 兼容端点；默认 DeepSeek deepseek-flash
# 可选：AGENT_MEMORY_LLM_BASE_URL / AGENT_MEMORY_LLM_MODEL / AGENT_MEMORY_DATA_DIR
uv run python -m agent_memory.server.http_server
# 监听 http://127.0.0.1:8765/mcp（只绑回环地址）
```

没有 LLM key 时，检索、手动写入、反馈、工作记忆、归档等不依赖 LLM 的功能照常可用；对话蒸馏可交给宿主自己做（见下文）。

### 2. 接入 agent

**方式一：MCP over HTTP（推荐，任何 MCP 客户端）**。把 `http://127.0.0.1:8765/mcp` 注册为 streamable-http 类型的 MCP server，然后让 agent 读一次 `http://127.0.0.1:8765/SKILL.md`。`/bootstrap` 路由给出一段可直接粘贴的接入指令。

**方式二：MCP stdio（宿主按会话拉起子进程）**。Claude Code / Kimi Code 的配置片段：

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "uv",
      "args": ["run", "python", "-m", "agent_memory.server.mcp_server"],
      "env": {
        "AGENT_MEMORY_DATA_DIR": "C:/Users/<you>/.agent-memory/data",
        "AGENT_MEMORY_LLM_API_KEY": "sk-..."
      }
    }
  }
}
```

**方式三：Python 库（LangGraph / LangChain 应用）**：

```python
from langgraph.prebuilt import create_react_agent
from agent_memory.long_term.adapters.langgraph.store import AgentMemoryStore
from agent_memory.long_term.adapters.langgraph.tools import build_memory_tools
from agent_memory.long_term.retrieve.resident import build_system_context

store = AgentMemoryStore()                       # LangGraph BaseStore，namespace ("memories", <scope>)
tools = build_memory_tools()                     # 17 个 ReAct tool，三层记忆全暴露
prompt = "你是用户的编程助手。\n\n" + build_system_context("repo:myproj")   # 常驻画像进 system prompt
agent = create_react_agent(model, tools, prompt=prompt, store=store)
```

可运行示例：`uv run python examples/langgraph_demo.py`。

**方式四：Skill**。把 `skills/agent-memory/` 复制到宿主的 skills 目录（Claude Code `~/.claude/skills/`，Kimi Code `~/.kimi-code/skills/`），配合上面任一 MCP 方式使用。Kimi Code 有一键安装脚本 `scripts/install_kimi_code.sh`（合并 mcp.json、装 Skill、装 subagent 只读覆盖、注册会话开头注入 hook）。

### 3. 没有 API key 的订阅制 agent

宿主本身就是大模型，蒸馏可以自己做：`memory_distill_prompt()` 拿协议 → 宿主在自己的上下文里产出 `{"memories": [...]}` → `memory_add(distilled_json=...)` 提交。服务端照常校验、脱敏、过门、对账。

更多用法（CLI 蒸馏、评估命令、离线整理、人工复核、hook、工作记忆、会话收尾）见 [使用手册](docs/usage.md)；宿主 runtime 必须自己承担的职责清单见 [接入指南 §四](docs/agent-integration.md)。

## MCP 工具一览

25 个 MCP tool（stdio 与 HTTP 共用同一业务实现）：

| 分组 | 工具 | 用途 |
|---|---|---|
| 读取 | `memory_context` `memory_search` `memory_wm_read` `memory_transcript_read` `memory_distill_prompt` `memory_consistency_check` | 一次组装上下文；混合检索（稠密 + BM25 → RRF → 置信度 × 时间衰减）；读工作记忆；读会话日志；宿主蒸馏协议；记忆层与索引一致性检查 |
| 写入 | `memory_add` `memory_update` `memory_forget` `memory_feedback` `memory_wm_write` `memory_wm_clear` | 长期记忆走完整管线；工作记忆只过脱敏 |
| 会话 | `memory_session_end` | 会话收尾：归档 + 联合蒸馏 + 清理已完成待办（有未完成待办时否决） |
| 复核 | `memory_review_list` `memory_review_resolve` | 管线不敢自动入库的候选交人裁决（approve / modify / discard） |
| 原文归档 | `memory_archive_search` `memory_archive_read` `memory_archive_sync` | 脱敏后只追加的原文归档，可检索；记忆只有要点或看起来不对时回溯原文 |
| 主动浮现 | `memory_surface` | 精确率优先的"记忆副手"，在 agent 会漏掉的时候提醒它 |
| 待确认队列 | `memory_confirm_enqueue` `memory_confirm_list` `memory_confirm_resolve` | 无人值守时把需要用户拍板的事挂起，其余照常处理 |
| 工作记忆整理 | `memory_wm_refresh` | 服务端按最近轮次增量整理目标 / 约束 / 待办 / 未决问题 |
| 情节卡片 | `memory_episode_pack` | 上下文压缩前把标识符、端口、路径、报错原文等原样细节存成卡片 |
| 遗忘请求 | `memory_forget_request` | 执行用户明确的遗忘要求：删记忆 + 擦原文片段，审计只记元数据 |

写类工具的描述都注明"仅限主 agent 调用"；subagent 只读，结论回传主 agent 后由它策展沉淀。Kimi Code 的 subagent 覆盖文件在 `agents/coder.md`。

## 仓库结构

```
agent-memory/
├── agent_memory/            # Python 包（import agent_memory）
│   ├── config.py / models.py    配置（AGENT_MEMORY_* 环境变量）与记忆条目 schema
│   ├── llm.py / confirmations.py  OpenAI 兼容 LLM 客户端；待确认队列
│   ├── long_term/               长期记忆：store（Markdown + SQLite 索引）/ ingest（脱敏、蒸馏、评价门、对账、
│   │                            变更传播、情节卡片、遗忘）/ retrieve（bge-m3 混合检索、注入、常驻画像、主动浮现）/
│   │                            evolve（离线整理闭环）/ adapters/langgraph
│   ├── working/                 工作记忆（任务状态 + 服务端增量整理）
│   ├── short_term/              短期记忆：宿主会话日志适配层
│   └── server/                  MCP stdio server、HTTP 常驻服务、MemoryService 业务层
├── skills/agent-memory/     # Skill（SKILL.md，HTTP 服务的 /SKILL.md 路由分发同一文件）
├── agents/coder.md          # Kimi Code subagent 覆盖：摘掉记忆写类工具
├── scripts/                 # 宿主 hook（每 N 轮强制蒸馏、会话开头注入工作记忆、主动浮现）与一键安装
├── examples/                # LangGraph 最小接入示例
├── evals/                   # 可信根（agent 不得修改）：layer1–3 / prefix 回归集 + MemCompass 冻结副本
├── docs/                    # 使用手册、接入指南、设计文档、研究与评测（见下）
├── tests/                   # 846 个测试（慢测试默认跳过：uv run pytest -m slow）
└── data/                    # 运行时数据（gitignored）：raw / memory / working / review_queue / snapshots / logs
                             #   + dev/（自建验证集）、external/<来源>/（外部测试集）
```

## 文档导航

| 想做什么 | 看哪里 |
|---|---|
| 把记忆接进我的 agent，宿主要负责什么 | [docs/agent-integration.md](docs/agent-integration.md) |
| 日常用法：CLI、评估命令、离线整理、人工复核、hook、会话收尾 | [docs/usage.md](docs/usage.md) |
| 理解三层记忆的设计与取舍 | [docs/design/memory-architecture.md](docs/design/memory-architecture.md) |
| 记忆现在怎么读、怎么写、为什么（memory-v1） | [docs/design/memory-v1-mechanism.md](docs/design/memory-v1-mechanism.md) |
| 机制是怎么从能力框架推导出来的、每一轮实测 | [docs/research/memory-v1-design.md](docs/research/memory-v1-design.md) |
| 好的 agent 记忆应该具备哪些能力、怎么度量 | [docs/research/agent-memory-capability-framework.md](docs/research/agent-memory-capability-framework.md) |
| 评测集 MemCompass 的设计、构造脚本、校验与运行 | [docs/research/benchmark-suite/README.md](docs/research/benchmark-suite/README.md) |
| 最新评测报告 | [docs/research/benchmark-suite/results/](docs/research/benchmark-suite/results/) |
| v0.2 每个机制为什么这么做 | [docs/research/optimization-v02.md](docs/research/optimization-v02.md) |
| 各版本改了什么 | [docs/CHANGELOG.md](docs/CHANGELOG.md) |
| 各阶段交付记录、缺陷复盘、可靠性加固 | [docs/history/](docs/history/) |
| 给在本仓库工作的人和 agent 的规范（红线、约定、行为语义） | [AGENTS.md](AGENTS.md) |
| 参与贡献 | [CONTRIBUTING.md](CONTRIBUTING.md) |

## 设计原则

- **D1 数据三层分离**：`data/raw` 只追加；`data/memory` 的 Markdown 是唯一事实来源；`data/index.db` 是可重建的派生索引，绝不手改。
- **D2 写入过门**：原始对话不直接入库，必须经脱敏 → 蒸馏 → 评价门 → 对账；蒸馏绝不提炼指令性内容。
- **D6 可信根**：`evals/`、rubric、发布门槛、审计日志禁止 agent 自行修改。评测集在 `docs/research/benchmark-suite/` 编写与核验，由维护者迁入冻结副本。
- **fail-closed 但不 fail-lost**：配置非法、校验失败、证据缺失直接报错，不静默降级；写入路径的内容永不因故障丢失。
- **本地优先**：所有数据是你磁盘上的 Markdown 与 SQLite 文件，服务默认只绑 127.0.0.1，没有任何数据出站。

## 已知局限与路线图

- **领先不是在每个外部集上都显著**：LoCoMo 显著领先，PersonaMem 与 LongMemEval-S 与朴素 RAG 持平。LongMemEval-S 的读数来自读路径版本（写入期的绝对日期、线索词、逐实例沉淀之前）；MemCompass 内部表格的读数来自 v0.2.2。
- **PersonaMem 的 suggest_new_ideas 类略低于朴素 RAG**（5 对 8 / 14）；该类对全文上下文也只有 6/14，属答题者层面的"选泛泛选项"。
- **关联与图式归纳（K6 离线归纳）未实现**：离线整理的验证逻辑属于可信根，需要新的提案类型。
- **写入成本**：同一人物会话密集时，对账对每条候选各调一次 LLM，几十场会话的重写可达小时级；按批判定是待办。
- **LangGraph 适配只覆盖 15 个基础工具**，v0.2 新增的 10 个目前仅 MCP 侧提供。
- 内部评测集为合成数据，评委—人工一致性研究尚未开展。

## 许可

MIT，见 [LICENSE](LICENSE)。`evals/memcompass/` 与 `docs/research/benchmark-suite/datasets/` 下的评测数据均为合成数据，同样按 MIT 发布。
