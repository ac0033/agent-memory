# docs 目录索引

| 路径 | 性质 | 说明 |
|---|---|---|
| [`agent-integration.md`](agent-integration.md) | 接入文档（现行） | 三种接入方式、25 个 MCP tool 一览、宿主 runtime 必须自己做的事、会话日志供料、scope 与 subagent 纪律 |
| [`usage.md`](usage.md) | 使用手册（现行） | 配置、CLI、评估命令、LangGraph / Skill、离线整理、人工复核、HTTP 服务、工作记忆、会话收尾、宿主蒸馏与 hook |
| [`design/memory-architecture.md`](design/memory-architecture.md) | 设计文档 | 三层记忆（短期 / 工作 / 长期）的分层与生命周期、各层职责、与实现的对照 |
| [`research/`](research/README.md) | 研究与评测（编写源头） | 能力框架（K1–K13）、MemCompass 评测套件源头与正式报告、v0.2 优化记录、历史草稿 |
| [`history/`](history/) | 历史记录 | [`milestones.md`](history/milestones.md) M0–M9 交付记录、[`defect-postmortem.md`](history/defect-postmortem.md) 管线缺陷复盘、[`m10-reliability-hardening.md`](history/m10-reliability-hardening.md) 可靠性加固计划与问题—测试矩阵 |

约定：`docs/research/benchmark-suite/` 是评测集的编写源头，`evals/memcompass/` 是它的冻结副本（可信根，agent 不得修改）。改规格在源头改，重新生成、核验后再迁入。
