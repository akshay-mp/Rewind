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
| **P0** | Foundation + OTel-shaped data model | 🚧 In progress |
| P1 | OTLP ingestion + OpenInference wiring | Planned |
| P2 | Read-only timeline UI | Planned |
| P3 | Replay engine + interceptor (the moat) | Planned |
| P4 | State checkpointing | Planned |
| P5 | Branching & diff UI | Planned |
| P5.5 | Batch parallel eval harness | Planned |
| P6 | Per-framework replay adapters | Planned |
| P7 | Local-model enrichment | Planned |
| P8 | Polish, packaging, distribution | Planned |

## Quick start (once P1 lands)

```bash
pipx install rewind-ai
rewind serve --otlp-port 4318 --db ./rewind.db
# Point your instrumented agent at OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

## Development

```bash
# from rewind/
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# quality gates
ruff check .          # lint
pylint src/rewind     # lint
mypy src/rewind       # type check
pytest                # tests + coverage

# deepsec vulnerability scan (per phase)
deepsec scan --src src/rewind --out .deepsec/phase0
```

## Layout

```
rewind/
  src/rewind/          Python package
  tests/               pytest suites (per-phase)
  web/                 React + Vite + TypeScript UI (P2+)
  docs/phases/         Per-phase: QA, security, dev-handoff, design, diagrams
  .deepsec/            Vulnerability scan reports
```

## Out of scope (v1)

- A bespoke capture proxy / capture decorator SDK (OTel + OpenInference solve this).
- Cloud / multi-user / team / sync (a trace is a local SQLite file).
- MCP security sandbox, model routing, mobile/remote access.

See `plan.md` (parent dir) for the full phased plan.
