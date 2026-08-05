"""Unit tests for Phase 4 — executable regression cases + suite runner.

Covers:

* ``run_frozen_verification`` — the deterministic frozen-replay core.
* Storage CRUD for ``regression_cases`` + ``regression_runs``.
* The regression-case + run HTTP endpoints in :mod:`rewind.eval_api`.
* :class:`rewind.suite_runner.SuiteRunner` — concurrent execution + progress.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rewind.enums import SpanKind, SpanStatus
from rewind.eval_api import mount_eval
from rewind.evaluate import run_frozen_verification
from rewind.models import Span, Trace, hash_payload
from rewind.storage import TraceStore
from rewind.suite_runner import SuiteRunner

_TRACE_ID = "b" * 32


def _llm_span(span_id: str, content: str = "hello world") -> Span:
    msgs = [{"role": "user", "content": "hi"}]
    return Span(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        name="chat.completions.create",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name="qwen3:32b",
        prompt_tokens=5,
        completion_tokens=3,
        total_tokens=8,
        messages_hash=hash_payload(msgs),
        raw_attributes={
            "gen_ai.request.model": "qwen3:32b",
            "gen_ai.response": {
                "choices": [{"message": {"role": "assistant", "content": content}}]
            },
        },
    )


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    s = TraceStore(str(tmp_path / "regression.db"))
    spans = [_llm_span("a" * 16, "the answer is 42")]
    s.upsert_trace(Trace(trace_id=_TRACE_ID, spans=spans))
    for sp in spans:
        s.insert_span(sp)
    return s


@pytest.fixture
def app(store: TraceStore) -> FastAPI:
    a = FastAPI()
    a.state.store = store
    mount_eval(a)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# --- storage CRUD ----------------------------------------------------------


class TestRegressionStorage:
    def test_upsert_and_get(self, store: TraceStore) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "smoke",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 1},
            }
        )
        case = store.get_regression_case(cid)
        assert case is not None
        assert case["name"] == "smoke"
        assert case["expected"] == {"span_count": 1}

    def test_get_missing_returns_none(self, store: TraceStore) -> None:
        assert store.get_regression_case("no-such") is None

    def test_list(self, store: TraceStore) -> None:
        for i in range(3):
            store.upsert_regression_case(
                {
                    "case_id": f"case-{i}",
                    "name": f"n{i}",
                    "seed_trace_id": _TRACE_ID,
                }
            )
        cases = store.list_regression_cases()
        assert len(cases) == 3

    def test_delete_cascades_runs(self, store: TraceStore) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {"case_id": cid, "name": "x", "seed_trace_id": _TRACE_ID}
        )
        store.insert_regression_run(
            {
                "run_id": str(uuid4()),
                "case_id": cid,
                "passed": True,
                "detail": "ok",
            }
        )
        assert store.delete_regression_case(cid) is True
        assert store.get_regression_case(cid) is None
        assert store.list_regression_runs(cid) == []


# --- run_frozen_verification ----------------------------------------------


class TestRunFrozenVerification:
    def test_passing_case(self, store: TraceStore) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "span-count-ok",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 1},
            }
        )
        result = asyncio.run(run_frozen_verification(cid, store=store))
        assert result.passed is True
        assert "all checks passed" in result.detail
        # Run persisted.
        runs = store.list_regression_runs(cid)
        assert len(runs) == 1
        assert runs[0]["passed"] is True

    def test_failing_span_count(self, store: TraceStore) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "span-count-bad",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 99},
            }
        )
        result = asyncio.run(run_frozen_verification(cid, store=store))
        assert result.passed is False
        assert "span_count drift" in result.detail

    def test_required_text_found(self, store: TraceStore) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "text-found",
                "seed_trace_id": _TRACE_ID,
                "expected": {"required_text": ["answer"]},
            }
        )
        result = asyncio.run(run_frozen_verification(cid, store=store))
        assert result.passed is True

    def test_required_text_missing(self, store: TraceStore) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "text-missing",
                "seed_trace_id": _TRACE_ID,
                "expected": {"required_text": ["nonexistent"]},
            }
        )
        result = asyncio.run(run_frozen_verification(cid, store=store))
        assert result.passed is False
        assert "missing required text" in result.detail

    def test_missing_case(self, store: TraceStore) -> None:
        result = asyncio.run(run_frozen_verification("no-such", store=store))
        assert result.passed is False
        assert "not found" in result.detail


# --- HTTP endpoints --------------------------------------------------------


class TestRegressionEndpoints:
    def test_create_then_get(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/regression-cases",
            json={
                "case_id": "rc-1",
                "name": "smoke",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 1},
            },
        )
        assert resp.status_code == 201
        assert resp.json()["name"] == "smoke"

        got = client.get("/api/v1/regression-cases/rc-1")
        assert got.status_code == 200
        assert got.json()["expected"] == {"span_count": 1}

    def test_create_missing_trace_404(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/regression-cases",
            json={"case_id": "rc-x", "name": "x", "seed_trace_id": "z" * 32},
        )
        assert resp.status_code == 404

    def test_list(self, client: TestClient) -> None:
        client.post(
            "/api/v1/regression-cases",
            json={"case_id": "rc-a", "name": "a", "seed_trace_id": _TRACE_ID},
        )
        client.post(
            "/api/v1/regression-cases",
            json={"case_id": "rc-b", "name": "b", "seed_trace_id": _TRACE_ID},
        )
        items = client.get("/api/v1/regression-cases").json()
        assert len(items) == 2

    def test_delete(self, client: TestClient) -> None:
        client.post(
            "/api/v1/regression-cases",
            json={"case_id": "rc-del", "name": "d", "seed_trace_id": _TRACE_ID},
        )
        resp = client.delete("/api/v1/regression-cases/rc-del")
        assert resp.status_code == 200
        assert client.get("/api/v1/regression-cases/rc-del").status_code == 404

    def test_run_case(self, client: TestClient) -> None:
        client.post(
            "/api/v1/regression-cases",
            json={
                "case_id": "rc-run",
                "name": "runnable",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 1},
            },
        )
        resp = client.post(
            "/api/v1/regression-cases/rc-run/run",
            json={},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["passed"] is True
        assert body["case_id"] == "rc-run"

    def test_list_runs(self, client: TestClient) -> None:
        client.post(
            "/api/v1/regression-cases",
            json={
                "case_id": "rc-runs",
                "name": "r",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 1},
            },
        )
        client.post("/api/v1/regression-cases/rc-runs/run", json={})
        client.post("/api/v1/regression-cases/rc-runs/run", json={})
        runs = client.get("/api/v1/regression-cases/rc-runs/runs").json()
        assert len(runs) == 2
        assert all(r["passed"] for r in runs)


# --- suite runner ----------------------------------------------------------


class TestSuiteRunner:
    def test_runs_all_and_summarizes(self, store: TraceStore) -> None:
        ids = []
        for i in range(3):
            cid = f"suite-{i}"
            store.upsert_regression_case(
                {
                    "case_id": cid,
                    "name": f"s{i}",
                    "seed_trace_id": _TRACE_ID,
                    "expected": {"span_count": 1},
                }
            )
            ids.append(cid)

        runner = SuiteRunner(store, case_ids=ids, concurrency=2)

        async def _drive() -> list[dict]:
            events = []
            async for event in runner.run():
                events.append(event)
            return events

        events = asyncio.run(_drive())
        types = [e["type"] for e in events]
        assert types[0] == "suite_started"
        assert types[-1] == "suite_finished"
        case_dones = [e for e in events if e["type"] == "case_done"]
        assert len(case_dones) == 3
        assert all(e["passed"] for e in case_dones)
        assert runner.summary["passed"] == 3
        assert runner.summary["failed"] == 0

    def test_mixed_pass_fail(self, store: TraceStore) -> None:
        store.upsert_regression_case(
            {
                "case_id": "passing",
                "name": "p",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 1},
            }
        )
        store.upsert_regression_case(
            {
                "case_id": "failing",
                "name": "f",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 99},
            }
        )
        runner = SuiteRunner(store, case_ids=["passing", "failing"])

        async def _drive() -> bool:
            async for event in runner.run():
                if event["type"] == "suite_finished":
                    return bool(event["passed"])
            return False

        passed = asyncio.run(_drive())
        assert passed is False
        assert runner.summary["passed"] == 1
        assert runner.summary["failed"] == 1
