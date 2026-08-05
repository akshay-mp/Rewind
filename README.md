# Rewind

### Time-travel debugging for AI agents — an OTel-in / replay-out engine

> Rewind an agent to any span, change a prompt, and re-run **live** from there —
> branching a new timeline you can diff against the original.
> Consumes standard OpenTelemetry / OpenInference traces. No cloud, no API keys,
> no persistent production proxy, no data leaving the machine.

---

## What it does

A developer running an agent on `qwen3:32b` via Ollama captures a run with any
OpenInference/OTel instrumentor, rewinds to span 4, edits the system prompt,
branches the execution forward live against the same local model, and sees a
side-by-side diff of what changed — all offline, in under a minute of setup.

## Architecture (the key insight)

```
Capture = PASSIVE   ->  an OTel span only exists *after* a call completes.
                        OpenTelemetry + OpenInference solve this. We ingest.
Replay  = ACTIVE    ->  to rewind we *inject* the cached response during a
                        re-run. That is runtime patching, not observability.
```

So Rewind does **not** need its own capture proxy. It needs:

1. A **local OTLP receiver** that stores traces into SQLite (production path,
   zero agent-side lock-in).
2. An **opt-in replay-time LLM-client wrapper** (`rewind.replay()`) — *only*
   active during a debug session, never in production.

## Status

| Phase | What | Status |
|---|---|---|
| P0 | Foundation + OTel-shaped data model | ✅ Done (`docs/phases/phase-0.md`) |
| P1 | OTLP ingestion + receiver + storage | ✅ Done (`docs/phases/phase-1.md`) |
| P2 | Read-only timeline UI | ✅ Done (`docs/phases/phase-2.md`) |
| P3 | Replay engine + interceptor (the moat) | ✅ Done (`docs/phases/phase-3.md`) |
| P4 | State checkpointing | ✅ Done (`docs/phases/phase-4.md`) |
| P5 | Branching & diff UI | ✅ Done (`docs/phases/phase-5.md`) |
| P5.5 | Batch parallel eval harness | ✅ Done (`docs/phases/phase-5.5.md`) |
| P6 | Per-framework replay adapters | ✅ Done (`docs/phases/phase-6.md`) |
| P7 | Local-model enrichment | ✅ Done (`docs/phases/phase-7.md`) |
| P8 | Polish, packaging, distribution | ✅ Done (`docs/phases/phase-8.md`) |

## Quick start

```bash
pip install rewind-ai
rewind serve --port 4318 --db ./rewind.db
# Point your OTel/OpenInference-instrumented agent at:
#   OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
# Run a trace, then inspect it in the browser:
rewind ui --port 8484 --db ./rewind.db
# → http://127.0.0.1:8484/ui
```

### Replay a recorded trace

```bash
# Read-only inspection (prints cursor + branch info):
rewind replay <trace_id> --mode frozen --db ./rewind.db

# Branch from span index 4 and go live from there:
rewind replay <trace_id> --branch-at 4 --mode branch --db ./rewind.db
```

### From Python (the load-bearing integration point)

```python
from rewind.replay import replay
from rewind.storage import TraceStore

store = TraceStore("~/.rewind/db.sqlite")

# Frozen replay — zero outbound calls, deterministic:
with replay(store, trace_id="<trace>", mode="frozen"):
    agent.run()  # every LLM call served from the recorded spans

# Branch from span 4, then go live:
with replay(store, trace_id="<trace>", branch_at=4, mode="branch"):
    agent.run()  # spans 0-3 from recording, span 4+ calls your live model
```

### Wrap a framework model (Phase 6 adapters)

One import + one wrapper call per agent — no upstream framework changes:

```python
# Google ADK
from rewind.adapters.adk import replay_llm
agent = Agent(model=replay_llm(real_adk_llm))

# CrewAI
from rewind.adapters.crewai import replay_llm
crew.llm = replay_llm(real_crewai_llm)

# PydanticAI
from rewind.adapters.pydantic_ai import replay_model
agent = Agent(model=replay_model(real_model))

# HuggingFace SmolAgents
from rewind.adapters.smolagents import replay_model
agent.model = replay_model(real_smol_model)

# LangGraph (Phase 3 — adapter pattern origin)
from rewind.adapters.langgraph import replay_chat_model
graph.compiled = replay_chat_model(real_chat_model)
```

Install the optional extras as needed:

```bash
pip install rewind-ai[adk]              # one framework
pip install rewind-ai[adk,crewai]       # several
pip install rewind-ai[adapters]         # all four
```

Without an extra installed, the corresponding factory raises
`rewind.adapters.<fw>.AdapterError` with an actionable install hint at call
time. `rewind --version` and `import rewind.adapters.<fw>` both succeed
without any framework installed.

### Eval a replay candidate against a baseline (Phase 5.5)

```bash
rewind eval suite.yaml --db ./rewind.db --suite-name my-suite
# exit 0 = PASS, 1 = FAIL, 2 = ERROR/validation
```

### Step through an agent interactively (Phase 9)

Pause an agent at every LLM/tool call, inspect the pending step, edit the
prompt or tool args, and approve / stop / step-once — all from the browser
UI. Then rewind to any prior step and re-run from there with different
edits. This is the capability LangSmith and Langfuse lack: they observe;
Rewind lets you **steer**.

```python
# 1. Write your agent as an async runner and register it.
from rewind.replay import ReplaySession
from rewind.stepping_api import register_runner

async def my_runner(session: ReplaySession) -> None:
    await agent.run()   # pauses at each LLM call automatically

register_runner("my-agent", my_runner)

# 2. Start the server (runs the registered runner behind the UI).
#    python examples/interactive_stepping.py --db ~/.rewind/rewind.db

# 3. Open http://127.0.0.1:8484/ui, click "sessions", start a session with
#    your trace id + runner ref. The agent pauses at each step; approve,
#    edit, or stop from the browser.
```

See [`examples/interactive_stepping.py`](examples/interactive_stepping.py)
for a complete worked example, and [`docs/phases/phase-9.md`](docs/phases/phase-9.md)
for the full design, threat model, and the runner contract.

For the seeded live-workbench walkthrough, including prompt variants,
token/cost checks, saved-step navigation, and browser-refresh validation, see
[`docs/interactive-workbench-testing.md`](docs/interactive-workbench-testing.md).

## Development

```bash
# from rewind/
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# full quality gate (run before commit)
ruff check src/rewind tests
pylint src/rewind/
mypy --strict src/rewind
python -m pytest tests --no-cov -q

# per-phase security scan (ruff S-rules + bandit, deepsec if available)
python scripts/security_scan.py --phase <N>

# frontend dev server (Vite 6 — must run from web/; --host avoids IPv6 trap)
cd web && pnpm dev   # or:  node_modules/.bin/vite --host 127.0.0.1
```

Latest local quality gate: 486 passing tests, 12 skipped framework-gated
adapter tests, and 48 deselected integration tests; mypy --strict is clean
across 35 source files. See `docs/interactive-workbench-testing.md` for the
interactive debugger checks.

## Layout

```
rewind/
  src/rewind/          Python package
    adapters/          Phase 6 — per-framework replay wrappers (adk, crewai,
                       pydantic_ai, smolagents, langgraph) + shared _common.py
    receiver.py        OTLP/HTTP ingest (Phase 1)
    replay.py          Frozen / branch / full replay engine (Phase 3)
    checkpoint.py      State snapshot/restore (Phase 4)
    diff.py            Trace diff (Phase 5)
    eval_api.py        Suite runner + baseline diff (Phase 5.5)
    cli.py             Click-based CLI: serve / ui / replay / eval / version
  tests/               pytest suites (per-phase, 486 passing locally)
  web/                 React + Vite + TypeScript timeline UI (P2)
  docs/
    phases/            Per-phase: QA, security, dev-handoff, design
    diagrams/          Architecture + sequence (.mmd) for each phase
  scripts/
    dev_seed_serve.py  Local dev harness
    security_scan.py   ruff S + bandit per-phase vulnerability scan
  .deepsec/            Vulnerability scan reports (when deepsec available)
  pyproject.toml       Strict ruff + pylint + mypy config; optional extras
```

See `docs/README.md` for a navigable index of all phase docs and diagrams.

## Out of scope (v1)

- A bespoke capture proxy / capture decorator SDK (OTel + OpenInference solve
  this; Phase 6 adds a thin adapter factory for replay, not capture).
- Cloud / multi-user / team / sync (a trace is a local SQLite file).
- MCP security sandbox, model routing, mobile/remote access.

See `plan.md` (parent dir) for the full phased plan.
