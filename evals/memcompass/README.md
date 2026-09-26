# MemCompass（运行副本）

> 本目录是 `docs/research/benchmark-suite/` 在 2026-09-26 同步的运行副本（来源提交 `232f3b0`，套件版本 0.3.0-draft）。
> 2026-09-25 起取消冻结：改动一律先在编写源头做，再用 `docs/research/benchmark-suite/tools/migrate_to_evals.py`
> 同步过来（逐字节一致），不在本目录单独改。改评分口径的同步要在下方更新记录里写明，改口径前后的读数不直接比较。

- 规模：8 个子集，373 条用例；切分 dev / test / held-out = 20 / 50 / 30（按 group 整体分配）。
- 人工核验：见 `datasets/verification.yaml`（用例文件本身不改写，核验以该文件为准）。
- 编写源头（构造脚本、设计文档、核验台）：`docs/research/benchmark-suite/`。

## 用法（仓库根目录）

```bash
# 校验数据（纯本地）
.venv/Scripts/python.exe evals/memcompass/tools/validate.py --quiet
# 跑一个被测版本（test 切分；每个进程约 3.5 GB 内存，16 GB 机器上一次只跑一个）
.venv/Scripts/python.exe evals/memcompass/runners/mc_run.py --subsets pr,ca,at,fg,mp,xa,ts,pf --splits test \
    --systems am --am-root <代码根目录> --am-label <标签> --env-file <你的 .env> --jobs 8
# 汇总与配对比较
.venv/Scripts/python.exe evals/memcompass/runners/mc_report.py data/logs/memcompass/<run_a> data/logs/memcompass/<run_b> --ref <参照系统>
# 用例健康检查（oracle 失败 / 全部失败 / 不给记忆也通过）
.venv/Scripts/python.exe evals/memcompass/tools/item_health.py data/logs/memcompass/<run> ... --out flags.json
```

held-out 切分只用于里程碑评测，平时的开发与调参不要跑它。

## 更新记录

| 日期 | 授权 | 改动 | 来源提交 |
|---|---|---|---|
| 2026-09-16 | 用户当日明确要求同步 | `runners/mc_common.py`、`runners/mc_report.py`：成本口径改用 API 的 usage 字段（token 数与覆盖率，字符数列保留）。只改成本统计，不改用例、评委与判定规则；与编写源头逐字节一致 | `843a541` |
| 2026-09-16 | 用户当日再次要求同步 | `runners/mc_run.py`、`runners/judge_agreement.py`：`--env-file` 不再默认作者本机路径（不给则读进程环境变量）；本 README 示例命令同样改为占位符。不改评测行为 | `a7643f1` |
| 2026-09-24 | 维护者当日授权（换用其他评委模型） | `runners/mc_common.py` 与编写源头同步（新增 CodeBuddy CLI 客户端与 `judge_codebuddy` 评委配置、嵌入缓存增量落盘、CodeBuddy 临时目录清理、积分计量）；`runners/mc_run.py` 新增 `--judge-role`（缺省 `judge` = Kimi K3，行为不变）。起因：Kimi Code 订阅已不含 CLI 权限（403），评委改用 `--judge-role judge_codebuddy`（glm-5.3-flash）。**换评委后的读数不与 Kimi K3 评的旧读数直接比较**：被比较的版本与对照组都用同一评委重跑（答题与被测系统调用走缓存）。不改用例、判定规则与评委提示词 | `c23b94c` |
| 2026-09-25 | agent 同步（2026-09-25 起无需逐次授权） | K9 口径修正：不完整察觉率认 memory-v1 证据束里的原话（mc_run.py `_carries_raw`），过度回溯率只算答题器主动调原文工具（mc_report.py），见框架 §8a；新增 surface_consistency.py（浮现一致性）与外部评测 runner。改口径前后的 ca/E 这两项读数不直接比较 | `cc6db32` |
| 2026-09-25 | agent 同步（2026-09-25 起无需逐次授权） | surface_consistency.py 用法示例去掉本机路径 | `cc6db32` |
| 2026-09-26 | agent 同步（2026-09-25 起无需逐次授权） | surface_consistency.py 新增 --temperature（副手采样温度对比） | `065f082` |
| 2026-09-26 | agent 同步（2026-09-25 起无需逐次授权） | mc_run.py 新增 --no-system-cache（重复采样核对噪声时被测系统内部调用也不走缓存） | `232f3b0` |
