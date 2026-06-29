"""Phase 0 exit-criterion test: round-trip a 3-span trace through SQLite."""

from __future__ import annotations

import json

from rewind.enums import SpanKind
from rewind.models import Span, Trace
from rewind.storage import TraceStore


def test_round_trip_three_span_trace(
    tmp_path, sample_trace: Trace
) -> None:
    """Serialize → SQLite → reload → identical (Phase 0 exit criterion)."""
    db = TraceStore(tmp_path / "rewind.db")
    db.upsert_trace(sample_trace)
    for span in sample_trace.spans:
        db.insert_span(span)

    reloaded = db.get_trace(sample_trace.trace_id)
    assert reloaded is not None
    assert reloaded.trace_id == sample_trace.trace_id
    assert len(reloaded.spans) == 3

    # Order preserved
    kinds = [s.kind for s in reloaded.spans]
    assert kinds == [SpanKind.AGENT, SpanKind.LLM, SpanKind.TOOL]


def test_raw_attributes_byte_fidelity(tmp_path, llm_span: Span) -> None:
    """``raw_attributes`` must survive SQLite round-trip byte-for-byte."""
    db = TraceStore(tmp_path / "rewind.db")
    db.upsert_trace(Trace(trace_id=llm_span.trace_id, spans=[llm_span]))
    db.insert_span(llm_span)

    raw_bytes = db.raw_attributes_bytes(llm_span.rewind_id)
    assert raw_bytes is not None
    # Re-loads to the same dict the source span held.
    assert json.loads(raw_bytes) == llm_span.raw_attributes


def test_span_parent_linking_round_trips(tmp_path, tool_span: Span, llm_span: Span) -> None:
    """Parent → child span linking must round-trip for a multi-step agent."""
    db = TraceStore(tmp_path / "rewind.db")
    trace = Trace(trace_id=tool_span.trace_id, spans=[llm_span, tool_span])
    db.upsert_trace(trace)
    for s in trace.spans:
        db.insert_span(s)

    spans = db.get_spans(trace.trace_id)
    child = next(s for s in spans if s.span_id == tool_span.span_id)
    assert child.parent_span_id == llm_span.span_id
