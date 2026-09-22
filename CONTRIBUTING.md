# 参与贡献

感谢你愿意花时间。本项目欢迎 issue、讨论和 PR；下面是几条能让贡献顺利合入的约定。

## 开发环境

```bash
uv sync                      # 一律用 uv，不往系统 Python 装任何东西
uv run pytest -q             # 全部测试（约 5 分钟，不需要网络与 API key）
uv run pytest -m slow        # 需要真实 bge-m3 模型的慢测试（默认跳过）
uv run ruff check .          # lint（E / F / I / UP，行长 100）
```

提 PR 前请确保 `uv run pytest -q` 全绿、`uv run ruff check .` 干净。

## 三条红线

[AGENTS.md](AGENTS.md) 里的三条架构红线对人和 agent 同样有效：

- **D1** `data/raw` 只追加；`data/memory` 是唯一事实来源；`data/index.db` 只能重建不能手改。
- **D2** 写入必须过脱敏 → 蒸馏 → 评价门 → 对账；蒸馏绝不提炼指令性内容。
- **D6** `evals/`、rubric、发布门槛、审计日志是可信根，**不接受直接修改的 PR**。评测集的改动请在编写源头 `docs/research/benchmark-suite/` 提出（改规格 / 构造脚本 → 重新生成 → `tools/validate.py` 通过），由维护者核验后迁入冻结副本。

## 改代码时

- 改了 `agent_memory/models.py` 或 `config.py` 的字段或校验规则，必须同步改 `tests/`。
- 保持 fail-closed：配置非法、校验失败、证据缺失直接报错，不静默降级；但写入路径的内容不能因故障丢失。
- 改动行为语义（评价门、蒸馏硬规则、对账、复核队列、scope 归一化）前先读 [AGENTS.md](AGENTS.md) 的"关键行为语义"一节。
- 新增 MCP tool 时同步更新 `skills/agent-memory/SKILL.md`、`docs/agent-integration.md` §三 与根目录两份 README 的工具表；写类工具的 description 需注明"仅限主 agent 调用"，并加进 `agents/coder.md` 的 `disallowedTools`。
- 行为有变化的改动在 `docs/CHANGELOG.md` 顶部条目里加一行。升版本时 `pyproject.toml`、`agent_memory/__init__.py` 的 `__version__` 与 CHANGELOG 顶部条目三处同步，`tests/test_public_hygiene.py` 会核对。
- 仓库里只放产品本身：本机路径、邮箱、密钥、私人材料不进版本库（同一测试会检查跟踪的文本文件）；开发者私人材料放 `.notes/`（已 gitignore）。

## 提 issue 时最有用的信息

- 宿主与接入方式（Claude Code / Kimi Code / LangGraph；HTTP / stdio / 库）；
- 是否配置了服务端 LLM（`AGENT_MEMORY_LLM_*`），或走的是宿主蒸馏；
- 相关 tool 的返回 JSON（`memory_add` 的 `pending_review`、`memory_search` 的 `status` 等）；
- 能复现的最小对话或用例。请先脱敏，不要贴密钥或真实工作数据。

## 许可

提交的贡献按仓库的 [MIT 许可](LICENSE) 发布。
