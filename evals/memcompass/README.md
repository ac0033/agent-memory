# MemCompass（冻结副本）

> 本目录是 `docs/research/benchmark-suite/` 在 2026-09-16 的冻结副本（来源提交 `07de752`，套件版本 0.3.0-draft），
> 属于 D6 可信根：**agent 不得修改**；更新由用户执行（在编写源头改规格、重新生成、重新核验后再迁入）。

- 规模：8 个子集，373 条用例；切分 dev / test / held-out = 20 / 50 / 30（按 group 整体分配）。
- 人工核验：见 `datasets/verification.yaml`（用例文件本身不改写，核验以该文件为准）。
- 编写源头（构造脚本、设计文档、核验台）：`docs/research/benchmark-suite/`。

## 用法（仓库根目录）

```bash
# 校验数据（纯本地）
.venv/Scripts/python.exe evals/memcompass/tools/validate.py --quiet
# 跑一个被测版本（test 切分；每个进程约 3.5 GB 内存，16 GB 机器上一次只跑一个）
.venv/Scripts/python.exe evals/memcompass/runners/mc_run.py --subsets pr,ca,at,fg,mp,xa,ts,pf --splits test \
    --systems am --am-root <代码根目录> --am-label <标签> --env-file <含 API key 的 .env 路径> --jobs 8
# 汇总与配对比较
.venv/Scripts/python.exe evals/memcompass/runners/mc_report.py data/logs/memcompass/<run_a> data/logs/memcompass/<run_b> --ref <参照系统>
# 用例健康检查（oracle 失败 / 全部失败 / 不给记忆也通过）
.venv/Scripts/python.exe evals/memcompass/tools/item_health.py data/logs/memcompass/<run> ... --out flags.json
```

held-out 切分只用于里程碑评测，平时的开发与调参不要跑它。

## 冻结后的更新记录

| 日期 | 授权 | 改动 | 来源提交 |
|---|---|---|---|
| 2026-09-16 | 用户当日明确要求同步 | `runners/mc_common.py`、`runners/mc_report.py`：成本口径改用 API 的 usage 字段（token 数与覆盖率，字符数列保留）。只改成本统计，不改用例、评委与判定规则；与编写源头逐字节一致 | `843a541` |
| 2026-09-16 | 用户当日再次要求同步 | `runners/mc_run.py`、`runners/judge_agreement.py`：`--env-file` 不再默认作者本机路径（不给则读进程环境变量）；本 README 示例命令同样改为占位符。不改评测行为 | `a7643f1` |
