"""TimeTravel framework adapters — bridge recorded spans to live agent loops.

Phase 3 shipped the first adapter (LangGraph) to validate the
adapter-first replay strategy (plan §6 Phase 3 Track 3B.1). Phase 6
extends coverage to four more agent frameworks so users can opt into
replay without falling back to the generic OpenAI monkey-patch. Each
adapter lives here as its own module:

* :mod:`agent_timetravel.adapters.langgraph` — :func:`replay_chat_model` wraps
  a ``langchain_core.BaseChatModel``.
* :mod:`agent_timetravel.adapters.adk` — :func:`replay_llm` wraps a
  ``google.adk.models.llms.BaseLlm``.
* :mod:`agent_timetravel.adapters.pydantic_ai` — :func:`replay_model` wraps a
  ``pydantic_ai.models.Model``.
* :mod:`agent_timetravel.adapters.crewai` — :func:`replay_llm` wraps a
  ``crewai.llms.base_llm.BaseLLM``.
* :mod:`agent_timetravel.adapters.smolagents` — :func:`replay_model` wraps a
  ``smolagents.models.Model``.

Imports remain lazy: the user's project only pays for the framework
they're actually replaying, and ``agent-timetravel --version`` stays fast when no
agent framework is installed.
"""
