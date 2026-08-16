# Operational Readiness

The Rewind workbench keeps operational records on the developer machine.
Its session controls cover the parts of a pre-production run that need to be
inspected before an agent reaches a deployed environment. The current
decorator-first entry point is `from rewind import RewindContext, rewind` with
`@rewind.agent`; use `rewind dev app:rewind` for a local workbench run.
For a custom title or separate registry, use `from rewind import Rewind,
RewindContext` and define `rewind = Rewind(title="...")` in your app module;
existing names such as `debugger` remain supported.

## Run Records

Complete a stepped session, then choose **Save regression case**. The local
record contains the resolved steps, review decisions, notes, prompt variants,
checkpoints, token totals, and execution latency. Use **Saved sessions** from
the completion view or the Sessions tab to search these records by trace,
runner, or date.

## Pricing And Latency

Pricing is configured locally in US dollars per million input, output, and
thinking tokens. For an on-device model, leave all three values at zero. The
workbench shows the total agent execution time separately from review time;
it does not count time spent paused for a developer decision as model latency.

## Reproducibility And Export

The completion summary includes model names, checkpoint count, prompt variant
count, parameter-set count, and tool-schema count. **Export redacted bundle**
downloads a `rewind-bundle/v1` JSON record. Rewind removes values associated
with common secret keys plus bearer and `sk-...` token strings before writing
the file. Review the exported bundle before sharing it outside the machine.
Use **Import bundle** in Saved sessions to bring a compatible exported record
back into the local session library for inspection and search.

## Provider And Framework Support

The verified interactive demo uses the local OpenAI-compatible Gemma/Unsloth
endpoint at `http://127.0.0.1:8888/v1`, configured with
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `REWIND_MODEL`. Check
`http://127.0.0.1:8888/v1/models` before recording or testing. Keep the API
key in the environment or local `.env`; never place it in source, logs,
screenshots, or a recording.

During an OpenAI-framework workbench run, official OpenAI Python SDK Chat
Completions calls (`chat.completions.create`, sync and async) are intercepted,
including when that SDK is configured for an OpenAI-compatible endpoint.
Replay adapters for LangGraph, Google ADK, CrewAI, PydanticAI, and SmolAgents
remain explicit; generic decorator auto-activation for them is unavailable and
reports an actionable wrapper. See
[replay adapters](./replay-adapters.md) and [wiring](./wiring.md) for the
integration snippets. This support statement does not claim any optional
framework package is installed locally.

For a short LinkedIn capture, follow the
[recording-ready checklist](./demo-recording.md). It describes production
preparation only; Rewind does not provide video capture or caption-burning
tooling.
