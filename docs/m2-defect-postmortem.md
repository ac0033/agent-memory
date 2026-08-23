# 管线缺陷复盘合集（postmortem）

本文档是缺陷复盘合集，按"表象 → 根因 → 通病类别 → 修复方式 → 对后续的
设计启示"逐条复盘。

前两节来自 M2 那轮 e2e 评估（layer2 = 18/20）暴露的两个真实管线缺陷，
M4（睡眠学习 / 定期整理循环）的设计应把它们当输入读；最后一节是 M4b
真实演示暴露、随后修复的两个缺陷。

## 缺陷 1：蒸馏产出的 id 含点号，整条候选被静默丢弃（layer2-03）

- **表象**：会话说"仓库从 Python 3.10 升到 3.12"，蒸馏模型产出 id 为
  `python-version-upgrade-to-3.12` 的候选，过不了 kebab-case 校验，
  整条被 `dropped_invalid` 计数后丢弃。库里只剩"锁定 3.10"的旧信息，
  召回时给出过期答案。
- **根因**：`_build_entry` 把 LLM 的自由文本 id 直接交给 pydantic 硬校验，
  校验失败只有"丢弃"一条路。LLM 产出含点号的 id（版本号天然带点）是
  高概率事件，不是异常输入。
- **通病类别**：A——丢弃式防御。"校验不过就扔"把可挽救的数据当成废品。
- **修复方式**：入库统一入口（`_build_entry`）先做规范化再做校验：
  `normalize_entry_id()`（models.py）把非 `[a-z0-9-]` 字符段折叠成单个
  连字符、去首尾连字符；confidence 非法值降为 medium；detail 超长截断到
  800 字符。规范化后仍构造不出合法 MemoryEntry 的（空 content、非 dict、
  id 规范化后为空等），写入 `data/review_queue/`（原始记录原样落盘 +
  失败原因），绝不静默丢弃。所有规范化改写记入
  `DistillResult.normalized_ids / normalized_fields`，可见可审计。
  content 超长不截断（截断会改变事实语义），同样进复核队列。

## 缺陷 2：状态撤销不传播，过期 procedural 记忆排第一（layer2-20）

- **表象**："功能开关 checkout_v2_enabled 已全量删除"把 semantic 状态
  记忆 UPDATE 了，但灰度期沉淀的"排查问题先看开关状态"的 procedural
  记忆没有任何机制感知到前提失效，召回时排第一给出过期建议。
- **根因**：reconcile 的 UPDATE/DELETE 只作用于目标条目本身。旧事实
  可能是其它既有记忆（尤其是 procedural 操作约定）的隐含前提，写入侧
  没有任何"谁依赖了我刚作废的这条事实"的反向检查。
- **通病类别**：B——变更不传播。
- **修复方式**：`ingest/propagate.py` 新增反向传播环节，reconcile 每次
  UPDATE/DELETE 落库后调用：以被取代/被删除的旧条目为 query 检索语义
  近邻（混合检索 + 稠密距离 ≤ 0.35），同 scope 的 procedural 条目全量
  并入（操作约定的前提依赖常被措辞差异掩盖，纯距离阈值会漏），逐条交
  LLM 三分类判定：
  - INVALIDATED（核心前提被推翻）→ 删除，审计快照追加到
    `data/logs/propagation.jsonl`（含被删条目完整 payload，即版本历史）；
  - NEEDS_REVISION（受影响但证据不足以机器改写）→ 进 review_queue
    人工复核，不强行收敛（书第 3 章：保留待确认状态）；
  - UNAFFECTED → 不动。
  LLM 判定失败或输出无法解析一律按不动作 + 进复核队列处理
  （fail-safe，宁可排队等人也不让坏决策落库）。只传播一跳，不级联。

## 通病审计发现的其它隐藏问题

1. **复核队列文件名带时间戳，重跑/重试会堆积重复待办**。
   `write_review_queue` 原文件名是 `{timestamp}_{id}.yaml`，同一条目
   每次排队都生成新文件。已改为内容哈希文件名（`<id>-<sha256前10位>.yaml`，
   哈希不含 queued_at），同条目同内容重复排队覆盖同一文件——借鉴
   memorax 的幂等键思路（`automatic:<client>:<hash>:...`）。queued_at
   保留在 payload 里，不丢时间信息。
2. **confidence 非法值、detail 超长也会整条丢弃**。与缺陷 1 同一行
   代码路径（pydantic 校验失败 → dropped_invalid），同属"能规范化却
   直接扔"。已一并改为规范化。
3. **`memories` 字段不是数组时整批静默丢弃**（`dropped_invalid = 1`，
   原始输出不保留）。现在原始输出整体进 review_queue，保留现场。
4. **审计后确认"该拒绝"的路径**（保持拒绝，但均已带原因可见）：
   gate 的脱敏残留 / 注入特征 / 开头祈使（非 procedural）是安全红线，
   拒绝入库合理（memorax 的 quarantine 思路：redaction_rejected 直接
   拦截）；脱敏后 < 10 字符的候选整条都是敏感信息，复核无价值，保留
   丢弃 + 计数（dropped_redacted）。这两类是"丢弃必须可见"的正当实现，
   未改动语义。

## 对 M4（睡眠学习循环）的设计启示

1. **整理 = 全量变更的批量传播**。M4 定期整理循环不需要另写一套失效
   判定的"机制"（近邻发现、三分类、审计、fail-safe 都与传播共用），但
   **判定函数不能原样复用**：`judge_propagation()` 的 prompt 语义是"旧
   事实已被撤销，请判定邻居是否失效"，拿来复核条目自身会把正常记忆恒判
   INVALIDATED（见下方"缺陷 3"）。离线抽查复核改用语义独立的
   `judge_validity()`。在线传播是一跳、变更驱动；
   M4 是离线、全量扫描，两者共享"失效判定"的内核机制而非同一份 prompt。
2. **级联传播留给 M4，不放在在线路径**。在线传播只走一跳（避免级联
   删除风暴拖慢写入）；失效条目删除后可能又让别的条目失去前提，这种
   二阶效应由 M4 在离线循环里反复跑传播直到不动点来收敛。
3. **review_queue 是 M4 的首要消费对象**。本轮把所有"机器判不了"的
   产出统一收进 review_queue（非法蒸馏记录、低置信度、冲突、
   NEEDS_REVISION、判定失败），且写入幂等。M4 的整理循环应把
   review_queue 当作输入队列：能自动收敛的收敛，不能的升级给人。
4. **审计日志与记忆本体分离**。INVALIDATED 的删除不是无痕的：
   `data/logs/propagation.jsonl` 留完整快照（谁触发的、旧事实是什么、
   删了谁、理由）。M4 做"失败轨迹贡献排除性知识"（书第 8 章）时，
   这份日志就是排除性知识的来源——被作废的结论值得作为反例保留，
   而不是只删不留痕。
5. **规范化优先于校验，校验失败优先于丢弃**。任何"LLM 产出不符合
   schema"的场景，默认动作顺序应是：规范化 → 复核队列 → （仅安全/无
   价值场景）计数丢弃。M4 蒸馏新记忆、合并旧记忆时复用
   `normalize_entry_id()` 与同一套 DistillResult 统计口径。

## M4b 真实演示暴露的缺陷（2026-08-20 确诊并修复）

### 缺陷 3：离线抽查复核恒判失效（调用方式错误）

- **表象**：evolve 演示里被抽查的正常记忆（如"仓库使用 Python 3.12"）
  全部被建议 invalidate 删除。
- **根因**：`consolidate.py` 的抽查复核调用
  `judge_propagation(old=entry, new=None, neighbor=entry)`——把条目指向
  自身且 new=None。propagation 的 prompt 语义是"旧事实已被删除/取代，
  请判定邻居是否失效"，于是每条被抽查的记忆都被判 INVALIDATED。这是
  调用方式错误：propagation 回答"某条记忆被撤销后牵连谁"，不能回答
  "这条记忆本身还成立吗"（设计启示 1 的"直接复用"表述有误，已修正）。
- **通病类别**：语义错配的复用——函数契约被拿来回答另一个问题。
- **修复方式**：`ingest/propagate.py` 新增 `judge_validity()`，独立
  prompt 语义：默认假设条目成立（VALID），只有明确证据（被近邻中更新的
  条目取代 / 与现存条目直接矛盾 / 内容含已过期的时间条件）才判
  INVALIDATED；证据不足以确认判 NEEDS_REVISION 交人工而不是判失效；
  输出不可解析按 VALID（fail-safe：宁可漏判也不错删）。`consolidate.py`
  复核时把该条目的语义近邻一并交给判定作对照。
- **验证**：单测断言正常记忆抽查不得判失效、复核 prompt 不含传播语义
  措辞、垃圾输出默认 VALID；真实 LLM 复跑场景 A，"仓库使用 Python
  3.12" 未再被判失效。

### 缺陷 4：boundary 档否决一切含 conflict 的提案

- **表象**：evolve 演示里 boundary 以"conflict 只标记未解决、矛盾场景
  仍复现"为由否决整份提案——含 conflict 的提案可能永远无法自动晋升。
- **根因**：boundary prompt 没区分两类变更。conflict/revise 的设计语义
  就是"机器不强行收敛、交人工裁决，apply 一律跳过"（apply.py 已正确
  跳过），属于"转人工"的正常产出，不是契约未覆盖。
- **修复方式**：`verify.py` boundary 档 prompt 明确两类评估标准——
  自动应用类（merge / invalidate / downgrade / archive）核查契约达成；
  转人工类（conflict / revise）只评估"标记是否准确、证据是否充分"，
  标记准确即视为该部分通过，"矛盾未在提案中解决"不是否决理由。交给
  LLM 的变更清单逐条标注"自动应用 / 转人工，apply 跳过"。
- **验证**：单测断言纯 conflict 提案不被 boundary 否决、prompt 标注
  转人工类别；真实 LLM 复跑场景 A，含 conflict + merge 的提案
  boundary 通过（理由："conflict 标记准确……merge 动作与根因匹配"），
  merge 正常晋升、conflict 留人工。
- **附带观察**：复跑中 boundary 曾以"冲突双方取值均无证据支持，证据
  充分性不足"否决一份演示提案（演示数据没挂 evidence）——这正是新
  语义要求的判定（标记证据不充分），补足 evidence 后通过。说明新标准
  下"证据充分性"是真实门槛，不是橡皮图章。
