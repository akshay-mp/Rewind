"""Read-only timeline API for the Phase 2 UI.

This module exposes the query surface the timeline UI consumes. It is a
**strict read-only API** — unlike :mod:`rewind.receiver`, no endpoint here
mutates the database. This separation is intentional and pinned by tests:

- ``rewind.receiver`` = **ingest surface** (write-only from the UI's POV).
- ``rewind.timeline`` = **query surface** for the timeline UI and future TUI.

Endpoints
---------
- ``GET /api/v1/traces``            — paginated trace list.
- ``GET /api/v1/traces/{trace_id}`` — full trace with spans on the root branch.
- ``GET /api/v1/traces/{trace_id}/spans``
                                   — flat span list (with optional filters).
- ``GET /api/v1/spans/{rewind_id}`` — single span with raw attributes expanded.
- ``GET /api/v1/search``            — search spans by model / kind / error / text.

Output models
-------------
``TraceSummary`` / ``SpanView`` are render-friendly projections of the Phase 0
domain models. They are intentionally separate from :class:`rewind.models.Span`
so the wire shape can shift without touching the storage layer's pydantic
contract. ``SpanView`` flattens the most useful fixed fields onto the
top-level JSON object and keeps ``raw_attributes`` intact for the inspector's
"raw JSON" toggle.

Design notes
------------
- All endpoints run on the same process as the OTLP receiver, sharing the
  ``TraceStore``. SQLite WAL lets reads interleave with writes seamlessly.
- No CORS: the UI is served from the same origin (``/ui`` mount). Browsers
  treat ``/api/*`` and ``/ui/*`` as same-origin; no pre-flight needed.
- No pagination token leak: pagination uses plain ``limit``/``offset`` numerics;
  trace_id enumeration via huge offsets is documented as acceptable for a
  local debug tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from rewind.enums import SpanKind, SpanStatus
from rewind.models import Span, Trace
from rewind.storage import TraceStore

#: Upper bound on ``limit`` to prevent pathological pagination scans.
_MAX_LIMIT = 500
#: Default page size for trace listing.
_DEFAULT_LIMIT = 50

#: Span kinds we will filter on. Anything else is a 400.
_ALLOWED_KINDS = {kind.value for kind in SpanKind}


# --- render-friendly projections -----------------------------------------


class TraceSummary(BaseModel):
    """A row in the trace list — no spans, no raw_attributes."""

    trace_id: str
    root_branch_id: UUID
    created_at: str
    span_count: int = Field(description="Span count on the root branch.")
    span_count_by_kind: dict[str, int] = Field(
        default_factory=dict, description="Counts per SpanKind value."
    )
    model_names: list[str] = Field(
        default_factory=list,
        description="Distinct model_name values seen on LLM spans, sorted.",
    )
    has_error: bool = Field(
        default=False, description="True if any span on the root branch is ERROR."
    )


class SpanView(BaseModel):
    """A single span rendered for the inspector.

    The structured fields mirror :class:`rewind.models.Span` minus
    ``trace_id`` (redundant in the per-span response). ``raw_attributes`` is
    left untouched — the UI toggles between the rendered view and raw JSON.
    """

    rewind_id: UUID
    span_id: str
    parent_span_id: str | None
    branch_id: UUID | None = Field(
        default=None, description="Branch this span lives on (None = root)."
    )
    name: str
    kind: SpanKind
    start_time: str
    end_time: str
    status: SpanStatus
    status_message: str | None
    model_name: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    messages_hash: str | None
    tools_hash: str | None
    raw_attributes: dict[str, Any]


class TraceDetail(BaseModel):
    """Full trace payload: summary plus the ordered span list."""

    trace_id: str
    root_branch_id: UUID
    created_at: str
    spans: list[SpanView]


class SpanSearchHit(BaseModel):
    """One row in a search-result set."""

    trace_id: str
    rewind_id: UUID
    span_id: str
    parent_span_id: str | None
    name: str
    kind: SpanKind
    status: SpanStatus
    model_name: str | None
    start_time: str
    snippet: str = Field(description="Up to 200 chars of matched text.")


class TraceListResponse(BaseModel):
    """Paginated trace index."""

    items: list[TraceSummary]
    total: int
    limit: int
    offset: int


class SearchResponse(BaseModel):
    """Search result envelope."""

    items: list[SpanSearchHit]
    total: int
    limit: int
    offset: int


# --- app factory ----------------------------------------------------------


def mount_timeline(app: FastAPI) -> None:
    """Register the read-only timeline API routes on ``app``.

    Phase 2 deliberately *mounts* onto the existing receiver app so the UI
    has one origin to talk to. This keeps CORS off the table (see threat
    model in ``docs/phases/phase-2.md``) and means the WebSocket-style live
    refresh added later can share the same store without a second process.
    """
    _register_routes(app)


# --- mappers --------------------------------------------------------------


def _trace_summary(trace: Trace) -> TraceSummary:
    """Project a :class:`Trace` into a list-row summary."""
    spans = trace.spans
    return TraceSummary(
        trace_id=trace.trace_id,
        root_branch_id=trace.root_branch_id,
        created_at=trace.created_at,
        span_count=len(spans),
        span_count_by_kind=trace.span_count_by_kind(),
        model_names=sorted(
            {sp.model_name for sp in spans if sp.model_name is not None}
        ),
        has_error=any(sp.status == SpanStatus.ERROR for sp in spans),
    )


def _span_view(span: Span, root_branch_id: UUID | None) -> SpanView:
    """Project a :class:`Span` into the inspector's wire shape."""
    branch_id: UUID | None = root_branch_id  # root branch only for now
    return SpanView(
        rewind_id=span.rewind_id,
        span_id=span.span_id,
        parent_span_id=span.parent_span_id,
        branch_id=branch_id,
        name=span.name,
        kind=span.kind,
        start_time=span.start_time,
        end_time=span.end_time,
        status=span.status,
        status_message=span.status_message,
        model_name=span.model_name,
        prompt_tokens=span.prompt_tokens,
        completion_tokens=span.completion_tokens,
        total_tokens=span.total_tokens,
        messages_hash=span.messages_hash,
        tools_hash=span.tools_hash,
        raw_attributes=span.raw_attributes,
    )


def _snippet(text: str, max_len: int = 200) -> str:
    """Truncate ``text`` to ``max_len`` chars for the search hit snippet."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


@dataclass(slots=True)
class _SearchParams:
    """Filter constraints applied to each candidate span during search.

    Encapsulating the optional filters as one struct keeps the search loop
    small (avoids ``too-many-locals`` / ``too-many-branches`` pylint hits)
    and makes future filter additions one-field cheap.
    """

    kind: str | None
    model: str | None
    status: SpanStatus | None


def _matches_filters(span: Span, params: _SearchParams) -> bool:
    """Return True if ``span`` passes all of ``params`` (None = wildcard)."""
    if params.kind is not None and span.kind.value != params.kind:
        return False
    if params.model is not None and (
        span.model_name is None or params.model.lower() not in span.model_name.lower()
    ):
        return False
    return not (params.status is not None and span.status != params.status)


def _search_traces(
    traces: list[Trace],
    store: TraceStore,
    pattern: re.Pattern[str],
    matchers: _SearchParams,
) -> list[SpanSearchHit]:
    """Walk all traces/spans, returning matches - the heart of ``/api/v1/search``.

    The store is only consulted to rehydrate spans by trace_id; everything
    else is in-memory filtering. We accept the ``store`` for forward-compat
    with a future SQL-backed search path that may not need the full trace.
    """
    del store  # currently unused; kept for the future SQL push-down.
    hits: list[SpanSearchHit] = []
    for trace in traces:
        for span in trace.spans:
            if not _matches_filters(span, matchers):
                continue
            text = _span_text(span)
            if not pattern.search(text):
                continue
            hits.append(
                SpanSearchHit(
                    trace_id=span.trace_id,
                    rewind_id=span.rewind_id,
                    span_id=span.span_id,
                    parent_span_id=span.parent_span_id,
                    name=span.name,
                    kind=span.kind,
                    status=span.status,
                    model_name=span.model_name,
                    start_time=span.start_time,
                    snippet=_snippet(text),
                )
            )
    return hits


def _span_text(span: Span) -> str:
    """Flatten a span's queryable text into one searchable string.

    Covers ``name``, ``status_message``, ``model_name``, and any string-typed
    value in ``raw_attributes`` under ~10k chars (avoids pulling megabyte
    blobs into the search index). Numeric/bytes/array values are skipped.
    """
    parts: list[str] = [span.name]
    if span.status_message:
        parts.append(span.status_message)
    if span.model_name:
        parts.append(span.model_name)
    for value in span.raw_attributes.values():
        if isinstance(value, str) and len(value) < 10_000:
            parts.append(value)
    return "\n".join(parts)


# --- routes ---------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:
    """Wire all Phase 2 read-only routes onto ``app``."""

    @app.get("/api/v1/traces", tags=["timeline"])
    def list_traces(
        request: Request,
        limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> TraceListResponse:
        """List traces in the store, newest-first by ``created_at`` row order.

        Trace *rows* don't carry an auto-increment id; we order by SQLite's
        implicit ``rowid`` (insertion order) descending, which mirrors user
        intuition: the most-recently-ingested trace is at the top.
        """
        store: TraceStore = request.app.state.store
        summaries, total = store.list_traces(limit=limit, offset=offset)
        return TraceListResponse(
            items=[_trace_summary(t) for t in summaries],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v1/traces/{trace_id}", tags=["timeline"])
    def get_trace(request: Request, trace_id: str) -> TraceDetail:
        """Return the full trace with spans on the root branch."""
        store: TraceStore = request.app.state.store
        trace = store.get_trace(trace_id)
        if trace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"trace {trace_id} not found",
            )
        return TraceDetail(
            trace_id=trace.trace_id,
            root_branch_id=trace.root_branch_id,
            created_at=trace.created_at,
            spans=[_span_view(sp, trace.root_branch_id) for sp in trace.spans],
        )

    @app.get("/api/v1/traces/{trace_id}/spans", tags=["timeline"])
    def list_spans(
        request: Request,
        trace_id: str,
        kind: str | None = Query(None, description="Filter by SpanKind value."),
        model: str | None = Query(None, description="Substring match on model_name."),
        status_filter: str | None = Query(
            None,
            alias="status",
            description="Filter by SpanStatus (OK|ERROR|UNSET).",
        ),
        parent_only: bool = Query(
            False, description="If true, only root spans (parent_span_id is null)."
        ),
    ) -> list[SpanView]:
        """Return the flat span list with optional filters."""
        if kind is not None and kind not in _ALLOWED_KINDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"kind '{kind}' invalid; expected one of {sorted(_ALLOWED_KINDS)}"
                ),
            )
        requested_status: SpanStatus | None = None
        if status_filter is not None:
            try:
                requested_status = SpanStatus(status_filter)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"status '{status_filter}' invalid; expected one of "
                        f"{[s.value for s in SpanStatus]}"
                    ),
                ) from exc

        store: TraceStore = request.app.state.store
        trace = store.get_trace(trace_id)
        if trace is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"trace {trace_id} not found",
            )

        # ``model`` substring is matched locally. The DB filter would be more
        # efficient, but this keeps the storage layer's API narrow in Phase 2;
        # a Phase 5 perf pass will push the filter down to SQL.
        spans = trace.spans
        if kind is not None:
            spans = [s for s in spans if s.kind.value == kind]
        if model is not None:
            spans = [
                s
                for s in spans
                if s.model_name is not None and model.lower() in s.model_name.lower()
            ]
        if requested_status is not None:
            spans = [s for s in spans if s.status == requested_status]
        if parent_only:
            spans = [s for s in spans if s.parent_span_id is None]
        return [_span_view(sp, trace.root_branch_id) for sp in spans]

    @app.get("/api/v1/spans/{rewind_id}", tags=["timeline"])
    def get_span(request: Request, rewind_id: UUID) -> SpanView:
        """Return a single span by ``rewind_id``.

        Searches the whole DB because spans don't carry their trace_id on
        the wire for this endpoint — we don't want the UI to have to know
        which trace a span belongs to just to inspect it.
        """
        store: TraceStore = request.app.state.store
        span = store.get_span(rewind_id)
        if span is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"span {rewind_id} not found",
            )
        # ``branch_id`` would be one of the trace's branches; Phase 2 only
        # resolves the root branch via get_trace. We leave it None here for
        # accuracy — the inspector shows the trace/branch separately anyway.
        return _span_view(span, root_branch_id=None)

    @app.get("/api/v1/search", tags=["timeline"])
    def search_spans(
        request: Request,
        q: str = Query(..., min_length=1, max_length=200, description="Search query."),
        kind: str | None = Query(None),
        model: str | None = Query(None),
        status_filter: str | None = Query(None, alias="status"),
        limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> SearchResponse:
        """Full-width text search across all spans in the store.

        The query is a case-insensitive substring match against a flattened
        text projection of each span (name, status_message, model_name, and
        short string values in raw_attributes). This is intentionally simple
        — it answers "where did the agent say X?" without a search index.
        Phase 5+ may introduce FTS5 when scale demands.
        """
        if kind is not None and kind not in _ALLOWED_KINDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"kind '{kind}' invalid; expected one of {sorted(_ALLOWED_KINDS)}"
                ),
            )
        requested_status: SpanStatus | None = None
        if status_filter is not None:
            try:
                requested_status = SpanStatus(status_filter)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"status '{status_filter}' invalid; expected one of "
                        f"{[s.value for s in SpanStatus]}"
                    ),
                ) from exc

        store: TraceStore = request.app.state.store
        pattern = re.compile(re.escape(q), re.IGNORECASE)

        traces, _ = store.list_traces(limit=10**9, offset=0)
        hits = _search_traces(
            traces=traces,
            store=store,
            pattern=pattern,
            matchers=_SearchParams(
                kind=kind,
                model=model,
                status=requested_status,
            ),
        )

        total = len(hits)
        page = hits[offset : offset + limit]
        return SearchResponse(items=page, total=total, limit=limit, offset=offset)


__all__ = [
    "SearchResponse",
    "SpanSearchHit",
    "SpanView",
    "TraceDetail",
    "TraceListResponse",
    "TraceSummary",
    "mount_timeline",
]
