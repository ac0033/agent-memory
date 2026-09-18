# AML 契约自测报告：agent-memory 在 LongMemEval-S 上的表现（2026-09-18）

> 目的：在决定是否参加 Agent Memory Leaderboard（AML）之前，按 AML 的 Add / Search 契约、用 AML 的题目、答题提示词与评分提示词，量一次 agent-memory 在真实长历史（每题约 48 个会话、约 12 万 token）上的水平，并与朴素 RAG 对照。这正是 MemCompass v0.3 缺失的 K4 长历史档位（v0.3 报告 §1.3c）。
> 结论先说：**样本 18 题上，记忆系统与朴素 RAG 分不出高下**（评委 A：14/18 对 12/18；评委 B：13/18 对 15/18；两评委一致的题：13 对 12）。证据检索两者都是 18/18 命中。记忆系统的价值不在这类问答题的正确率上。

## 1. 怎么测的

| 项 | 本次自测 | AML 正式评测 | 偏差说明 |
|---|---|---|---|
| 数据 | LongMemEval-S（cleaned，HF `xiaowu0162/longmemeval-cleaned`），500 题中按 6 个题型分层各抽 3 题，seed=1，共 18 题（含 2 题弃答题） | 公开集 + 私有集 | 只有公开题；n=18 只看方向 |
| Add | 每个 haystack 会话调用一次 `MemoryService.add(conversation_json=...)`，走 归档 → 蒸馏 → 评价门 → 对账；每题一个隔离的临时数据目录与 scope | 每会话一次 Add，>20 条消息或 >2,000 词时切分 | 我们不切分，整段会话一次写入 |
| Search | query 为题面原文，top_k=100：混合检索的记忆条目（≤40，实际 20）在前，原文归档命中补足；每条 content 前缀会话日期 | 同 | 一致 |
| 答题 | AML `data/longmemeval-s/pipeline.py` 的 `OPEN_ENDED_ANSWER_TEMPLATE` 原文 | 同 | 一致 |
| 评分 | AML `ACCURACY_PROMPT` 原文，二值 CORRECT / WRONG | 同 | 一致 |
| 内部 LLM 与答题器 | deepseek-v4.1-flash（经 WorkBuddy 内置 CodeBuddy CLI 调用） | gpt-4o-mini | **不同模型**，分数不能与榜单直接比 |
| 评委 | A：glm-5.3-flash；B：deepseek-v4.1-flash（均经 WorkBuddy CLI） | 平台评委 | 两评委一致率 32/36 |
| 嵌入 | bge-m3，`embedding_max_seq_length=512`（CPU 机器，见 §6） | — | 向量只看每条消息前 512 token，原文与 BM25 不受影响 |
| 对照 | 朴素 RAG：原始消息逐条切块，bge-m3 + BM25 → RRF，同一 top_k=100 | — | 与 MemCompass 报告同一实现 |

被测代码 `8e591dc`（agent-memory v0.2.2 + 本轮 runner 改动）。runner：`runners/aml_selftest.py`；结果：`data/logs/aml_selftest/aml-s18/`（gitignored，本地保留）。另有先导轮 `aml-pilot`（5 题，内部 LLM 与答题器走 DeepSeek 官方 API `deepseek-flash`，评委 DeepSeek 同源），用于核对链路一致性。

## 2. 结果

### 2.1 正确率（题型各 3 题）

| 题型 | 记忆系统 · 评委 A | 朴素 RAG · 评委 A | 记忆系统 · 评委 B | 朴素 RAG · 评委 B |
|---|---|---|---|---|
| knowledge-update | 2/3 | 1/3 | 2/3 | 2/3 |
| multi-session | 2/3 | 3/3 | 2/3 | 3/3 |
| single-session-assistant | 3/3 | 3/3 | 3/3 | 3/3 |
| single-session-preference | 3/3 | 1/3 | 2/3 | 3/3 |
| single-session-user | 2/3 | 2/3 | 2/3 | 2/3 |
| temporal-reasoning | 2/3 | 2/3 | 2/3 | 2/3 |
| **合计** | **14/18** | **12/18** | **13/18** | **15/18** |

- 评委 A（glm-5.3-flash）下：记忆系统独赢 3、朴素 RAG 独赢 1、都对 11、都错 3。
- 两评委一致率 32/36；4 处分歧全部在 single-session-preference 与 knowledge-update 的"是否包含金标要点"上（例如金标 "The music shop on Main St."，朴素 RAG 答 "Rhythm Central on Main St"，A 判错、B 判对）。分歧 4 题中 3 题偏向朴素 RAG，所以**排名随评委翻转**。
- 两评委一致的题：记忆系统 13 对、4 错、1 有争议；朴素 RAG 12 对、3 错、3 有争议。
- n=18、单种子，任何差异都在噪声内，不做显著性检验。

### 2.2 逐题

| 题型 | 题 | 记忆系统 | 朴素 RAG | 证据会话排名（记忆 / RAG） | 系统内部积分 | 沉淀记忆条数 |
|---|---|---|---|---|---|---|
| knowledge-update | 031748ae_abs（弃答） | ✗ | ✗ | 1 / 1 | 5.8 | 89 |
| knowledge-update | 22d2cb42 | ✓ | ✗ (B ✓) | 1 / 1 | — | 111 |
| knowledge-update | dad224aa | ✓ | ✓ | 2 / 5 | 6.9 | 109 |
| multi-session | 46a3abf7 | ✗ | ✓ | 1 / 1 | 12.7 | 109 |
| multi-session | 681a1674 | ✓ | ✓ | 1 / 1 | 10.0 | 74 |
| multi-session | 81507db6 | ✓ | ✓ | 1 / 1 | 7.7 | 98 |
| single-session-assistant | 6ae235be | ✓ | ✓ | 21 / 1 | 9.6 | 77 |
| single-session-assistant | ac031881 | ✓ | ✓ | 21 / 1 | 14.5 | 117 |
| single-session-assistant | f523d9fe | ✓ | ✓ | 2 / 1 | 8.8 | 58 |
| single-session-preference | 06f04340 | ✓ | ✓ | 1 / 11 | 5.8 | 97 |
| single-session-preference | 0a34ad58 | ✓ | ✗ (B ✓) | 1 / 1 | 5.6 | 98 |
| single-session-preference | 1c0ddc50 | ✓ (B ✗) | ✗ (B ✓) | 1 / 4 | 5.3 | 82 |
| single-session-user | 86f00804 | ✓ | ✓ | 21 / 1 | 5.6 | 87 |
| single-session-user | bc8a6e93_abs（弃答） | ✗ | ✗ | 1 / 1 | 6.4 | 113 |
| single-session-user | d52b4f67 | ✓ | ✓ | 22 / 1 | 5.0 | 83 |
| temporal-reasoning | gpt4_385a5000 | ✓ | ✓ | 1 / 1 | 11.8 | 105 |
| temporal-reasoning | gpt4_7bc6cf22 | ✗ | ✗ | 1 / 1 | 10.9 | 91 |
| temporal-reasoning | gpt4_ec93e27f | ✓ | ✓ | 1 / 2 | 6.3 | 107 |

（✓/✗ 为评委 A；括号内为评委 B 的不同判定。22d2cb42 的系统积分在积分统计修复前跑完，未记录。）

### 2.3 检索

- 证据会话召回@100：两者 18/18。
- 记忆系统返回的 78 条里前 20 条是蒸馏记忆、后 58 条是原文归档命中；有 4 题证据会话只出现在原文部分（排名 21–22），说明对应事实**没有被蒸馏成记忆**，靠原文回退才答对。朴素 RAG 的证据排名中位数为 1。
- 记忆系统的答题提示平均 4.7 万字符，朴素 RAG 8.1 万字符。

### 2.4 成本与耗时

| 项 | 记忆系统 | 朴素 RAG |
|---|---|---|
| 每题写入耗时 | 约 15 分钟（48 个会话，每会话一次蒸馏 + 对账，经 CLI 每次调用约 18 s） | 0（只嵌入） |
| 每题嵌入耗时（两者共用缓存） | 约 20 分钟（CPU，bge-m3，约 500 条消息） | 同 |
| 每题内部 LLM | 约 61 次调用，输入约 68 万 token（其中大量为重复的系统提示，命中缓存）、输出约 6.7 万 token | 0 |
| 每题积分 | 系统 5–15 + 答题 0.2–0.3 | 答题约 0.3 |
| 全轮积分 | 约 160–180（早期 3 行未计系统积分） | 含在内 |
| 全轮墙钟 | 约 23 小时（2026-09-17 11:54 → 09-18 10:38），其中大半是 CPU 嵌入、电池降频与睡眠 | — |

先导轮 `aml-pilot`（DeepSeek 官方 API，5 题 9 行有效结果）与本轮在共同题上的判定 **9/9 一致**，说明换到 WorkBuddy 链路没有改变结果。

## 3. 观察

1. **蒸馏出来的记忆是中文的**。英文对话进来，库里存的是"用户习惯 7:30 起床"——`distill.py` 的 schema 写死了"content：一句话原子事实（中文）"。答题模型跨语言多数能答对，但这是系统性的额外负担，也不符合 agent 中立的定位；应改为"跟随对话语言"。
2. **弃答题两套系统都编答案**（2/2）。AML 的答题模板要求"只用给定记忆"，但模型面对相近事实（给侄女烤过蛋糕 / 问的是给叔叔烤了什么）仍作答；记忆系统没有额外的"证据不足"信号。这是 K12/H1 的能力缺口，与 MemCompass 里的可信性子集不同（那里测的是拒绝投毒，不是拒答无据问题）。
3. **多会话计数题失手**（46a3abf7："我一共有几个鱼缸"）：先导轮与本轮都答 2（金标 3），朴素 RAG 都答 3。蒸馏把"计划设隔离缸"这类状态压成了摘要，跨会话计数就漏了一个；这是蒸馏"选择性"规则的代价，在 MemCompass 的短历史里没暴露过。
4. **原文回退在长历史上是刚需**：4/18 题的证据只在原文命中里。v0.2 的 P03/P23 设计在这里得到了正面验证。
5. **朴素 RAG 在这类题上就是很强**：证据排名中位数 1，答题模型拿到原文就能答。这与 MemCompass v0.3 的结论一致——结构化记忆的收益不在"多答对几道问答题"。

## 4. 对参赛的判断

- **技术上可以参评**：Add / Search 契约已经在本 runner 里映射完成，写一层 HTTP 适配（`user_id` → 隔离 scope；同步 Add；top_k=100 返回 memories + 原文）即可；本轮已验证隔离、写入、检索、答题全链路。
- **成绩预期**：在 LongMemEval 类问答题上大致与朴素 RAG 持平，本轮 18 题约 72%–78%；正式评测换成 gpt-4o-mini 后数字会变，方向不会。榜单上的领先者是专门打磨过的检索栈，**争名次不现实**。
- **值得参评的理由**：平台替我们出答题与评委成本，LongMemEval-S 就是我们缺的长历史档位；smoke 模式每小时一次、只对自己可见，是低成本的外部对照。
- **参评前该做的**：(a) 蒸馏语言跟随对话；(b) 弃答：检索置信度低或证据只在原文且不相关时，让答题器有"证据不足"的依据；(c) Add 内部模型换成 gpt-4o-mini 并做一次英文验证；(d) 公网端点或"学术·代码"路线，按用户此前的原则等 AML 回复后再定。

## 5. 与正式评测的偏差（诚实清单）

1. 内部 LLM、答题器不是 gpt-4o-mini；评委不是平台评委。
2. 只有公开题、18 题、单种子。
3. 整段会话一次写入，未按 20 条 / 2,000 词切分。
4. 嵌入输入截到 512 token（CPU 时间所迫），可能略降原文检索质量；两套系统同等受影响。
5. 评委 A 与答题器不同源（glm 对 deepseek）；评委 B 与答题器同源（deepseek），因此以评委 A 为主、B 作交叉。

## 6. 运行事故与处置（供复现者参考）

- **CPU 嵌入是瓶颈**：bge-m3 在 i5-1135G7 上每条消息 2–6 秒，一题 500 条；新增 `Settings.embedding_max_seq_length`（环境变量 `AGENT_MEMORY_EMBEDDING_MAX_SEQ_LENGTH`）并在 runner 里分批预热、每批落盘缓存。
- **内存**：16 GB 机器把 277 MB 的数据集整份 `json.load` 后再加载模型会 MemoryError / torch 段错误；改为先抽样成小文件（`data/external/lme_s_sample_seed1_n{1,3}.json`）。
- **原生崩溃**：主进程每 1–3 小时一次 0xC0000005（Windows + Python 3.14 + torch/sqlite-vec），用守护脚本自动 `--resume`；LLM 调用与向量都有磁盘缓存，续跑基本不重复花费。
- **WorkBuddy CLI 间歇失败**：stderr 显示经本机代理（127.0.0.1:7897）的 TLS 连接被断开；客户端已改为容忍非 JSON 输出、短退避重试并落盘失败样本（`data/logs/aml_selftest/codebuddy_failures/`）。
- **推理模型的 max_tokens**：deepseek 系列把思考与可见回答算在同一预算里，512 会导致回答为空（先导轮第一题因此误判），现为 4096。
- **电源与睡眠**：笔记本用电池时降频 15–20 倍，睡眠时进程冻结；两者合计占了全轮墙钟的一半以上。
- **评委额度**：Kimi 会员月额度与 token-plan 周额度同日耗尽，评委改走 WorkBuddy CLI（`--judge-role judge_codebuddy`），并提供 `--rejudge` 供事后换评委复判。

## 7. 复现

```bash
# 数据：把 longmemeval_s_cleaned.json 放到 data/external/，再抽样
uv run python -c "import sys; sys.path.insert(0,'docs/research/benchmark-suite/runners'); from aml_selftest import load_questions; import json; from pathlib import Path; json.dump(load_questions(Path('data/external/longmemeval_s_cleaned.json'),3,1,None), open('data/external/lme_s_sample_seed1_n3.json','w',encoding='utf-8'), ensure_ascii=False)"
# 运行（全部经 WorkBuddy CLI；--resume 可续跑）
set AGENT_MEMORY_EMBEDDING_MAX_SEQ_LENGTH=512
uv run python docs/research/benchmark-suite/runners/aml_selftest.py --run-id aml-s18 --data data/external/lme_s_sample_seed1_n3.json --n-per-type 3 \
    --systems naive_rag,am --system-via codebuddy --answer-via codebuddy --judge-role judge_codebuddy --judge-model glm-5.3-flash --env-file <你的 .env>
# 第二评委复判
uv run python docs/research/benchmark-suite/runners/aml_selftest.py --run-id aml-s18 --resume --rejudge --data data/external/lme_s_sample_seed1_n3.json --n-per-type 3 \
    --systems naive_rag,am --system-via codebuddy --answer-via codebuddy --judge-role judge_codebuddy --judge-model deepseek-v4.1-flash --env-file <你的 .env>
```
