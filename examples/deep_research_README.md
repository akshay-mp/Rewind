# Deep Research under Rewind

> **Which deep-research demo should I use?** There are three; pick by goal:
>
> | Demo | Best for | Status |
> |---|---|---|
> | **[`../web-demo/`](../web-demo/)** + [`../docs/demo-run.md`](../docs/demo-run.md) | **Showing the demo to people** — the polished three-panel UI, click-to-branch, live diff. | ✅ Recommended |
> | **[`deep_research_demo.py`](./deep_research_demo.py)** | Headless / CI / scripting — same 8-step agent, runs the full capture→frozen→branch loop in pure Python via Rewind's real engine. | ✅ Working |
> | **[`deep_research.py`](./deep_research.py)** (this file's companion) | Maximum fidelity to the upstream ODR LangGraph graph (parallel researchers, structured outputs). | ⚠️ Heavier; needs Python 3.11–3.13 (see caveat below) |
>
> The rest of this README covers `deep_research.py` (the ODR-via-LangGraph path).

The canonical end-to-end integration: **[open_deep_research](https://github.com/langchain-ai/open_deep_research)**
(LangGraph's multi-node deep-research agent) running under Rewind's time-travel
debugger. This proves Rewind works on a real, multi-node, tool-calling agent —
not just the toy demos.

The driver runs three phases:

| Phase | What happens | Proves |
|---|---|---|
| **A. Capture** | Run the agent once live; every LLM call captured as an OTel span via OpenInference → `rewind serve`. | The capture pipeline ingests a real LangGraph agent. |
| **B. Frozen** | Re-run under `rewind.replay(mode=FROZEN)` + OpenAI intercept. Every LLM call served from the fixture — **zero outbound traffic**. | The core "no egress" guarantee holds on a multi-node agent. |
| **C. Branch + Diff** | Fork at a researcher span, change the topic, re-run live. `span_diff` flags the divergence; `message_diff` shows token-level change. | Branching & diffing work on a real agent. |

## Prerequisites

```bash
# 1. Rewind itself (from the repo)
pip install -e ".[dev]"

# 2. The deep-research extra
pip install open-deep-research openinference-instrumentation-langchain
#    (or, once declared:  pip install rewind-ai[deepresearch])

# 3. A local model server reachable via the OpenAI-compatible API:
#    Unsloth (default), Ollama, or even real OpenAI.
```

## Run

```bash
# terminal 1 — the Rewind receiver
rewind serve

# terminal 2 — your local model server (e.g. Unsloth)
#   (make sure it's serving OpenAI-compatible /v1/chat/completions)

# terminal 3 — the integration
python examples/deep_research.py
```

Then open **http://127.0.0.1:8484/ui/** to inspect the captured trace, the
branch tree, and the diff view.

## Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | Where the OpenAI-compatible client points (Unsloth). Point at Ollama with `http://localhost:11434/v1`; omit for real OpenAI. |
| `OPENAI_API_KEY` | `local` | API key for the local server. |
| `REWIND_MODEL` | `unsloth/Llama-3.1-8B-Instruct` | Model name passed to the agent. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://127.0.0.1:4318` | Where OpenInference ships spans (the Rewind receiver). |
| `REWIND_DB` | `~/.rewind/rewind.db` | SQLite DB path (or pass `--db <path>`). |

To use Ollama instead of Unsloth, no code change is needed:

```bash
OPENAI_BASE_URL=http://localhost:11434/v1 REWIND_MODEL=qwen3:32b \
  python examples/deep_research.py
```

## How it works (the wiring)

Open Deep Research builds one module-global LangChain model via
`init_chat_model(...)`; every node (clarify → write_brief → supervisor →
researcher → final_report) calls `.ainvoke()` on it, which bottoms out at
`openai...Completions.create`. Rewind intercepts **there**:

- **Capture:** OpenInference's LangChain instrumentor emits OTel spans to the
  Rewind receiver, which stores them in SQLite.
- **Frozen replay:** `rewind.openai_intercept.patch()` monkey-patches the
  OpenAI `create`; during a `rewind.replay(mode=FROZEN)` context it serves
  each recorded response verbatim — no model server contacted.
- **Branch:** `rewind.replay(mode=BRANCH, branch_at=N)` serves the prefix
  from fixtures then forwards divergent calls live, capturing the new tail
  under a fresh `branch_id`.

No `open_deep_research` source changes are required.

## Notes & limitations

- **Python version caveat (3.14):** `open-deep-research` transitively pulls
  `langgraph-cli[inmem]` → `jsonschema-rs<0.30`, which has no Python 3.14
  wheel (PyO3 maxes at 3.13). On 3.14 you must install ODR with `--no-deps`
  after satisfying its runtime deps manually (`langgraph`, `langchain-openai`,
  `langchain-community`, `langchain-tavily`, `langchain-mcp-adapters`, `mcp`).
  On Python 3.11–3.13 a plain `pip install rewind-ai[deepresearch]` works.
  **If you just want to run the demo, use `deep_research_demo.py` or the
  `web-demo/` instead — neither has this dependency.**
- The agent runs with `search_api="none"` (no external web search) so the
  replay path is hermetic. To use a real search backend (Tavily / OpenAI /
  Anthropic web search), set it in the config and wrap the search tool with
  `@rewind.tool(...)` so its results are cached during frozen replay.
- Frozen replay requires **non-streaming** model calls. ODR uses
  `.ainvoke()` (non-streaming) by default, so this is satisfied out of the
  box; do not enable streaming config.
- See `tests/integration/test_deep_research_replay.py` for a hermetic
  (no-model, no-network) version of the same three-phase contract.
