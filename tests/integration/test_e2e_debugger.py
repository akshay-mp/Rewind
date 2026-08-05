"""Phase 5.5 — end-to-end debugger flow integration tests.

Exercises the full stepping-server + interceptor stack through the
decision kinds a developer drives from the workbench UI. These complement
``test_workbench_flows.py`` (which pins the tool-interceptor contracts) by
walking the high-level approve/stop/mock/skip/restart-from UX.

Marked ``integration`` so the default ``pytest`` run skips them.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from rewind import tool as rewind_tool
from rewind.enums import ReplayMode, SpanKind, SpanStatus
from rewind.models import Span, Trace
from rewind.replay import replay as replay_ctx
from rewind.stepping import Decision, DecisionKind, ThreadBridgeChannel
from rewind.storage import TraceStore
from rewind.tool_intercept import _tool_args_hash

pytestmark = pytest.mark.integration

_TRACE_ID = "abcdef1234567890abcdef1234567890"


def _tool_span(*, span_id: str, name: str, args: tuple[Any, ...], output: Any) -> Span:
    return Span(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        name=name,
        kind=SpanKind.TOOL,
        status=SpanStatus.OK,
        raw_attributes={
            "gen_ai.tool.name": name,
            "gen_ai.tool.input": {"args": list(args), "kwargs": {}},
            "gen_ai.tool.input_hash": _tool_args_hash(args, {}),
            "gen_ai.tool.output": output,
        },
    )


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    s = TraceStore(str(tmp_path / "e2e_debugger.db"))
    span = _tool_span(
        span_id="t" * 16,
        name="search",
        args=("query",),
        output={"result": "ok"},
    )
    s.upsert_trace(Trace(trace_id=_TRACE_ID, spans=[span]))
    s.insert_span(span)
    return s


@contextmanager
def _approver(
    channel: ThreadBridgeChannel, decisions: list[Decision]
) -> Iterator[threading.Thread]:
    """Feed ``decisions`` to paused steps in order."""
    iter_d = iter(decisions)

    def approve() -> None:
        for _ in range(len(decisions)):
            while channel.take_step() is None:
                continue
            channel.decide(next(iter_d))

    t = threading.Thread(target=approve, daemon=True)
    t.start()
    try:
        yield t
    finally:
        t.join(timeout=5)


# --- flows -----------------------------------------------------------------


def test_approve_then_complete_flow(store: TraceStore) -> None:
    """The canonical happy path: pause → approve → cached output served."""
    channel = ThreadBridgeChannel()
    args = ("query",)

    @rewind_tool(name="search")
    def search(query: str) -> dict[str, str]:
        raise AssertionError("cache hit should not run live")

    with _approver(channel, [Decision(kind=DecisionKind.APPROVE)]), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        result = search(*args)
    assert result == {"result": "ok"}


def test_stop_terminates_flow(store: TraceStore) -> None:
    """STOP at the gate raises SteppingStopped (normal termination)."""
    from rewind.stepping import SteppingStopped

    channel = ThreadBridgeChannel()
    args = ("query",)

    @rewind_tool(name="search")
    def search(query: str) -> dict[str, str]:
        raise AssertionError("should not run on STOP")

    with _approver(channel, [Decision(kind=DecisionKind.STOP)]), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ), pytest.raises(SteppingStopped):
        search(*args)


def test_mock_flow(store: TraceStore) -> None:
    """MOCK substitutes a result without the live call."""
    channel = ThreadBridgeChannel()
    args = ("query",)

    @rewind_tool(name="search")
    def search(query: str) -> dict[str, str]:
        raise AssertionError("mock should not run live")

    with _approver(
        channel,
        [Decision(kind=DecisionKind.MOCK, mock_result={"mocked": True})],
    ), replay_ctx(store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel):
        result = search(*args)
    assert result == {"mocked": True}


def test_skip_flow(store: TraceStore) -> None:
    """SKIP returns a structured skip sentinel."""
    channel = ThreadBridgeChannel()
    args = ("query",)

    @rewind_tool(name="search")
    def search(query: str) -> dict[str, str]:
        raise AssertionError("skip should not run live")

    with _approver(channel, [Decision(kind=DecisionKind.SKIP)]), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        result = search(*args)
    assert result["rewind"] == "tool skipped"


def test_reject_flow(store: TraceStore) -> None:
    """REJECT returns a structured reject sentinel with the reason."""
    channel = ThreadBridgeChannel()
    args = ("query",)

    @rewind_tool(name="search")
    def search(query: str) -> dict[str, str]:
        raise AssertionError("reject should not run live")

    with _approver(
        channel, [Decision(kind=DecisionKind.REJECT, reason="unsafe")]
    ), replay_ctx(store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel):
        result = search(*args)
    assert result["rewind"] == "tool rejected"
    assert result["reason"] == "unsafe"


def test_restart_from_edit_flow(store: TraceStore) -> None:
    """EDIT to divergent args forces a live-forward (the "restart from N" UX)."""
    channel = ThreadBridgeChannel()
    live_calls: list[str] = []

    @rewind_tool(name="search")
    def search(query: str) -> dict[str, str]:
        live_calls.append(query)
        return {"result": "live", "query": query}

    with _approver(
        channel, [Decision(kind=DecisionKind.EDIT, args=["different"])]
    ), replay_ctx(store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel):
        result = search("query")  # rewritten to "different"
    assert live_calls == ["different"]
    assert result == {"result": "live", "query": "different"}
