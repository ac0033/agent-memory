# docs/research 目录索引

这里是研究与评测的**编写源头和历史记录**，不是接入或使用文档。只想接入记忆服务的读者请看仓库根目录的 `README.md` 与 `docs/agent-integration.md`。

| 目录 / 文件 | 性质 | 说明 |
|---|---|---|
| `agent-memory-capability-framework.md` | 设计框架（现行） | 13 项能力（K1–K13）与 3 项质量属性的定义、成熟度判定标准（§6.2）、现状定级（§8）、后续步骤（§9） |
| `benchmark-suite/` | 评测套件 MemCompass 的**编写源头**（现行） | 构造脚本 `build/`、数据卡与用例、runner、核验工具、设计文档、正式报告 `results/`。改规格在这里改，重新生成后再迁入 `evals/memcompass/` |
| `memory-v1-design.md` | 机制重设计（现行，2026-09-21 起） | 从能力框架反推的三条原则与七个机制、外部验证矩阵、准入纪律、逐轮实测（含被证伪的预测与撤回的改动）。机制的现状说明见 `../design/memory-v1-mechanism.md` |
| `optimization-v02.md` | 优化记录（v0.2–v0.4） | v0.2 起按框架 §9 顺序落地的机制、测后修订、各版评测结果、自行拍板的决定 |
| `eval-drafts/` | **历史草稿**（2026-09-13，已被 benchmark-suite 取代） | 两个专项的种子用例与 runner 草稿、冒烟结果。保留原因：`benchmark-suite/build/migrate_drafts.py` 与数据卡的"迁移"一节引用它；朴素 RAG 对照组 `runner-draft/naive_rag.py` 仍被 runner 复用。不要在这里新增内容 |

外部评测数据与自建验证集不入库，放在 `data/external/<来源>/` 与 `data/dev/`（布局见 `benchmark-suite/README.md` §8）。

`evals/` 是可信根（AGENTS.md D6）：里面的 `memcompass/` 是本目录套件的冻结副本，agent 不得修改，更新由用户从源头迁入。
