"""Turn a bare LangGraph graph (or any langchain runnable) into a workbench app.

``rewind dev app:main`` — and the ``rewind app:main`` shorthand — accepts not
just a :class:`rewind.Rewind` registry but the objects a LangGraph project
naturally exports:

* a compiled LangGraph graph / langchain runnable (anything exposing callable
  ``invoke`` + ``ainvoke``), wrapped into a single-agent registry;
* a plain callable, registered as-is with framework auto-detection.

The graph wrapper exposes a plain ``query`` string (plus an optional
``config`` object) in the start-agent dialog: the developer types the request
as text and the wrapper builds the standard chat-state input
``{"messages": [{"role": "user", "content": query}]}`` internally — no JSON
typing in the UI.
"""

from __future__ import annotations

import inspect
from typing import Any

from rewind.agents import Rewind

__all__ = ["GraphAppError", "is_langchain_runnable", "registry_from_object"]


class GraphAppError(ValueError):
    """Raised when an ``app:attr`` target cannot become a workbench app."""


def is_langchain_runnable(obj: Any) -> bool:
    """Duck-type a langgraph/langchain runnable without importing either.

    A runnable exposes ``invoke`` and ``ainvoke`` and lives in a
    ``langgraph``/``langchain`` module — the module check keeps foreign
    objects with coincidental ``invoke`` methods (HTTP clients, SDKs) out.
    """
    module = type(obj).__module__.lower()
    if not module.startswith(("langgraph", "langchain")):
        return False
    return callable(getattr(obj, "invoke", None)) and callable(
        getattr(obj, "ainvoke", None)
    )


def registry_from_object(obj: Any, name: str = "graph") -> Rewind:
    """Build a one-agent :class:`Rewind` registry around ``obj``.

    Accepts a langgraph/langchain runnable or a plain callable. Raises
    :class:`GraphAppError` with an actionable message otherwise.
    """
    if is_langchain_runnable(obj):
        return _registry_from_graph(obj, name=name)
    if callable(obj):
        return _registry_from_callable(obj, name=name)
    raise GraphAppError(
        f"{name!r} is {type(obj).__name__}; expected a rewind.Rewind instance, "
        "a LangGraph graph / langchain runnable, or a callable"
    )


def _registry_from_graph(graph: Any, *, name: str) -> Rewind:
    registry = Rewind(title=name)

    async def run(query: str, config: dict[str, Any] | None = None) -> Any:
        return await graph.ainvoke(
            {"messages": [{"role": "user", "content": query}]}, config or None
        )

    registry.agent(
        name=name,
        framework="langgraph",
        target=graph,
        description=(
            f"LangGraph graph {type(graph).__name__} — type your query and "
            "Rewind builds the graph input."
        ),
    )(run)
    return registry


def _registry_from_callable(func: Any, *, name: str) -> Rewind:
    if any(
        param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
        for param in inspect.signature(func).parameters.values()
    ):
        raise GraphAppError(
            f"{name!r} uses *args or **kwargs; rewrite it with named parameters "
            "or wrap it in a function with an explicit signature"
        )
    registry = Rewind(title=name)
    registry.agent(name=name)(func)
    return registry
