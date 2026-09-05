# M10 可靠性加固计划与问题—测试矩阵

> 状态：非可信根修复已完成；一项可信根入口加固等待明确授权。本文记录
> 2026-09-05 只读审查后获授权的修复工作及独立复验后的补强。
> 本轮不改写 `data/raw`、历史审计日志、`data/snapshots`、`evals/`、rubric、
> 发布门槛或 `long_term/evolve/verify.py` 的三档判定逻辑。

## 1. 起点与验收口径

开始修复时工作区已有 23 个已跟踪文件被修改，另有 `.workbuddy/`、`agents/`、
`scripts/memory_session_context_hook.py`、`tests/test_session_context_hook.py` 未跟踪。
这些是用户现有工作，修复必须保留并在其上增量实施。

初始变更判断：

- 保留并提交 M8/M9 实现、配套文档与测试：差异形成完整的 LLM 失败归档、宿主
  蒸馏、HTTP 工作记忆路由、hook、scope 归一化及 subagent 写工具约束，现有常规
  与慢测试均通过，属于有价值的功能增量。
- 保留并提交 `agents/coder.md`：它是安装脚本实际引用的 subagent 工具硬闸，
  不是机器部署配置。
- 排除 `.workbuddy/memory/2026-09-02.md`：内容记录本机 WorkBuddy 配置路径和个人
  接入状态，属于机器侧运行记忆，不是可发布源码；将 `.workbuddy/` 加入忽略规则，
  文件本身不删除。
- 本机部署脚本、`.env`、真实 `data/` 继续由已有 `.gitignore` 排除。密钥扫描只
  命中测试假值与文档占位符，没有发现真实凭据。
- `docs/m10-reliability-hardening-plan.md` 由本轮在实现修改前新增，将与原有改动
  一起进入可恢复基线；因此基线提交明确是“原有 M8/M9 工作 + 修复计划”，不是
  纯粹的修复前原始状态。

基线：常规测试 672 passed / 2 deselected；两项真实 BGE-M3 慢测试 2 passed；
Ruff clean。新增修复不得删测试、降低门槛或依赖付费外部服务。

验收原则：

1. 写入先完成输入校验、脱敏和所需计算，失败时不破坏旧事实；
2. Markdown 正式层与 SQLite 派生索引发生分歧时，操作必须失败并恢复，或留下
   明确、可重放的恢复记录；
3. 同一规则在 CLI、MCP/HTTP、LangGraph 三个入口保持一致；
4. 并发写入采用跨线程、跨进程互斥和版本检查，不能用单个 SQLite 连接锁冒充
   跨文件事务；
5. D6 可信根问题从调用边界加固，禁止修改验证三档本身。

## 2. 实施阶段

- [x] S1 输入边界：严格 ID、scope、归档分段、content/detail 脱敏和评价门、
  外部 LLM 前脱敏、证据来源校验。
- [x] S2 可靠写入：原子文件替换、进程锁、预计算向量、补偿回滚、一致性检查，
  覆盖新增、更新、删除、反馈、复核、对账和传播。
- [x] S3 并发语义：同批目标冲突重新判定；检索计数原子更新；工作记忆版本比较；
  hook 状态仅在成功后提交。
- [x] S4 检索与接入：scope 内向量召回、无 LLM 保守降级、复核门和 pending_review、
  HTTP Host 防护、LangGraph namespace/工具参数对齐。
- [x] S5 进化边界：强制有效 VerifyReport；在 LLM 客户端/调用边界严格校验 JSON
  字段类型。`verify.py` 三档逻辑保持不动。
- [x] S6 验证与运维：针对性回归、常规/慢测试、Ruff、隔离多进程与故障注入；
  之后按 AGENTS.md 重启 HTTP 计划任务并做只读健康检查。

## 3. 问题—回归测试矩阵

| ID | 问题/风险 | 目标行为 | 回归证据 | 状态 |
|---|---|---|---|---|
| P01 | UPDATE 新 ID 冲突先删旧事实 | 预检冲突，旧条目不变，候选进复核 | reconcile 冲突测试 | 已解决 |
| P02 | 同批 UPDATE 同一目标部分成功 | 后续候选重新查目标并进复核，不中断整批 | 同批双更新测试 | 已解决 |
| P03 | model_copy 绕过 schema | 更新/反馈/CLI 均完整重校验 | 超长、profile-low、非法 confidence | 已解决 |
| P04 | source/session_id 路径穿越 | 仅安全段名且归档始终位于 raw | `../` 与绝对路径测试 | 已解决 |
| P05 | LangGraph namespace 串 scope | get/delete 必须匹配目标 scope | 跨 namespace 读删测试 | 已解决 |
| P06 | detail 未过门/未脱敏 | content 与 detail 使用相同安全入口 | 注入 detail、secret detail 测试 | 已解决 |
| P07 | boundary 字符串 false 误判 | LLM JSON schema 在调用边界拒绝错误类型 | 直接 verify 入口最小补丁待 D6 授权 | 未解决 |
| P08 | context 无 query 绕复核门 | context 任意调用均执行复核门 | ask/strict 无 query 测试 | 已解决 |
| P09 | 传播队列未进入 pending_review | 所有本次新增待办均返回 | propagation pending 测试 | 已解决 |
| P10 | scope 候选被全库挤占 | 候选获取在 scope 内完成或扩大到完整过滤 | >80 外 scope 测试 | 已解决 |
| P11 | hook 请求失败仍标已注入 | 仅成功响应后提交 session 状态 | 失败后重试测试 | 已解决 |
| P12 | apply 可缺验证报告 | 缺报告与错误类型均拒绝 | apply None 测试 | 已解决 |
| P13 | Markdown/索引分步失败不可恢复 | 持久 journal + 启动恢复 + 深一致性报告 | 进程硬退出、陈旧正文/向量、孤立派生行 | 已解决 |
| P14 | 计数覆盖正文更新 | 锁内重新读取并只改计数 | 确定性线程交错测试 | 已解决 |
| P15 | 全局 ID 唯一性存在竞态 | 跨进程锁内检查与创建 | 多线程/多进程同 ID 测试 | 已解决 |
| P16 | session_end 清理覆盖新待办 | 版本比较/重新读取，只删除原快照 done 项 | 蒸馏期间新增 todo 测试 | 已解决 |
| P17 | 门拒绝后仍清 done todo | 仅确认已沉淀或已入复核的结论才清理 | 全候选拒绝测试 | 已解决 |
| P18 | 传播先删后审计 | 审计预写或失败补偿，历史记录只追加 | 审计 OSError 测试 | 已解决 |
| P19 | 非法 ID glob 删除破坏一致性 | 所有按 ID 操作先严格校验 | `*`, `?`, `../` 删除测试 | 已解决 |
| P20 | 无 LLM LangGraph 把变更当 NOOP | 近邻一律人工复核，不猜事实关系 | 端口变更 + 真实模型/假模型测试 | 已解决 |
| R01 | 同 session 追加证据偏移错误 | evidence 指向本批归档实际行区间 | 两批追加测试 | 已解决 |
| R02 | SDK 异常未统一为临时失败 | LLM 客户端包装 SDK 异常为 LLMError | APIConnectionError 测试 | 已解决 |
| R03 | 非法 verdict 静默 UNAFFECTED | 非法结构进入人工复核 | 传播非法枚举测试 | 已解决 |
| R04 | `/wm_blocks` 缺 Host 防护 | 自定义路由与 MCP 使用同等 Host 校验 | 恶意 Host 测试 | 已解决 |
| R05 | 原始敏感数据发给外部 LLM | 默认先脱敏 LLM 输入，同时 raw 保留原始证据 | 捕获 LLM prompt 测试 | 已解决 |
| R06 | 宿主蒸馏伪造 evidence | 无对应 raw 证据时明确降级为待复核/无证据来源 | distilled evidence 测试 | 已解决 |
| R07 | 复核队列并发裁决 | 同一待办只能有一个成功裁决 | 并发 resolve 测试 | 已解决 |
| R08 | HTTP/LangGraph 能力参数不同 | acknowledge_pending、宿主蒸馏等关键读写语义对齐 | tool schema/调用测试 | 已解决 |
| R09 | hook 状态文件并发丢更新 | 原子写与进程锁 | 多进程状态测试 | 已解决 |
| R10 | 工作区/索引漂移缺乏观测 | 提供只读一致性检查与清晰错误 | consistency 测试 | 已解决 |
| R11 | feedback 锁外读取覆盖并发正文 | 读取、判定、更新使用同一写锁 | 确定性并发交错测试 | 已解决 |
| R12 | 复核已入库但队列删除失败后不可重试 | 相同正式条目视为已应用并完成清理 | queue delete OSError 重试测试 | 已解决 |
| R13 | 对账 LLM 判决使用陈旧目标版本 | 落库前比较判决快照，变化即转复核 | 判决期间并发更新测试 | 已解决 |
| R14 | session_end 空蒸馏误清 done todo | 仅有正式入库或待复核落点时清理 | 空 memories 测试 | 已解决 |
| R15 | 日志 tool 轮次使 evidence 行号错位 | 蒸馏轮次显式映射到 raw JSONL 行 | tool 夹层日志测试 | 已解决 |
| R16 | 传播删除完成审计失败造成无记录丢失 | 恢复被删条目并转人工复核 | completion audit OSError 测试 | 已解决 |
| R17 | LangGraph 写工具缺 subagent 约束/蒸馏协议 | 10 个写工具均声明主 agent 限制并补齐协议工具 | tool 描述与调用测试 | 已解决 |
| R18 | replace 进程中断后同时保留新旧事实 | journal 保存事务前镜像，恢复时回滚整个操作 | replace 中途 `os._exit` 测试 | 已解决 |
| R19 | evolution 进程中断留下部分变更 | 整批应用持久标记 + 整理前快照恢复 | 第二条变更前 `os._exit` 测试 | 已解决 |
| R20 | CAS 检查与写锁之间仍有竞态 | 期望版本比较下沉到协调写入器锁内 | 检查后并发更新测试 | 已解决 |
| R21 | memory_update 覆盖并发 confidence | 锁内读取、合并、校验和更新 | update/feedback 确定性交错测试 | 已解决 |
| R22 | feedback 与 review_resolve 锁序相反 | 统一按 memory_write → review_queue 获取 | 双线程完成时限测试 | 已解决 |
| R23 | 真实 BGE 批量浮点差异误报损坏 | 校验持久向量自身哈希，不重新批量嵌入比较 | 真实 BGE 三项慢测试 | 已解决 |
| R24 | 深检查信任 hash 而漏实际 meta 漂移 | 逐字段比较实际 meta 与 Markdown | scope 隔离篡改测试 | 已解决 |
| R25 | 有既存 raw 证据的宿主候选也被降级 | 校验证据文件与行范围后进入正常对账 | 有效/越界 evidence 测试 | 已解决 |

## 4. 完成记录

实现集中在协调写入器、跨进程文件锁、严格输入/LLM 边界、保守对账、工作记忆
版本控制和接入层语义对齐。新增 `memory_consistency_check` 作为只读漂移探针。

最终验证：常规套件 726 passed / 3 deselected；真实 BGE-M3 慢测试 3 passed；
`uv run ruff check .` 干净。慢测试原先在同一 Windows 进程连续创建两份 Torch
模型时稳定触发原生 access violation，改为模块级复用一份模型后整套通过。D6
清单中的 `evals/`、阈值、`verify.py` 判定逻辑、历史日志和快照均未修改。
`apply_proposal` 已在可信根外拒绝伪造对象和字段类型直接错误的报告；但
`verify_proposal` 会先把自定义 LLM 返回的字符串 `"false"` 转成布尔 `True`，形成
结构合法的错误报告，随后仍可触发正式层变更。最小修复需要在该可信根入口套用
既有 `ValidatingLLMClient`。
