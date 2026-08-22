"""``agent-timetravel app:main`` shorthand and bare-graph workbench launch tests."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import uvicorn
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agent_timetravel.cli import cli
from agent_timetravel.graph_app import GraphAppError, is_langchain_runnable, registry_from_object

_HAS_LANGCHAIN = importlib.util.find_spec("langchain_core") is not None


class FakeGraph:
    """Duck-typed compiled graph living in a langgraph module path."""

    __module__ = "langgraph.graph.state"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def invoke(self, inputs: Any, config: Any = None) -> Any:
        return asyncio.run(self.ainvoke(inputs, config))

    async def ainvoke(self, inputs: Any, config: Any = None) -> Any:
        self.calls.append((inputs, config))
        return {"messages": ["done"]}


def test_is_langchain_runnable_matches_graph_module_only() -> None:
    assert is_langchain_runnable(FakeGraph())

    class Foreign:
        def invoke(self, *_: Any) -> None:
            ...

        async def ainvoke(self, *_: Any) -> None:
            ...

    assert not is_langchain_runnable(Foreign())
    assert not is_langchain_runnable(lambda: None)


def test_registry_from_graph_builds_langgraph_agent() -> None:
    graph = FakeGraph()
    registry = registry_from_object(graph, name="main")

    definition = registry.get("main")
    assert definition is not None
    assert definition.framework == "langgraph"
    schema = definition.input_schema
    # The dialog shows a plain query string — no JSON typing for the user.
    assert schema["properties"]["query"]["type"] == "string"
    assert set(schema["required"]) == {"query"}
    if _HAS_LANGCHAIN:
        assert definition.available

    async def scenario() -> Any:
        return await definition.compile_runner(
            {"query": "Compare RLHF vs DPO.", "config": {"recursion_limit": 5}}
        )(SimpleNamespace())

    result = asyncio.run(scenario())
    assert result == {"messages": ["done"]}
    # The wrapper builds the chat-state input from the query text.
    assert graph.calls == [
        (
            {"messages": [{"role": "user", "content": "Compare RLHF vs DPO."}]},
            {"recursion_limit": 5},
        )
    ]


def test_registry_from_callable_uses_auto_detection() -> None:
    registry = registry_from_object(lambda prompt: prompt, name="main")
    definition = registry.get("main")
    assert definition is not None
    assert definition.input_schema["required"] == ["prompt"]


def test_registry_from_object_rejects_unsupported_targets() -> None:
    with pytest.raises(GraphAppError, match=r"expected a timetravel\.TimeTravel instance"):
        registry_from_object(42, name="main")

    def varargs(*args: Any) -> Any:
        return args

    with pytest.raises(GraphAppError, match=r"\*args or \*\*kwargs"):
        registry_from_object(varargs, name="main")


def test_shorthand_launches_graph_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = ModuleType("timetravel_graph_test_app")
    graph = FakeGraph()
    module.main = graph
    monkeypatch.setitem(sys.modules, module.__name__, module)

    served: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        served["app"] = app

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(
        cli,
        [
            f"{module.__name__}:main",
            "--db",
            str(tmp_path / "shorthand.db"),
            "--no-open",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "(1 agents," in result.output
    listing = TestClient(served["app"]).get("/api/v1/agents").json()
    assert listing["items"][0]["name"] == "main"
    assert listing["items"][0]["framework"] == "langgraph"
    if _HAS_LANGCHAIN:
        assert listing["items"][0]["available"] is True


def test_dev_rejects_unsupported_object_with_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = ModuleType("timetravel_graph_bad_app")
    module.main = 42
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = CliRunner().invoke(
        cli,
        ["dev", f"{module.__name__}:main", "--db", str(tmp_path / "bad.db"), "--no-open"],
    )

    assert result.exit_code != 0
    assert "expected a timetravel.TimeTravel instance" in result.output
