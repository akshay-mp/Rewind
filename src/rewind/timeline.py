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

from rewind.diff import (
    BranchNode,
    MessageDiff,
    SpanDiff,
    branch_tree,
    message_diff,
    span_diff,
)
from rewind.enums import SpanKind, SpanStatus
from rewind.models import Branch, Span, Trace
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


# --- Phase 5: branch + diff projections -----------------------------------


class BranchNodeView(BaseModel):
    """Recursive branch tree node — one per :class:`rewind.models.Branch`.

    Used by the timeline UI's branch picker. ``children`` is recursive; the
    root node has ``parent_branch_id is None``.

    Field shape mirrors :class:`rewind.diff.BranchNode` 1:1 — the
    duplication is the cost of a clean layer split (pure dataclass vs
    Pydantic BaseModel). Pylint's duplicate-code detector flags it; the
    alternative (sharing one type across layers) would couple the diff
    engine to the wire shape.
    """

    branch_id: UUID
    trace_id: str
    parent_branch_id: UUID | None
    branch_at_index: int | None
    mode: str
    label: str
    created_at: str
    children: list[BranchNodeView] = Field(default_factory=list)


class SpanPairView(BaseModel):
    """One row of a side-by-side comparison.

    ``left`` and ``right`` are optional :class:`SpanView`s — at divergent
    indices one may be missing (``left_only`` / ``right_only``). The spans
    are identical technical projections (so the inspector can render them
    uniformly), but ``branch_id`` is carried per-side so the UI can label
    which branch each came from.
    """

    index: int
    left: SpanView | None
    right: SpanView | None
    status: str
    is_first_divergence: bool


class SpanDiffView(BaseModel):
    """Span-sequence diff between two branches.

    The Phase 5 exit criterion *"Diffing two branches marks exactly which
    span first diverged"* is captured by :attr:`first_divergence_index`
    plus the :attr:`is_first_divergence` sentinel on exactly one row.
    """

    pairs: list[SpanPairView]
    first_divergence_index: int | None
    left_count: int
    right_count: int
    identical: bool


class MessageFragmentView(BaseModel):
    """One segment of a token-aligned message diff."""

    text: str
    kind: str


class MessageDiffView(BaseModel):
    """Token-level diff of two assistant messages.

    The Phase 5 exit criterion *"token-level message diff renders
    add/remove/change correctly"* is captured here.
    """

    left: str
    right: str
    fragments: list[MessageFragmentView]
    added_tokens: int
    removed_tokens: int
    identical: bool


class CreateBranchRequest(BaseModel):
    """Request body for ``POST /traces/{trace_id}/branches``.

    Captures the *"Branch from span N with an edited system prompt"* user
    action. ``parent_branch_id`` defaults to the trace root branch when
    omitted. ``mode`` is textual (ReplayMode) — the storage layer accepts
    anything; the replay context manager will re-validate when the user
    drives the branch live.
    """

    parent_branch_id: UUID | None = Field(
        default=None,
        description="Branch to fork from. Defaults to the trace root.",
    )
    branch_at_index: int = Field(
        ..., ge=0, description="0-based span index where the branch diverges."
    )
    mode: str = Field(default="frozen", description="ReplayMode for the branch.")
    label: str = Field(
        default="", description="Human-readable label for the branch picker."
    )


class CreateBranchResponse(BaseModel):
    """Response for branch creation — the new branch row, materialised."""

    branch: BranchNodeView


class CheckpointView(BaseModel):
    """One checkpoint row rendered for the inspector.

    Mirrors :class:`rewind.models.Checkpoint` 1:1. The ``payload`` is the
    full agent-visible state snapshot captured at the cursor; the list
    endpoint includes it so the UI can render captured state without a
    second round-trip per checkpoint.
    """

    checkpoint_id: UUID
    trace_id: str
    branch_id: UUID
    name: str
    cursor_index: int
    label: str
    payload: dict[str, Any]
    created_at: str


# --- Phase 2.1: durable experiment record projections ---------------------


class PromptVersionView(BaseModel):
    """One prompt-variant experiment row.

    A prompt version is an immutable record of an A/B experiment initiated
    from an executed step: the base messages/model, the edited variant, and
    (once completed) the model's response + usage. Persisted so a page
    refresh or a teammate's machine can hydrate the full experiment history.
    """

    version_id: str
    trace_id: str
    cursor_index: int
    base_messages: list[Any] = Field(default_factory=list)
    messages: list[Any] = Field(default_factory=list)
    base_model: str = ""
    model: str = ""
    branch_id: str = ""
    parent_version_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    author_note: str = ""
    updated_at: str = ""
    assertions: dict[str, Any] = Field(default_factory=dict)
    evaluator_names: list[str] = Field(default_factory=list)
    status: str = "running"
    created_at: str = ""
    result: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = None
    completed_at: str | None = None
    reasoning: str | None = None
    pricing: dict[str, Any] = Field(default_factory=dict)
    assertion_result: dict[str, Any] = Field(default_factory=dict)
    review_verdict: str | None = None
    review_note: str | None = None
    evaluator_results: dict[str, Any] = Field(default_factory=dict)


class PromptVersionResultView(BaseModel):
    """Request body for ``PUT /prompt-versions/{id}/result``."""

    result: str = Field(..., description="The model's response text.")
    usage: dict[str, Any] = Field(
        default_factory=dict, description="Token usage breakdown."
    )
    latency_ms: int | None = Field(None, description="Wall-clock latency in ms.")
    completed_at: str = Field(default="", description="ISO timestamp of completion.")
    reasoning: str | None = None
    pricing: dict[str, Any] = Field(default_factory=dict)
    assertion_result: dict[str, Any] = Field(default_factory=dict)
    review_verdict: str | None = None
    review_note: str | None = None
    evaluator_results: dict[str, Any] = Field(default_factory=dict)


class CreatePromptVersionRequest(BaseModel):
    """Request body for ``POST /traces/{trace_id}/prompt-versions``."""

    version_id: str = Field(..., description="Client-generated unique id.")
    cursor_index: int = Field(..., ge=0, description="Step cursor this variant forks from.")
    base_messages: list[Any] = Field(default_factory=list)
    messages: list[Any] = Field(default_factory=list)
    base_model: str = ""
    model: str = ""
    branch_id: str = ""
    parent_version_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    author_note: str = ""
    updated_at: str = ""
    assertions: dict[str, Any] = Field(default_factory=dict)
    evaluator_names: list[str] = Field(default_factory=list, max_length=20)
    created_at: str = ""


class AssertionProfileView(BaseModel):
    """A reusable expected-output check set.

    Assertion profiles can be attached to multiple steps/variants so a QA
    bar (e.g. "must cite sources, no PII, under 500 tokens") is defined once
    and reused across experiments.
    """

    profile_id: str
    name: str
    required_text: list[str] = Field(default_factory=list)
    forbidden_text: list[str] = Field(default_factory=list)
    require_json: bool = False
    require_citations: bool = False
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    created_at: str = ""


class StepReviewView(BaseModel):
    """A developer review plus durable expected-output checks for a step."""

    trace_id: str
    cursor_index: int
    review_note: str | None = None
    review_verdict: str | None = Field(
        None, description='"accepted" | "rejected" | null'
    )
    assertions: dict[str, Any] = Field(default_factory=dict)
    assertion_result: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = ""


# --- Phase 5.1: execution DAG projection ----------------------------------


class DAGNodeView(BaseModel):
    """One node in the execution DAG (recursive).

    Mirrors :class:`rewind.dag.DAGNode`. ``children`` is recursive; root
    nodes have ``parent_span_id is None``.
    """

    span_id: str
    name: str
    kind: str
    status: str
    parent_span_id: str | None
    start_time: str
    children: list[DAGNodeView] = Field(default_factory=list)


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


def _register_routes(app: FastAPI) -> None:  # pylint: disable=too-many-statements
    """Wire all Phase 2/5 routes onto ``app``.

    Statement count is high (82) because each route is one block of
    handler + validation + store lookup + projection. Splitting into
    per-endpoint modules would force an artificial separation (the
    routes share :func:`_ensure_trace_exists` and the projection helpers
    below). The single-function form keeps the read surface auditable:
    one function = the entire HTTP contract."""

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

    # ------------------------------------------------------------------
    # Phase 5: branch tree, span diff, message diff, branch creation
    # ------------------------------------------------------------------
    # Endpoints below break the strict read-only contract above by one
    # method: ``POST`` to create a branch row. Branch creation is a
    # bookkeeping operation (a row in ``branches`` table); it does not
    # spawn live agent runs (``mode='frozen'`` by default). The user
    # later drives replay through the Python replay context manager.
    # ------------------------------------------------------------------

    @app.get(
        "/api/v1/traces/{trace_id}/branches",
        tags=["timeline", "branches"],
    )
    def list_branch_tree(request: Request, trace_id: str) -> BranchNodeView:
        """Return the branch tree for a trace, rooted at the original branch.

        The tree is flat in storage (``branches`` rows by ``parent_branch_id``)
        and recursively assembled here. Returns ``404`` if the trace has
        no branches (shouldn't happen — every inserted trace auto-creates a
        root branch — but defensible).
        """
        store: TraceStore = request.app.state.store
        _ensure_trace_exists(store, trace_id)
        branches = store.list_branches(trace_id)
        tree = branch_tree(branches)
        if tree is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"trace {trace_id} has no branches",
            )
        return _branch_node_view(tree)

    @app.get(
        "/api/v1/traces/{trace_id}/dag",
        tags=["timeline", "dag"],
    )
    def get_trace_dag(
        request: Request, trace_id: str
    ) -> list[DAGNodeView]:
        """Return the execution DAG (parent → children tree) for a trace.

        Built from the root-branch spans' ``parent_span_id`` pointers. The
        UI renders this as a collapsible tree showing which LLM call spawned
        which tool call. Returns a list of root nodes (spans with no parent).
        """
        store: TraceStore = request.app.state.store
        _ensure_trace_exists(store, trace_id)
        spans = store.get_spans(trace_id)
        # pylint: disable=import-outside-toplevel
        from rewind.dag import build_dag
        # pylint: enable=import-outside-toplevel
        roots = build_dag(spans)
        return [_dag_node_view(n) for n in roots]

    @app.get(
        "/api/v1/traces/{trace_id}/diff",
        tags=["timeline", "diff"],
    )
    def diff_branches(
        request: Request,
        trace_id: str,
        left: UUID = Query(..., description="Left branch id."),  # noqa: B008
        right: UUID = Query(..., description="Right branch id."),  # noqa: B008
    ) -> SpanDiffView:
        """Side-by-side span diff of two branches on the same trace.

        Both branches are loaded via :meth:`TraceStore.get_spans`, which
        transparently unions the parent prefix in (so a forked branch sees
        spans 0..branch_at_index from its parent + its own subsequent
        spans). The first divergence is identified by comparing
        ``(kind, messages_hash, tools_hash)`` — see
        :func:`rewind.diff.span_diff`.
        """
        store: TraceStore = request.app.state.store
        _ensure_trace_exists(store, trace_id)
        # Existence check first — ``get_spans`` unions the root-prefix in,
        # so a bogus branch_id would otherwise silently return the root's
        # spans (and look like an empty diff against itself).
        if not _branch_exists(store, trace_id, left):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"branch {left} not found on trace {trace_id}",
            )
        if not _branch_exists(store, trace_id, right):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"branch {right} not found on trace {trace_id}",
            )
        left_spans = store.get_spans(trace_id, branch_id=left)
        right_spans = store.get_spans(trace_id, branch_id=right)
        return _span_diff_view(
            span_diff(left_spans, right_spans),
            left_branch_id=left,
            right_branch_id=right,
        )

    @app.get(
        "/api/v1/spans/{rewind_id}/message-diff",
        tags=["timeline", "diff"],
    )
    def diff_messages(
        request: Request,
        rewind_id: UUID,
        other: UUID = Query(..., description="Span to diff against."),  # noqa: B008
    ) -> MessageDiffView:
        """Token-level diff of two LLM spans' assistant responses.

        Pulls ``gen_ai.response`` (OpenInference convention) or
        ``raw_response`` from each span's ``raw_attributes``, extracts the
        assistant message text, and runs :func:`rewind.diff.message_diff`.
        """
        store: TraceStore = request.app.state.store
        left_span = store.get_span(rewind_id)
        right_span = store.get_span(other)
        if left_span is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"span {rewind_id} not found",
            )
        if right_span is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"span {other} not found",
            )
        return _message_diff_view(
            message_diff(
                _extract_message_text(left_span),
                _extract_message_text(right_span),
            )
        )

    @app.post(
        "/api/v1/traces/{trace_id}/branches",
        tags=["timeline", "branches"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_branch(
        request: Request,
        trace_id: str,
        body: CreateBranchRequest,
    ) -> CreateBranchResponse:
        """Create a new branch row — the *\"Branch from here\"* action.

        Persists a :class:`rewind.models.Branch` row whose
        ``parent_branch_id`` defaults to the trace root when omitted.
        The branch is ``frozen`` by default (bookkeeping only); to drive
        it live, call :func:`rewind.replay.replay` from Python with the
        returned ``branch_id``.
        """
        store: TraceStore = request.app.state.store
        trace = _ensure_trace_exists(store, trace_id)
        # ``trace.root_branch_id`` is just an identifier on the trace row —
        # the actual root *branch* row may carry a different UUID (e.g. when
        # the trace was seeded via direct row inserts in tests). Resolve the
        # real root from ``list_branches`` so the new branch's parent
        # pointer is reachable in the tree.
        if body.parent_branch_id is not None:
            parent = body.parent_branch_id
        else:
            branches = store.list_branches(trace_id)
            root_branch_row = next(
                (b for b in branches if b.parent_branch_id is None),
                None,
            )
            # No root branch exists → fall back to the trace's stored
            # identifier (preserves legacy behaviour for traces seeded
            # without an explicit root branch row).
            parent = (
                trace.root_branch_id
                if root_branch_row is None
                else root_branch_row.branch_id
            )
        branch = Branch(
            trace_id=trace_id,
            parent_branch_id=parent,
            branch_at_index=body.branch_at_index,
            mode=body.mode,
            label=body.label,
        )
        store.insert_branch(branch)
        # Re-read to confirm row landed.
        branches = store.list_branches(trace_id)
        tree = branch_tree(branches)
        # Find the node we just created.
        node = _find_branch_node(tree, branch.branch_id) if tree else None
        if node is None:
            # Defensive: insert succeeded but the row isn't visible —
            # surface a 500 with enough context to debug.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"branch {branch.branch_id} inserted but not found in tree"
                ),
            )
        return CreateBranchResponse(branch=_branch_node_view(node))

    @app.get(
        "/api/v1/traces/{trace_id}/branches/{branch_id}/checkpoints",
        tags=["timeline", "checkpoints"],
    )
    def list_checkpoints(
        request: Request,
        trace_id: str,
        branch_id: UUID,
    ) -> list[CheckpointView]:
        """List every checkpoint on a branch in cursor order.

        Each row carries its full ``payload`` so the inspector can render
        captured state without a follow-up request per checkpoint.
        """
        store: TraceStore = request.app.state.store
        _ensure_trace_exists(store, trace_id)
        if not _branch_exists(store, trace_id, branch_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"branch {branch_id} not found on trace {trace_id}",
            )
        checkpoints = store.list_checkpoints(branch_id)
        return [CheckpointView(**cp.model_dump()) for cp in checkpoints]

    @app.get(
        "/api/v1/branches/{branch_id}/checkpoints/{name}",
        tags=["timeline", "checkpoints"],
    )
    def get_checkpoint(
        request: Request,
        branch_id: UUID,
        name: str,
    ) -> CheckpointView:
        """Return a single checkpoint by ``(branch_id, name)`` with full payload.

        Used by the inspector's per-checkpoint drill-down. The checkpoint
        name is URL-encoded by the caller; FastAPI decodes ``name`` before
        it reaches this handler.
        """
        store: TraceStore = request.app.state.store
        checkpoint = store.get_checkpoint(branch_id, name)
        if checkpoint is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"checkpoint '{name}' not found on branch {branch_id}",
            )
        return CheckpointView(**checkpoint.model_dump())

    # --- Phase 2.1: durable experiment records ----------------------------
    @app.post(
        "/api/v1/traces/{trace_id}/prompt-versions",
        tags=["timeline", "prompt-versions"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_prompt_version(
        request: Request,
        trace_id: str,
        body: CreatePromptVersionRequest,
    ) -> PromptVersionView:
        """Persist a new prompt-variant experiment.

        The UI calls this when the developer initiates an A/B variant from a
        reviewed step. The version is stored ``running`` until
        ``PUT .../result`` lands.
        """
        store: TraceStore = request.app.state.store
        _ensure_trace_exists(store, trace_id)
        row = {
            "version_id": body.version_id,
            "trace_id": trace_id,
            "cursor_index": body.cursor_index,
            "base_messages": body.base_messages,
            "messages": body.messages,
            "base_model": body.base_model,
            "model": body.model,
            "branch_id": body.branch_id,
            "parent_version_id": body.parent_version_id,
            "parameters": body.parameters,
            "author_note": body.author_note,
            "updated_at": body.updated_at or body.created_at,
            "assertions": body.assertions,
            "evaluator_names": body.evaluator_names,
            "created_at": body.created_at,
        }
        store.upsert_prompt_version(row)
        return PromptVersionView(
            version_id=body.version_id,
            trace_id=trace_id,
            cursor_index=body.cursor_index,
            base_messages=body.base_messages,
            messages=body.messages,
            base_model=body.base_model,
            model=body.model,
            branch_id=body.branch_id,
            parent_version_id=body.parent_version_id,
            parameters=body.parameters,
            author_note=body.author_note,
            updated_at=body.updated_at or body.created_at,
            assertions=body.assertions,
            evaluator_names=body.evaluator_names,
            status="running",
            created_at=body.created_at,
        )

    @app.get(
        "/api/v1/traces/{trace_id}/prompt-versions",
        tags=["timeline", "prompt-versions"],
    )
    def list_prompt_versions(
        request: Request,
        trace_id: str,
        cursor: int | None = Query(None, ge=0, description="Optional step cursor."),
    ) -> list[PromptVersionView]:
        """List all prompt-variant experiments for a step, hydrated with results."""
        store: TraceStore = request.app.state.store
        _ensure_trace_exists(store, trace_id)
        rows = store.list_prompt_versions(trace_id, cursor)
        return [PromptVersionView(**r) for r in rows]

    @app.put(
        "/api/v1/prompt-versions/{version_id}/result",
        tags=["timeline", "prompt-versions"],
    )
    def put_prompt_version_result(
        request: Request,
        version_id: str,
        body: PromptVersionResultView,
    ) -> PromptVersionView:
        """Persist a completed variant's result + usage, marking it ``completed``."""
        store: TraceStore = request.app.state.store
        # Re-read to confirm the version exists.
        existing = store.get_prompt_version(version_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"prompt version {version_id} not found",
            )
        store.set_prompt_version_result(
            {
                "version_id": version_id,
                "result": body.result,
                "usage": body.usage,
                "latency_ms": body.latency_ms,
                "completed_at": body.completed_at,
                "reasoning": body.reasoning,
                "pricing": body.pricing,
                "assertion_result": body.assertion_result,
                "review_verdict": body.review_verdict,
                "review_note": body.review_note,
                "evaluator_results": body.evaluator_results,
            }
        )
        refreshed = store.get_prompt_version(version_id)
        return PromptVersionView(**refreshed)  # type: ignore[arg-type]

    @app.get(
        "/api/v1/assertion-profiles",
        tags=["timeline", "assertion-profiles"],
    )
    def list_assertion_profiles(request: Request) -> list[AssertionProfileView]:
        """List all reusable assertion profiles, newest-first."""
        store: TraceStore = request.app.state.store
        return [AssertionProfileView(**r) for r in store.list_assertion_profiles()]

    @app.post(
        "/api/v1/assertion-profiles",
        tags=["timeline", "assertion-profiles"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_assertion_profile(
        request: Request,
        body: AssertionProfileView,
    ) -> AssertionProfileView:
        """Create or update a reusable assertion profile (upsert by profile_id)."""
        store: TraceStore = request.app.state.store
        store.upsert_assertion_profile(body.model_dump())
        return body

    @app.post(
        "/api/v1/traces/{trace_id}/reviews",
        tags=["timeline", "reviews"],
        status_code=status.HTTP_201_CREATED,
    )
    def upsert_review(
        request: Request,
        trace_id: str,
        body: StepReviewView,
    ) -> StepReviewView:
        """Create or update a developer review for a step (note + verdict)."""
        store: TraceStore = request.app.state.store
        _ensure_trace_exists(store, trace_id)
        if body.trace_id != trace_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="body trace_id must match the URL trace_id",
            )
        store.upsert_step_review(body.model_dump())
        return body

    @app.get(
        "/api/v1/traces/{trace_id}/reviews",
        tags=["timeline", "reviews"],
    )
    def list_reviews(
        request: Request,
        trace_id: str,
    ) -> list[StepReviewView]:
        """List all developer reviews for a trace, ordered by cursor."""
        store: TraceStore = request.app.state.store
        _ensure_trace_exists(store, trace_id)
        return [StepReviewView(**r) for r in store.list_step_reviews(trace_id)]


BranchNodeView.model_rebuild()
DAGNodeView.model_rebuild()


# --- Phase 5 helpers ------------------------------------------------------


def _ensure_trace_exists(store: TraceStore, trace_id: str) -> Trace:
    """404 if the trace isn't in the store; else return it."""
    trace = store.get_trace(trace_id)
    if trace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"trace {trace_id} not found",
        )
    return trace


def _branch_exists(
    store: TraceStore, trace_id: str, branch_id: UUID
) -> bool:
    """``True`` if ``branch_id`` is one of ``trace_id``'s branches."""
    return any(
        b.branch_id == branch_id for b in store.list_branches(trace_id)
    )


def _branch_node_view(node: BranchNode) -> BranchNodeView:
    """Recursively project a :class:`BranchNode` into the wire shape."""
    return BranchNodeView(
        branch_id=node.branch_id,
        trace_id=node.trace_id,
        parent_branch_id=node.parent_branch_id,
        branch_at_index=node.branch_at_index,
        mode=node.mode,
        label=node.label,
        created_at=node.created_at,
        children=[_branch_node_view(child) for child in node.children],
    )


def _dag_node_view(node: Any) -> Any:  # noqa: ANN401
    """Recursively project a :class:`rewind.dag.DAGNode` into the wire shape.

    Typed ``Any`` to avoid importing :class:`DAGNode` at module top (the dag
    module is tiny and the projection is structural). The runtime shape is
    guaranteed by :func:`rewind.dag.build_dag`.
    """
    cls = DAGNodeView
    return cls(
        span_id=node.span_id,
        name=node.name,
        kind=node.kind,
        status=node.status,
        parent_span_id=node.parent_span_id,
        start_time=node.start_time,
        children=[_dag_node_view(c) for c in node.children],
    )


def _find_branch_node(
    node: BranchNode | None, branch_id: UUID
) -> BranchNode | None:
    """DFS the branch tree for ``branch_id``; returns the node or ``None``."""
    if node is None:
        return None
    if node.branch_id == branch_id:
        return node
    for child in node.children:
        found = _find_branch_node(child, branch_id)
        if found is not None:
            return found
    return None


def _span_diff_view(
    diff: SpanDiff,
    *,
    left_branch_id: UUID,
    right_branch_id: UUID,
) -> SpanDiffView:
    """Project a :class:`SpanDiff` into the wire shape."""
    pairs: list[SpanPairView] = []
    for pair in diff.pairs:
        pairs.append(
            SpanPairView(
                index=pair.index,
                left=(
                    _span_view(pair.left, left_branch_id)
                    if pair.left is not None
                    else None
                ),
                right=(
                    _span_view(pair.right, right_branch_id)
                    if pair.right is not None
                    else None
                ),
                status=pair.status,
                is_first_divergence=pair.is_first_divergence,
            )
        )
    return SpanDiffView(
        pairs=pairs,
        first_divergence_index=diff.first_divergence_index,
        left_count=diff.left_count,
        right_count=diff.right_count,
        identical=diff.identical,
    )


def _message_diff_view(diff: MessageDiff) -> MessageDiffView:
    """Project a :class:`MessageDiff` into the wire shape."""
    return MessageDiffView(
        left=diff.left,
        right=diff.right,
        fragments=[
            MessageFragmentView(text=f.text, kind=f.kind) for f in diff.fragments
        ],
        added_tokens=diff.added_tokens,
        removed_tokens=diff.removed_tokens,
        identical=diff.identical,
    )


def _extract_message_text(span: Span) -> str:
    """Pull the assistant message text out of a span's ``raw_attributes``.

    Order of preference (mirrors ``openai_intercept._materialise_chat_completion``):

    1. ``gen_ai.response.choices[0].message.content`` (OpenInference).
    2. ``raw_response.choices[0].message.content`` (older exporters).
    3. ``gen_ai.completion`` string (some SDKs flatten the whole choice).
    4. Empty string (privacy-skinned exporter — diff is a no-op).

    The function never raises; a missing/odd payload becomes "" so the
    diff endpoint degrades gracefully rather than 500-ing.
    """
    attrs = span.raw_attributes or {}
    for key in ("gen_ai.response", "raw_response", "response"):
        content = _extract_choice_content(attrs.get(key))
        if content is not None:
            return content
    # Some exporters flatten the completion to a string under gen_ai.completion.
    completion = attrs.get("gen_ai.completion")
    if isinstance(completion, str):
        return completion
    return ""


def _extract_choice_content(payload: object) -> str | None:
    """Drill into ``payload.choices[0].message.content`` if the shape fits.

    Returns ``None`` for any shape that doesn't match so the caller can
    fall through to the next candidate key. Kept as a helper so the
    outer extractor stays flat (pylint ``too-many-nested-blocks``).
    """
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    return content if isinstance(content, str) else None


__all__ = [
    "AssertionProfileView",
    "BranchNodeView",
    "CheckpointView",
    "CreateBranchRequest",
    "CreateBranchResponse",
    "CreatePromptVersionRequest",
    "DAGNodeView",
    "MessageDiffView",
    "MessageFragmentView",
    "PromptVersionResultView",
    "PromptVersionView",
    "SearchResponse",
    "SpanDiffView",
    "SpanPairView",
    "SpanSearchHit",
    "SpanView",
    "StepReviewView",
    "TraceDetail",
    "TraceListResponse",
    "TraceSummary",
    "mount_timeline",
]
