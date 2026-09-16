# agent-memory

Local, agent-neutral long-term memory infrastructure for LLM agents. 中文文档见 [README.md](README.md)。

It runs as a small service on your own machine and gives any agent three layers of memory:

- **Long-term memory** — facts, preferences and procedures that survive across sessions and projects. Nothing is written raw: every candidate goes through *redaction → distillation → evaluation gate → reconciliation*, so the store holds curated atomic statements with evidence pointers back to the original conversation.
- **Working memory** — the current task's goal, constraints, todos, open questions and variables, kept per scope and injected at the start of the next session.
- **Short-term memory** — the current conversation, read directly from the host's own transcript files (no duplicate log).

Three ways to plug in:

| Path | For | Entry point |
|---|---|---|
| **MCP over HTTP** (recommended) | Claude Code, Kimi Code, any MCP client | `http://127.0.0.1:8765/mcp` + read `/SKILL.md` |
| **MCP stdio** | hosts that spawn servers per session | `uv run python -m agent_memory.server.mcp_server` |
| **Python library** | LangGraph / LangChain apps | `agent_memory.long_term.adapters.langgraph` (`AgentMemoryStore`, `build_memory_tools()`) |

The full integration guide, including what the host runtime must do itself, is in [docs/agent-integration.md](docs/agent-integration.md) (Chinese).

## Quick start

```bash
git clone https://github.com/ac0033/agent-memory.git
cd agent-memory
uv sync                      # Python 3.14, sqlite-vec, bge-m3 embeddings (downloaded on first use)
uv run pytest -q             # 780+ tests, no network or API key needed
export AGENT_MEMORY_LLM_API_KEY=sk-...          # any OpenAI-compatible endpoint; default is DeepSeek
# optional: AGENT_MEMORY_LLM_BASE_URL / AGENT_MEMORY_LLM_MODEL / AGENT_MEMORY_DATA_DIR
uv run python -m agent_memory.server.http_server
# listening on http://127.0.0.1:8765/mcp (loopback only)
```

Then register the URL as a streamable-HTTP MCP server in your agent host and have the agent read
`http://127.0.0.1:8765/SKILL.md` once — the Skill explains when to search, when to write, how scopes work and how
human review is handled. `http://127.0.0.1:8765/bootstrap` is a paste-ready onboarding instruction.

Without an LLM key everything that does not need distillation still works (search, manual writes, feedback,
working memory, archives). Subscription-only hosts with no API key can distill on their side using the protocol
returned by `memory_distill_prompt` and submit candidates with `memory_add(distilled_json=...)`; the server still
runs validation, redaction, the gate and reconciliation on them.

## What the agent gets (25 MCP tools)

| Group | Tools | Purpose |
|---|---|---|
| Read | `memory_context`, `memory_search`, `memory_wm_read`, `memory_transcript_read`, `memory_distill_prompt`, `memory_consistency_check` | One-call context assembly (profile + working memory + recall), on-demand hybrid search (dense + BM25, RRF, confidence × time decay), transcript reading, host-side distillation protocol, store/index consistency check |
| Write | `memory_add`, `memory_update`, `memory_forget`, `memory_feedback`, `memory_wm_write`, `memory_wm_clear` | Long-term writes through the full pipeline; working-memory writes (redaction only) |
| Session | `memory_session_end` | Archive + distill + clean up at the end of a session (vetoed while todos are pending) |
| Review | `memory_review_list`, `memory_review_resolve` | Human review queue for candidates the pipeline would not commit on its own |
| Raw archive (v0.2) | `memory_archive_search`, `memory_archive_read`, `memory_archive_sync` | Redacted, append-only archive of the original conversations, searchable; the agent falls back to it when a memory is only a gist or looks wrong |
| Proactive recall (v0.2) | `memory_surface` | A precision-first "memory assistant" that surfaces history the agent would otherwise miss |
| Confirmation queue (v0.2) | `memory_confirm_enqueue`, `memory_confirm_list`, `memory_confirm_resolve` | Park decisions that need the user while working unattended |
| Working memory (v0.2) | `memory_wm_refresh` | Server-side incremental refresh of goal / constraints / todos / open questions from recent turns |
| Episodes (v0.2) | `memory_episode_pack` | Save exact details (ids, ports, paths, error text) before context compaction |
| Forgetting (v0.2) | `memory_forget_request` | Execute an explicit user request to forget: delete memories and blank the matching archive spans, metadata-only audit |

Every write tool's description states that it is for the main agent only; a ready-made subagent override for Kimi
Code and the standard constraint text for other hosts are in [docs/agent-integration.md](docs/agent-integration.md) §7.

## Design in one paragraph

Memory entries are Markdown files (the single source of truth) with a rebuildable SQLite index (sqlite-vec 1024-d
+ FTS5). Raw conversations are archived append-only after redaction. The write path never trusts its input: the
distiller refuses to extract instructions, the gate blocks prompt-injection patterns and leaked secrets, and
reconciliation decides ADD / UPDATE / DELETE / NOOP against existing neighbours, sending unresolved conflicts to a
human review queue instead of guessing. Everything fails closed but never loses data: the raw transcript is archived
before distillation, and rejected candidates can be forced into review. Scopes (`global`, `repo:<name>`,
`agent:<name>`) isolate projects; a search only sees the current scope plus `global`. An offline "evolve" loop
consolidates the store with snapshots and rollback. The three non-negotiable rules — data-layer separation, gated
writes, and a trusted root that agents may not edit — are spelled out in [AGENTS.md](AGENTS.md).

## How well does it work?

We measure rather than claim. MemCompass, an 8-subset / 373-case capability benchmark written for this project,
lives in [`evals/memcompass/`](evals/memcompass/) (frozen copy) with its source in
[`docs/research/benchmark-suite/`](docs/research/benchmark-suite/); the v0.3 report is
[`results/2026-09-16-v03-report.md`](docs/research/benchmark-suite/results/2026-09-16-v03-report.md).
Headlines from the test split, paired against the previous version of this same system:

- Forget-on-request compliance 100% vs 40%; retrieval-layer leakage 0% vs 70%.
- Retroactive back-fill from the archive 88% vs 19%.
- System-level proactive recall F0.5 77% vs 41%; cross-host injection 83% vs 0%; temporal questions 97% vs 57%.

The honest caveat: on histories of only 2–8 sessions, a naive RAG baseline (keep everything, retrieve every turn)
matches or beats this system on most plain question-answering subsets. The structured memory wins where raw
retrieval structurally cannot — forgetting, poisoning resistance, working-memory state and knowing *when* to
speak up. A long-history tier (~350k tokens per case) is the next planned benchmark step; see the report §1.3c.

## Repository map

```
agent_memory/        library + servers (long_term / working / short_term / server)
skills/agent-memory/ the Skill (SKILL.md) served at /SKILL.md
agents/coder.md      Kimi Code subagent override (write tools removed)
scripts/             hooks and installers for hosts
evals/               trusted root: legacy eval layers + MemCompass frozen copy (agents must not edit)
docs/                integration guide, research, benchmark source, reports
```

## License

MIT — see [LICENSE](LICENSE). Benchmark datasets under `evals/memcompass/` and
`docs/research/benchmark-suite/datasets/` are synthetic and released under the same license.
