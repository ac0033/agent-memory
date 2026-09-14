# runner 草稿（behavior_eval.py + naive_rag.py）

> 状态：v0.1 草稿（2026-09-13）。属于 D6 可信根，**用户审核后亲自迁入 `evals/runners/`**，agent 不得直接写入 `evals/`。
> 已获用户授权：在本目录起草代码，并用临时库加固定答题器在本地试跑种子用例。

## 1. 文件

| 文件 | 作用 |
|---|---|
| `behavior_eval.py` | 行为级评测 runner：专项二（K9、K10，E 模式）；专项一（K7、K8，S 模式和 E 模式） |
| `naive_rag.py` | 朴素 RAG 基线：在原始轮次上做 bge-m3 稠密检索 + 字符 bigram BM25，结果用 RRF 融合 |

## 2. 运行（在仓库根目录）

```bash
# 1) 不调 LLM 的冒烟：检查灌库、检索和 S 模式注入，并输出朴素 RAG 的余弦分布，用于标定阈值
HF_HUB_OFFLINE=1 .venv/Scripts/python.exe docs/research/eval-drafts/runner-draft/behavior_eval.py --dry-run

# 2) 真实运行：密钥只从 --env-file 读取 --env-key 指定的变量，全程不打印
.venv/Scripts/python.exe docs/research/eval-drafts/runner-draft/behavior_eval.py \
    --env-file D:/4_Projects/.env --env-key DEEPSEEK_API_KEY --jobs 4

# 3) 正式评测时使用异源评委（例如 DashScope 的兼容接口）
    ... --judge-env-key DASHSCOPE_API_KEY \
        --judge-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 --judge-model qwen-plus
```

常用参数：
- `--suite`：选择专项；
- `--modes S,E`：选择模式；
- `--conditions`：只跑指定的对照组；
- `--case`：只跑单条用例；
- `--seeds`：种子数；
- `--rag-threshold`：朴素 RAG 的触发阈值；
- `--no-cache`：不使用缓存。

输出：`data/logs/behavior_eval/<时间戳>/`，包括 `results.jsonl`（逐题完整轨迹）和 `summary.md`。该目录已被 gitignore。

## 3. 对照组

| 专项 / 模式 | 对照组 | 说明 |
|---|---|---|
| 专项二 / E | `no_memory` | 只有 ask_user、act |
| | `am_current` | 当前 agent-memory 实际具备的能力：memory_search + ask_user + act。没有原文检索，也没有待确认队列 |
| | `naive_rag` | 只有原文检索：archive_search + ask_user + act |
| | `full_surface` | 目标能力面：memory_search + archive_search / read + ask_user + queue_confirmation + act |
| 专项一 / S | `am_current_s` | 当前系统没有主动浮现机制，永远不注入 |
| | `am_retrieve_always` | 每轮用触发消息检索长期记忆，无条件注入 |
| | `naive_rag_always` / `naive_rag_threshold` | 朴素 RAG 无条件注入 / 最高余弦 ≥ 阈值时才注入 |
| 专项一 / E | `no_memory` / `am_current` / `am_inject_always` / `naive_rag_inject` | 答题器分别在无记忆、可自行检索、自动注入长期记忆、自动注入原文片段四种条件下回复 |

所有对照组共用同一个答题器、同一个评委、同一个注入字符预算（`recall_budget_chars`）。

## 4. 实现要点与已知局限

- **灌库**：直接写入用例里的预置记忆，经过评价门，并用 oracle 完成对账（带 supersedes 的判为 UPDATE），与 e2e_eval 的规则降级模式一致。每条任务使用独立的临时库。
- **答题器协议**：用 `complete_json` 实现逐步工具调用，每步输出一个 JSON，最多 8 步。act 只记录不执行；ask_user 返回用例里的模拟回复；无人值守时 ask_user 返回错误。
- **种子**：现有的 `OpenAILLMClient` 不暴露 temperature 参数。多种子是通过在 system prompt 末尾追加 `[run-seed N]` 来避开缓存、得到不同采样，而不是真正控制温度。**这一点需要在迁入前决定**：是否给 LLM 客户端加 temperature 参数（这会修改 `agent_memory/llm.py`，不属于 D6）。
- **评委同源**：默认评委与答题器是同一个模型，summary 里会标注"只适合冒烟"。正式评测请用 `--judge-*` 参数改成异源评委。
- **并发与段错误**：dry-run 实测发现，多线程并发执行 sqlite-vec 或嵌入等本地原生调用时，偶发段错误（exit 139）。现在用 `_native_lock` 把灌库、建索引、检索、关库串行化，LLM 调用仍然并发。
- **只支持预置记忆灌库**：还没有接入"真实蒸馏灌库"（README §3.3 中的附加项）。
- **迁入时要改的地方**：`CASE_FILES` 的路径改为 `evals/datasets/<suite>/*.yaml`（按用例拆分后的单文件）；`sys.path` 的写法与 e2e_eval 保持一致。
