"""Unit tests for ``rewind.timeline`` — the Phase 2 read-only API.

We exercise the full surface of ``mount_timeline`` through FastAPI's
``TestClient``: list traces, fetch a trace, list spans with filters, search,
single-span fetch, and all validation / 404 paths. The store is a real
SQLite-backed ``TraceStore`` at a temp path so each test sees an isolated DB;
this matches the pattern in ``tests/test_receiver.py`` and lets us rely on
the same on-disk round-trip behaviour.

The fixtures mirror the ones in ``conftest.py`` but are kept local: the
timeline tests need precise control over the trace's shape (e.g. multi-kind,
multi-span, error spans) so we build bespoke fixtures per test case.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rewind.enums import SpanKind, SpanStatus
from rewind.models import Span, Trace
from rewind.storage import TraceStore
from rewind.timeline import mount_timeline

# --- shared helpers --------------------------------------------------------

_TRACE_ID = "a" * 32
_AGENT_HEX = "1111111111111111"
_LLM_HEX = "2222222222222222"
_TOOL_HEX = "3333333333333333"
_BAD_HEX = "4444444444444444"


def _span(
    *,
    span_id: str,
    parent: str | None,
    name: str,
    kind: SpanKind,
    status: SpanStatus = SpanStatus.UNSET,
    status_message: str | None = None,
    model: str | None = None,
    prompt: int | None = None,
    completion: int | None = None,
    total: int | None = None,
    raw: dict[str, object] | None = None,
    trace_id: str = _TRACE_ID,
) -> Span:
    """Build a Span with sensible test defaults."""
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        name=name,
        kind=kind,
        status=status,
        status_message=status_message,
        model_name=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        raw_attributes=raw or {},
    )


def _demo_trace() -> Trace:
    """A 3-span trace: agent root + LLM child + tool child."""
    agent = _span(
        span_id=_AGENT_HEX,
        parent=None,
        name="adk.agent.CustomerCareAgent",
        kind=SpanKind.AGENT,
        raw={"openinference.span.kind": "AGENT", "label": "demo"},
    )
    llm = _span(
        span_id=_LLM_HEX,
        parent=_AGENT_HEX,
        name="chat.completions.openai",
        kind=SpanKind.LLM,
        model="gpt-4o",
        prompt=42,
        completion=7,
        total=49,
        raw={
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.response.model": "gpt-4o",
            "gen_ai.usage.prompt_tokens": 42,
            "gen_ai.usage.completion_tokens": 7,
            "llm.input_messages": "[{\"role\":\"user\",\"content\":\"hi\"}]",
        },
    )
    tool = _span(
        span_id=_TOOL_HEX,
        parent=_AGENT_HEX,
        name="tool.search_products",
        kind=SpanKind.TOOL,
        status=SpanStatus.OK,
        raw={"tool.name": "search_products", "tool.output": "[]"},
    )
    return Trace(trace_id=_TRACE_ID, spans=[agent, llm, tool])


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Timeline-only app wired to a fresh SQLite store.

    We bypass ``create_app`` (which mounts the receiver + UI too) to keep these
    tests focused on the read API. The store is shared so every endpoint sees
    a consistent in-memory + on-disk snapshot.
    """
    db = tmp_path / "ph2_timeline.db"
    store = TraceStore(str(db))
    trace = _demo_trace()
    store.upsert_trace(trace)
    for span in trace.spans:
        store.insert_span(span, branch_id=trace.root_branch_id)

    app = FastAPI()
    app.state.store = store
    mount_timeline(app)
    with TestClient(app) as c:
        yield c


# --- GET /api/v1/traces ----------------------------------------------------


class TestListTraces:
    """``GET /api/v1/traces`` — paginated listing."""

    def test_returns_single_trace(self, client: TestClient) -> None:
        resp = client.get("/api/v1/traces")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["limit"] == 50
        assert body["offset"] == 0
        assert len(body["items"]) == 1
        summary = body["items"][0]
        assert summary["trace_id"] == _TRACE_ID
        # Span count tracks the root branch only.
        assert summary["span_count"] == 3
        assert summary["span_count_by_kind"] == {
            "gen_ai.agent": 1,
            "gen_ai.llm": 1,
            "gen_ai.tool": 1,
        }
        assert summary["model_names"] == ["gpt-4o"]
        assert summary["has_error"] is False

    def test_explicit_pagination_params(self, client: TestClient) -> None:
        resp = client.get("/api/v1/traces", params={"limit": 5, "offset": 0})
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 5

    def test_limit_below_one_rejected(self, client: TestClient) -> None:
        resp = client.get("/api/v1/traces", params={"limit": 0})
        assert resp.status_code == 422

    def test_limit_above_max_rejected(self, client: TestClient) -> None:
        resp = client.get("/api/v1/traces", params={"limit": 501})
        assert resp.status_code == 422

    def test_negative_offset_rejected(self, client: TestClient) -> None:
        resp = client.get("/api/v1/traces", params={"offset": -1})
        assert resp.status_code == 422

    def test_empty_store_returns_zero_total(
        self, tmp_path: Path
    ) -> None:
        store = TraceStore(str(tmp_path / "empty.db"))
        app = FastAPI()
        app.state.store = store
        mount_timeline(app)
        with TestClient(app) as fresh:
            resp = fresh.get("/api/v1/traces")
            assert resp.status_code == 200
            assert resp.json()["total"] == 0
            assert resp.json()["items"] == []


# --- GET /api/v1/traces/{trace_id} ----------------------------------------


class TestGetTrace:
    """``GET /api/v1/traces/{trace_id}`` — full detail with root spans."""

    def test_returns_full_trace(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/traces/{_TRACE_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["trace_id"] == _TRACE_ID
        assert body["root_branch_id"] is not None
        assert len(body["spans"]) == 3

    def test_span_view_has_projected_fields(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/traces/{_TRACE_ID}")
        spans = resp.json()["spans"]
        llm = next(s for s in spans if s["kind"] == "gen_ai.llm")
        # Fixed fields are projected onto the top level …
        assert llm["model_name"] == "gpt-4o"
        assert llm["prompt_tokens"] == 42
        assert llm["completion_tokens"] == 7
        assert llm["total_tokens"] == 49
        # The view does not pre-compute duration; the UI derives it from
        # start_time/end_time. Both are ISO-strings on the view.
        assert "T" in llm["start_time"]
        assert "T" in llm["end_time"]
        # … and raw_attributes survive verbatim for the inspector toggle.
        assert llm["raw_attributes"]["gen_ai.request.model"] == "gpt-4o"

    def test_unknown_trace_returns_404(self, client: TestClient) -> None:
        bogus = "b" * 32
        resp = client.get(f"/api/v1/traces/{bogus}")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


# --- GET /api/v1/traces/{trace_id}/spans ----------------------------------


class TestSpanFilters:
    """``GET /api/v1/traces/{trace_id}/spans`` — filter combinations."""

    def test_no_filters_returns_all_spans(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/traces/{_TRACE_ID}/spans")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_filter_by_kind(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans", params={"kind": "gen_ai.llm"}
        )
        assert resp.status_code == 200
        spans = resp.json()
        assert len(spans) == 1
        assert spans[0]["kind"] == "gen_ai.llm"

    def test_filter_by_kind_returns_zero_when_no_match(
        self, client: TestClient
    ) -> None:
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans", params={"kind": "gen_ai.mcp"}
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_invalid_kind_rejected(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans", params={"kind": "nope"}
        )
        assert resp.status_code == 400
        assert "kind" in resp.json()["detail"].lower()

    def test_filter_by_model_substring(self, client: TestClient) -> None:
        # "gpt" should match the LLM span (model_name="gpt-4o").
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans", params={"model": "gpt"}
        )
        assert resp.status_code == 200
        spans = resp.json()
        assert len(spans) == 1
        assert spans[0]["model_name"] == "gpt-4o"

    def test_filter_by_model_case_insensitive(
        self, client: TestClient
    ) -> None:
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans", params={"model": "GPT-4O"}
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_filter_by_status_ok(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans", params={"status": "OK"}
        )
        assert resp.status_code == 200
        spans = resp.json()
        assert len(spans) == 1
        assert spans[0]["name"] == "tool.search_products"

    def test_filter_by_invalid_status(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans", params={"status": "BOOM"}
        )
        assert resp.status_code == 400
        assert "status" in resp.json()["detail"].lower()

    def test_parent_only_returns_roots(self, client: TestClient) -> None:
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans", params={"parent_only": True}
        )
        assert resp.status_code == 200
        spans = resp.json()
        assert len(spans) == 1  # only the agent root.
        assert spans[0]["parent_span_id"] is None

    def test_combined_filters(self, client: TestClient) -> None:
        # Both an LLM-kind filter and a model filter that matches → the LLM.
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/spans",
            params={"kind": "gen_ai.llm", "model": "gpt"},
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_span_filters_unknown_trace_404(self, client: TestClient) -> None:
        resp = client.get(f"/api/v1/traces/{'c' * 32}/spans")
        assert resp.status_code == 404


# --- GET /api/v1/spans/{rewind_id} ----------------------------------------


class TestGetSpan:
    """``GET /api/v1/spans/{rewind_id}`` — single-span lookup."""

    def test_fetch_known_span(self, client: TestClient) -> None:
        # First fetch the trace to discover a rewind_id.
        detail = client.get(f"/api/v1/traces/{_TRACE_ID}").json()
        target = next(s for s in detail["spans"] if s["kind"] == "gen_ai.tool")
        rewind_id = target["rewind_id"]

        resp = client.get(f"/api/v1/spans/{rewind_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["rewind_id"] == rewind_id
        assert body["name"] == "tool.search_products"

    def test_unknown_rewind_id_404(self, client: TestClient) -> None:
        bogus = UUID(int=0)
        resp = client.get(f"/api/v1/spans/{bogus}")
        assert resp.status_code == 404

    def test_malformed_rewind_id_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/spans/not-a-uuid")
        assert resp.status_code == 422


# --- GET /api/v1/search ----------------------------------------------------


class TestSearch:
    """``GET /api/v1/search?q=…`` — full-store text search."""

    def test_search_by_name(self, client: TestClient) -> None:
        resp = client.get("/api/v1/search", params={"q": "customer"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["name"] == "adk.agent.CustomerCareAgent"

    def test_search_by_model(self, client: TestClient) -> None:
        resp = client.get("/api/v1/search", params={"q": "gpt-4o"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["model_name"] == "gpt-4o"

    def test_search_matches_attribute_value(self, client: TestClient) -> None:
        # The LLM raw_attributes include an llm.input_messages blob with "hi".
        resp = client.get("/api/v1/search", params={"q": "hi"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "gen_ai.llm"

    def test_search_case_insensitive(self, client: TestClient) -> None:
        resp = client.get("/api/v1/search", params={"q": "CUSTOMERCARE"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_search_no_match(self, client: TestClient) -> None:
        resp = client.get("/api/v1/search", params={"q": "totally-absent-string"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []

    def test_search_with_kind_filter(self, client: TestClient) -> None:
        # "tool" is a substring in both the agent name and tool name; the kind
        # filter restricts to just the tool span. Wait — "tool" isn't in the
        # agent name; the test below uses a different query to be precise.
        resp = client.get(
            "/api/v1/search",
            params={"q": "search_products", "kind": "gen_ai.tool"},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["kind"] == "gen_ai.tool"

    def test_search_with_invalid_kind_400(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/search", params={"q": "agent", "kind": "garbage"}
        )
        assert resp.status_code == 400

    def test_search_with_status_filter(self, client: TestClient) -> None:
        # "search_products" matches the OK tool span.
        resp = client.get(
            "/api/v1/search",
            params={"q": "search_products", "status": "OK"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_search_with_invalid_status_400(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/search", params={"q": "agent", "status": "nope"}
        )
        assert resp.status_code == 400

    def test_search_empty_query_422(self, client: TestClient) -> None:
        # min_length=1 should reject an empty q.
        resp = client.get("/api/v1/search", params={"q": ""})
        assert resp.status_code == 422

    def test_search_missing_query_422(self, client: TestClient) -> None:
        resp = client.get("/api/v1/search")
        assert resp.status_code == 422

    def test_search_pagination(self, client: TestClient) -> None:
        # Use a query that hits all three spans ("a" is in each name).
        resp = client.get(
            "/api/v1/search",
            params={"q": "a", "limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2

        page2 = client.get(
            "/api/v1/search", params={"q": "a", "limit": 2, "offset": 2}
        ).json()
        assert len(page2["items"]) == 1


# --- error span behaviour (cross-cutting) ---------------------------------


class TestErrorSpans:
    """An ERROR span should populate ``has_error`` on the trace summary."""

    def test_error_span_flags_trace(
        self, tmp_path: Path
    ) -> None:
        store = TraceStore(str(tmp_path / "err.db"))
        trace = Trace(
            trace_id="d" * 32,
            spans=[
                _span(
                    span_id=_AGENT_HEX,
                    parent=None,
                    name="boom.agent",
                    kind=SpanKind.AGENT,
                    status=SpanStatus.ERROR,
                    status_message="upstream 500",
                    trace_id="d" * 32,
                ),
            ],
        )
        store.upsert_trace(trace)
        for span in trace.spans:
            store.insert_span(span, branch_id=trace.root_branch_id)

        app = FastAPI()
        app.state.store = store
        mount_timeline(app)
        with TestClient(app) as c:
            summary = c.get("/api/v1/traces").json()["items"][0]
            assert summary["has_error"] is True

            # And the error status is searchable.
            hits = c.get(
                "/api/v1/search", params={"q": "upstream 500"}
            ).json()["items"]
            assert len(hits) == 1
            assert hits[0]["status"] == "ERROR"
