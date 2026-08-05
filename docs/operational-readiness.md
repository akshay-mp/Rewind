# Operational Readiness

The Rewind workbench keeps operational records on the developer machine.
Its session controls cover the parts of a pre-production run that need to be
inspected before an agent reaches a deployed environment.

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

The interactive demo uses any OpenAI-compatible endpoint via
`OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `REWIND_MODEL`. This includes local
Unsloth, Ollama, llama.cpp/vLLM-style servers, and hosted OpenAI-compatible
providers. The package also ships replay adapters for LangGraph, Google ADK,
CrewAI, PydanticAI, and SmolAgents; see [replay adapters](./replay-adapters.md)
and [wiring](./wiring.md) for the integration snippets.
