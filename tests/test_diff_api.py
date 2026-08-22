"""Unit tests for the Phase 5 HTTP API surface.

These cover the four new endpoints mounted by :mod:`agent_timetravel.timeline`:

* ``GET  /api/v1/traces/{trace_id}/branches``       — branch tree.
* ``GET  /api/v1/traces/{trace_id}/diff``           — span-sequence diff.
* ``GET  /api/v1/spans/{timetravel_id}/message-diff``   — token-level message diff.
* ``POST /api/v1/traces/{trace_id}/branches``       — create a branch.

The tests use Starlette's :class:`TestClient` against the production
:func:`agent_timetravel.receiver.create_app` factory — no subprocess, no socket, no
network. The store is a real SQLite file on tmp_path so the assertions
exercise the full request → store → response pipeline.

Pure-logic coverage of the diff engine itself lives in
``tests/test_diff.py``; these tests pin the *wiring* (HTTP status codes,
JSON shape, error envelopes, branch-tree materialisation through storage).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_timetravel.enums import SpanKind, SpanStatus
from agent_timetravel.models import Branch, Span, Trace
from agent_timetravel.receiver import create_app
from agent_timetravel.storage import TraceStore

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    """Fresh TraceStore backed by a tmp db file."""
    return TraceStore(str(tmp_path / "diff-api.db"))


@pytest.fixture
def client(store: TraceStore) -> TestClient:
    """TestClient wired to a real receiver app with our tmp store."""
    return TestClient(create_app(store))


def _seed_trace_with_two_branches(store: TraceStore) -> tuple[str, UUID, UUID]:
    """Seed a trace with two divergent branches.

    Returns ``(trace_id, left_branch_id, right_branch_id)``:
    - left branch has spans [s0, s1_alpha]
    - right branch has spans [s0, s1_beta]
    The two branches share s0; span index 1 is the divergence point.
    """
    trace_id = "d" * 24 + "00000001"
    root_branch = Branch(
        trace_id=trace_id,
        parent_branch_id=None,
        branch_at_index=None,
        mode="frozen",
        label="root",
    )
    left_branch = Branch(
        trace_id=trace_id,
        parent_branch_id=root_branch.branch_id,
        branch_at_index=0,
        mode="frozen",
        label="left-variant",
    )
    right_branch = Branch(
        trace_id=trace_id,
        parent_branch_id=root_branch.branch_id,
        branch_at_index=0,
        mode="frozen",
        label="right-variant",
    )
    store.upsert_trace(Trace(trace_id=trace_id, spans=[]))
    store.insert_branch(root_branch)
    store.insert_branch(left_branch)
    store.insert_branch(right_branch)

    # Common prefix span on root branch (lives under root_branch_id).
    shared = Span(
        trace_id=trace_id,
        span_id="00" * 8,
        parent_span_id=None,
        name="shared-llm",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        messages_hash="shared-hash",
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:00:01Z",
        status=SpanStatus.OK,
        raw_attributes={},
    )
    store.insert_span(shared, branch_id=None)  # root prefix
    # The two divergent spans attach to their respective branches.
    left_only = Span(
        trace_id=trace_id,
        span_id="aa" * 8,
        parent_span_id=None,
        name="left-llm",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        messages_hash="left-hash",
        start_time="2026-01-01T00:00:02Z",
        end_time="2026-01-01T00:00:03Z",
        status=SpanStatus.OK,
        raw_attributes={},
    )
    right_only = Span(
        trace_id=trace_id,
        span_id="bb" * 8,
        parent_span_id=None,
        name="right-llm",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        messages_hash="right-hash",
        start_time="2026-01-01T00:00:02Z",
        end_time="2026-01-01T00:00:03Z",
        status=SpanStatus.OK,
        raw_attributes={},
    )
    store.insert_span(left_only, branch_id=left_branch.branch_id)
    store.insert_span(right_only, branch_id=right_branch.branch_id)
    return trace_id, left_branch.branch_id, right_branch.branch_id


def _seed_trace_with_message_spans(
    store: TraceStore,
    *,
    left_content: str = "alpha beta",
    right_content: str = "alpha gamma",
) -> tuple[UUID, UUID]:
    """Seed a trace with two LLM spans carrying ``gen_ai.response`` payloads."""
    trace_id = "m" * 24 + "00000001"
    store.upsert_trace(Trace(trace_id=trace_id, spans=[]))
    left_span = Span(
        trace_id=trace_id,
        span_id="11" * 8,
        parent_span_id=None,
        name="left-msg",
        kind=SpanKind.LLM,
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:00:01Z",
        status=SpanStatus.OK,
        raw_attributes={
            "gen_ai.response": {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": left_content},
                        "finish_reason": "stop",
                    }
                ],
            }
        },
    )
    right_span = Span(
        trace_id=trace_id,
        span_id="22" * 8,
        parent_span_id=None,
        name="right-msg",
        kind=SpanKind.LLM,
        start_time="2026-01-01T00:00:02Z",
        end_time="2026-01-01T00:00:03Z",
        status=SpanStatus.OK,
        raw_attributes={
            "gen_ai.response": {
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": right_content},
                        "finish_reason": "stop",
                    }
                ],
            }
        },
    )
    store.insert_span(left_span)
    store.insert_span(right_span)
    return left_span.timetravel_id, right_span.timetravel_id


# ----------------------------------------------------------------------
# GET /traces/{trace_id}/branches
# ----------------------------------------------------------------------


def test_branch_tree_returns_nested_structure(
    client: TestClient, store: TraceStore
) -> None:
    """The branch tree endpoint returns a nested, root-first structure."""
    trace_id, left_bid, right_bid = _seed_trace_with_two_branches(store)
    resp = client.get(f"/api/v1/traces/{trace_id}/branches")

    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == trace_id
    assert body["parent_branch_id"] is None  # root.
    # Root has two children (left + right variants).
    assert len(body["children"]) == 2
    child_ids = {child["branch_id"] for child in body["children"]}
    assert str(left_bid) in child_ids
    assert str(right_bid) in child_ids
    # Each child carries the labels we seeded.
    labels = {child["label"] for child in body["children"]}
    assert labels == {"left-variant", "right-variant"}


def test_branch_tree_404_for_unknown_trace(client: TestClient) -> None:
    """An unknown trace id yields 404, not an empty tree."""
    resp = client.get("/api/v1/traces/ffffffff/branches")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ----------------------------------------------------------------------
# GET /traces/{trace_id}/diff?left=&right=
# ----------------------------------------------------------------------


def test_diff_branches_flags_first_divergence(
    client: TestClient, store: TraceStore
) -> None:
    """The diff endpoint marks the first divergent span index."""
    trace_id, left_bid, right_bid = _seed_trace_with_two_branches(store)
    resp = client.get(
        f"/api/v1/traces/{trace_id}/diff",
        params={"left": str(left_bid), "right": str(right_bid)},
    )

    assert resp.status_code == 200
    body = resp.json()
    # Phase 5 exit criterion: first divergence is flagged.
    first = [p for p in body["pairs"] if p["is_first_divergence"]]
    assert len(first) == 1
    assert first[0]["index"] == 1  # span 0 is shared, span 1 diverges.
    assert body["first_divergence_index"] == 1
    assert body["identical"] is False
    # Each pair carries side-labelled ``branch_id``.
    assert body["pairs"][1]["left"]["branch_id"] == str(left_bid)
    assert body["pairs"][1]["right"]["branch_id"] == str(right_bid)


def test_diff_branches_identical_when_same_branch(
    client: TestClient, store: TraceStore
) -> None:
    """Diffing a branch against itself yields no divergence."""
    trace_id, left_bid, _ = _seed_trace_with_two_branches(store)
    resp = client.get(
        f"/api/v1/traces/{trace_id}/diff",
        params={"left": str(left_bid), "right": str(left_bid)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["identical"] is True
    assert body["first_divergence_index"] is None


def test_diff_branches_404_for_missing_branch(
    client: TestClient, store: TraceStore
) -> None:
    """An unknown branch id yields 404."""
    trace_id, left_bid, _ = _seed_trace_with_two_branches(store)
    bogus = uuid4()
    resp = client.get(
        f"/api/v1/traces/{trace_id}/diff",
        params={"left": str(left_bid), "right": str(bogus)},
    )
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


# ----------------------------------------------------------------------
# GET /spans/{timetravel_id}/message-diff?other=
# ----------------------------------------------------------------------


def test_message_diff_endpoint_returns_token_diff(
    client: TestClient, store: TraceStore
) -> None:
    """The message-diff endpoint surfaces add/remove/change classifications."""
    left_rid, right_rid = _seed_trace_with_message_spans(store)
    resp = client.get(
        f"/api/v1/spans/{left_rid}/message-diff",
        params={"other": str(right_rid)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["left"] == "alpha beta"
    assert body["right"] == "alpha gamma"
    assert body["identical"] is False
    kinds = {f["kind"] for f in body["fragments"]}
    # A pure replace should produce a "changed" fragment with no raw
    # add/remove fragments emitted.
    assert "changed" in kinds
    assert body["added_tokens"] == 1
    assert body["removed_tokens"] == 1


def test_message_diff_endpoint_preserves_arrows_in_changed_sides(
    client: TestClient, store: TraceStore
) -> None:
    """The wire model carries arrow-containing replacements without splitting."""
    left_rid, right_rid = _seed_trace_with_message_spans(
        store,
        left_content="prefix old→left suffix",
        right_content="prefix new→right suffix",
    )
    resp = client.get(
        f"/api/v1/spans/{left_rid}/message-diff",
        params={"other": str(right_rid)},
    )

    assert resp.status_code == 200
    changed = [fragment for fragment in resp.json()["fragments"] if fragment["kind"] == "changed"]
    assert len(changed) == 1
    assert changed[0]["removed"] == "old→left"
    assert changed[0]["added"] == "new→right"


def test_message_diff_endpoint_identical_for_same_span(
    client: TestClient, store: TraceStore
) -> None:
    """Diffing a span's message against itself returns identical."""
    left_rid, _ = _seed_trace_with_message_spans(store)
    resp = client.get(
        f"/api/v1/spans/{left_rid}/message-diff",
        params={"other": str(left_rid)},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["identical"] is True
    assert body["added_tokens"] == 0
    assert body["removed_tokens"] == 0


def test_message_diff_endpoint_404_for_missing_span(
    client: TestClient, store: TraceStore
) -> None:
    """An unknown span id yields 404."""
    left_rid, _ = _seed_trace_with_message_spans(store)
    bogus = uuid4()
    resp = client.get(
        f"/api/v1/spans/{left_rid}/message-diff",
        params={"other": str(bogus)},
    )
    assert resp.status_code == 404


def test_message_diff_endpoint_handles_spans_without_response_payload(
    client: TestClient, store: TraceStore
) -> None:
    """A span with no ``gen_ai.response`` degrades to an empty-string diff."""
    trace_id = "n" * 24 + "00000001"
    store.upsert_trace(Trace(trace_id=trace_id, spans=[]))
    left = Span(
        trace_id=trace_id,
        span_id="33" * 8,
        parent_span_id=None,
        name="bare",
        kind=SpanKind.LLM,
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:00:01Z",
        status=SpanStatus.OK,
        raw_attributes={"openinference.span.kind": "LLM"},
    )
    right = Span(
        trace_id=trace_id,
        span_id="44" * 8,
        parent_span_id=None,
        name="bare",
        kind=SpanKind.LLM,
        start_time="2026-01-01T00:00:02Z",
        end_time="2026-01-01T00:00:03Z",
        status=SpanStatus.OK,
        raw_attributes={"openinference.span.kind": "LLM"},
    )
    store.insert_span(left)
    store.insert_span(right)

    resp = client.get(
        f"/api/v1/spans/{left.timetravel_id}/message-diff",
        params={"other": str(right.timetravel_id)},
    )

    assert resp.status_code == 200
    body = resp.json()
    # Both extracted as "" → identical.
    assert body["left"] == ""
    assert body["right"] == ""
    assert body["identical"] is True


# ----------------------------------------------------------------------
# POST /traces/{trace_id}/branches
# ----------------------------------------------------------------------


def test_create_branch_persists_row(
    client: TestClient, store: TraceStore
) -> None:
    """POST /branches creates a branch row visible in the tree."""
    trace_id, _, _ = _seed_trace_with_two_branches(store)
    # The trace's root branch is the first row in list_branches.
    root_branch = store.list_branches(trace_id)[0]
    resp = client.post(
        f"/api/v1/traces/{trace_id}/branches",
        json={
            "parent_branch_id": str(root_branch.branch_id),
            "branch_at_index": 2,
            "mode": "frozen",
            "label": "test-fork",
        },
    )

    assert resp.status_code == 201
    body = resp.json()
    node = body["branch"]
    assert node["label"] == "test-fork"
    assert node["branch_at_index"] == 2
    assert node["parent_branch_id"] == str(root_branch.branch_id)
    # The branch should now also appear in the tree endpoint.
    tree = client.get(f"/api/v1/traces/{trace_id}/branches").json()
    flat_ids = _flatten_branch_ids(tree)
    assert node["branch_id"] in flat_ids


def test_create_branch_defaults_parent_to_trace_root(
    client: TestClient, store: TraceStore
) -> None:
    """Omitting ``parent_branch_id`` forks from the trace root."""
    trace_id, _, _ = _seed_trace_with_two_branches(store)
    root_branch = store.list_branches(trace_id)[0]

    resp = client.post(
        f"/api/v1/traces/{trace_id}/branches",
        json={"branch_at_index": 1, "label": "implicit-parent"},
    )

    assert resp.status_code == 201
    assert resp.json()["branch"]["parent_branch_id"] == str(root_branch.branch_id)


def test_create_branch_rejects_negative_index(
    client: TestClient, store: TraceStore
) -> None:
    """Negative ``branch_at_index`` is rejected by Pydantic validation."""
    trace_id, _, _ = _seed_trace_with_two_branches(store)
    resp = client.post(
        f"/api/v1/traces/{trace_id}/branches",
        json={"branch_at_index": -1, "label": "invalid"},
    )
    assert resp.status_code == 422  # Pydantic validation error.


def test_create_branch_404_for_unknown_trace(client: TestClient) -> None:
    """POSTing to an unknown trace yields 404 before any row is written."""
    resp = client.post(
        "/api/v1/traces/unknown/branches",
        json={"branch_at_index": 0},
    )
    assert resp.status_code == 404


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _flatten_branch_ids(node: dict) -> set[str]:
    """Flatten a branch tree dict into a set of all branch_id strings."""
    ids = {node["branch_id"]}
    for child in node.get("children", []):
        ids |= _flatten_branch_ids(child)
    return ids
