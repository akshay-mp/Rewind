# Rewind Demo Agents

Three minimal end-to-end examples that exercise Rewind's full pipeline —
**OTLP capture → SQLite storage → timeline UI** — using OpenInference
auto-instrumentation.

Each demo is a single-file Python script designed to:

1. auto-capture every LLM / tool call as an OTel span,
2. ship those spans to a locally-running `rewind serve` instance via OTLP/HTTP,
3. print the captured trace id so the user can switch to the Web UI and
   inspect the timeline,
4. degrade gracefully when the optional instrumentation package isn't
   installed — the script still runs, the user still gets a useful error.

Each example is intentionally **small** — ~50 lines, one file, zero project
structure — so they're easy to copy-paste into a new agent skeleton.

## Prerequisites (one-time)

```bash
# Install Rewind (any of:)
pipx install rewind-ai
pip install rewind-ai            # if no pipx
pip install -e .                 # dev install from the repo

# Install one OpenInference instrumentation package per example:
pip install openinference-instrumentation-openai

# Start the receiver (in one terminal):
rewind serve

# Open the Timeline UI (in another terminal):
rewind ui
```

That's it. Each demo script below is hermetic — set `OTEL_EXPORTER_OTLP_ENDPOINT`
and run.

## Demos

| File | Agent pattern | Surfaces |
|---|---|---|
| [`tool_caller.py`](./tool_caller.py) | Single-shot tool-caller: user asks → LLM picks a tool → tool runs → LLM summarises. | LLM spans, tool span, tool-result span. Smallest possible multi-span trace. |
| [`rag_loop.py`](./rag_loop.py) | Retrieval-augmented loop: user asks → retrieve context → LLM answers → repeat for one follow-up. | Sequential LLM spans, retrieval tool span, parent `gen_ai.agent` span. |
| [`multi_step_coder.py`](./multi_step_coder.py) | Multi-step coding agent: think → write code → run code → reflect → revise. | Branching span tree with parent/child trace links; surfaces when tools fail. |

Each demo wraps the actual `openai` SDK with OpenInference's instrumentation
context manager; the `OTEL_EXPORTER_OTLP_ENDPOINT` env var (set by these
scripts to `http://127.0.0.1:4318`) points the OTLP exporter at Rewind.

## After running

Once a demo prints `trace_id=...`, switch to the Rewind UI running on
`http://127.0.0.1:8484/ui/` — the trace will appear in the list almost
instantly. From there:

- Click any LLM span to view the full message content + token counts.
- Use **Branch from here** to re-run with a changed prompt.
- Diff two branches to see exactly where output diverged.

## What these demos **don't** do

- **No Replay**: these demos exercise capture only. Replay (`rewind.replay()`
  ctxmgr) is documented separately under `docs/phases/phase-3.md` — it's the
  opt-in debug-mode counterpart to capture.
- **No Eval**: these demos produce one trace per run. Running them under
  `rewind eval` (Phase 5.5) is the way to score agent variants at scale.
- **No framework deps**: each demo uses plain `openai` so they run anywhere
  with `pip install openinference-instrumentation-openai`. Demos for ADK /
  CrewAI / PydanticAI / SmolAgents follow the same shape but import those
  frameworks' own OpenInference instrumentation packages.
