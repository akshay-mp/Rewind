# TimeTravel Demo Agents

Three minimal end-to-end examples that exercise TimeTravel's full pipeline —
**OTLP capture → SQLite storage → timeline UI** — using OpenInference
auto-instrumentation.

Each demo is a single-file Python script designed to:

1. auto-capture every LLM / tool call as an OTel span,
2. ship those spans to a locally-running `agent-timetravel serve` instance via OTLP/HTTP,
3. print the captured trace id so the user can switch to the Web UI and
   inspect the timeline,
4. degrade gracefully when the optional instrumentation package isn't
   installed — the script still runs, the user still gets a useful error.

Each example is intentionally **small** — ~50 lines, one file, zero project
structure — so they're easy to copy-paste into a new agent skeleton.

## Prerequisites (one-time)

```bash
# Install TimeTravel (any of:)
pipx install agent-timetravel
pip install agent-timetravel            # if no pipx
pip install -e .                 # dev install from the repo

# Install one OpenInference instrumentation package per example:
pip install openinference-instrumentation-openai

# Start the receiver (in one terminal):
agent-timetravel serve

# Open the Timeline UI (in another terminal):
agent-timetravel ui
```

That's it. Each demo script below is hermetic — set `OTEL_EXPORTER_OTLP_ENDPOINT`
and run.

## Demos

### deepagents deep-research — the modern integration ⭐

**[`deepagents_research/`](./deepagents_research/)** — a foreign LangGraph
project (the deepagents deep-research agent) under the interactive
step-by-step workbench with one dependency and one `app.py`. Plain-text
query input, every LLM and tool call (subagents included) gated in the
debugger, local-model support. Start here — this is the recommended
integration path.

```bash
cd deepagents_research    # after arranging the graph project + .env
agent-timetravel app:main       # browser opens at http://127.0.0.1:8484/ui
```

### Capture-only (toy agents)

| File | Agent pattern | Surfaces |
|---|---|---|
| [`tool_caller.py`](./tool_caller.py) | Single-shot tool-caller: user asks → LLM picks a tool → tool runs → LLM summarises. | LLM spans, tool span, tool-result span. Smallest possible multi-span trace. |
| [`rag_loop.py`](./rag_loop.py) | Retrieval-augmented loop: user asks → retrieve context → LLM answers → repeat for one follow-up. | Sequential LLM spans, retrieval tool span, parent `gen_ai.agent` span. |
| [`multi_step_coder.py`](./multi_step_coder.py) | Multi-step coding agent: think → write code → run code → reflect → revise. | Branching span tree with parent/child trace links; surfaces when tools fail. |

Each demo wraps the actual `openai` SDK with OpenInference's instrumentation
context manager; the `OTEL_EXPORTER_OTLP_ENDPOINT` env var (set by these
scripts to `http://127.0.0.1:4318`) points the OTLP exporter at TimeTravel.

### Deep-research integration (capture + replay + branch + diff)

| File | What it is | Status |
|---|---|---|
| [`deep_research_demo.py`](./deep_research_demo.py) | **The recommended live demo (Python/CLI).** A flattened 8-step deep-research agent run against a local model (Unsloth/Ollama) through TimeTravel's real capture → frozen → branch+diff engine. Runs in ~3 min on a 27B model. | ✅ Working |
| [`deep_research.py`](./deep_research.py) | A heavier variant that drives the full `open_deep_research` LangGraph graph. More faithful to ODR but heavier/fragile — see its README for the Python-3.14 / `jsonschema-rs` caveat. | ⚠️ Needs 3.11–3.13 |
| [`deep_research_README.md`](./deep_research_README.md) | Setup + env vars + how-it-works for the Python demos. | — |

### The polished web UI

| Path | What it is |
|---|---|
| [`../web-demo/`](../web-demo/) | A Next.js + shadcn/ui app with a three-panel debugger: span timeline (left), span detail + prompt editor (right), branch diff (modal). Talks to your local model via the OpenAI SDK and mirrors spans into TimeTravel's receiver. **This is the demo to show people.** |
| [`../docs/demo-run.md`](../docs/demo-run.md) | Step-by-step run guide for the web demo against Unsloth Studio. |

## After running

Once a demo prints `trace_id=...`, switch to the TimeTravel UI running on
`http://127.0.0.1:8484/ui/` — the trace will appear in the list almost
instantly. From there:

- Click any LLM span to view the full message content + token counts.
- Use **Branch from here** to re-run with a changed prompt.
- Diff two branches to see exactly where output diverged.

## What these demos **don't** do

- **No Replay** *(the capture-only demos above)*: `tool_caller`/`rag_loop`/
  `multi_step_coder` exercise capture only. For the full capture → frozen
  replay → branch → diff loop, use [`deep_research_demo.py`](./deep_research_demo.py)
  or the [`web-demo/`](../web-demo/) UI — see [`docs/demo-run.md`](../docs/demo-run.md).
- **No Eval**: these demos produce one trace per run. Running them under
  `agent-timetravel eval` (Phase 5.5) is the way to score agent variants at scale.
- **No framework deps** *(capture-only demos)*: each uses plain `openai` so
  they run anywhere with `pip install openinference-instrumentation-openai`.
  Demos for ADK / CrewAI / PydanticAI / SmolAgents follow the same shape but
  import those frameworks' own OpenInference instrumentation packages.
