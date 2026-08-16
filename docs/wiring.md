# Per-Framework OpenInference Wiring

Rewind accepts OTLP/HTTP from any OpenTelemetry-compatible source. The
per-framework pages below cover the most common agent frameworks — each
follows the same three-step shape:

1. `pip install openinference-instrumentation-<framework>`.
2. Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`.
3. Call `<Framework>Instrumentor().instrument()` once at process import.

That's it. Every LLM call, tool call, and agent-level span afterwards is
captured and visible in Rewind within ~100ms.

For end-to-end runnable demos, see [`examples/`](../examples).

---

## OpenAI / Ollama / generic chat-completions

OpenInference's OpenAI instrumentation monkey-patches
`openai.resources.chat.completions.Completions.create` (and the async +
streaming variants). Works equally well against `OPENAI_BASE_URL` pointed
at a local Ollama (recommended for fully-offline Rewind demos).

```bash
pip install openinference-instrumentation-openai opentelemetry-sdk opentelemetry-exporter-otlp
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
# Optional, for Ollama:
export OPENAI_BASE_URL=http://localhost:11434/v1
export OPENAI_API_KEY=dummy
```

```python
from openinference.instrumentation.openai import OpenAIInstrumentor
OpenAIInstrumentor().instrument()
# Now any call to openai.ChatCompletion.create is captured.
```

**Example**: [`examples/tool_caller.py`](../examples/tool_caller.py).

---

## Google ADK

```bash
pip install "google-adk>=1.28.1,<2" openinference-instrumentation-google-adk
```

```python
from openinference.instrumentation.google_adk import ADKInstrumentor
ADKInstrumentor().instrument()
```

Captures `google.adk.models.BaseLlm.generate_content_async`. **Replay
adapter**: [`docs/replay-adapters.md`](./replay-adapters.md#adk) — wrap
your ADK `BaseLlm` with `rewind.adapters.adk.replay_llm(real_model)` to
get time-travel branching in debug mode.

---

## LangGraph / LangChain

```bash
pip install openinference-instrumentation-langchain
```

```python
from openinference.instrumentation.langchain import LangchainInstrumentor
LangchainInstrumentor().instrument()
```

Captures `langchain_core.language_models.BaseChatModel` invocations,
LangGraph node transitions, and LangChain tool calls. **Replay adapter**:
`rewind.adapters.langgraph.replay_chat_model(model)` — see
[`docs/replay-adapters.md`](./replay-adapters.md#langgraph).

---

## CrewAI

```bash
pip install openinference-instrumentation-crewai
```

```python
from openinference.instrumentation.crewai import CrewAIInstrumentor
CrewAIInstrumentor().instrument()
```

Captures `BaseLLM.call[_async]` / `get_response[_async]`. **Replay
adapter**: `rewind.adapters.crewai.replay_llm(llm)` — see
[`docs/replay-adapters.md`](./replay-adapters.md#crewai).

---

## PydanticAI

```bash
pip install openinference-instrumentation-pydantic-ai
```

```python
from openinference.instrumentation.pydantic_ai import PydanticAIInstrumentor
PydanticAIInstrumentor().instrument()
```

Captures `pydantic_ai.models.Model.request[_stream]`. **Replay adapter**:
`rewind.adapters.pydantic_ai.replay_model(model)` — see
[`docs/replay-adapters.md`](./replay-adapters.md#pydanticai).

---

## SmolAgents (HuggingFace)

```bash
pip install openinference-instrumentation-smolagents
```

```python
from openinference.instrumentation.smolagents import SmolagentsInstrumentor
SmolagentsInstrumentor().instrument()
```

Captures `smolagents.models.Model.__call__`, `generate`, and `astream`.
**Replay adapter**: `rewind.adapters.smolagents.replay_model(model)` —
see [`docs/replay-adapters.md`](./replay-adapters.md#smolagents).

---

## MCP tool calls

```bash
pip install openinference-instrumentation-mcp
```

```python
from openinference.instrumentation.mcp import MCPInstrumentor
MCPInstrumentor().instrument()
```

Captures tool invocations routed through the Model Context Protocol
(regardless of which agent framework is the parent). This is the one
instrumentation package you almost always want **in addition to** your
LLM instrumentation — MCP is the open standard for tool-call capture and
Rewind emits one `gen_ai.mcp` span per MCP tool invocation.

---

## Common pitfalls

### "I see LLM spans but not tool calls"

Three usual causes:

1. The MCP / tool instrumentation package isn't installed (it's separate
   from the LLM instrumentation package).
2. The tool framework you're using doesn't have an OpenInference
   instrumentation package yet — fall back to a manual span via the
   OTel SDK, or branch from the LLM span that *references* the tool call.
3. The instrumentor wasn't `.instrument()`-ed before any actual calls
   happened — instrumentation must run at import time, not later.

### "Trace is captured but messages are empty"

Some instrumentation packages log message content only when
`OPENINFERENCE_CAPTURE_MESSAGE_CONTENT=true` is set. That's the default
in recent versions, but older environments may have it off. Set it
explicitly:

```bash
export OPENINFERENCE_CAPTURE_MESSAGE_CONTENT=true
```

### "Volume is overwhelming my disk"

A busy agent loop produces a lot of spans. Two mitigations:

1. `OTEL_BSP_MAX_QUEUE_SIZE=10000` — caps the in-flight batch.
2. Delete `~/.rewind/rewind.db` between projects — Rewind doesn't ship a
   retention policy in v1; an operator's local SQLite file is the
   single source of truth.
