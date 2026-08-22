"""Unit tests for the Phase 1.1 checkpoint state inspector API.

Covers the two read endpoints added to :mod:`agent_timetravel.timeline`:

- ``GET /api/v1/traces/{trace_id}/branches/{branch_id}/checkpoints``
- ``GET /api/v1/branches/{branch_id}/checkpoints/{name}``

The store is a real SQLite-backed ``TraceStore`` at a temp path so each
test sees an isolated DB — matching the pattern in ``tests/test_timeline.py``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_timetravel.enums import SpanKind, SpanStatus
from agent_timetravel.models import Branch, Checkpoint, Span, Trace
from agent_timetravel.storage import TraceStore
from agent_timetravel.timeline import mount_timeline

# --- shared helpers --------------------------------------------------------

_TRACE_ID = "a" * 32
_ROOT_SPAN_HEX = "1111111111111111"


def _root_span() -> Span:
    """A single root span so the trace + branch exist for checkpoints."""
    return Span(
        trace_id=_TRACE_ID,
        span_id=_ROOT_SPAN_HEX,
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


def _demo_trace() -> Trace:
    return Trace(trace_id=_TRACE_ID, spans=[_root_span()])


def _checkpoint(
    *,
    branch_id: UUID,
    name: str,
    cursor_index: int = 0,
    label: str = "",
    payload: dict[str, object] | None = None,
) -> Checkpoint:
    return Checkpoint(
        trace_id=_TRACE_ID,
        branch_id=branch_id,
        name=name,
        cursor_index=cursor_index,
        label=label,
        payload=payload or {},
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Timeline-only app wired to a fresh SQLite store with one branch.

    The branch row is inserted explicitly so ``list_branches`` returns it
    and the ``_branch_exists`` guard in the list endpoint succeeds.
    """
    db = tmp_path / "checkpoint_inspector.db"
    store = TraceStore(str(db))
    trace = _demo_trace()
    store.upsert_trace(trace)
    for span in trace.spans:
        store.insert_span(span, branch_id=trace.root_branch_id)

    app = FastAPI()
    app.state.store = store
    mount_timeline(app)
    with TestClient(app) as c:
        c._store = store  # type: ignore[attr-defined]  # expose for tests
        yield c


def _make_branch(client: TestClient) -> UUID:
    """Insert a child branch off the trace root and return its id."""
    store: TraceStore = client._store  # type: ignore[attr-defined]
    branches = store.list_branches(_TRACE_ID)
    root_branch = next(
        (b for b in branches if b.parent_branch_id is None), None
    )
    parent = root_branch.branch_id if root_branch else trace_root(store)
    branch = Branch(
        trace_id=_TRACE_ID,
        parent_branch_id=parent,
        branch_at_index=0,
        mode="frozen",
        label="checkpoint-test-branch",
    )
    store.insert_branch(branch)
    return branch.branch_id


def trace_root(store: TraceStore) -> UUID:
    """Resolve the trace's stored root branch id."""
    return store.get_trace(_TRACE_ID).root_branch_id  # type: ignore[union-attr]


# --- GET .../branches/{branch_id}/checkpoints ------------------------------


class TestListCheckpoints:
    """``GET /api/v1/traces/{trace_id}/branches/{branch_id}/checkpoints``."""

    def test_list_returns_all_rows_in_cursor_order(
        self, client: TestClient
    ) -> None:
        branch_id = _make_branch(client)
        store: TraceStore = client._store  # type: ignore[attr-defined]
        store.upsert_checkpoint(
            _checkpoint(
                branch_id=branch_id,
                name="second",
                cursor_index=5,
                payload={"items": [1, 2, 3]},
            )
        )
        store.upsert_checkpoint(
            _checkpoint(
                branch_id=branch_id,
                name="first",
                cursor_index=2,
                payload={"step": "init"},
            )
        )

        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/branches/{branch_id}/checkpoints"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        # cursor_index ascending (2 before 5)
        assert body[0]["name"] == "first"
        assert body[0]["cursor_index"] == 2
        assert body[0]["payload"] == {"step": "init"}
        assert body[1]["name"] == "second"
        assert body[1]["payload"] == {"items": [1, 2, 3]}

    def test_list_empty_branch_returns_empty_list(
        self, client: TestClient
    ) -> None:
        branch_id = _make_branch(client)
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/branches/{branch_id}/checkpoints"
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_missing_trace_404(self, client: TestClient) -> None:
        branch_id = uuid4()
        resp = client.get(
            f"/api/v1/traces/{'b' * 32}/branches/{branch_id}/checkpoints"
        )
        assert resp.status_code == 404

    def test_list_missing_branch_404(self, client: TestClient) -> None:
        bogus_branch = uuid4()
        resp = client.get(
            f"/api/v1/traces/{_TRACE_ID}/branches/{bogus_branch}/checkpoints"
        )
        assert resp.status_code == 404


# --- GET /api/v1/branches/{branch_id}/checkpoints/{name} -------------------


class TestGetCheckpoint:
    """``GET /api/v1/branches/{branch_id}/checkpoints/{name}``."""

    def test_get_by_name_returns_payload(self, client: TestClient) -> None:
        branch_id = _make_branch(client)
        store: TraceStore = client._store  # type: ignore[attr-defined]
        store.upsert_checkpoint(
            _checkpoint(
                branch_id=branch_id,
                name="snapshot",
                cursor_index=3,
                label="after-fetch",
                payload={"cart": {"sku": "abc", "qty": 2}},
            )
        )

        resp = client.get(
            f"/api/v1/branches/{branch_id}/checkpoints/snapshot"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "snapshot"
        assert body["branch_id"] == str(branch_id)
        assert body["cursor_index"] == 3
        assert body["label"] == "after-fetch"
        assert body["payload"] == {"cart": {"sku": "abc", "qty": 2}}

    def test_get_missing_checkpoint_404(self, client: TestClient) -> None:
        branch_id = _make_branch(client)
        resp = client.get(
            f"/api/v1/branches/{branch_id}/checkpoints/does-not-exist"
        )
        assert resp.status_code == 404
        assert "does-not-exist" in resp.json()["detail"]

    def test_get_on_missing_branch_still_404(self, client: TestClient) -> None:
        # ``get_checkpoint`` has no branch precondition; a missing checkpoint
        # on a non-existent branch still surfaces as a 404 by name.
        bogus_branch = uuid4()
        resp = client.get(
            f"/api/v1/branches/{bogus_branch}/checkpoints/whatever"
        )
        assert resp.status_code == 404
