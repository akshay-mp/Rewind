# deepagents deep-research under TimeTravel

The modern, minimal integration: a foreign LangGraph project (the
[deepagents](https://github.com/langchain-ai/deepagents) deep-research
example) under the TimeTravel step-by-step debugger with **one dependency
and one integration file**.

## Setup

```bash
# 1. Get the graph project (from the deepagents repo):
#    examples/deep_research — then copy this directory's app.py into it
#    (or run from here with research_agent importable).

# 2. Install TimeTravel alongside it:
pip install agent-timetravel[langgraph]        # or: uv add ...

# 3. Configure .env:
cp .env.example .env   # then fill in the keys
```

`.env`:

```
# Local OpenAI-compatible model server (Unsloth / vLLM / Ollama):
OPENAI_BASE_URL=http://127.0.0.1:8888/v1
OPENAI_API_KEY=local
TIMETRAVEL_MODEL=unsloth/gemma-4-12b-it-GGUF
TIMETRAVEL_TEMPERATURE=0.3
TAVILY_API_KEY=...          # the graph's search tool needs it
# (omit the local-server vars to fall back to ANTHROPIC_API_KEY)
```

## Run

```bash
timetravel app:main      # ≡ timetravel dev app:main; browser opens at :8484/ui
```

Start Agent → `deep_research` → **type your question as plain text** (e.g.
*"Compare RLHF vs DPO for aligning large language models, with citations."*)
— TimeTravel builds the graph input internally.

Every chat-model call (orchestrator **and** subagents) and every tool call
(`tavily_search`, `think_tool`, `write_todos`, filesystem tools) pauses in
the debugger with the full prompt, context breakdown, token counts, and
cost. Approve to step, edit the prompt or tool arguments and re-run, step
back to saved state without new calls, or restart-from a checkpoint to
branch and diff.

## What the integration actually is

Two things, nothing else:

1. the `agent-timetravel[langgraph]` dependency,
2. `app.py` — imports the graph's own prompts/tools, rebuilds it with the
   env-driven model, and registers it:

```python
from agent_timetravel import TimeTravel

main = TimeTravel(title="Deep Research")

@main.agent(name="deep_research", framework="langgraph", target=agent_graph)
async def run(query: str, config: dict | None = None):
    return await agent_graph.ainvoke(
        {"messages": [{"role": "user", "content": query}]}, config or None
    )
```

No model wrapping, no instrumentation setup — `framework="langgraph"`
activates stepping, replay, and capture automatically.
