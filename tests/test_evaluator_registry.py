"""Unit tests for Phase 3.4 — registered custom evaluators + ``POST /evaluate``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rewind.enums import SpanKind, SpanStatus
from rewind.models import Span, Trace
from rewind.stepping_api import (
    EvaluatorResult,
    mount_stepping,
    register_evaluator,
)
from rewind.storage import TraceStore

_TRACE_ID = "e" * 32


def _root_span() -> Span:
    return Span(
        trace_id=_TRACE_ID,
        span_id="1" * 16,
        parent_span_id=None,
        name="agent.root",
        kind=SpanKind.AGENT,
        status=SpanStatus.UNSET,
        status_message=None,
        model_name=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        raw_attributes={},
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    store = TraceStore(str(tmp_path / "eval_registry.db"))
    trace = Trace(trace_id=_TRACE_ID, spans=[_root_span()])
    store.upsert_trace(trace)
    for span in trace.spans:
        store.insert_span(span, branch_id=trace.root_branch_id)

    app = FastAPI()
    app.state.store = store
    mount_stepping(app)
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    """Snapshot + restore the evaluator registry per test."""
    from rewind import stepping_api

    saved = dict(stepping_api._EVALUATORS)
    stepping_api._EVALUATORS.clear()
    yield
    stepping_api._EVALUATORS.clear()
    stepping_api._EVALUATORS.update(saved)


# --- registry --------------------------------------------------------------


class TestRegistry:
    def test_register_and_resolve(self) -> None:
        async def cites_source(result: str, context: dict[str, Any]) -> EvaluatorResult:
            return EvaluatorResult(passed="source" in result.lower())

        register_evaluator("cites_source", cites_source)
        from rewind.stepping_api import get_evaluator

        assert get_evaluator("cites_source") is cites_source

    def test_empty_name_rejected(self) -> None:
        async def fn(result: str, context: dict[str, Any]) -> EvaluatorResult:
            return EvaluatorResult(passed=True)

        with pytest.raises(ValueError, match="non-empty"):
            register_evaluator("", fn)


# --- POST /api/v1/evaluate -------------------------------------------------


class TestEvaluateEndpoint:
    def test_lists_registered_names_without_exposing_callables(self, client: TestClient) -> None:
        async def cites_source(result: str, context: dict[str, Any]) -> EvaluatorResult:
            return EvaluatorResult(passed="source" in result)

        register_evaluator("cites_source", cites_source)
        response = client.get("/api/v1/evaluators")
        assert response.status_code == 200
        assert response.json() == ["cites_source"]

    def test_passing_evaluator(self, client: TestClient) -> None:
        async def has_citation(result: str, context: dict[str, Any]) -> EvaluatorResult:
            ok = "[1]" in result or "source" in result.lower()
            return EvaluatorResult(passed=ok, detail="citation check")

        register_evaluator("has_citation", has_citation)
        resp = client.post(
            "/api/v1/evaluate",
            json={"name": "has_citation", "result": "Paris is 18C [1]"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "has_citation"
        assert body["passed"] is True
        assert body["detail"] == "citation check"

    def test_failing_evaluator(self, client: TestClient) -> None:
        async def has_citation(result: str, context: dict[str, Any]) -> EvaluatorResult:
            return EvaluatorResult(passed="[1]" in result)

        register_evaluator("has_citation", has_citation)
        resp = client.post(
            "/api/v1/evaluate",
            json={"name": "has_citation", "result": "no citation here"},
        )
        assert resp.status_code == 200
        assert resp.json()["passed"] is False

    def test_unknown_evaluator_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/evaluate",
            json={"name": "no-such", "result": "x"},
        )
        assert resp.status_code == 404
        assert "no-such" in resp.json()["detail"]

    def test_evaluator_exception_returns_fail_not_500(
        self, client: TestClient
    ) -> None:
        async def boom(result: str, context: dict[str, Any]) -> EvaluatorResult:
            raise RuntimeError("evaluator blew up")

        register_evaluator("boom", boom)
        resp = client.post(
            "/api/v1/evaluate",
            json={"name": "boom", "result": "x"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["passed"] is False
        assert "evaluator blew up" in body["detail"]

    def test_context_forwarded(self, client: TestClient) -> None:
        async def check_min_length(
            result: str, context: dict[str, Any]
        ) -> EvaluatorResult:
            minimum = int(context.get("min", 0))
            return EvaluatorResult(passed=len(result) >= minimum)

        register_evaluator("min_len", check_min_length)
        resp = client.post(
            "/api/v1/evaluate",
            json={"name": "min_len", "result": "hi", "context": {"min": 5}},
        )
        assert resp.json()["passed"] is False
        resp2 = client.post(
            "/api/v1/evaluate",
            json={"name": "min_len", "result": "hello world", "context": {"min": 5}},
        )
        assert resp2.json()["passed"] is True
