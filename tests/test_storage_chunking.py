"""Unit tests for Phase 4's chunking / pagination guarantees.

The Phase 4 exit criterion: ``A trace with 100k+ spans loads its timeline
without OOM``. The integration test enumerates the 100k scenario under
``tracemalloc``; this unit file covers the smaller invariants:

* :meth:`TraceStore.get_spans_paginated` returns the right slice + total.
* :meth:`TraceStore.iter_spans` streams the same sequence as the eager load.
* Negative / out-of-range arguments fail-closed (input validation).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_timetravel.enums import SpanKind
from agent_timetravel.models import Span, Trace
from agent_timetravel.storage import TraceStore


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    """Fresh TraceStore backed by a tmp file."""
    return TraceStore(str(tmp_path / "chunk-test.db"))


def _seed_trace(store: TraceStore, n_spans: int, trace_id: str = "c" * 32) -> str:
    """Insert one root trace with ``n_spans`` spans attached to its root branch."""
    trace = Trace(trace_id=trace_id, spans=[])
    store.upsert_trace(trace)
    for i in range(n_spans):
        store.insert_span(
            Span(
                trace_id=trace_id,
                span_id=f"{i:016x}",
                name=f"span-{i}",
                kind=SpanKind.LLM,
                start_time="2026-01-01T00:00:00Z",
                end_time="2026-01-01T00:00:01Z",
                raw_attributes={},
            )
        )
    return trace_id


# ----------------------------------------------------------------------
# get_spans_paginated
# ----------------------------------------------------------------------
def test_paginated_first_page_has_total_count(store: TraceStore) -> None:
    """The first page returns the right slice + a total count."""
    tid = _seed_trace(store, n_spans=10)
    page, total = store.get_spans_paginated(tid, limit=4, offset=0)
    assert total == 10
    assert [s.name for s in page] == ["span-0", "span-1", "span-2", "span-3"]


def test_paginated_offset_skips(store: TraceStore) -> None:
    """Offset skips spans correctly across pages."""
    tid = _seed_trace(store, n_spans=10)
    page, total = store.get_spans_paginated(tid, limit=3, offset=7)
    assert total == 10
    assert [s.name for s in page] == ["span-7", "span-8", "span-9"]


def test_paginated_offset_beyond_end_returns_empty(store: TraceStore) -> None:
    """Offset past the end returns an empty page (still with correct total)."""
    tid = _seed_trace(store, n_spans=5)
    page, total = store.get_spans_paginated(tid, limit=10, offset=100)
    assert total == 5
    assert page == []


def test_paginated_limit_clamped(store: TraceStore) -> None:
    """A ``limit`` of 0 is clamped to 1 (docs guarantee min=1)."""
    tid = _seed_trace(store, n_spans=3)
    page, total = store.get_spans_paginated(tid, limit=0)
    assert total == 3
    assert len(page) == 1
    assert page[0].name == "span-0"


def test_paginated_limit_upper_bound(store: TraceStore) -> None:
    """A ``limit`` >10_000 is clamped to 10_000 — protects query memory."""
    tid = _seed_trace(store, n_spans=3)
    # No assertion on the result shape here; just that no ValueError fires
    # for a huge limit. The clamp is internal so we don't observe it directly.
    page, total = store.get_spans_paginated(tid, limit=10**6)
    assert total == 3
    assert len(page) == 3


def test_paginated_negative_offset_raises(store: TraceStore) -> None:
    """Negative offset is rejected with ``ValueError``."""
    tid = _seed_trace(store, n_spans=3)
    with pytest.raises(ValueError, match="offset must be >= 0"):
        store.get_spans_paginated(tid, offset=-1)


# ----------------------------------------------------------------------
# iter_spans
# ----------------------------------------------------------------------
def test_iter_spans_streams_in_order(store: TraceStore) -> None:
    """The streaming iterator yields the same sequence as the eager load."""
    tid = _seed_trace(store, n_spans=15)
    streamed = [s.name for s in store.iter_spans(tid, chunk_size=4)]
    assert streamed == [f"span-{i}" for i in range(15)]


def test_iter_spans_matches_eager_load(store: TraceStore) -> None:
    """A 1000-span trace streams the same Span list as ``get_trace``."""
    tid = _seed_trace(store, n_spans=1000)
    streamed = list(store.iter_spans(tid, chunk_size=100))
    eager = store.get_spans(tid)
    assert len(streamed) == len(eager) == 1000
    assert [s.name for s in streamed] == [s.name for s in eager]


def test_iter_spans_invalid_chunk_size(store: TraceStore) -> None:
    """A non-positive chunk_size is rejected."""
    tid = _seed_trace(store, n_spans=1)
    with pytest.raises(ValueError, match="chunk_size must be >= 1"):
        list(store.iter_spans(tid, chunk_size=0))


def test_iter_spans_empty_trace_yields_nothing(store: TraceStore) -> None:
    """A trace with zero spans streams nothing (not an error)."""
    tid = "d" * 32
    store.upsert_trace(Trace(trace_id=tid, spans=[]))
    streamed = list(store.iter_spans(tid))
    assert streamed == []
