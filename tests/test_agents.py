"""Decorator-first agent and workbench API coverage."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Annotated

import pytest
import uvicorn
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretBytes, SecretStr

from rewind import Rewind, RewindContext, rewind
from rewind.cli import cli
from rewind.receiver import create_app
from rewind.stepping_api import _SESSIONS
from rewind.storage import TraceStore

_HAS_LANGCHAIN = importlib.util.find_spec("langchain_core") is not None


def test_public_rewind_registry_supports_decorator_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rewind, "_agents", {})

    assert isinstance(rewind, Rewind)

    @rewind.agent(framework="openai")
    def public_api_agent(question: str) -> str:
        return question

    assert rewind.get("public_api_agent") is not None


def test_decorator_is_direct_pass_through_and_excludes_context() -> None:
    debugger = Rewind()
    seen: list[bool] = []

    @debugger.agent(framework="openai")
    def answer(question: str, context: RewindContext | None = None) -> str:
        seen.append(context is not None)
        return question.upper()

    assert answer("hello") == "HELLO"
    definition = debugger.get("answer")
    assert definition is not None
    assert "context" not in definition.input_schema["properties"]
    assert definition.input_schema["properties"]["question"]["type"] == "string"
    assert seen == [False]


def test_duplicate_names_are_rejected() -> None:
    debugger = Rewind()

    @debugger.agent("same", framework="openai")
    def first() -> None:
        return None

    assert first() is None

    with pytest.raises(ValueError, match="duplicate"):
        debugger.agent("same", framework="openai")(lambda: None)


def test_fresh_agent_session_validates_injects_and_persists_result(tmp_path: Path) -> None:
    debugger = Rewind()
    seen: list[tuple[str, bool]] = []

    @debugger.agent(framework="openai", description="greet", tags=("test",))
    async def greet(
        name: str, token: SecretStr, context: RewindContext | None = None
    ) -> dict[str, str]:
        seen.append((token.get_secret_value(), context is not None))
        return {"greeting": f"Hello {name}"}

    client = TestClient(create_app(TraceStore(str(tmp_path / "agent.db")), debugger))
    listing = client.get("/api/v1/agents").json()
    assert listing["total"] == 1
    assert listing["items"][0]["tags"] == ["test"]
    assert "context" not in listing["items"][0]["input_schema"]["properties"]

    response = client.post(
        "/api/v1/agents/greet/sessions",
        json={"inputs": {"name": "Ada", "token": "secret"}},
    )
    assert response.status_code == 201
    session = client.get(f"/api/v1/sessions/{response.json()['session_id']}").json()
    assert session["status"] == "done"
    assert session["input_payload"]["token"] == "*" * 10
    assert session["result_payload"] == {"greeting": "Hello Ada"}
    assert seen == [("secret", True)]


def test_fresh_agent_root_branch_and_restart_child_are_api_visible(tmp_path: Path) -> None:
    debugger = Rewind()

    @debugger.agent(framework="openai")
    def answer(value: str) -> str:
        return value

    store = TraceStore(str(tmp_path / "branches.db"))
    client = TestClient(create_app(store, debugger))
    started = client.post(
        "/api/v1/agents/answer/sessions",
        json={"inputs": {"value": "first"}},
    )
    assert started.status_code == 201
    start_body = started.json()
    trace_id = start_body["trace_id"]
    root_branch_id = start_body["branch_id"]

    branches = store.list_branches(trace_id)
    assert len(branches) == 1
    assert str(branches[0].branch_id) == root_branch_id
    assert branches[0].parent_branch_id is None

    tree = client.get(f"/api/v1/traces/{trace_id}/branches")
    assert tree.status_code == 200
    assert tree.json()["branch_id"] == root_branch_id

    restarted = client.post(
        f"/api/v1/sessions/{start_body['session_id']}/restart-from",
        json={"branch_at": 0, "inputs": {"value": "second"}},
    )
    assert restarted.status_code == 201
    child_branch_id = restarted.json()["branch_id"]
    assert child_branch_id != root_branch_id

    tree = client.get(f"/api/v1/traces/{trace_id}/branches")
    assert tree.status_code == 200
    assert tree.json()["children"][0]["branch_id"] == child_branch_id

    diff = client.get(
        f"/api/v1/traces/{trace_id}/diff",
        params={"left": root_branch_id, "right": child_branch_id},
    )
    assert diff.status_code == 200
    assert diff.json()["identical"] is True


def test_unavailable_agent_start_includes_availability_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    debugger = Rewind()

    @debugger.agent(framework="langgraph")
    def unavailable(value: str) -> str:
        return value

    definition = debugger.get("unavailable")
    assert definition is not None
    monkeypatch.setattr("rewind.agents.FrameworkPlugin.available", lambda _plugin: False)
    assert not definition.available
    assert definition.availability_reason

    client = TestClient(create_app(TraceStore(str(tmp_path / "unavailable.db")), debugger))
    response = client.post(
        "/api/v1/agents/unavailable/sessions",
        json={"inputs": {"value": "ignored"}},
    )
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["availability_reason"] == definition.availability_reason


def test_restart_rejects_agent_that_became_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    debugger = Rewind()
    calls: list[str] = []

    @debugger.agent(framework="openai")
    def answer(value: str) -> str:
        calls.append(value)
        return value

    client = TestClient(create_app(TraceStore(str(tmp_path / "restart-unavailable.db")), debugger))
    started = client.post(
        "/api/v1/agents/answer/sessions",
        json={"inputs": {"value": "first"}},
    )
    assert started.status_code == 201
    assert calls == ["first"]

    monkeypatch.setattr("rewind.agents.FrameworkPlugin.available", lambda _plugin: False)
    definition = debugger.get("answer")
    assert definition is not None
    assert not definition.available
    reason = definition.availability_reason
    assert reason

    restarted = client.post(
        f"/api/v1/sessions/{started.json()['session_id']}/restart-from",
        json={"branch_at": 0},
    )
    assert restarted.status_code == 409
    assert restarted.json()["detail"] == {
        "message": "agent 'answer' is unavailable",
        "availability_reason": reason,
    }
    assert calls == ["first"]


@pytest.mark.skipif(not _HAS_LANGCHAIN, reason="langchain-core not installed")
def test_sync_agent_uses_to_thread_without_revalidating(monkeypatch: pytest.MonkeyPatch) -> None:
    debugger = Rewind()
    calls: list[str] = []

    @debugger.agent(framework="langgraph")
    def sync_agent(value: int) -> int:
        calls.append("function")
        return value + 1

    definition = debugger.get("sync_agent")
    assert definition is not None
    validated = definition.validate_inputs({"value": 4})
    to_thread_calls: list[str] = []
    original_to_thread = asyncio.to_thread

    async def spy_to_thread(function: object, *args: object, **kwargs: object) -> object:
        to_thread_calls.append(getattr(function, "__name__", "unknown"))
        return await original_to_thread(function, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("rewind.agents.asyncio.to_thread", spy_to_thread)

    class Session:
        trace_id = "a" * 32
        branch_id = "b" * 36

    result = asyncio.run(definition.compile_runner(validated)(Session()))
    assert result == 5
    assert to_thread_calls == ["sync_agent"]
    assert calls == ["function"]


class _StructuredResult(BaseModel):
    answer: str
    secret: SecretStr


def test_structured_result_is_json_safe_and_redacted(tmp_path: Path) -> None:
    debugger = Rewind()

    @debugger.agent(framework="openai")
    def structured() -> _StructuredResult:
        return _StructuredResult(answer="ok", secret="s" * 3)

    client = TestClient(create_app(TraceStore(str(tmp_path / "structured.db")), debugger))
    response = client.post("/api/v1/agents/structured/sessions", json={"inputs": {}})
    assert response.status_code == 201
    session = client.get(f"/api/v1/sessions/{response.json()['session_id']}").json()
    assert session["result_payload"] == {"answer": "ok", "secret": "*" * 10}


def test_stream_recovers_terminal_event_after_fast_agent_is_removed(tmp_path: Path) -> None:
    debugger = Rewind()

    @debugger.agent(framework="openai")
    def immediate() -> dict[str, str]:
        return {"status": "finished"}

    client = TestClient(create_app(TraceStore(str(tmp_path / "stream.db")), debugger))
    started = client.post("/api/v1/agents/immediate/sessions", json={"inputs": {}})
    assert started.status_code == 201
    session_id = started.json()["session_id"]
    assert _SESSIONS.get(session_id) is None

    stream = client.get(f"/api/v1/sessions/{session_id}/stream")
    assert stream.status_code == 200
    assert stream.text.startswith("data: ")
    event = json.loads(stream.text.removeprefix("data: ").strip())
    assert event["type"] == "done"
    assert event["result_payload"] == {"status": "finished"}


def test_invalid_agent_inputs_are_rejected(tmp_path: Path) -> None:
    debugger = Rewind()

    @debugger.agent(framework="openai")
    def answer(question: str) -> str:
        return question

    client = TestClient(create_app(TraceStore(str(tmp_path / "invalid.db")), debugger))
    response = client.post("/api/v1/agents/answer/sessions", json={"inputs": {}})
    assert response.status_code == 422


def test_auto_detection_prefers_explicit_target_and_advertises_capabilities() -> None:
    class GraphTarget:
        __module__ = "langgraph.graph"

    debugger = Rewind()

    @debugger.agent(target=GraphTarget)
    def run(prompt: str) -> str:
        return prompt

    definition = debugger.get("run")
    assert definition is not None
    assert definition.framework == "langgraph"
    assert definition.capabilities["interactive_llm"] is True
    assert definition.capabilities["native_tool_calls"] is True
    if _HAS_LANGCHAIN:
        assert definition.available
        assert definition.availability_reason is None
    else:
        assert definition.availability_reason is not None


def test_dev_reports_actionable_import_errors() -> None:
    result = CliRunner().invoke(cli, ["dev", "missing_module:debugger"])
    assert result.exit_code != 0
    assert "could not import" in result.output


def test_dev_accepts_rewind_registry_with_registered_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(rewind, "_agents", {})
    module = ModuleType("rewind_cli_test_app")
    exec(  # noqa: S102 - controlled temporary module fixture
        '''
from rewind import rewind

@rewind.agent(framework="openai")
def answer(question: str) -> str:
    return question
''',
        module.__dict__,
    )
    monkeypatch.setitem(sys.modules, module.__name__, module)
    served: dict[str, object] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        served["app"] = app
        served.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(
        cli,
        [
            "dev",
            f"{module.__name__}:rewind",
            "--db",
            str(tmp_path / "dev.db"),
            "--no-open",
        ],
    )

    assert result.exit_code == 0
    assert "(1 agents," in result.output
    assert served["app"] is not None
    assert served["host"] == "127.0.0.1"
    assert served["port"] == 8484
    assert served["log_level"] == "warning"
    listing = TestClient(served["app"]).get("/api/v1/agents").json()
    assert listing["items"][0]["name"] == "answer"


def test_secret_agent_restart_requires_override_and_masks_new_inputs(tmp_path: Path) -> None:
    debugger = Rewind()
    calls: list[str] = []

    @debugger.agent(framework="openai")
    def secret_agent(token: SecretStr) -> str:
        calls.append(token.get_secret_value())
        return "done"

    client = TestClient(create_app(TraceStore(str(tmp_path / "restart.db")), debugger))
    started = client.post(
        "/api/v1/agents/secret_agent/sessions",
        json={"inputs": {"token": "first"}},
    )
    assert started.status_code == 201
    session_id = started.json()["session_id"]

    rejected = client.post(
        f"/api/v1/sessions/{session_id}/restart-from",
        json={"branch_at": 0},
    )
    assert rejected.status_code == 400
    assert "inputs override" in rejected.text

    restarted = client.post(
        f"/api/v1/sessions/{session_id}/restart-from",
        json={"branch_at": 0, "inputs": {"token": "second"}},
    )
    assert restarted.status_code == 201
    detail = client.get(f"/api/v1/sessions/{restarted.json()['session_id']}").json()
    assert detail["input_payload"]["token"] == "*" * 10
    assert calls == ["first", "second"]


class _NestedSecretBytes(BaseModel):
    token: Annotated[SecretBytes | None, "sensitive credential"]


def test_secret_bytes_nested_restart_requires_override_and_masks_inputs(
    tmp_path: Path,
) -> None:
    debugger = Rewind()
    calls: list[bytes] = []

    @debugger.agent(framework="openai")
    def secret_bytes_agent(credentials: _NestedSecretBytes | None = None) -> str:
        assert credentials is not None
        parsed = _NestedSecretBytes.model_validate(credentials)
        assert parsed.token is not None
        calls.append(parsed.token.get_secret_value())
        return "done"

    client = TestClient(create_app(TraceStore(str(tmp_path / "secret-bytes.db")), debugger))
    started = client.post(
        "/api/v1/agents/secret_bytes_agent/sessions",
        json={"inputs": {"credentials": {"token": "first"}}},
    )
    assert started.status_code == 201
    session_id = started.json()["session_id"]

    rejected = client.post(
        f"/api/v1/sessions/{session_id}/restart-from",
        json={"branch_at": 0},
    )
    assert rejected.status_code == 400
    assert "credentials" in rejected.text

    restarted = client.post(
        f"/api/v1/sessions/{session_id}/restart-from",
        json={
            "branch_at": 0,
            "inputs": {"credentials": {"token": "second"}},
        },
    )
    assert restarted.status_code == 201
    detail = client.get(f"/api/v1/sessions/{restarted.json()['session_id']}").json()
    assert detail["input_payload"]["credentials"]["token"] == "*" * 10
    assert calls == [b"first", b"second"]
