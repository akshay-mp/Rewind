# Rewind — Documentation Index

> Time-travel debugging for AI agents. **OTel-in / replay-out.**
> Phases 0 through 8 are complete (full plan delivered). See `README.md`
> (parent) for install + usage, or jump straight to [`quickstart.md`](quickstart.md)
> for the 5-minute install-to-trace flow.

## Per-phase docs (`phases/`)

Each phase has a uniform 6-section doc — System Design / Architecture Diagram /
Sequence Diagrams / QA (test inventory + exit criteria) / Security (threat
model + scan results) / Developer Handoff. Read the latest phase doc that
touched the surface you're working on; earlier phases link forward.

| Phase | Title | Doc | Status |
|---|---|---|---|
| 0   | Foundation + OTel-shaped data model                       | [`phase-0.md`](phases/phase-0.md)   | ✅ |
| 1   | OTLP ingestion + receiver + storage                       | [`phase-1.md`](phases/phase-1.md)   | ✅ |
| 2   | Read-only timeline UI (React + Vite + TS)                 | [`phase-2.md`](phases/phase-2.md)   | ✅ |
| 3   | Replay engine + OpenAI interceptor (the moat)            | [`phase-3.md`](phases/phase-3.md)   | ✅ |
| 4   | State checkpointing                                       | [`phase-4.md`](phases/phase-4.md)   | ✅ |
| 5   | Branching & diff UI                                       | [`phase-5.md`](phases/phase-5.md)   | ✅ |
| 5.5 | Batch parallel eval harness                                | [`phase-5.5.md`](phases/phase-5.5.md) | ✅ |
| 6   | Remaining per-framework replay adapters (ADK/CrewAI/PydanticAI/SmolAgents) | [`phase-6.md`](phases/phase-6.md) | ✅ |
| 7   | Local-model enrichment (quant, VRAM, chat-template, quant divergence) | [`phase-7.md`](phases/phase-7.md) | ✅ |
| 8   | Polish, packaging, distribution (default DB, extras, benchmark, demos, user docs) | [`phase-8.md`](phases/phase-8.md) | ✅ |

## Phase diagrams (`diagrams/`)

Each phase publishes three Mermaid diagrams — an **architecture** diagram and
two **sequence** diagrams covering the primary flows. Files are `.mmd`; render
from VS Code's Mermaid previewer, or by embedding in Markdown. All .mmd files
live under `diagrams/` and follow the naming convention
`phase<N>-(architecture|sequence-<flow>).mmd`.

| Phase | Architecture | Sequences |
|---|---|---|
| 0   | `phase0-architecture.mmd`                       | `phase0-sequence-roundtrip.mmd`; ER schema: `phase0-er-schema.mmd` |
| 1   | `phase1-architecture.mmd`                       | `phase1-sequence-ingest.mmd`, `phase1-sequence-integration.mmd` |
| 2   | `phase2-architecture.mmd`                       | `phase2-sequence-search.mmd`, `phase2-sequence-timeline.mmd` |
| 3   | `phase3-architecture.mmd`                       | `phase3-sequence-branch.mmd`, `phase3-sequence-frozen-replay.mmd` |
| 4   | `phase4-architecture.mmd`                       | `phase4-sequence-checkpoint.mmd`, `phase4-sequence-rollback.mmd` |
| 5   | `phase5-architecture.mmd`                       | `phase5-sequence-branch.mmd`, `phase5-sequence-diff.mmd` |
| 5.5 | `phase5.5-architecture.mmd`                     | `phase5.5-sequence-baseline.mmd`, `phase5.5-sequence-parallel.mmd` |
| 6   | `phase6-architecture.mmd`                       | `phase6-sequence-branch-divergence.mmd`, `phase6-sequence-frozen-replay.mmd` |
| 7   | `phase7-architecture.mmd`                       | `phase7-sequence-enrich.mmd`, `phase7-sequence-quant-flag.mmd` |
| 8   | `phase8-architecture.mmd`                       | _no new runtime flows — sequences documented inline in `phases/phase-8.md` §3_ |

## Other docs (parent `../`)

| Doc | Purpose |
|---|---|
| [`README.md`](../README.md)   | Project overview, status table, install + quick-start, dev workflow |
| [`quickstart.md`](quickstart.md) | 5-minute install-to-trace flow (end-user entry point) |
| [`demo-run.md`](demo-run.md) | **Live demo run guide** — capture → branch → diff against a local model (Unsloth) in the polished `web-demo/` UI. The doc to follow when showing Rewind to people. |
| [`wiring.md`](wiring.md)     | Per-framework OpenInference wiring recipes (OpenAI, ADK, LangGraph, CrewAI, PydanticAI, SmolAgents, MCP) |
| [`branching-diff-walkthrough.md`](branching-diff-walkthrough.md) | The core debugging workflow (branch + diff) end-to-end |
| [`replay-adapters.md`](replay-adapters.md) | Per-framework replay adapter usage (ADK/CrewAI/PydanticAI/SmolAgents) |
| [`../web-demo/`](../web-demo/) | The Next.js + shadcn/ui debugger frontend (three-panel timeline / span detail / branch diff). See `demo-run.md` for setup. |
| [`../examples/deep_research_demo.py`](../examples/deep_research_demo.py) | Python/CLI equivalent of the live demo (headless capture→frozen→branch). |
| [`../plan.md`](../../plan.md) | Original full phased plan, competitive analysis, architectural asymmetry |
| `memories/repo/`              | Per-phase completion notes + project conventions (repo-scoped; machine-curated) |

## Quality snapshot (Phase 8 exit — full plan delivered)

- **Tests:** 332 passing / 12 skipped (the 12 are framework-gated adapter
  suites requiring an uninstalled framework).
- **Lint:** `ruff check src/rewind tests` clean; `pylint src/rewind/` 10.00/10.
- **Types:** `mypy --strict src/rewind` clean across 29 source files.
- **Security:** `ruff select S` clean; `bandit -r src/rewind` clean; `deepsec`
  skipped when unavailable on `PATH`.
- **Performance:** interceptor p99 = 0.167µs (target <100µs; ~600× headroom).

Reproduce via the command block in `README.md §Development` or any phase doc
§6 (Developer Handoff).
