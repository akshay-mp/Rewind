"""Unit tests for the Phase A interactive stepping primitive.

Covers:

* :class:`AsyncioChannel` — push/decide mechanics.
* :func:`gate_async` returns ``None`` for non-INTERACTIVE modes and when no
  channel is attached (the zero-regression invariant for FROZEN/BRANCH/FULL).
* APPROVE / EDIT / STOP / STEP_ONCE decisions behave as specified.
* :func:`decide_with_validation` rejects self-inconsistent decisions.
* End-to-end: an async OpenAI-intercept dispatch pauses at the gate and is
  driven by a scripted channel.
* :class:`ThreadBridgeChannel` sync→async handoff for the tool path.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from rewind.enums import ReplayMode, SpanKind, SpanStatus
from rewind.models import Span, Trace, hash_payload
from rewind.replay import ReplaySession
from rewind.replay import replay as replay_ctx
from rewind.stepping import (
    AsyncioChannel,
    Decision,
    DecisionKind,
    Step,
    StepKind,
    SteppingStopped,
    ThreadBridgeChannel,
    decide_with_validation,
    gate_async,
    gate_sync,
)
from rewind.storage import TraceStore


# ----------------------------------------------------------------------
# Fixtures — mirror test_replay.py shapes
# ----------------------------------------------------------------------
def _llm_span(
    trace_id: str,
    *,
    span_id: str,
    messages: list[dict[str, str]],
    model: str = "qwen3:32b",
    response_content: str = "hello",
) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        name="chat.completions.create",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name=model,
        prompt_tokens=10,
        completion_tokens=2,
        total_tokens=12,
        messages_hash=hash_payload(messages),
        raw_attributes={
            "gen_ai.request.model": model,
            "gen_ai.response.model": model,
            "gen_ai.response": {
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": response_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        },
    )


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    return TraceStore(str(tmp_path / "stepping.db"))


@pytest.fixture
def trace_id() -> str:
    return "abcd1234abcd1234abcd1234abcd1234"


@pytest.fixture
def seeded_session(
    store: TraceStore, trace_id: str
) -> ReplaySession:
    """A FROZEN session over a 2-LLM-span trace, no channel attached."""
    msgs_a = [{"role": "user", "content": "hello"}]
    msgs_b = [{"role": "user", "content": "follow up"}]
    spans = [
        _llm_span(trace_id, span_id="a" * 16, messages=msgs_a, response_content="hi"),
        _llm_span(trace_id, span_id="b" * 16, messages=msgs_b, response_content="bye"),
    ]
    trace = Trace(trace_id=trace_id, spans=spans)
    store.upsert_trace(trace)
    for s in spans:
        store.insert_span(s)
    return ReplaySession.for_root(store, trace_id, mode=ReplayMode.FROZEN)


def _step(cursor: int = 0) -> Step:
    return Step(
        kind=StepKind.LLM,
        payload={"model": "m", "messages": [], "params": {}},
        cursor=cursor,
    )


# ----------------------------------------------------------------------
# AsyncioChannel mechanics
# ----------------------------------------------------------------------
async def test_asyncio_channel_push_and_decide() -> None:
    """Channel round-trip: push a step, an approver resolves it."""
    channel = AsyncioChannel()

    async def approver() -> Decision:
        step = await channel.next_step()
        assert step.kind is StepKind.LLM
        channel.decide(Decision(kind=DecisionKind.APPROVE))
        return Decision(kind=DecisionKind.APPROVE)

    approver_task = asyncio.create_task(approver())
    decision = await channel.submit(_step())
    await approver_task
    assert decision.kind is DecisionKind.APPROVE


# ----------------------------------------------------------------------
# gate_async — the zero-regression invariant
# ----------------------------------------------------------------------
async def test_gate_async_noop_when_frozen(seeded_session: ReplaySession) -> None:
    """FROZEN mode with no channel → gate returns None (proceed as today)."""
    decision = await gate_async(seeded_session, _step())
    assert decision is None


async def test_gate_async_noop_when_branch_no_channel(
    seeded_session: ReplaySession,
) -> None:
    """BRANCH mode with no channel → gate returns None."""
    seeded_session.mode = ReplayMode.BRANCH
    decision = await gate_async(seeded_session, _step())
    assert decision is None


async def test_gate_async_noop_when_interactive_no_channel(
    seeded_session: ReplaySession,
) -> None:
    """INTERACTIVE mode but no channel attached → gate returns None.

    This is the degenerate case: mode is set but approval is None. We fall
    through rather than raising so a caller can flip the mode without
    immediately wiring a channel.
    """
    seeded_session.mode = ReplayMode.INTERACTIVE
    assert seeded_session.approval is None
    decision = await gate_async(seeded_session, _step())
    assert decision is None


# ----------------------------------------------------------------------
# gate_async — INTERACTIVE + channel
# ----------------------------------------------------------------------
async def test_gate_async_approve(seeded_session: ReplaySession) -> None:
    """APPROVE returns the decision unchanged; session mode stays INTERACTIVE."""
    seeded_session.mode = ReplayMode.INTERACTIVE
    channel = AsyncioChannel()
    seeded_session.approval = channel

    async def approve() -> None:
        await channel.next_step()
        channel.decide(Decision(kind=DecisionKind.APPROVE))

    approver_task = asyncio.create_task(approve())
    decision = await gate_async(seeded_session, _step())
    await approver_task
    assert decision is not None
    assert decision.kind is DecisionKind.APPROVE
    # Mode unchanged after a plain APPROVE.
    assert seeded_session.mode is ReplayMode.INTERACTIVE


async def test_gate_async_stop_raises(seeded_session: ReplaySession) -> None:
    """STOP surfaces as a returned decision; the dispatcher raises."""
    seeded_session.mode = ReplayMode.INTERACTIVE
    channel = AsyncioChannel()
    seeded_session.approval = channel

    async def stopper() -> None:
        await channel.next_step()
        channel.decide(Decision(kind=DecisionKind.STOP))

    approver_task = asyncio.create_task(stopper())
    decision = await gate_async(seeded_session, _step())
    await approver_task
    assert decision is not None
    assert decision.kind is DecisionKind.STOP
    # The dispatcher (not the gate) raises SteppingStopped; verify the
    # exception carries the step that was stopped at.
    raised = SteppingStopped(_step())
    assert raised.step.kind is StepKind.LLM


async def test_gate_async_step_once_disarms(seeded_session: ReplaySession) -> None:
    """STEP_ONCE flips the session back to BRANCH after this call."""
    seeded_session.mode = ReplayMode.INTERACTIVE
    channel = AsyncioChannel()
    seeded_session.approval = channel

    async def step_once() -> None:
        await channel.next_step()
        channel.decide(Decision(kind=DecisionKind.STEP_ONCE))

    approver_task = asyncio.create_task(step_once())
    decision = await gate_async(seeded_session, _step())
    await approver_task
    assert decision is not None
    assert decision.kind is DecisionKind.STEP_ONCE
    # The session disarmed to BRANCH so subsequent calls run free.
    assert seeded_session.mode is ReplayMode.BRANCH


async def test_gate_async_edit_passes_overrides(seeded_session: ReplaySession) -> None:
    """EDIT carries the override fields through to the dispatcher."""
    seeded_session.mode = ReplayMode.INTERACTIVE
    channel = AsyncioChannel()
    seeded_session.approval = channel

    async def editor() -> None:
        await channel.next_step()
        channel.decide(
            Decision(
                kind=DecisionKind.EDIT,
                messages=[{"role": "user", "content": "rewritten"}],
                model="gpt-5",
            )
        )

    approver_task = asyncio.create_task(editor())
    decision = await gate_async(seeded_session, _step())
    await approver_task
    assert decision is not None
    assert decision.kind is DecisionKind.EDIT
    assert decision.messages == [{"role": "user", "content": "rewritten"}]
    assert decision.model == "gpt-5"


# ----------------------------------------------------------------------
# decide_with_validation
# ----------------------------------------------------------------------
def test_validation_edit_requires_override() -> None:
    """An EDIT with no override fields is rejected."""
    with pytest.raises(ValueError, match="EDIT"):
        decide_with_validation(Decision(kind=DecisionKind.EDIT))


def test_validation_approve_rejects_overrides() -> None:
    """APPROVE carrying an override is rejected (use EDIT)."""
    with pytest.raises(ValueError, match="must not carry overrides"):
        decide_with_validation(
            Decision(kind=DecisionKind.APPROVE, model="gpt-5")
        )


def test_validation_stop_clean() -> None:
    """A clean STOP passes through."""
    out = decide_with_validation(Decision(kind=DecisionKind.STOP))
    assert out.kind is DecisionKind.STOP


def test_validation_edit_with_messages_ok() -> None:
    """An EDIT with messages passes."""
    out = decide_with_validation(
        Decision(kind=DecisionKind.EDIT, messages=[{"role": "user", "content": "x"}])
    )
    assert out.messages is not None


# ----------------------------------------------------------------------
# ThreadBridgeChannel (sync tool path)
# ----------------------------------------------------------------------
def test_thread_bridge_channel_sync_round_trip(seeded_session: ReplaySession) -> None:
    """Sync submit blocks until an async-side decide resolves it.

    Uses a background thread to play the approver so the test's main thread
    can call the blocking submit_sync.
    """
    seeded_session.mode = ReplayMode.INTERACTIVE
    channel = ThreadBridgeChannel()
    seeded_session.approval = channel

    decided = threading.Event()

    def approver() -> None:
        # Spin until the step is visible, then resolve it.
        while channel.take_step() is None:
            continue
        channel.decide(Decision(kind=DecisionKind.APPROVE))
        decided.set()

    t = threading.Thread(target=approver, daemon=True)
    t.start()
    decision = channel.submit_sync(_step())
    t.join(timeout=2)
    assert decided.is_set()
    assert decision.kind is DecisionKind.APPROVE


def test_gate_sync_noop_when_no_channel(seeded_session: ReplaySession) -> None:
    """Sync gate is a no-op without a channel (tool path zero-regression)."""
    seeded_session.mode = ReplayMode.INTERACTIVE
    assert seeded_session.approval is None
    assert gate_sync(seeded_session, _step()) is None


# ----------------------------------------------------------------------
# replay() ctx manager threads the channel
# ----------------------------------------------------------------------
def test_replay_ctx_threads_approval(store: TraceStore, trace_id: str) -> None:
    """The replay() ctx manager attaches the approval channel to the session."""
    # Seed a minimal trace so for_root doesn't raise.
    span = _llm_span(trace_id, span_id="a" * 16, messages=[{"role": "user", "content": "x"}])
    trace = Trace(trace_id=trace_id, spans=[span])
    store.upsert_trace(trace)
    store.insert_span(span)

    channel = AsyncioChannel()
    with replay_ctx(
        store,
        trace_id,
        mode=ReplayMode.INTERACTIVE,
        approval=channel,
    ) as session:
        assert session.mode is ReplayMode.INTERACTIVE
        assert session.approval is channel


def test_replay_ctx_default_no_channel(store: TraceStore, trace_id: str) -> None:
    """Without the approval kwarg, session.approval is None (no behavior change)."""
    span = _llm_span(trace_id, span_id="a" * 16, messages=[{"role": "user", "content": "x"}])
    trace = Trace(trace_id=trace_id, spans=[span])
    store.upsert_trace(trace)
    store.insert_span(span)

    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN) as session:
        assert session.approval is None
