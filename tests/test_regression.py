"""Unit tests for Phase 4 — executable regression cases + suite runner.

Covers:

* ``run_frozen_verification`` — the deterministic frozen-replay core.
* Storage CRUD for ``regression_cases`` + ``regression_runs``.
* The regression-case + run HTTP endpoints in :mod:`timetravel.eval_api`.
* :class:`timetravel.suite_runner.SuiteRunner` — concurrent execution + progress.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_timetravel.enums import SpanKind, SpanStatus
from agent_timetravel.eval_api import mount_eval
from agent_timetravel.evaluate import run_frozen_verification
from agent_timetravel.models import Span, Trace, hash_payload
from agent_timetravel.storage import TraceStore
from agent_timetravel.suite_runner import SuiteRunner

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

    def test_factory_exception_detail_is_generic(self, store: TraceStore) -> None:
        canary = "factory-secret-should-not-leak"
        cid = "direct-factory-error"
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "factory error",
                "seed_trace_id": _TRACE_ID,
            }
        )

        def raising_factory(_store: TraceStore, _scenario: object) -> tuple[list, UUID]:
            raise RuntimeError(canary)

        result = asyncio.run(
            run_frozen_verification(cid, store=store, factory=raising_factory)
        )

        assert canary not in result.detail
        assert result.detail == "error: regression case could not be executed"
        assert result.passed is False

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

    def test_frozen_checks_run_against_captured_output(self, store: TraceStore) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "captured-checks",
                "seed_trace_id": _TRACE_ID,
                "expected": {
                    # Captured interactive history can include a branch tail;
                    # this must not be compared with the root replay count.
                    "captured_step_count": 2,
                    "checks": [
                        {
                            "cursor": 0,
                            "result": "the answer is 42",
                            "usage": {"total_tokens": 8},
                            "assertions": {
                                "requiredText": ["answer"],
                                "forbiddenText": ["password"],
                                "maxTokens": 10,
                            },
                        }
                    ]
                },
            }
        )
        result = asyncio.run(run_frozen_verification(cid, store=store))
        assert result.passed is True

    def test_captured_interactive_case_does_not_require_root_span_count(
        self,
        store: TraceStore,
    ) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "captured-no-root-span-count",
                "seed_trace_id": _TRACE_ID,
                "expected": {
                    "captured_step_count": 3,
                    "captured_steps": [
                        {
                            "cursor": 0,
                            "result": "approved answer",
                            "usage": {"total_tokens": 8},
                            "assertions": {"requiredText": ["approved"]},
                        }
                    ],
                },
            }
        )
        result = asyncio.run(run_frozen_verification(cid, store=store))
        assert result.passed is True

    def test_captured_cost_assertion_uses_saved_pricing(self, store: TraceStore) -> None:
        cid = str(uuid4())
        store.upsert_regression_case(
            {
                "case_id": cid,
                "name": "captured-cost-budget",
                "seed_trace_id": _TRACE_ID,
                "expected": {
                    "pricing": {
                        "inputPerMillion": 0,
                        "cachedInputPerMillion": 0,
                        "outputPerMillion": 10,
                        "thinkingPerMillion": 0,
                    },
                    "captured_steps": [
                        {
                            "cursor": 0,
                            "result": "expensive answer",
                            "usage": {
                                "input_tokens": 0,
                                "final_tokens": 1_000,
                                "thinking_tokens": 0,
                                "total_tokens": 1_000,
                            },
                            "assertions": {"maxCostUsd": 0.001},
                        }
                    ],
                },
            }
        )
        result = asyncio.run(run_frozen_verification(cid, store=store))
        assert result.passed is False
        assert "cost budget exceeded" in result.detail

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

    def test_run_case_resolves_registered_factory(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_timetravel import eval_api

        factory_ref = "test-regression-factory"

        def factory(store: TraceStore, scenario: object) -> tuple[list[Span], UUID]:
            del store, scenario
            return [], UUID(int=0)

        monkeypatch.setitem(eval_api._REGRESSION_FACTORIES, factory_ref, factory)
        client.post(
            "/api/v1/regression-cases",
            json={
                "case_id": "rc-custom-factory",
                "name": "custom",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 0},
            },
        )

        response = client.post(
            "/api/v1/regression-cases/rc-custom-factory/run",
            json={"factory_ref": factory_ref},
        )

        assert response.status_code == 200
        assert response.json()["passed"] is True
        assert response.json()["branch_id"] == str(UUID(int=0))

    def test_run_case_factory_error_persists_generic_failure(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_timetravel import eval_api

        canary = "factory-secret-should-not-leak-http"
        factory_ref = "test-regression-raising-factory"

        def raising_factory(_store: TraceStore, _scenario: object) -> tuple[list, UUID]:
            raise RuntimeError(canary)

        monkeypatch.setitem(eval_api._REGRESSION_FACTORIES, factory_ref, raising_factory)
        client.post(
            "/api/v1/regression-cases",
            json={
                "case_id": "rc-factory-error",
                "name": "factory error",
                "seed_trace_id": _TRACE_ID,
            },
        )

        response = client.post(
            "/api/v1/regression-cases/rc-factory-error/run",
            json={"factory_ref": factory_ref},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["passed"] is False
        assert body["branch_id"] is None
        assert canary not in response.text
        runs = client.get("/api/v1/regression-cases/rc-factory-error/runs").json()
        assert len(runs) == 1
        assert runs[0]["detail"] == "error: regression case could not be executed"

    def test_run_case_rejects_unknown_factory(self, client: TestClient) -> None:
        client.post(
            "/api/v1/regression-cases",
            json={
                "case_id": "rc-unknown-factory",
                "name": "unknown",
                "seed_trace_id": _TRACE_ID,
            },
        )

        response = client.post(
            "/api/v1/regression-cases/rc-unknown-factory/run",
            json={"factory_ref": "does-not-exist"},
        )

        assert response.status_code == 404
        assert "factory" in response.json()["detail"]

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

    def test_suite_stream_emits_progress_events(self, client: TestClient) -> None:
        client.post(
            "/api/v1/regression-cases",
            json={
                "case_id": "rc-suite-stream",
                "name": "streamed",
                "seed_trace_id": _TRACE_ID,
                "expected": {"span_count": 1},
            },
        )
        response = client.post(
            "/api/v1/regression-suites/stream",
            json=["rc-suite-stream"],
        )

        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: ").strip())
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert [event["type"] for event in events] == [
            "suite_started",
            "case_done",
            "suite_finished",
        ]
        assert events[1]["passed"] is True
        assert events[2]["summary"] == {
            "total": 1,
            "passed": 1,
            "failed": 0,
            "errored": 0,
        }

    def test_suite_stream_rejects_empty_case_list(self, client: TestClient) -> None:
        response = client.post("/api/v1/regression-suites/stream", json=[])

        assert response.status_code == 400
        assert "at least one" in response.json()["detail"]


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

    def test_factory_exception_is_generic_in_progress_events(
        self,
        store: TraceStore,
    ) -> None:
        canary = "factory-secret-should-not-leak"
        case_id = "suite-factory-error"
        store.upsert_regression_case(
            {
                "case_id": case_id,
                "name": "factory error",
                "seed_trace_id": _TRACE_ID,
            }
        )

        def raising_factory(_store: TraceStore, _scenario: object) -> tuple[list, UUID]:
            raise RuntimeError(canary)

        runner = SuiteRunner(store, case_ids=[case_id], factory=raising_factory)

        async def _drive() -> list[dict]:
            events = []
            async for event in runner.run():
                events.append(event)
            return events

        events = asyncio.run(_drive())
        encoded_events = json.dumps(events)
        case_done = next(event for event in events if event["type"] == "case_done")

        assert canary not in encoded_events
        assert case_done["passed"] is False
        assert case_done["detail"] == "error: regression case could not be executed"
        assert runner.summary["failed"] == 1
        assert runner.summary["errored"] == 0

    def test_unexpected_task_exception_is_generic_and_errored(
        self,
        store: TraceStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from agent_timetravel import suite_runner

        canary = "unexpected-task-secret-should-not-leak"

        async def raising_run(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError(canary)

        monkeypatch.setattr(suite_runner, "run_frozen_verification", raising_run)
        runner = SuiteRunner(store, case_ids=["unexpected-task"])

        async def _drive() -> list[dict]:
            events = []
            async for event in runner.run():
                events.append(event)
            return events

        events = asyncio.run(_drive())
        encoded_events = json.dumps(events)
        case_done = next(event for event in events if event["type"] == "case_done")

        assert canary not in encoded_events
        assert case_done["detail"] == "error: regression case could not be executed"
        assert runner.summary["errored"] == 1
