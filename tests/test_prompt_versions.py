"""Unit tests for Phase 2.1 — durable experiment record API.

Covers the prompt-version, assertion-profile, and step-review endpoints
added to :mod:`agent_timetravel.timeline` (and the backing CRUD in
:mod:`agent_timetravel.storage`).

- ``POST/GET /api/v1/traces/{trace_id}/prompt-versions``
- ``PUT /api/v1/prompt-versions/{id}/result``
- ``POST/GET /api/v1/assertion-profiles``
- ``POST/GET /api/v1/traces/{trace_id}/reviews``
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_timetravel.enums import SpanKind, SpanStatus
from agent_timetravel.models import Span, Trace
from agent_timetravel.storage import TraceStore
from agent_timetravel.timeline import mount_timeline

_TRACE_ID = "c" * 32


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
    db = tmp_path / "prompt_versions.db"
    store = TraceStore(str(db))
    trace = Trace(trace_id=_TRACE_ID, spans=[_root_span()])
    store.upsert_trace(trace)
    for span in trace.spans:
        store.insert_span(span, branch_id=trace.root_branch_id)

    app = FastAPI()
    app.state.store = store
    mount_timeline(app)
    with TestClient(app) as c:
        yield c


# --- prompt versions -------------------------------------------------------


class TestPromptVersions:
    def test_create_then_list(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/traces/{_TRACE_ID}/prompt-versions",
            json={
                "version_id": "pv-1",
                "cursor_index": 2,
                "base_messages": [{"role": "user", "content": "hi"}],
                "messages": [{"role": "user", "content": "hi v2"}],
                "base_model": "gpt-4o",
                "model": "gpt-4o-mini",
                "branch_id": "branch-a",
                "parent_version_id": "parent-1",
                "parameters": {
                    "temperature": 0.2,
                    "seed": 7,
                    "max_tokens": 300,
                    "response_format": {"type": "json_object"},
                    "tool_choice": "auto",
                },
                "author_note": "try stricter output",
                "assertions": {"requiredText": ["source"]},
                "evaluator_names": ["cites_source"],
                "created_at": "2026-08-03T00:00:00+00:00",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["version_id"] == "pv-1"
        assert body["status"] == "running"
        assert body["messages"] == [{"role": "user", "content": "hi v2"}]

        listed = client.get(
            f"/api/v1/traces/{_TRACE_ID}/prompt-versions",
            params={"cursor": 2},
        )
        assert listed.status_code == 200
        items = listed.json()
        assert len(items) == 1
        assert items[0]["version_id"] == "pv-1"
        assert items[0]["result"] is None  # not completed yet
        assert items[0]["parameters"]["seed"] == 7
        assert items[0]["parent_version_id"] == "parent-1"
        assert items[0]["evaluator_names"] == ["cites_source"]

    def test_put_result_marks_completed(self, client: TestClient) -> None:
        client.post(
            f"/api/v1/traces/{_TRACE_ID}/prompt-versions",
            json={
                "version_id": "pv-2",
                "cursor_index": 0,
                "messages": [],
                "created_at": "2026-08-03T00:00:00+00:00",
            },
        )
        resp = client.put(
            "/api/v1/prompt-versions/pv-2/result",
            json={
                "result": "Paris is 18C",
                "usage": {"total_tokens": 42},
                "latency_ms": 350,
                "completed_at": "2026-08-03T00:00:01+00:00",
                "reasoning": "short internal plan",
                "pricing": {"name": "test", "outputPerMillion": 2.0},
                "assertion_result": {"passed": False, "failures": ["missing source"]},
                "review_verdict": "rejected",
                "review_note": "needs a citation",
                "evaluator_results": {"cites_source": {"passed": False}},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["result"] == "Paris is 18C"
        assert body["usage"] == {"total_tokens": 42}
        assert body["latency_ms"] == 350
        assert body["reasoning"] == "short internal plan"
        assert body["assertion_result"]["passed"] is False
        assert body["review_verdict"] == "rejected"
        assert body["evaluator_results"]["cites_source"]["passed"] is False

    def test_existing_version_id_is_immutable(self, client: TestClient) -> None:
        payload = {
            "version_id": "pv-immutable",
            "cursor_index": 0,
            "messages": [{"role": "user", "content": "original"}],
            "model": "model-a",
            "parameters": {"seed": 1},
        }
        response = client.post(
            f"/api/v1/traces/{_TRACE_ID}/prompt-versions",
            json=payload,
        )
        assert response.status_code == 201
        changed = {
            **payload,
            "messages": [{"role": "user", "content": "changed"}],
            "model": "model-b",
            "parameters": {"seed": 2},
        }
        response = client.post(
            f"/api/v1/traces/{_TRACE_ID}/prompt-versions",
            json=changed,
        )
        assert response.status_code == 201
        item = client.get(f"/api/v1/traces/{_TRACE_ID}/prompt-versions").json()[0]
        assert item["messages"] == payload["messages"]
        assert item["model"] == "model-a"
        assert item["parameters"] == {"seed": 1}

    def test_list_without_cursor_hydrates_all_variants(self, client: TestClient) -> None:
        for version_id, cursor in (("pv-all-1", 0), ("pv-all-2", 3)):
            response = client.post(
                f"/api/v1/traces/{_TRACE_ID}/prompt-versions",
                json={"version_id": version_id, "cursor_index": cursor},
            )
            assert response.status_code == 201
        response = client.get(f"/api/v1/traces/{_TRACE_ID}/prompt-versions")
        assert response.status_code == 200
        assert {item["version_id"] for item in response.json()} == {"pv-all-1", "pv-all-2"}

    def test_put_result_missing_version_404(self, client: TestClient) -> None:
        resp = client.put(
            "/api/v1/prompt-versions/no-such-id/result",
            json={"result": "x"},
        )
        assert resp.status_code == 404

    def test_create_missing_trace_404(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/traces/{'d' * 32}/prompt-versions",
            json={"version_id": "pv-x", "cursor_index": 0},
        )
        assert resp.status_code == 404

    def test_list_empty_returns_empty_list(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/prompt-versions",
            params={"cursor": 99},
        )
        assert resp.status_code == 200
        assert resp.json() == []


# --- assertion profiles ----------------------------------------------------


class TestAssertionProfiles:
    def test_create_then_list(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/assertion-profiles",
            json={
                "profile_id": "ap-1",
                "name": "must-cite",
                "required_text": ["source"],
                "forbidden_text": ["SSN"],
                "require_json": False,
                "require_citations": True,
                "max_tokens": 500,
                "max_cost_usd": 0.05,
                "created_at": "2026-08-03T00:00:00+00:00",
            },
        )
        assert resp.status_code == 201
        listed = client.get("/api/v1/assertion-profiles")
        assert listed.status_code == 200
        items = listed.json()
        assert len(items) == 1
        assert items[0]["name"] == "must-cite"
        assert items[0]["require_citations"] is True
        assert items[0]["max_tokens"] == 500

    def test_upsert_updates_existing(self, client: TestClient) -> None:
        client.post(
            "/api/v1/assertion-profiles",
            json={
                "profile_id": "ap-2",
                "name": "v1",
                "created_at": "2026-08-03T00:00:00+00:00",
            },
        )
        client.post(
            "/api/v1/assertion-profiles",
            json={
                "profile_id": "ap-2",
                "name": "v2",
                "require_json": True,
                "created_at": "2026-08-03T00:00:00+00:00",
            },
        )
        items = client.get("/api/v1/assertion-profiles").json()
        assert len(items) == 1
        assert items[0]["name"] == "v2"
        assert items[0]["require_json"] is True

    def test_list_empty(self, client: TestClient) -> None:
        assert client.get("/api/v1/assertion-profiles").json() == []


# --- step reviews ----------------------------------------------------------


class TestStepReviews:
    def test_upsert_then_list(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/traces/{_TRACE_ID}/reviews",
            json={
                "trace_id": _TRACE_ID,
                "cursor_index": 3,
                "review_note": "good answer",
                "review_verdict": "accepted",
                "assertions": {"requiredText": ["source"], "maxTokens": 100},
                "assertion_result": {"passed": True, "failures": []},
                "updated_at": "2026-08-03T00:00:00+00:00",
            },
        )
        assert resp.status_code == 201
        listed = client.get(f"/api/v1/traces/{_TRACE_ID}/reviews")
        assert listed.status_code == 200
        items = listed.json()
        assert len(items) == 1
        assert items[0]["review_verdict"] == "accepted"
        assert items[0]["cursor_index"] == 3
        assert items[0]["assertions"]["maxTokens"] == 100
        assert items[0]["assertion_result"]["passed"] is True

    def test_upsert_mismatched_trace_id_400(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/traces/{_TRACE_ID}/reviews",
            json={
                "trace_id": "wrong-trace",
                "cursor_index": 0,
            },
        )
        assert resp.status_code == 400

    def test_upsert_missing_trace_404(self, client: TestClient) -> None:
        resp = client.post(
            f"/api/v1/traces/{'e' * 32}/reviews",
            json={"trace_id": "e" * 32, "cursor_index": 0},
        )
        assert resp.status_code == 404

    def test_multiple_reviews_ordered_by_cursor(self, client: TestClient) -> None:
        for cursor in (5, 1, 3):
            client.post(
                f"/api/v1/traces/{_TRACE_ID}/reviews",
                json={
                    "trace_id": _TRACE_ID,
                    "cursor_index": cursor,
                    "review_verdict": "accepted",
                    "updated_at": "2026-08-03T00:00:00+00:00",
                },
            )
        items = client.get(f"/api/v1/traces/{_TRACE_ID}/reviews").json()
        cursors = [i["cursor_index"] for i in items]
        assert cursors == [1, 3, 5]
