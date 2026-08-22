# Quickstart

From zero to viewing a captured trace in your browser in **under five
minutes**.

## 1. Install TimeTravel

```bash
pipx install agent-timetravel          # recommended — isolated, no venv juggling
# OR
pip install agent-timetravel
# OR, from the repo:
pip install -e .
```

Verify:

```bash
timetravel --version
```

## Decorator-first workbench

For a local interactive agent, define one `TimeTravel` object and load it with:

```python
from agent_timetravel import TimeTravelContext, timetravel

@timetravel.agent(description="Answer a question", tags=("demo",))
async def answer(question: str, context: TimeTravelContext | None = None) -> str:
    return question
```

Start it with `timetravel dev app:timetravel`. Direct calls to `answer(...)` remain
ordinary pass-through calls; `TimeTravelContext` is injected only for workbench
runs. For a custom title or separate registry, use:

```python
from agent_timetravel import TimeTravel, TimeTravelContext

timetravel = TimeTravel(title="My agents")
```

Existing names such as `debugger` remain supported. Official OpenAI Python SDK
Chat Completions calls
(`chat.completions.create`, sync and async) are intercepted in this path,
including when that SDK is configured for an OpenAI-compatible endpoint.
LangGraph / langchain apps get the same auto-activation: every
`BaseChatModel` and `BaseTool` `invoke`/`ainvoke` is stepped, replayed, and
captured — and `timetravel app:main` accepts a bare compiled graph as the target
(no TimeTravel-specific code in the app). Replay adapters for the remaining
frameworks (CrewAI, PydanticAI, ADK, SmolAgents) remain explicit; generic
decorator auto-activation for them is unavailable and reports an actionable
wrapper hint. See [`replay-adapters.md`](./replay-adapters.md).

For the live verified demo, use the exact local model and UI setup in
[`interactive-workbench-testing.md`](./interactive-workbench-testing.md).

## 2. Start the receiver

```bash
timetravel serve
```

Output will look like:

```
timetravel serve → http://127.0.0.1:4318/v1/traces  (db=~/.timetravel/timetravel.db, version=0.1.2)
```

The receiver now accepts OTLP/HTTP at `http://127.0.0.1:4318/v1/traces`
and persists every span into `~/.timetravel/timetravel.db`.

## 3. Open the Timeline UI (second terminal)

```bash
timetravel ui
```

A browser tab opens at `http://127.0.0.1:8484/ui/`. It's empty until
you ship your first trace — that's step 4.

## 4. Capture a trace from any OpenInference-instrumented agent

Pick the easiest path: install an OpenInference instrumentation package
and run any agent that uses the underlying SDK. Three complete example
agents live in [`examples/`](../examples); the absolute simplest:

```bash
pip install openinference-instrumentation-openai opentelemetry-sdk opentelemetry-exporter-otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
python examples/tool_caller.py
```

You should see the trace appear in the Timeline UI within ~100ms of the
script exiting. Click any span to inspect the full message content,
token counts, model name, and raw OpenInference attributes.

## 5. Branch & diff (the killer feature)

Once a trace is loaded:

1. Click any LLM span → **"Branch from here"**.
2. Edit the system prompt or any message.
3. Click **"Run live"** → a new branch appears in the branch tree.
4. Click **"Diff"** between the original and the new branch.

The diff view highlights the first divergent span and lets you walk
token-level changes between the two runs.

## What's next

- Follow the [live workbench verification](./interactive-workbench-testing.md)
  for the decorator-first stepping flow.
- Use the [recording-ready checklist](./demo-recording.md) when preparing a
  short product demo; it is a production checklist, not video tooling.
- See [`docs/wiring.md`](./wiring.md) for per-framework instrumentation
  recipes (OpenAI, ADK, LangGraph, CrewAI, PydanticAI, SmolAgents, MCP).
- See [`docs/branching-diff-walkthrough.md`](./branching-diff-walkthrough.md)
  for an in-depth tour of branching and diffing.
- See [`docs/replay-adapters.md`](./replay-adapters.md) to wire replay
  (time-travel) into a debug iteration loop.
- See [`docs/phases/phase-7.md`](./phases/phase-7.md) for local-model
  enrichment commands (`timetravel enrich`, `timetravel render-template`).

## Troubleshooting

**"Trace doesn't appear in the UI"** — verify your agent actually emitted.
The fastest way is `timetravel --help` to check the DB path, then query it:

```bash
sqlite3 ~/.timetravel/timetravel.db "SELECT COUNT(*) FROM traces;"
```

If the count is zero, the OTLP exporter isn't reaching TimeTravel — check
`OTEL_EXPORTER_OTLP_ENDPOINT` and that `timetravel serve` is bound to the same
interface the exporter can reach.

**"receiver says `db=~/.timetravel/timetravel.db` but the file doesn't exist"** —
that's expected: TimeTravel creates `~/.timetravel/` on first write. If your
agent hasn't sent a trace yet, the file is not yet created.

**"`timetravel serve` crashes with `Address already in use`"** — another
timetravel instance (or another process) is on 4318. Use `--port 4319` and
update `OTEL_EXPORTER_OTLP_ENDPOINT` accordingly.
