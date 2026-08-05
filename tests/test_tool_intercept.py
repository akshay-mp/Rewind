"""Phase 3 Track 3B.3 — unit tests for the Rewind tool-call interceptor.

Strategy
--------
We exercise the public ``@rewind.tool()`` decorator with a small seeded
span tree whose ``gen_ai.tool.input_hash`` is computed using the very
same normaliser the dispatcher uses (``_tool_args_hash``). This keeps
the tests stable under future changes to the JSON canonicalisation.

Cases covered
-------------
* No active replay session → wrapper is transparent.
* FROZEN + matching span at cursor → cached output returned, cursor advances.
* FROZEN + mismatched args → :class:`ToolCacheMiss`.
* FROZEN + cursor exhausted → :class:`ToolCacheMiss`.
* BRANCH + mismatch → live function called once + new span captured.
* ``kind`` parameter selects between TOOL and MCP spans.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rewind import tool as rewind_tool
from rewind.enums import ReplayMode, SpanKind, SpanStatus
from rewind.models import Span, Trace
from rewind.replay import active_session
from rewind.replay import replay as replay_ctx
from rewind.storage import TraceStore
from rewind.tool_intercept import ToolCacheMiss, _tool_args_hash


# ----------------------------------------------------------------------
# Helpers / fixtures
# ----------------------------------------------------------------------
def _tool_span(
    trace_id: str,
    *,
    span_id: str,
    name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any] | None = None,
    output: Any = "[result]",
    kind: SpanKind = SpanKind.TOOL,
) -> Span:
    """Build a tool span with a content-addressable input hash."""
    kwargs = kwargs or {}
    args_hash = _tool_args_hash(args, kwargs)
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        name=name,
        kind=kind,
        status=SpanStatus.OK,
        raw_attributes={
            "gen_ai.tool.name": name,
            "gen_ai.tool.input": {"args": list(args), "kwargs": dict(kwargs)},
            "gen_ai.tool.input_hash": args_hash,
            "gen_ai.tool.output": output,
        },
    )


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    return TraceStore(str(tmp_path / "rewind.db"))


@pytest.fixture
def trace_id() -> str:
    return "abcd1234abcd1234abcd1234abcd1234"


@pytest.fixture
def seeded_tool_trace(
    store: TraceStore, trace_id: str
) -> tuple[TraceStore, Span, tuple[str]]:
    """Seed a trace with exactly one ``search`` tool span at cursor 0."""
    args = ("price of AAPL",)
    span = _tool_span(
        trace_id,
        span_id="t" * 16,
        name="search",
        args=args,
        output=[{"symbol": "AAPL", "price": 195.42}],
    )
    store.upsert_trace(Trace(trace_id=trace_id, spans=[span]))
    store.insert_span(span)
    return store, span, args


# ----------------------------------------------------------------------
# No active replay → transparent passthrough
# ----------------------------------------------------------------------
def test_tool_decorator_passthrough_without_session() -> None:
    """Without ``replay()`` active, the wrapper behaves like the function."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @rewind_tool(name="search")
    def search(query: str, *, limit: int = 5) -> list[str]:
        calls.append(((query,), {"limit": limit}))
        return [f"hit-{query}-{limit}"]

    assert active_session() is None
    result = search("hello", limit=2)
    assert result == ["hit-hello-2"]
    assert calls == [(("hello",), {"limit": 2})]


# ----------------------------------------------------------------------
# FROZEN cache hit
# ----------------------------------------------------------------------
def test_frozen_serves_cached_tool_output_and_advances_cursor(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """A matching span returns the recorded output; cursor advances past it."""
    store, span, args = seeded_tool_trace

    @rewind_tool(name="search")
    def search(query: str) -> list[dict[str, float]]:
        raise AssertionError("live search must not run in frozen mode")

    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN) as session:
        assert session.cursor == 0
        result = search(*args)
        assert result == [{"symbol": "AAPL", "price": 195.42}]
        # Cursor advanced past the consumed tool span.
        assert session.cursor == 1
        assert span.span_id in {s.span_id for s in session.recorded_spans()}


# ----------------------------------------------------------------------
# FROZEN cache miss (different args)
# ----------------------------------------------------------------------
def test_frozen_raises_toolcachemiss_on_args_mismatch(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """Different tool args in FROZEN mode raise ToolCacheMiss."""
    store, _span, _args = seeded_tool_trace

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("should not be called")

    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN), \
            pytest.raises(ToolCacheMiss, match="search"):
        search("different query entirely")


def test_frozen_raises_toolcachemiss_when_cursor_exhausted(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """After the only span is consumed, a second call is a miss."""
    store, _span, args = seeded_tool_trace

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("should not be called")

    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN) as session:
        search(*args)  # consumes the one recorded span
        assert session.cursor == 1
        with pytest.raises(ToolCacheMiss, match="search"):
            search(*args)  # cursor exhausted


def test_frozen_raises_toolcachemiss_on_name_mismatch(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """A different tool name in FROZEN mode doesn't match the cached span."""
    store, _span, args = seeded_tool_trace

    @rewind_tool(name="calculate")
    def calculate(expr: str) -> float:
        raise AssertionError("should not be called")

    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN), \
            pytest.raises(ToolCacheMiss, match="calculate"):
        calculate(*args)


# ----------------------------------------------------------------------
# BRANCH forward + capture
# ----------------------------------------------------------------------
def test_branch_calls_live_and_records_new_span(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """In BRANCH mode a cache miss forwards live and records a TOOL span."""
    store, _span, args = seeded_tool_trace

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        return [f"live-{query}"]

    with replay_ctx(store, trace_id, mode=ReplayMode.BRANCH) as session:
        # Different args → miss → forward live.
        result = search("different query")
        assert result == ["live-different query"]
        # New span captured; cursor moves to len(recorded_spans).
        assert session.cursor == len(session.recorded_spans())
        branch_spans = store.get_spans(trace_id, branch_id=session.branch_id)
        new_tools = [
            s
            for s in branch_spans
            if s.name == "search"
            and s.raw_attributes.get("gen_ai.tool.input_hash")
            != _tool_args_hash(args, {})
        ]
        assert len(new_tools) == 1
        assert new_tools[0].raw_attributes["gen_ai.tool.output"] == [
            "live-different query"
        ]


def test_branch_serves_cached_on_match_then_forwards_on_next_call(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """BRANCH mode still prefers cache; only misses forward live."""
    store, _span, args = seeded_tool_trace

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        return [f"live-{query}"]

    with replay_ctx(store, trace_id, mode=ReplayMode.BRANCH) as session:
        # First call matches the recorded args → served from cache.
        # The cached output is the recorded payload (Any), so cast for mypy.
        cached: Any = search(*args)
        assert cached == [{"symbol": "AAPL", "price": 195.42}]
        assert session.cursor == 1
        # Second call misses → live.
        live = search("next query")
        assert live == ["live-next query"]


# ----------------------------------------------------------------------
# kind selection (TOOL vs MCP)
# ----------------------------------------------------------------------
def test_kind_parameter_distinguishes_tool_from_mcp(
    store: TraceStore,
    trace_id: str,
) -> None:
    """``kind`` filters the span lookup, so a TOOL span doesn't satisfy MCP."""
    args = ("/etc/hosts",)
    # Seed an MCP span (not TOOL).
    mcp_span = _tool_span(
        trace_id,
        span_id="m" * 16,
        name="read_file",
        args=args,
        output="# hosts file",
        kind=SpanKind.MCP,
    )
    store.upsert_trace(Trace(trace_id=trace_id, spans=[mcp_span]))
    store.insert_span(mcp_span)

    @rewind_tool(name="read_file", kind=SpanKind.MCP)
    def read_file(path: str) -> str:
        raise AssertionError("live MCP call must not run in frozen replay")

    @rewind_tool(name="read_file")  # default kind = TOOL
    def read_file_tool(path: str) -> str:
        raise AssertionError("live tool call must not run in frozen replay")

    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN):
        # MCP-typed wrapper hits the MCP span.
        assert read_file(*args) == "# hosts file"
    # Cursor rewound by a fresh session; a TOOL-typed wrapper now misses.
    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN), \
            pytest.raises(ToolCacheMiss, match="read_file"):
        read_file_tool(*args)


# ----------------------------------------------------------------------
# Phase 9 — INTERACTIVE tool stepping
#
# The sync @rewind.tool() path is the only genuinely sync-only interception
# surface. These tests drive the ThreadBridgeChannel with a background
# approver thread so the main thread's blocking gate_sync call resolves.
# ----------------------------------------------------------------------
import threading  # noqa: E402

from rewind.stepping import (  # noqa: E402
    Decision,
    DecisionKind,
    SteppingStopped,
    ThreadBridgeChannel,
)


def _start_approver(channel: ThreadBridgeChannel, decision: Decision) -> threading.Thread:
    """Start a daemon thread that resolves the next paused step with ``decision``.

    Polls ``channel.take_step()`` until a step appears, then publishes the
    decision. Returns the thread so the test can join it.
    """

    def approve() -> None:
        while channel.take_step() is None:
            continue
        channel.decide(decision)

    t = threading.Thread(target=approve, daemon=True)
    t.start()
    return t


def test_interactive_approve_proceeds_with_tool_call(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """APPROVE at the gate lets the tool call proceed normally."""
    store, _span, args = seeded_tool_trace
    channel = ThreadBridgeChannel()

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        return [f"live-{query}"]

    with replay_ctx(
        store, trace_id, mode=ReplayMode.INTERACTIVE, approval=channel
    ) as session:
        # The recorded span matches the call args, so after APPROVE the
        # cache lookup hits and the cached output is returned — the live
        # function body is never reached.
        t = _start_approver(channel, Decision(kind=DecisionKind.APPROVE))
        result = search(*args)
        t.join(timeout=2)
        assert result == [{"symbol": "AAPL", "price": 195.42}]
        assert session.cursor == 1


def test_interactive_edit_rewrites_tool_args(
    store: TraceStore,
    trace_id: str,
) -> None:
    """An EDIT decision replaces the tool args before the cache lookup.

    Seeded span records ``("AAPL",)``; the developer edits to ``("MSFT",)``.
    The edited args_hash won't match the recorded span, so the call falls
    through to live-forward + capture under the session's branch.
    """
    # Seed a span for "AAPL".
    aapl_args = ("AAPL",)
    span = _tool_span(
        trace_id,
        span_id="a" * 16,
        name="search",
        args=aapl_args,
        output=[{"symbol": "AAPL", "price": 195.42}],
    )
    store.upsert_trace(Trace(trace_id=trace_id, spans=[span]))
    store.insert_span(span)

    channel = ThreadBridgeChannel()

    @rewind_tool(name="search")
    def search(query: str) -> list[dict[str, str]]:
        return [{"symbol": query, "price": "live"}]

    with replay_ctx(
        store, trace_id, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        # Edit the call to "MSFT" — diverges from the recorded "AAPL" span.
        t = _start_approver(
            channel,
            Decision(kind=DecisionKind.EDIT, args=("MSFT",)),
        )
        result = search(*aapl_args)  # original args passed by the agent
        t.join(timeout=2)
        # The edited args reached the live function, not the cached ones.
        assert result == [{"symbol": "MSFT", "price": "live"}]


def test_interactive_stop_raises_stepping_stopped(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """STOP at the gate raises SteppingStopped; the live body is never called."""
    store, _span, args = seeded_tool_trace
    channel = ThreadBridgeChannel()

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("live search must not run on STOP")

    with replay_ctx(
        store, trace_id, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        t = _start_approver(channel, Decision(kind=DecisionKind.STOP))
        with pytest.raises(SteppingStopped):
            search(*args)
        t.join(timeout=2)


def test_interactive_mock_result_never_calls_live_tool(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """MOCK resolves the tool without touching the wrapped function."""
    store, _span, args = seeded_tool_trace
    channel = ThreadBridgeChannel()

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("mocked tool must not run")

    def resolve() -> None:
        while channel.take_step() is None:
            continue
        channel.decide(Decision(kind=DecisionKind.MOCK, mock_result={"items": []}))

    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    with replay_ctx(store, trace_id, mode=ReplayMode.INTERACTIVE, approval=channel):
        assert search(*args) == {"items": []}
    t.join(timeout=2)


def test_interactive_skip_returns_structured_result(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """SKIP produces an inspectable result and never calls the live tool."""
    store, _span, args = seeded_tool_trace
    channel = ThreadBridgeChannel()

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("skipped tool must not run")

    def resolve() -> None:
        while channel.take_step() is None:
            continue
        channel.decide(Decision(kind=DecisionKind.SKIP))

    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    with replay_ctx(store, trace_id, mode=ReplayMode.INTERACTIVE, approval=channel):
        assert search(*args) == {"rewind": "tool skipped", "tool": "search"}
    t.join(timeout=2)


def test_interactive_reject_returns_structured_reject(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """REJECT (Phase 1.4) veto's the tool call and returns a structured result.

    The live tool body must NOT run — REJECT is a hard veto resolved before
    the cache lookup, like MOCK/SKIP. The optional ``reason`` is surfaced
    back to the agent so it can react to the refusal.
    """
    store, _span, args = seeded_tool_trace
    channel = ThreadBridgeChannel()

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("rejected tool must not run")

    def resolve() -> None:
        while channel.take_step() is None:
            continue
        channel.decide(Decision(kind=DecisionKind.REJECT, reason="vetoed by developer"))

    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    with replay_ctx(store, trace_id, mode=ReplayMode.INTERACTIVE, approval=channel):
        result = search(*args)
    t.join(timeout=2)

    assert result == {
        "rewind": "tool rejected",
        "tool": "search",
        "reason": "vetoed by developer",
    }


def test_interactive_reject_without_reason_uses_default(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """A REJECT with no ``reason`` surfaces a default message."""
    store, _span, args = seeded_tool_trace
    channel = ThreadBridgeChannel()

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("rejected tool must not run")

    def resolve() -> None:
        while channel.take_step() is None:
            continue
        channel.decide(Decision(kind=DecisionKind.REJECT))

    t = threading.Thread(target=resolve, daemon=True)
    t.start()
    with replay_ctx(store, trace_id, mode=ReplayMode.INTERACTIVE, approval=channel):
        result = search(*args)
    t.join(timeout=2)

    assert result["rewind"] == "tool rejected"
    assert result["reason"] == "rejected by developer"


def test_interactive_no_channel_falls_through_to_cache(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """INTERACTIVE without a channel behaves like BRANCH — no-op gate."""
    store, _span, args = seeded_tool_trace

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("cached hit should not reach the live body")

    # No approval= kwarg → session.approval is None → gate returns None.
    with replay_ctx(store, trace_id, mode=ReplayMode.INTERACTIVE):
        result = search(*args)
        assert result == [{"symbol": "AAPL", "price": 195.42}]


def test_interactive_async_only_channel_raises_stepping_stopped(
    seeded_tool_trace: tuple[TraceStore, Span, tuple[str]],
    trace_id: str,
) -> None:
    """A sync tool stepped with an async-only channel surfaces a clear error.

    ``gate_sync`` duck-types for ``submit_sync``; an AsyncioChannel lacks it,
    so the gate raises SteppingStopped with an actionable hint rather than
    silently skipping the pause (which would hide the contract violation).
    """
    from rewind.stepping import AsyncioChannel

    store, _span, args = seeded_tool_trace
    channel = AsyncioChannel()

    @rewind_tool(name="search")
    def search(query: str) -> list[str]:
        raise AssertionError("should not be reached")

    with replay_ctx(
        store, trace_id, mode=ReplayMode.INTERACTIVE, approval=channel
    ), pytest.raises(SteppingStopped) as exc_info:
        search(*args)
    # The actionable hint rides on __cause__ (gate_sync raises `from RuntimeError`).
    cause = exc_info.value.__cause__
    assert isinstance(cause, RuntimeError)
    assert "ThreadBridgeChannel" in str(cause)
