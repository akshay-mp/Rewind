"""Phase 1.5 integration tests — workbench decision flows end-to-end.

Exercises the stepping server's decision kinds through the real
interception layer (``@timetravel.tool`` + the OpenAI monkey-patch) so the
"zero external calls" / "exactly once" contracts are pinned at the
integration level, not just the unit level.

Flows covered (per ``docs/implementation_plan.md`` §1.5):

* **mock**   — MOCK a tool call: ``mock_result`` delivered, no live call.
* **skip**   — SKIP a tool call: structured skip result, no live call.
* **reject** — REJECT a tool call: structured reject result, no live call.
* **retry**  — a live-forward divergence (EDIT) re-invokes the tool once.
* **timetravel** — STEP_ONCE on a tool advances exactly one step with no live
  call when the recorded span matches.
* **forward** — APPROVE on a tool serves the cached output, no live call.

Tool stepping is the genuinely sync interception surface; it uses
:class:`~agent_timetravel.stepping.ThreadBridgeChannel` with a background approver
thread (mirrors ``tests/test_tool_intercept.py``). The LLM stepping path
is async-only and is covered by ``test_stepping.py`` at the unit level
and ``test_openai_intercept.py``; here we focus on the full
store → replay → tool → decision → capture round trip.

Marked ``integration`` so the default ``pytest`` run skips them; run with
``pytest -m integration``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_timetravel import tool as timetravel_tool
from agent_timetravel.enums import ReplayMode, SpanKind, SpanStatus
from agent_timetravel.models import Span, Trace
from agent_timetravel.replay import replay as replay_ctx
from agent_timetravel.stepping import (
    Decision,
    DecisionKind,
    ThreadBridgeChannel,
)
from agent_timetravel.storage import TraceStore
from agent_timetravel.tool_intercept import _tool_args_hash

pytestmark = pytest.mark.integration

_TRACE_ID = "f1e2d3c4b5a6978869584738291a2b3c"


# --- seed trace ------------------------------------------------------------


def _tool_span(
    *, span_id: str, name: str, args: tuple[Any, ...], output: Any
) -> Span:
    """Build a tool span with a content-addressable input hash."""
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


def _seed_tool_trace(store: TraceStore) -> tuple[TraceStore, tuple[str]]:
    """Seed a trace with exactly one ``get_weather`` tool span at cursor 0."""
    args = ("Paris",)
    span = _tool_span(
        span_id="t" * 16,
        name="get_weather",
        args=args,
        output=[{"city": "Paris", "temp_c": 18}],
    )
    store.upsert_trace(Trace(trace_id=_TRACE_ID, spans=[span]))
    store.insert_span(span)
    return store, args


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    s = TraceStore(str(tmp_path / "workbench_flows.db"))
    return s


@pytest.fixture
def seeded(store: TraceStore) -> tuple[TraceStore, tuple[str]]:
    return _seed_tool_trace(store)


# --- approver helper -------------------------------------------------------


@contextmanager
def _approver_thread(
    channel: ThreadBridgeChannel, decisions: list[Decision]
) -> Iterator[threading.Thread]:
    """Start a daemon thread that feeds ``decisions`` to paused steps in order.

    Polls ``channel.take_step()`` until each step appears, then publishes the
    next decision. Yields the thread so the test can join it after the call.
    """
    iter_decisions = iter(decisions)

    def approve() -> None:
        for _ in range(len(decisions)):
            while channel.take_step() is None:
                continue
            channel.decide(next(iter_decisions))

    t = threading.Thread(target=approve, daemon=True)
    t.start()
    try:
        yield t
    finally:
        t.join(timeout=5)


# --- the flows -------------------------------------------------------------


def test_mock_delivers_result_no_live_call(
    seeded: tuple[TraceStore, tuple[str]],
) -> None:
    """MOCK returns ``mock_result`` without invoking the live tool."""
    store, args = seeded
    channel = ThreadBridgeChannel()

    @timetravel_tool(name="get_weather")
    def get_weather(city: str) -> list[dict[str, Any]]:
        raise AssertionError("mocked tool must not run")

    with _approver_thread(
        channel,
        [Decision(kind=DecisionKind.MOCK, mock_result={"city": "Paris", "mocked": True})],
    ), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        result = get_weather(*args)

    assert result == {"city": "Paris", "mocked": True}


def test_skip_makes_no_live_call(seeded: tuple[TraceStore, tuple[str]]) -> None:
    """SKIP returns a structured skip result without invoking the live tool."""
    store, args = seeded
    channel = ThreadBridgeChannel()

    @timetravel_tool(name="get_weather")
    def get_weather(city: str) -> list[dict[str, Any]]:
        raise AssertionError("skipped tool must not run")

    with _approver_thread(channel, [Decision(kind=DecisionKind.SKIP)]), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        result = get_weather(*args)

    assert result == {"timetravel": "tool skipped", "tool": "get_weather"}


def test_reject_makes_no_live_call(seeded: tuple[TraceStore, tuple[str]]) -> None:
    """REJECT returns a structured reject result without invoking the live tool."""
    store, args = seeded
    channel = ThreadBridgeChannel()

    @timetravel_tool(name="get_weather")
    def get_weather(city: str) -> list[dict[str, Any]]:
        raise AssertionError("rejected tool must not run")

    with _approver_thread(
        channel, [Decision(kind=DecisionKind.REJECT, reason="vetoed")]
    ), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        result = get_weather(*args)

    assert result == {
        "timetravel": "tool rejected",
        "tool": "get_weather",
        "reason": "vetoed",
    }


def test_retry_live_call_exactly_once(seeded: tuple[TraceStore, tuple[str]]) -> None:
    """EDIT to divergent args forces exactly one live call.

    The recorded span is for ``("Paris",)``; the developer edits to
    ``("Tokyo",)``. The edited args_hash misses the cache, so the wrapped
    function runs once and the new span is captured under the branch.
    """
    store, _args = seeded
    channel = ThreadBridgeChannel()
    live_calls: list[str] = []

    @timetravel_tool(name="get_weather")
    def get_weather(city: str) -> list[dict[str, Any]]:
        live_calls.append(city)
        return [{"city": city, "temp_c": 42, "live": True}]

    with _approver_thread(
        channel, [Decision(kind=DecisionKind.EDIT, args=["Tokyo"])]
    ), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        result = get_weather("Paris")  # args rewritten to "Tokyo" by gate

    assert live_calls == ["Tokyo"], "live tool must be called exactly once with edited args"
    assert result == [{"city": "Tokyo", "temp_c": 42, "live": True}]


def test_timetravel_step_once_no_live_call(seeded: tuple[TraceStore, tuple[str]]) -> None:
    """STEP_ONCE advances exactly one step with zero live calls.

    The recorded span matches the call args; after STEP_ONCE the cache hit
    is served and the live body never runs. STEP_ONCE then disarms to
    BRANCH, but the run ends here.
    """
    store, args = seeded
    channel = ThreadBridgeChannel()

    @timetravel_tool(name="get_weather")
    def get_weather(city: str) -> list[dict[str, Any]]:
        raise AssertionError("step_once on a cache hit must not run the live tool")

    with _approver_thread(channel, [Decision(kind=DecisionKind.STEP_ONCE)]), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        result = get_weather(*args)

    assert result == [{"city": "Paris", "temp_c": 18}]


def test_forward_approve_no_live_call(seeded: tuple[TraceStore, tuple[str]]) -> None:
    """APPROVE on a matching recorded span serves the cached output: no live call."""
    store, args = seeded
    channel = ThreadBridgeChannel()

    @timetravel_tool(name="get_weather")
    def get_weather(city: str) -> list[dict[str, Any]]:
        raise AssertionError("approved cache hit must not run the live tool")

    with _approver_thread(channel, [Decision(kind=DecisionKind.APPROVE)]), replay_ctx(
        store, _TRACE_ID, mode=ReplayMode.INTERACTIVE, approval=channel
    ):
        result = get_weather(*args)

    assert result == [{"city": "Paris", "temp_c": 18}]
