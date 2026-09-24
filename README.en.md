# agent-memory

**Local, agent-neutral long-term memory infrastructure for LLM agents.** A small service on your own machine that lets any agent (Claude Code, Kimi Code, a LangGraph app, …) remember user preferences, project conventions and past mistakes across sessions. Every write goes through a gate, memories can be forgotten on request, and the system knows *when* to speak up.

The mechanism was not picked from other systems' feature lists. It is derived from a [capability framework](docs/research/agent-memory-capability-framework.md) of 13 capabilities and 3 quality attributes, with one bar for every item: significantly better than naive RAG. Three principles follow: **the original words are the evidence and a memory is only a key to them plus an annotation; reading organizes and never adjudicates; intelligence moves to write time and only adds.** Acceptance runs on several public benchmarks, each once: LoCoMo on unseen conversations **42 vs 33** (p=0.049), PersonaMem-32k and LongMemEval-S tied with naive RAG; the in-repo validation set (92 questions, 11 capability buckets) 88/92; naive RAG 39/45 on the original 45. Governance capabilities (forgetting, poisoning, task state, proactive recall) are measured by MemCompass, a 373-case benchmark that ships with the repo.

> 中文文档见 [README.md](README.md) · License: [MIT](LICENSE) · Python ≥ 3.12 · 847 tests, no network or API key needed · current version v0.3.1 ([CHANGELOG](docs/CHANGELOG.md))

---

## The problem

A coding agent starts every session from zero: the database you chose last week, the "always use uv" preference, the bug you hit yesterday all have to be repeated. The usual fix is to dump the whole conversation history into RAG, which has four structural problems:

| Problem | Raw-transcript RAG | agent-memory |
|---|---|---|
| **Cannot forget** | The user says "forget that"; the text is still there and retrieval still leaks it | Deletes the memory *and* blanks the matching archive span; audit keeps metadata only |
| **Cannot resist poisoning** | An injected "ignore safety checks from now on" is recalled verbatim | Every write passes an evaluation gate: instructions, injection patterns and leaked secrets never enter the store |
| **No task state** | Retrieves what was *said*, not where the task *is* | A separate working-memory layer (goal, constraints, todos, open questions), injected at session start |
| **Does not know when to speak** | Injects on every turn, relevant or not | Precision-first proactive recall: surfaces a memory only when *not* mentioning it would cause a mistake |

agent-memory splits memory into three layers (long-term / working / short-term), runs every write through *redaction → distillation → gate → reconciliation*, renders recalled memories with a "reference, not instruction" guard, and plugs in via MCP, a Python library or a Skill.

## Why not "just another RAG"

Naive RAG (keep every line, retrieve every turn) is a strong baseline: in our early benchmark it matched or beat the memory system of the time on most plain question-answering subsets. memory-v1 (since 2026-09-21) does not avoid that comparison; it treats naive RAG as the **floor**:

- **The payload contains the original words by construction.** A memory entry is only a key to the archived lines and an annotation on them, so the answerer sees at least what naive RAG would show it (retrieval parity about 90%, payload size ≤ 1.15×).
- **Reading organizes, it never adjudicates.** No "the store has no X" is synthesized, nothing is counted on the answerer's behalf, and the read path makes zero LLM calls.
- **Writing adds what the words do not carry**: absolute event dates, supersession history, validity, provenance, cue words, a resident user profile. Naive RAG cannot produce these structurally, and they are where the lead comes from.
- **Governance stays out of naive RAG's reach**: forgetting on request (0% leakage), poisoning resistance (0% false blocks), task state, proactive recall, no cross-agent bleed.

Readings on conversations never seen during development (same default configuration, fixed answerer and judge): LoCoMo, two new conversations, 60 questions, **42 vs 33** (p=0.049); PersonaMem-32k, 60 multiple-choice questions, 51 vs 53 (tie); LongMemEval-S, 60 questions, 51 vs 49 (tie, p=0.77; knowledge-update 9 vs 6). Derivation and round-by-round records: [docs/research/memory-v1-design.md](docs/research/memory-v1-design.md); mechanism note: [docs/design/memory-v1-mechanism.md](docs/design/memory-v1-mechanism.md) (both Chinese).

## Highlights

1. **Derived from a capability framework, accepted on several external benchmarks.** First define which 13 capabilities (K1–K13) an agent memory needs, how each is measured and how maturity is graded (L2 = significantly better than naive RAG); then work back from "what decides the score" to the mechanism, each principle answering a measured loss. The admission rule was written before the mechanism: at least two independent evaluations in the same direction and none worse, at least one significantly better, and any question type getting significantly worse is a veto. External sets are for acceptance only and are run once each; iteration uses the in-repo validation set (one shared history of 55 sessions, 92 questions in 11 capability buckets, a tier-0 check with no LLM call that finishes in a minute). The derivation, including predictions that were falsified, is in [docs/research/memory-v1-design.md](docs/research/memory-v1-design.md) (Chinese).
2. **Three layers, one call.** Long-term (facts / preferences / procedures across sessions), working (the current task's goal, constraints, todos, open questions) and short-term (the host's own transcript, read in place, never duplicated). `memory_context` returns all three as separate blocks in one call.
3. **Gated writes that never lose data.** Every candidate passes redaction → distillation → gate → reconciliation (ADD / UPDATE / DELETE / NOOP). Conflicts the pipeline cannot settle go to a human review queue instead of a guess. The raw transcript is archived *before* distillation, and gate rejections can be forced into review.
4. **Memory is reference, not instruction.** The distiller refuses to extract instructions, the gate blocks prompt-injection patterns, and every injected block carries a guard preamble. The current request always wins over a recalled memory.
5. **Verifiable forgetting.** `memory_forget_request` deletes memories and blanks the matching archive spans; the audit log holds metadata only. Retrieval-layer and answer-layer leakage: 0%.
6. **Proactive recall, precision first.** LLM cue expansion + one-hop spreading + a per-item "would omitting this cause an error / is the principle transferable" judgement. System-level F0.5 77%, false interjections 2/13.
7. **Bitemporal memories with history.** `valid_from / valid_to` on every entry; on UPDATE the old version folds into `history`; distillation resolves relative dates against the session date and detects retroactive corrections. As-of questions: 97%.
8. **The words arrive with the memory.** Redacted, append-only archive with a derived index (vectors + word-level and trigram full text); retrieval returns *evidence bundles* — the original lines plus their annotations (supersession history, validity, provenance, event date), words first. A memory that merely restates lines already shown is not rendered. The answerer can always go back to the evidence.
9. **Offline evolution loop with rollback.** `agent-memory evolve` merges duplicates, re-verifies old entries and downgrades stale ones, producing a *proposal* rather than editing the store; three independent checks (boundary / retention / safety) each hold a veto; snapshot before promotion, audit after, rollback at any time.
10. **Host-neutral, works without an API key.** MCP over HTTP, MCP stdio, LangGraph library, Skill. Subscription-only hosts distill in their own context using the protocol from `memory_distill_prompt`; the server still validates, redacts, gates and reconciles.
11. **A benchmark and an honest report.** MemCompass: 8 subsets, 373 synthetic cases covering what public benchmarks do not (forgetting, poisoning, proactive recall, task state, cross-agent, bitemporal…), with programmatic gold labels, independent judges, paired statistics, ablations and control groups. The report says where naive RAG beats us.

## Results

Three kinds of data, three purposes: **external benchmarks** (LoCoMo, PersonaMem-32k, LongMemEval-S) are for acceptance only, each source run once with naive RAG answering in the same run; the **in-repo validation set** is the integration test, written per capability dimension and reused freely; the **internal benchmark MemCompass** covers the governance capabilities public benchmarks do not. Acceptance bar: no external set worse than naive RAG, at least one significantly better.

**External acceptance (memory-v1, 2026-09-22; each source run once, naive RAG in the same run, same answerer deepseek-v4.1-flash, same judge glm-5.3-flash, the public AML answer and grading prompts):**

| Source | n | Naive RAG | memory-v1 | Paired |
|---|---|---|---|---|
| LoCoMo (unseen conv-41/42) | 60 | 33 (55%) | **42 (70%)** | 13 wins / 4 losses, McNemar p=0.049 |
| LoCoMo first run (conv-26/30) | 54 | 31 (57%) | **37 (69%)** | 10 wins / 4 losses, p=0.18, same direction |
| PersonaMem-32k (4 histories, multiple choice) | 60 | 53 | 51 | 2 wins / 4 losses, p=0.69 (tie); full context 47 |
| LongMemEval-S | 60 | 49 | **51** | 7 wins / 5 losses, p=0.77 (tie); knowledge-update 9 vs 6, abstention 4/5 vs 1/5 |

On LoCoMo the lead comes mainly from adversarial questions with a false premise (9 vs 0, the static reading protocol in the payload header), plus one each on multi-hop and temporal; single-hop and open-domain lose one each; payload size is 1.15× naive RAG. On PersonaMem, fact recall 21/21, change reasons 6/6 and recommendations 7/7 equal naive RAG; the only trailing category, suggest_new_ideas (5 vs 8 of 14), is also 6/14 for full context.

The in-repo validation set (one shared history of 55 sessions, 92 questions in 11 capability buckets) scores 88/92; on the original 45 questions naive RAG scores 39 and full context 40, with no bucket below either control, and none of the 21 answerable questions phrased with a synonym, a general term or a described-but-unnamed thing is wrongly refused. Method: [benchmark README §8](docs/research/benchmark-suite/README.md) (Chinese).

**Internal benchmark** (readings from v0.2.2): [MemCompass v0.3](docs/research/benchmark-suite/README.md) (8 subsets / 373 cases, built in this repo, all synthetic). The table is the **paired comparison of v0.2.2 against the previous version** on the test split (193 cases), McNemar exact test:

| Capability | Subset / mode | v0.2.2 | Previous | p | Mechanism evidence |
|---|---|---|---|---|---|
| Forget on request | fg / QA | 100% (15/15) | 40% | .004 | Retrieval hard leakage 0% vs 70% |
| Back-fill from archive | ca / end-to-end | 88% (30/33) | 19% | <.001 | Drops to 75% with archive tools ablated |
| Proactive recall (system) | pr / system | 78% (25/32, F0.5 77%) | 41% (13/32) | .004 | Back to 13/32 with surfacing ablated |
| Cross-agent migration (system) | xa / system | 83% | 0% | .002 | Host-neutral archive and injection |
| Temporal questions | at / QA | 97% | 57% | .007 | Bitemporal fields, retroactive corrections |
| Present fidelity | pf / end-to-end | 100% | 84% | .016 | Episode cards before context compaction |
| Poisoning robustness | mp / QA | 100% | 89% | .250 | Attack success 0% for both; false-block 0% vs 21% |

**Honest footnotes**

- Each case has only 2–8 sessions; naive RAG ties or wins most plain QA subsets (previous section). A long-history tier (~350k tokens per case) is not built yet and is the most important next step.
- Single seed; subsets with n < 50 have wide intervals, flagged "direction only" in the report.
- Answerer, judge and case reviser are from different vendors (DeepSeek / Kimi K3 / Claude); inter-judge agreement 85%–98% (κ 0.69–0.93). No human-agreement study yet.

Full report (controls, ablations, cost, item health, limitations): [`docs/research/benchmark-suite/results/2026-09-16-v03-report.md`](docs/research/benchmark-suite/results/2026-09-16-v03-report.md) (Chinese). Reproduction: [`evals/memcompass/README.md`](evals/memcompass/README.md).

## Architecture

```
                 ┌──────────────────────────────────────────────────────┐
   host agent    │  memory_context = profile + working memory + recall  │
 (MCP / lib / Skill)  memory_surface = proactive recall (precision-first)│
                 └───────────────▲──────────────────────────▲───────────┘
                                 │ read                      │ read
        ┌────────────────────────┴────────┐   ┌──────────────┴──────────────┐
        │ long-term  data/memory/*.md     │   │ working  data/working/       │
        │ single source of truth, scoped  │   │ goal/constraints/todos/open  │
        │ + rebuildable index.db          │   │ server-side wm_refresh       │
        │   (sqlite-vec 1024d + FTS5)     │   └──────────────▲──────────────┘
        └────────────────────────▲────────┘                  │ redaction only
                                 │ write                     │
   ┌─────────────────────────────┴──────────────────────────────────────┐
   │ write path: redact → distill → gate → reconcile (ADD/UPDATE/DELETE/ │
   │             NOOP) → propagate;  undecidable → data/review_queue/    │
   └─────────────────────────────▲──────────────────────────────────────┘
                                 │ archive first, then distill
        ┌────────────────────────┴────────┐   ┌─────────────────────────────┐
        │ raw archive  data/raw/ (append) │   │ short-term = host transcript │
        │ redacted + derived raw_index    │   │ parsed in place by adapters  │
        └─────────────────────────────────┘   └─────────────────────────────┘

   offline: agent-memory evolve → proposal → 3 checks → snapshot → promote → audit / rollback
```

Scopes: `global`, `repo:<name>`, `agent:<name>`; a search sees the current scope plus `global`. The three non-negotiable rules (data-layer separation, gated writes, a trusted root agents may not edit) are in [AGENTS.md](AGENTS.md) (Chinese).

## Quick start

### 1. Install and run

```bash
git clone https://github.com/ac0033/agent-memory.git
cd agent-memory
uv sync                      # Python ≥ 3.12; bge-m3 embeddings downloaded on first use (~2 GB)
uv run pytest -q             # 847 tests, no network or API key needed

export AGENT_MEMORY_LLM_API_KEY=sk-...          # any OpenAI-compatible endpoint; default is DeepSeek deepseek-flash
# optional: AGENT_MEMORY_LLM_BASE_URL / AGENT_MEMORY_LLM_MODEL / AGENT_MEMORY_DATA_DIR
uv run python -m agent_memory.server.http_server
# listening on http://127.0.0.1:8765/mcp (loopback only)
```

Without an LLM key, everything that does not need distillation still works (search, manual writes, feedback, working memory, archives). Distillation can be delegated to the host (step 3).

### 2. Connect an agent

**Option A: MCP over HTTP (recommended, any MCP client).** Register `http://127.0.0.1:8765/mcp` as a streamable-HTTP MCP server and have the agent read `http://127.0.0.1:8765/SKILL.md` once. `/bootstrap` returns a paste-ready onboarding instruction.

**Option B: MCP stdio (host spawns the server per session).** Claude Code / Kimi Code config:

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "uv",
      "args": ["run", "python", "-m", "agent_memory.server.mcp_server"],
      "env": {
        "AGENT_MEMORY_DATA_DIR": "C:/Users/<you>/.agent-memory/data",
        "AGENT_MEMORY_LLM_API_KEY": "sk-..."
      }
    }
  }
}
```

**Option C: Python library (LangGraph / LangChain apps).**

```python
from langgraph.prebuilt import create_react_agent
from agent_memory.long_term.adapters.langgraph.store import AgentMemoryStore
from agent_memory.long_term.adapters.langgraph.tools import build_memory_tools
from agent_memory.long_term.retrieve.resident import build_system_context

store = AgentMemoryStore()                       # LangGraph BaseStore, namespace ("memories", <scope>)
tools = build_memory_tools()                     # 17 ReAct tools covering all three layers
prompt = "You are the user's coding assistant.\n\n" + build_system_context("repo:myproj")
agent = create_react_agent(model, tools, prompt=prompt, store=store)
```

Runnable example: `uv run python examples/langgraph_demo.py`.

**Option D: Skill.** Copy `skills/agent-memory/` into the host's skills directory (Claude Code `~/.claude/skills/`, Kimi Code `~/.kimi-code/skills/`) and combine with either MCP option. Kimi Code has a one-shot installer, `scripts/install_kimi_code.sh` (merges mcp.json, installs the Skill and the read-only subagent override, registers the session-start hook).

### 3. Subscription-only hosts without an API key

The host is itself an LLM, so it can distill: call `memory_distill_prompt()` for the protocol, produce `{"memories": [...]}` in its own context, submit with `memory_add(distilled_json=...)`. The server still validates, redacts, gates and reconciles.

More usage (CLI distillation, evaluation commands, the evolve loop, human review, hooks, working memory, session end) is in the [usage guide](docs/usage.md) (Chinese); what the host runtime must do itself is in the [integration guide §4](docs/agent-integration.md) (Chinese).

## The 25 MCP tools

| Group | Tools | Purpose |
|---|---|---|
| Read | `memory_context`, `memory_search`, `memory_wm_read`, `memory_transcript_read`, `memory_distill_prompt`, `memory_consistency_check` | One-call context assembly; hybrid search (dense + BM25 → RRF → confidence × time decay); working memory; transcript reading; host-side distillation protocol; store/index consistency check |
| Write | `memory_add`, `memory_update`, `memory_forget`, `memory_feedback`, `memory_wm_write`, `memory_wm_clear` | Long-term writes through the full pipeline; working-memory writes (redaction only) |
| Session | `memory_session_end` | Archive + distill + clean up at session end (vetoed while todos are pending) |
| Review | `memory_review_list`, `memory_review_resolve` | Human review queue for candidates the pipeline would not commit on its own |
| Raw archive | `memory_archive_search`, `memory_archive_read`, `memory_archive_sync` | Redacted, append-only, searchable archive; fall back to it when a memory is only a gist or looks wrong |
| Proactive recall | `memory_surface` | Precision-first "memory assistant" that surfaces history the agent would otherwise miss |
| Confirmation queue | `memory_confirm_enqueue`, `memory_confirm_list`, `memory_confirm_resolve` | Park decisions that need the user while working unattended |
| Working memory | `memory_wm_refresh` | Server-side incremental refresh of goal / constraints / todos / open questions |
| Episodes | `memory_episode_pack` | Save exact details (ids, ports, paths, error text) before context compaction |
| Forgetting | `memory_forget_request` | Execute an explicit forget request: delete memories and blank matching archive spans, metadata-only audit |

Every write tool's description states it is for the main agent only; subagents are read-only and hand their conclusions back to the main agent. The Kimi Code subagent override lives in `agents/coder.md`.

## Repository map

```
agent-memory/
├── agent_memory/            # Python package (import agent_memory)
│   ├── config.py / models.py    settings (AGENT_MEMORY_* env vars) and the memory-entry schema
│   ├── llm.py / confirmations.py  OpenAI-compatible LLM client; confirmation queue
│   ├── long_term/               store (Markdown + SQLite index) / ingest (redact, distill, gate, reconcile,
│   │                            propagate, episodes, forget) / retrieve (bge-m3 hybrid search, injection,
│   │                            resident profile, surfacing) / evolve (offline loop) / adapters/langgraph
│   ├── working/                 working memory (task state + server-side refresh)
│   ├── short_term/              transcript adapters for host session logs
│   └── server/                  MCP stdio server, HTTP server, MemoryService
├── skills/agent-memory/     # the Skill (SKILL.md), also served at /SKILL.md
├── agents/coder.md          # Kimi Code subagent override (write tools removed)
├── scripts/                 # host hooks (periodic distillation, session-start injection, surfacing) and installer
├── examples/                # minimal LangGraph example
├── evals/                   # trusted root (agents must not edit): layer1–3 / prefix sets + MemCompass frozen copy
├── docs/                    # usage, integration, design, research and benchmark (see below)
├── tests/                   # 847 tests (slow ones skipped by default: uv run pytest -m slow)
└── data/                    # runtime data (gitignored): raw / memory / working / review_queue / snapshots / logs
```

## Documentation

| I want to… | Read |
|---|---|
| Plug memory into my agent and know what the host is responsible for | [docs/agent-integration.md](docs/agent-integration.md) |
| Everyday usage: CLI, evaluation commands, evolve, review, hooks, session end | [docs/usage.md](docs/usage.md) |
| Understand the three-layer design and its trade-offs | [docs/design/memory-architecture.md](docs/design/memory-architecture.md) |
| How memory is read and written today, and why (memory-v1) | [docs/design/memory-v1-mechanism.md](docs/design/memory-v1-mechanism.md) (Chinese) |
| How the mechanism was derived from the capability framework, round by round | [docs/research/memory-v1-design.md](docs/research/memory-v1-design.md) (Chinese) |
| See which capabilities a good agent memory needs and how to measure them | [docs/research/agent-memory-capability-framework.md](docs/research/agent-memory-capability-framework.md) |
| Build, validate and run the MemCompass benchmark | [docs/research/benchmark-suite/README.md](docs/research/benchmark-suite/README.md) |
| Read the latest evaluation report | [docs/research/benchmark-suite/results/](docs/research/benchmark-suite/results/) |
| Know why each v0.2 mechanism was built | [docs/research/optimization-v02.md](docs/research/optimization-v02.md) |
| What changed in each version | [docs/CHANGELOG.md](docs/CHANGELOG.md) |
| Milestone history, defect post-mortems, reliability hardening | [docs/history/](docs/history/) |
| Rules for people and agents working in this repo | [AGENTS.md](AGENTS.md) |
| Contribute | [CONTRIBUTING.md](CONTRIBUTING.md) |

All documents except this file are currently in Chinese.

## Design principles

- **D1 Data-layer separation**: `data/raw` is append-only; the Markdown files in `data/memory` are the single source of truth; `data/index.db` is a rebuildable derived index and is never edited by hand.
- **D2 Gated writes**: raw conversations never enter the store directly; every candidate passes redaction → distillation → gate → reconciliation, and the distiller never extracts instructions.
- **D6 Trusted root**: `evals/`, rubrics, release thresholds and audit logs may not be modified by agents. The benchmark is authored and verified in `docs/research/benchmark-suite/` and migrated to the frozen copy by the maintainer.
- **Fail closed, never fail lost**: invalid config, failed validation or missing evidence raise immediately instead of silently degrading; nothing on the write path is lost to a failure.
- **Local first**: all data is Markdown and SQLite on your disk; the server binds 127.0.0.1 by default; nothing leaves the machine.

## Known limitations and roadmap

- **The lead is not significant on every external set**: LoCoMo is a significant win, PersonaMem and LongMemEval-S are ties with naive RAG. On LongMemEval-S, temporal reasoning was re-answered by both systems with the question date added (the AML answer template carries no current date, so "how many days ago" is unanswerable for any system); the MemCompass table is from v0.2.2.
- **PersonaMem's suggest_new_ideas category trails naive RAG** (5 vs 8 of 14); full context also gets only 6/14 there, a "pick the generic option" failure on the answerer's side.
- **Schema induction (K6, offline) not implemented**: the evolve verifier is part of the trusted root and needs a new proposal type.
- **Write cost**: with dense sessions about one person, reconciliation makes one LLM call per candidate, so rewriting a few dozen sessions can take hours; batched judgement is the next lever.
- **The LangGraph adapter covers the 15 base tools**; the 10 v0.2 tools are MCP-only for now.
- **Chinese-only internal benchmark**, synthetic data; no judge-vs-human agreement study yet.

## License

MIT, see [LICENSE](LICENSE). Benchmark datasets under `evals/memcompass/` and `docs/research/benchmark-suite/datasets/` are synthetic and released under the same license.
