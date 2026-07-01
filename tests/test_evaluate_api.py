"""Unit tests for ``rewind.eval_api`` — the Phase 5.5 HTTP surface.

Mirrors the pattern of ``tests/test_timeline.py``: real FastAPI app via
``TestClient``, real ``TraceStore`` at a temp path, and isolated fixture
traces per test method so each test sees a clean DB.

We exercise:
  * ``POST /api/v1/evals`` happy-path (PASS) and the validation paths
    (400 invalid YAML / 413 oversized).
  * ``GET /api/v1/evals`` paginated list.
  * ``GET /api/v1/evals/{run_id}`` happy-path, 400 bad-uuid, 404 missing.
  * ``GET /api/v1/evals/{run_id}/baseline?baseline_run_id=...`` happy-path
    (changed + unchanged scenarios) and the two 404 paths.
  * ``DELETE /api/v1/evals/{run_id}`` happy-path, 404 missing, cascade.
  * ``parse_suite_from_yaml`` unit-level loader edge cases (no shared HTTP).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rewind.enums import SpanKind
from rewind.eval_api import (
    EvalRunDetailView,
    EvalRunListResponse,
    EvalRunSummaryView,
    mount_eval,
    parse_suite_from_yaml,
)
from rewind.evaluate import SuiteValidationError
from rewind.models import Span, Trace
from rewind.storage import TraceStore

if TYPE_CHECKING:
    pass


# --- shared fixtures -------------------------------------------------------

_TRACE_ID = "a" * 32
_GOOD_SUITE_YAML = f"""
name: ok-suite
concurrency: 2
scenarios:
  - name: happy
    seed_trace_id: {_TRACE_ID}
    candidate_mode: frozen
    evaluators:
      - kind: token_budget
        expected:
          max_total_tokens: 1000
""".strip()


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    """A TraceStore with a 3-span seed trace already written."""
    s = TraceStore(tmp_path / "rewind.db")
    spans = [
        Span(
            trace_id=_TRACE_ID,
            span_id="1111111111111111",
            parent_span_id=None,
            name="adk.agent.Bot",
            kind=SpanKind.AGENT,
            start_time="2026-06-29T10:00:00+00:00",
            end_time="2026-06-29T10:00:05+00:00",
            raw_attributes={},
        ),
        Span(
            trace_id=_TRACE_ID,
            span_id="2222222222222222",
            parent_span_id=None,
            name="chat.completions",
            kind=SpanKind.LLM,
            model_name="qwen3:32b",
            prompt_tokens=42,
            completion_tokens=7,
            total_tokens=49,
            start_time="2026-06-29T10:00:01+00:00",
            end_time="2026-06-29T10:00:02+00:00",
            raw_attributes={
                "gen_ai.system": "openai",
                "gen_ai.usage.prompt_tokens": 42,
                "gen_ai.usage.completion_tokens": 7,
                "gen_ai.usage.total_tokens": 49,
                "gen_ai.response": {
                    "choices": [
                        {"message": {"role": "assistant", "content": "ok"}}
                    ]
                },
            },
        ),
        Span(
            trace_id=_TRACE_ID,
            span_id="3333333333333333",
            parent_span_id=None,
            name="search",
            kind=SpanKind.TOOL,
            start_time="2026-06-29T10:00:03+00:00",
            end_time="2026-06-29T10:00:04+00:00",
            raw_attributes={"tool.name": "search", "gen_ai.tool.result": "result 42"},
        ),
    ]
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


# --- POST /api/v1/evals ----------------------------------------------------


class TestCreateEvalRun:
    def test_creates_run_returns_201(self, client: TestClient) -> None:
        resp = client.post("/api/v1/evals", json={"suite_yaml": _GOOD_SUITE_YAML})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["run_id"]
        run = body["run"]
        assert run["suite_name"] == "ok-suite"
        assert run["overall_verdict"] == "pass"
        # All UUIDs / timestamps are strings, not nested objects.
        assert isinstance(run["run_id"], str)
        assert UUID(run["run_id"])
        assert isinstance(run["started_at"], str)
        # Scenario shape.
        assert len(run["scenarios"]) == 1
        sc = run["scenarios"][0]
        assert sc["name"] == "happy"
        assert sc["verdict"] == "pass"
        assert sc["rollup"]["total_tokens"] == 49
        assert sc["rollup"]["prompt_tokens"] == 42

    def test_suite_name_override(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/evals",
            json={"suite_yaml": _GOOD_SUITE_YAML, "suite_name": "renamed"},
        )
        assert resp.status_code == 201
        assert resp.json()["run"]["suite_name"] == "renamed"

    def test_invalid_yaml_returns_400(self, client: TestClient) -> None:
        bad = "name: x\nscenarios: []\n"  # empty scenarios
        resp = client.post("/api/v1/evals", json={"suite_yaml": bad})
        assert resp.status_code == 400
        assert "validation" in resp.json()["detail"].lower()

    def test_malformed_kind_returns_400(self, client: TestClient) -> None:
        bad = f"""
name: bad
scenarios:
  - name: x
    seed_trace_id: {_TRACE_ID}
    evaluators:
      - kind: not_a_kind
        expected: {{}}
""".strip()
        resp = client.post("/api/v1/evals", json={"suite_yaml": bad})
        assert resp.status_code == 400

    def test_too_large_suite_returns_413(self, client: TestClient) -> None:
        # Build a YAML doc > _MAX_SUITE_YAML_BYTES (256 KiB). Fill with a
        # long comment line, serialised several times.
        padding = "# " + ("x" * 1024) + "\n"
        bloated = "name: big\n" + padding * 300 + "scenarios: []\n"
        # Empty scenarios would normally 400, but the 413 check fires first.
        assert len(bloated.encode("utf-8")) > 256 * 1024
        resp = client.post("/api/v1/evals", json={"suite_yaml": bloated})
        assert resp.status_code == 413


# --- GET /api/v1/evals -----------------------------------------------------


class TestListEvalRuns:
    def test_empty_returns_zero_total(self, client: TestClient) -> None:
        resp = client.get("/api/v1/evals")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["limit"] == 50
        assert body["offset"] == 0

    def test_lists_one_run_after_create(self, client: TestClient) -> None:
        client.post("/api/v1/evals", json={"suite_yaml": _GOOD_SUITE_YAML})
        resp = client.get("/api/v1/evals")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["suite_name"] == "ok-suite"
        assert item["overall_verdict"] == "pass"

    def test_pagination_limit_enforced(self, client: TestClient) -> None:
        # Create 3 runs, ask limit=2 offset=1.
        for _ in range(3):
            client.post("/api/v1/evals", json={"suite_yaml": _GOOD_SUITE_YAML})
        resp = client.get("/api/v1/evals?limit=2&offset=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["limit"] == 2
        assert body["offset"] == 1

    def test_limit_below_one_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/evals?limit=0")
        assert resp.status_code == 422  # FastAPI validation error.

    def test_limit_above_max_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/evals?limit=10000")
        assert resp.status_code == 422


# --- GET /api/v1/evals/{run_id} -------------------------------------------


class TestGetEvalRun:
    def test_fetch_existing(self, client: TestClient) -> None:
        run_id = client.post(
            "/api/v1/evals", json={"suite_yaml": _GOOD_SUITE_YAML}
        ).json()["run_id"]
        resp = client.get(f"/api/v1/evals/{run_id}")
        assert resp.status_code == 200
        run = resp.json()
        assert run["run_id"] == run_id
        assert len(run["scenarios"]) == 1

    def test_bad_uuid_returns_400(self, client: TestClient) -> None:
        resp = client.get("/api/v1/evals/not-a-uuid")
        assert resp.status_code == 400

    def test_missing_returns_404(self, client: TestClient) -> None:
        # Valid UUID but not persisted.
        ghost = str(uuid4())
        resp = client.get(f"/api/v1/evals/{ghost}")
        assert resp.status_code == 404


# --- GET /api/v1/evals/{run_id}/baseline ----------------------------------


class TestBaselineDiff:
    def _create_run(self, client: TestClient, max_tokens: int) -> str:
        yaml_text = f"""
name: x
scenarios:
  - name: scen
    seed_trace_id: {_TRACE_ID}
    candidate_mode: frozen
    evaluators:
      - kind: token_budget
        expected:
          max_total_tokens: {max_tokens}
""".strip()
        return client.post(
            "/api/v1/evals", json={"suite_yaml": yaml_text}
        ).json()["run_id"]

    def test_two_runs_diff_changed_verdicts(self, client: TestClient) -> None:
        # Baseline: PASS (budget=1000, actual=49). Candidate: FAIL (budget=0).
        baseline_run = self._create_run(client, max_tokens=1000)
        candidate_run = self._create_run(client, max_tokens=0)
        resp = client.get(
            f"/api/v1/evals/{candidate_run}/baseline?baseline_run_id={baseline_run}"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["baseline_run_id"] == baseline_run
        assert body["candidate_run_id"] == candidate_run
        assert body["overall_changed"] is True
        assert len(body["scenarios"]) == 1
        row = body["scenarios"][0]
        assert row["scenario_name"] == "scen"
        assert row["changed"] is True
        assert row["baseline_verdict"] == "pass"
        assert row["candidate_verdict"] == "fail"

    def test_baseline_missing_returns_404(self, client: TestClient) -> None:
        run = self._create_run(client, max_tokens=1000)
        ghost = str(uuid4())
        resp = client.get(
            f"/api/v1/evals/{run}/baseline?baseline_run_id={ghost}"
        )
        assert resp.status_code == 404

    def test_candidate_missing_returns_404(self, client: TestClient) -> None:
        ghost = str(uuid4())
        run = self._create_run(client, max_tokens=1000)
        resp = client.get(
            f"/api/v1/evals/{ghost}/baseline?baseline_run_id={run}"
        )
        assert resp.status_code == 404

    def test_bad_uuid_returns_400(self, client: TestClient) -> None:
        run = self._create_run(client, max_tokens=1000)
        resp = client.get(
            f"/api/v1/evals/{run}/baseline?baseline_run_id=garbage"
        )
        assert resp.status_code == 400


# --- DELETE /api/v1/evals/{run_id} ----------------------------------------


class TestDeleteEvalRun:
    def test_deletes_existing(self, client: TestClient) -> None:
        run_id = client.post(
            "/api/v1/evals", json={"suite_yaml": _GOOD_SUITE_YAML}
        ).json()["run_id"]
        resp = client.delete(f"/api/v1/evals/{run_id}")
        assert resp.status_code == 200
        # And a subsequent GET confirms the row is gone.
        assert client.get(f"/api/v1/evals/{run_id}").status_code == 404

    def test_bad_uuid_returns_400(self, client: TestClient) -> None:
        resp = client.delete("/api/v1/evals/not-a-uuid")
        assert resp.status_code == 400

    def test_missing_returns_404(self, client: TestClient) -> None:
        ghost = str(uuid4())
        resp = client.delete(f"/api/v1/evals/{ghost}")
        assert resp.status_code == 404


# --- parse_suite_from_yaml unit tests -------------------------------------


class TestParseSuiteFromYaml:
    def test_minimal_suite_parses(self) -> None:
        suite = parse_suite_from_yaml(_GOOD_SUITE_YAML)
        assert suite.name == "ok-suite"
        assert suite.concurrency == 2
        assert len(suite.scenarios) == 1
        sc = suite.scenarios[0]
        assert sc.name == "happy"
        assert sc.candidate_mode.value == "frozen"
        assert sc.branch_at_index is None
        assert sc.seed_trace_id == _TRACE_ID

    def test_missing_name_raises(self) -> None:
        bad = "scenarios: []\n"
        with pytest.raises(SuiteValidationError):
            parse_suite_from_yaml(bad)

    def test_missing_scenarios_raises(self) -> None:
        bad = "name: x\n"
        with pytest.raises(SuiteValidationError):
            parse_suite_from_yaml(bad)

    def test_duplicate_scenario_names_caught_by_validate(self) -> None:
        # validate_suite (via parse_suite_from_yaml pre-flight) must catch
        # duplicates — they'd break baseline-diff name-lookup.
        dup = f"""
name: dup
scenarios:
  - name: same
    seed_trace_id: {_TRACE_ID}
    evaluators:
      - kind: token_budget
        expected: {{max_total_tokens: 1000}}
  - name: same
    seed_trace_id: {_TRACE_ID}
    evaluators:
      - kind: token_budget
        expected: {{max_total_tokens: 1000}}
""".strip()
        with pytest.raises(SuiteValidationError):
            parse_suite_from_yaml(dup)

    def test_scenario_with_branch_at_index(self) -> None:
        yaml_text = f"""
name: branched
scenarios:
  - name: b
    seed_trace_id: {_TRACE_ID}
    candidate_mode: branch
    branch_at_index: 2
    evaluators:
      - kind: goal_check
        expected: {{pattern: ""}}
""".strip()
        suite = parse_suite_from_yaml(yaml_text)
        sc = suite.scenarios[0]
        assert sc.candidate_mode.value == "branch"
        assert sc.branch_at_index == 2


# --- direct view-model round-trips ---------------------------------------


class TestViewModels:
    """Smoke-test the Pydantic view models can be constructed."""

    def test_eval_run_summary_view_constructs(self) -> None:
        v = EvalRunSummaryView(
            run_id=str(uuid4()),
            suite_name="x",
            started_at="2026-06-29T10:00:00+00:00",
            finished_at="2026-06-29T10:00:30+00:00",
            overall_verdict="pass",
        )
        assert v.overall_verdict == "pass"

    def test_eval_run_list_response_constructs_empty(self) -> None:
        r = EvalRunListResponse(items=[], total=0, limit=50, offset=0)
        assert r.items == []
        assert r.total == 0

    def test_eval_run_detail_view_constructs_with_scenarios(self) -> None:
        from rewind.enums import EvalVerdict

        v = EvalRunDetailView(
            run_id=str(uuid4()),
            suite_name="x",
            started_at="2026-06-29T10:00:00+00:00",
            finished_at="2026-06-29T10:00:30+00:00",
            overall_verdict=EvalVerdict.PASS,
            scenarios=[],
        )
        assert v.overall_verdict == EvalVerdict.PASS
        assert v.scenarios == []
