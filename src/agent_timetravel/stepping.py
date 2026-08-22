"""Phase A — Interactive step-through debugging primitive.

The "stop at each agent response" mechanism. This module is **pure logic**:
no FastAPI, no UI, no I/O beyond the channel the caller supplies. It depends
only on stdlib (``asyncio``, ``dataclasses``, ``queue``, ``threading``) and
the :class:`~timetravel.replay.ReplaySession` contract.

Three concepts:

* :class:`Step` — a description of a pending call (LLM messages/params/tools,
  or a tool invocation with its args). Built by the dispatcher at the gate.
* :class:`Decision` — the human's verdict: ``APPROVE``, ``EDIT`` (carry a
  mutated payload), ``STOP`` (terminate the run), or ``STEP_ONCE`` (approve
  this one and drop back to non-interactive forwarding until the next
  attach).
* :class:`ApprovalChannel` — the protocol a dispatcher awaits on. The agent
  pushes a :class:`Step`, the channel yields a :class:`Decision`. Two
  concrete impls ship here: :class:`AsyncioChannel` (for the async adapters
  and the OpenAI async intercept) and :class:`ThreadBridgeChannel` (for the
  sync ``@timetravel.tool()`` path; the sync side blocks on a
  :class:`threading.Event` while an asyncio coroutine pumps decisions across
  the thread boundary).

The high-level entry point for dispatchers is :func:`gate_async`, which
encapsulates the whole "should I pause, and what do I do with the answer"
policy so every adapter/interceptor stays one line:

::

    decision = await gate_async(session, step)
    if decision is None:
        # not interactive / no channel — proceed as today
    elif decision.kind is Decision.STOP:
        raise SteppingStopped(step)
    elif decision.kind is Decision.EDIT:
        ...apply decision.messages / decision.params / decision.args...

The sync dual (:func:`gate_sync`) exists for the tool-interceptor path. It
shares the same :class:`Decision`/`Step` shapes and blocks its thread until
the channel resolves.

Design invariants
-----------------
* **Zero behaviour change when no channel is attached.** ``gate_*`` is a
  pure no-op for FROZEN / BRANCH / FULL_RERUN or when
  ``session.approval is None``. The existing 291 tests must stay green.
* **Per-task isolation.** The channel lives on the
  :class:`~timetravel.replay.ReplaySession`, which is stored in a
  :class:`~contextvars.ContextVar`. Concurrent sessions (the Phase 5.5 eval
  harness) never share channel state.
* **No import-time deps.** Nothing here imports FastAPI, openai, or any
  framework. Channels are transport-agnostic; the Phase B HTTP layer will
  provide its own :class:`ApprovalChannel` impl bridging SSE + POST.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import UNIQUE, StrEnum, verify
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from agent_timetravel.replay import ReplaySession

__all__ = [
    "ApprovalChannel",
    "AsyncioChannel",
    "Decision",
    "DecisionKind",
    "InteractiveSession",
    "RunControlBreakpoint",
    "RunControlIntent",
    "Step",
    "StepKind",
    "SteppingStopped",
    "ThreadBridgeChannel",
    "complete_step",
    "complete_step_sync",
    "gate_async",
    "gate_sync",
]


# ----------------------------------------------------------------------
# Step description
# ----------------------------------------------------------------------
@verify(UNIQUE)
class StepKind(StrEnum):
    """What kind of call is paused at the gate.

    Mirrors :class:`~timetravel.enums.SpanKind` minus ``UNKNOWN`` — stepping only
    applies to LLM and tool calls (you don't pause at a framework
    orchestration node, because TimeTravel doesn't intercept those).
    """

    LLM = "llm"
    TOOL = "tool"
    MCP = "mcp"


@dataclass(frozen=True, slots=True)
class Step:
    """A pending call surfaced to the developer for approval.

    ``payload`` is a transport-friendly dict the UI renders directly:

    * For an LLM step: ``{"model": str, "messages": [...], "tools": [...],
      "params": {...}}``.
    * For a tool step: ``{"name": str, "args": [...], "kwargs": {...}}``.

    The dispatcher is responsible for building ``payload`` in the shape the
    UI expects; :func:`gate_async` / :func:`gate_sync` pass it through
    untouched. Keeping the shape caller-owned means each framework adapter
    can emit its native message format without a TimeTravel-side projection.
    """

    kind: StepKind
    payload: dict[str, Any]
    #: Cursor position at the moment of the pause. Lets the UI show "step
    #: 4 of 12" and supports the future "restart from step N" action.
    cursor: int


# ----------------------------------------------------------------------
# Decisions
# ----------------------------------------------------------------------
@verify(UNIQUE)
class DecisionKind(StrEnum):
    """What the developer decided at a paused step.

    * ``APPROVE`` — proceed with the call unchanged.
    * ``EDIT`` — proceed, but with the supplied overrides applied to the
      outbound call (mutated messages / params / tool args). Only the
      non-``None`` fields are applied; ``None`` means "leave as-is".
    * ``STOP`` — terminate the run. The dispatcher raises
      :class:`SteppingStopped`.
    * ``STEP_ONCE`` — approve this call and flip the session to
      non-interactive forwarding until the channel is re-armed. Useful for
      "step over" UX: advance exactly one step, then run freely.
    * ``MOCK`` — do not call the tool; return ``mock_result`` to the agent.
    * ``SKIP`` — do not call the tool; return a structured skip result.
    * ``REJECT`` — refuse the tool call outright; return a structured error
      to the agent without invoking the live tool (Phase 1.4). Lets the
      developer veto a dangerous action mid-run and feed a reason back.
    * ``RUN_UNTIL_BREAKPOINT`` — approve this call AND arm "run until
      breakpoint" run-control so subsequent steps auto-approve until a
      breakpoint fires (Phase 1.3). The browser-driven equivalent of
      :meth:`RunControlIntent.run_until_breakpoint`.
    """

    APPROVE = "approve"
    EDIT = "edit"
    STOP = "stop"
    STEP_ONCE = "step_once"
    MOCK = "mock"
    SKIP = "skip"
    REJECT = "reject"
    RUN_UNTIL_BREAKPOINT = "run_until_breakpoint"


@dataclass(frozen=True, slots=True)
class Decision:
    """The verdict returned for a :class:`Step`.

    For ``APPROVE`` / ``STEP_ONCE`` only ``kind`` is meaningful. For
    ``EDIT`` the optional override fields are applied. ``STOP`` carries no
    payload. Fields default to ``None`` so an ``APPROVE`` is just
    ``Decision(kind=DecisionKind.APPROVE)``.
    """

    kind: DecisionKind
    #: For ``EDIT`` of an LLM step: the replacement message list.
    messages: list[dict[str, Any]] | None = None
    #: For ``EDIT`` of an LLM step: merged into the call kwargs (temperature,
    #: max_tokens, etc.). ``None`` means leave unchanged.
    params: dict[str, Any] | None = None
    #: For ``EDIT`` of a tool step: replacement positional args.
    args: list[Any] | None = None
    #: For ``EDIT`` of a tool step: replacement keyword args.
    kwargs: dict[str, Any] | None = None
    #: For ``EDIT`` of an LLM step: a model-name override.
    model: str | None = None
    #: For ``MOCK``: the value returned to the agent instead of calling a tool.
    mock_result: Any = None
    #: For ``REJECT``: optional human-readable rationale surfaced back to the
    #: agent as the rejection reason (Phase 1.4).
    reason: str | None = None


class SteppingStopped(RuntimeError):
    """Raised to unwind the agent run when the developer chooses STOP.

    Distinct from :class:`~timetravel.replay.ReplayError` (which signals a
    determinism contract violation) — ``SteppingStopped`` is a normal,
    developer-initiated termination. Dispatchers let it propagate; the
    Phase B runner catches it and marks the session ``done`` (not
    ``errored``).
    """

    def __init__(self, step: Step) -> None:
        self.step = step
        super().__init__(f"stepping stopped at cursor={step.cursor} ({step.kind.value})")


# ----------------------------------------------------------------------
# Session bookkeeping (persisted by the Phase B stepping server)
# ----------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class InteractiveSession:
    """One server-side step-through debug run's persisted state.

    Stored in the ``interactive_sessions`` table. The actual paused-step
    traffic flows over SSE + POST /decide; this row exists so the UI can
    list/resume sessions and survive an SSE reconnect. Spans captured during
    the run live in the existing ``spans`` table under ``branch_id``.

    The ``status`` field is the lifecycle marker the runner mutates:

    * ``running`` — runner task is executing the agent.
    * ``paused``  — blocked at the gate awaiting a decision.
    * ``done``    — agent completed normally OR developer sent STOP.
    * ``errored`` — the runner task raised an unexpected exception.

    ``done`` covers both natural completion and ``SteppingStopped`` because
    STOP is a normal, developer-initiated termination (see
    :class:`SteppingStopped`), not a contract violation.
    """

    session_id: str
    trace_id: str
    branch_id: str
    runner_ref: str
    agent_ref: str | None = None
    input_payload: dict[str, Any] | None = None
    result_payload: Any = None
    status: str = "running"
    error_message: str | None = None
    created_at: str = ""
    updated_at: str = ""
    #: Server-owned run-control intent (Phase 1.2). Persisted so a page
    #: refresh or SSE reconnect doesn't lose "pause after current step" or
    #: "run until breakpoint". The runner reads it at each gate; see
    #: :class:`RunControlIntent`.
    run_control: RunControlIntent = field(
        default_factory=lambda: RunControlIntent()
    )


@dataclass(frozen=True, slots=True)
class RunControlBreakpoint:
    """A server-evaluated breakpoint stored with a run-control intent."""

    type: str
    value: str
    label: str = ""
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "value": self.value,
            "label": self.label,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, data: object) -> RunControlBreakpoint | None:
        if not isinstance(data, dict):
            return None
        rule_type = data.get("type")
        value = data.get("value")
        if not isinstance(rule_type, str) or not isinstance(value, str) or not value.strip():
            return None
        return cls(
            type=rule_type,
            value=value,
            label=str(data.get("label") or ""),
            enabled=bool(data.get("enabled", True)),
        )

    def matches(self, step: Step) -> bool:
        """Return whether this rule matches a pending step payload."""
        if not self.enabled or not self.value.strip():
            return False
        value = self.value.strip().lower()
        payload = step.payload
        if self.type == "tool_name":
            return value in str(payload.get("name", "")).lower()
        if self.type == "model_name":
            return value in str(payload.get("model", "")).lower()
        if self.type == "message_contains":
            messages = json.dumps(payload.get("messages", ""), default=str).lower()
            return value in messages
        if self.type == "token_limit":
            try:
                threshold = float(value)
                max_tokens = float((payload.get("params") or {}).get("max_tokens", 0))
            except (TypeError, ValueError):
                return False
            return max_tokens >= threshold
        return False


@dataclass(slots=True)
class RunControlIntent:
    """Server-owned run-control intent read at every stepping gate.

    Phase 1.2 moves "what should the runner do next" out of the browser's
    volatile state and into the persisted session row. The SSE channel
    consults this at each gate instead of trusting a client-side flag, so a
    page refresh or dropped EventSource no longer silently changes behaviour.

    * ``pause_after_current`` — approve the in-flight step, then re-pause
      before the *next* one. The UI's "step forward" button sets this,
      delivers APPROVE, and the runner naturally stops again one step later.
    * ``run_until_breakpoint`` — keep auto-approving until a step lands on a
      named breakpoint, then pause. The UI's "continue" button sets this;
      :func:`gate_async` / :func:`gate_sync` clear it when a breakpoint fires.
    """

    pause_after_current: bool = False
    run_until_breakpoint: bool = False
    breakpoints: tuple[RunControlBreakpoint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the JSON stored in the ``run_control`` column."""
        return {
            "pause_after_current": self.pause_after_current,
            "run_until_breakpoint": self.run_until_breakpoint,
            "breakpoints": [rule.to_dict() for rule in self.breakpoints],
        }

    @classmethod
    def from_dict(cls, data: object) -> RunControlIntent:
        """Reconstruct from a JSON-decoded value; tolerant of old rows."""
        if not isinstance(data, dict):
            return cls()
        raw_breakpoints = data.get("breakpoints", [])
        if not isinstance(raw_breakpoints, list):
            raw_breakpoints = []
        return cls(
            pause_after_current=bool(data.get("pause_after_current", False)),
            run_until_breakpoint=bool(data.get("run_until_breakpoint", False)),
            breakpoints=tuple(
                rule
                for item in raw_breakpoints
                for rule in (RunControlBreakpoint.from_dict(item),)
                if rule is not None
            ),
        )


# ----------------------------------------------------------------------
# Channel protocol
# ----------------------------------------------------------------------
@runtime_checkable
class ApprovalChannel(Protocol):
    """The contract a dispatcher awaits on at each paused step.

    One method, async: push a :class:`Step`, await a :class:`Decision`.
    Implementations may block indefinitely (the human is slow); the
    dispatcher's ``await`` is what holds the agent call open.

    The same protocol serves both the asyncio and thread-bridged paths —
    :class:`ThreadBridgeChannel` exposes ``submit`` as a coroutine too, so
    sync dispatchers call it via :func:`asyncio.run_coroutine_threadsafe`
    (see :func:`gate_sync`).
    """

    async def submit(self, step: Step) -> Decision:
        """Push ``step`` to the approver and await their verdict."""


# ----------------------------------------------------------------------
# Concrete channels
# ----------------------------------------------------------------------
@dataclass
class AsyncioChannel:
    """In-process asyncio channel for the async adapters / LLM intercept.

    A pair of queues: the agent pushes the step on ``_pending``, the
    approver (a test, a CLI prompt task, or the Phase B SSE bridge) puts
    the decision on ``_decisions``. Both are unbounded — at any time only
    one step is pending, so capacity 1 would also suffice, but unbounded
    avoids a subtle deadlock if ``decide`` races ahead of ``submit``.

    The approver side is intentionally generic: tests inject a coroutine
    factory, the Phase B server injects an SSE-driven coroutine. Either
    way the channel doesn't know where decisions come from.
    """

    _pending: asyncio.Queue[Step] = field(default_factory=asyncio.Queue)
    _decisions: asyncio.Queue[Decision] = field(default_factory=asyncio.Queue)

    async def submit(self, step: Step) -> Decision:
        await self._pending.put(step)
        return await self._decisions.get()

    async def next_step(self) -> Step:
        """Await the next pending step (approver side)."""
        return await self._pending.get()

    def decide(self, decision: Decision) -> None:
        """Resolve the current pending step (approver side, non-blocking)."""
        self._decisions.put_nowait(decide_with_validation(decision))


class _PendingSlot:
    """Single-slot handoff for the thread-bridged channel.

    A :class:`threading.Event` signals "step pushed, awaiting decision"; the
    decision lands in a plain attribute guarded by a second event. We use
    a slot rather than a :class:`queue.Queue` so the asyncio side can
    poll the slot without consuming from a queue that the sync side owns.
    """

    __slots__ = ("_decision_ready", "_lock", "_step_ready", "decision", "step")

    def __init__(self) -> None:
        self.step: Step | None = None
        self.decision: Decision | None = None
        self._step_ready = threading.Event()
        self._decision_ready = threading.Event()
        self._lock = threading.Lock()

    def push_and_wait(self, step: Step) -> Decision:
        """Sync side: publish ``step``, block until a decision arrives."""
        with self._lock:
            self.step = step
            self.decision = None
            self._step_ready.set()
            self._decision_ready.clear()
        self._decision_ready.wait()
        with self._lock:
            decision = self.decision
            if decision is None:
                # Internal-only invariant: ``_decision_ready`` is only ever
                # set by :meth:`publish_decision`, which always assigns
                # ``self.decision`` under the same lock. A None here means
                # the slot was reset concurrently — a programming error, not
                # a user-facing condition. Raise rather than ``assert`` so
                # the check survives ``python -O``.
                raise RuntimeError(
                    "ThreadBridgeSlot invariant violated: _decision_ready set "
                    "without a decision (concurrent reset?)"
                )
            self.step = None
            self._step_ready.clear()
            return decision

    def take_step(self) -> Step | None:
        """Asyncio side: non-blocking peek at the pushed step."""
        return self.step if self._step_ready.is_set() else None

    def publish_decision(self, decision: Decision) -> None:
        """Asyncio side: resolve the pending step."""
        with self._lock:
            self.decision = decide_with_validation(decision)
            self._decision_ready.set()


@dataclass
class ThreadBridgeChannel:
    """Sync→async bridge for the ``@timetravel.tool()`` path.

    The sync tool wrapper calls :meth:`submit_sync` and blocks its thread
    on a :class:`_PendingSlot`. A cooperating asyncio task (installed by
    the Phase B runner, or a test's ``asyncio.run_coroutine_threadsafe``
    pump) drains the slot and publishes decisions.

    ``submit`` (the protocol coroutine) is a thin wrapper that delegates to
    :meth:`submit_sync` via :func:`asyncio.get_event_loop`'s executor — kept
    so this class satisfies :class:`ApprovalChannel` uniformly. Sync
    dispatchers should call :meth:`submit_sync` directly to avoid the
    executor round-trip.
    """

    _slot: _PendingSlot = field(default_factory=_PendingSlot)

    def submit_sync(self, step: Step) -> Decision:
        """Sync entry point — blocks the calling thread until decided."""
        return self._slot.push_and_wait(step)

    async def submit(self, step: Step) -> Decision:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.submit_sync, step)

    def take_step(self) -> Step | None:
        """Asyncio side: peek at the pending step without blocking."""
        return self._slot.take_step()

    def decide(self, decision: Decision) -> None:
        """Asyncio side: resolve the pending step (non-blocking)."""
        self._slot.publish_decision(decision)


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def decide_with_validation(decision: Decision) -> Decision:
    """Reject self-inconsistent decisions at the channel boundary.

    Keeps the dispatchers simple: they can trust that an ``EDIT`` carries
    at least one override, and that ``APPROVE``/``STOP``/``STEP_ONCE``
    carry none. A bad decision from a buggy approver (or a malformed HTTP
    body in Phase B) fails fast here rather than silently no-op'ing.
    """
    if decision.kind is DecisionKind.EDIT:
        has_override = any(
            v is not None
            for v in (decision.messages, decision.params, decision.args,
                      decision.kwargs, decision.model)
        )
        if not has_override:
            raise ValueError(
                "Decision(kind=EDIT) must carry at least one of "
                "messages/params/args/kwargs/model"
            )
    elif decision.kind in (
        DecisionKind.APPROVE,
        DecisionKind.STOP,
        DecisionKind.STEP_ONCE,
        DecisionKind.SKIP,
        DecisionKind.RUN_UNTIL_BREAKPOINT,
    ):
        has_leak = any(
            v is not None
                for v in (decision.messages, decision.params, decision.args,
                      decision.kwargs, decision.model, decision.mock_result)
        )
        if has_leak:
            raise ValueError(
                f"Decision(kind={decision.kind.value}) must not carry overrides; "
                "use kind=EDIT to mutate the call"
            )
    elif decision.kind is DecisionKind.REJECT:
        # ``reason`` is the only accepted payload; reject any call overrides.
        has_leak = any(
            v is not None
                for v in (decision.messages, decision.params, decision.args,
                      decision.kwargs, decision.model, decision.mock_result)
        )
        if has_leak:
            raise ValueError(
                "Decision(kind=reject) must not carry overrides; "
                "use kind=edit to mutate the call or kind=mock to substitute a result"
            )
    elif decision.kind is DecisionKind.MOCK:
        # None is a valid mock value, so MOCK is always accepted.
        pass
    return decision


# ----------------------------------------------------------------------
# Gate helpers — the single choke point dispatchers call
# ----------------------------------------------------------------------
def _should_pause(session: ReplaySession) -> bool:
    """True only when the session is INTERACTIVE *and* a channel is attached.

    Inlined rather than a method on ``ReplaySession`` so this module stays
    the sole owner of the stepping policy. The session just carries the
    channel field; the policy lives here.
    """
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enums import ReplayMode
    # pylint: enable=import-outside-toplevel

    return (
        session.mode is ReplayMode.INTERACTIVE
        and session.approval is not None
    )


async def gate_async(session: ReplaySession, step: Step) -> Decision | None:
    """Consult the approval channel for an async dispatcher.

    Returns ``None`` when the session isn't pausing (the dispatcher proceeds
    exactly as today). Otherwise awaits the channel and applies the
    ``STEP_ONCE`` side-effect (disarming the session's "pause" flag until
    re-armed) before returning the :class:`Decision`. ``STOP`` is returned
    unchanged — the caller raises :class:`SteppingStopped` so the unwind
    point is explicit at each dispatch site.

    ``RUN_UNTIL_BREAKPOINT`` arms the run-control flag (via the channel when
    it supports it) and is returned to the dispatcher as ``APPROVE`` so the
    call proceeds (Phase 1.3).
    """
    if not _should_pause(session):
        return None
    channel = session.approval
    if channel is None:
        # ``_should_pause`` returned True, which requires ``approval is not
        # None`` — so reaching here is an internal invariant violation, not
        # a user-facing condition. Raise rather than ``assert`` so the check
        # survives ``python -O``.
        raise RuntimeError(
            "stepping invariant violated: _should_pause returned True with "
            "no approval channel attached"
        )
    decision = await channel.submit(step)
    if decision.kind is DecisionKind.STEP_ONCE:
        # Drop out of interactive mode until the caller re-arms. Re-arming
        # is a Phase B UI concern (a "continue" button flips the mode back).
        # For Phase A (Python API only) the caller controls this by setting
        # ``session.mode`` directly.
        _disarm(session)
    elif decision.kind is DecisionKind.RUN_UNTIL_BREAKPOINT:
        _arm_run_until_breakpoint(channel)
        # The dispatcher treats this exactly like APPROVE for the current call.
        return Decision(kind=DecisionKind.APPROVE, reason=decision.reason)
    return decision


def gate_sync(session: ReplaySession, step: Step) -> Decision | None:
    """Sync dual of :func:`gate_async`, for the ``@timetravel.tool()`` path.

    The channel must be a :class:`ThreadBridgeChannel` (or any object with a
    ``submit_sync`` method); a plain :class:`AsyncioChannel` cannot service a
    sync call without an event loop. We duck-type rather than isinstance so
    custom channels (Phase B) don't have to inherit from anything.
    """
    if not _should_pause(session):
        return None
    channel = session.approval
    submit_sync: Callable[[Step], Decision] | None = getattr(channel, "submit_sync", None)
    if submit_sync is None:
        # An async-only channel was attached but a sync tool is paused. This
        # is a programmer error: surface it rather than silently skipping the
        # gate (which would hide the stepping contract violation).
        raise SteppingStopped(
            step=step
        ) from RuntimeError(
            "sync tool stepped with an async-only ApprovalChannel; "
            "attach a ThreadBridgeChannel for sync-tool stepping"
        )
    decision: Decision = submit_sync(step)
    if decision.kind is DecisionKind.STEP_ONCE:
        _disarm(session)
    elif decision.kind is DecisionKind.RUN_UNTIL_BREAKPOINT:
        _arm_run_until_breakpoint(channel)
        return Decision(kind=DecisionKind.APPROVE, reason=decision.reason)
    return decision


def _arm_run_until_breakpoint(channel: Any) -> None:  # noqa: ANN401
    """Arm ``run_until_breakpoint`` on channels that carry run-control.

    Duck-typed: in-process test channels (:class:`AsyncioChannel`,
    :class:`ThreadBridgeChannel`) don't carry run-control, so this is a
    no-op for them. Only :class:`SSEApprovalChannel` exposes ``set_run_control``.

    Typed ``Any`` (not :class:`ApprovalChannel`) because the helper is called
    from :func:`gate_sync` where the channel is still ``ApprovalChannel | None``
    to mypy's view (the non-None narrowing happens via ``getattr`` on
    ``submit_sync``, which mypy doesn't propagate). The runtime guard
    (``getattr ... is None``) makes the channel non-None by construction.
    """
    set_rc = getattr(channel, "set_run_control", None)
    if set_rc is None:
        return
    current = getattr(channel, "run_control", RunControlIntent())
    set_rc(
        RunControlIntent(
            pause_after_current=current.pause_after_current,
            run_until_breakpoint=True,
            breakpoints=current.breakpoints,
        )
    )


def _disarm(session: ReplaySession) -> None:
    """Flip an INTERACTIVE session back to BRANCH after a STEP_ONCE.

    BRANCH (not FULL_RERUN) so the recorded-prefix contract still holds —
    stepping "over" continues to honour the cache for already-served spans.
    """
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enums import ReplayMode
    # pylint: enable=import-outside-toplevel

    session.mode = ReplayMode.BRANCH


async def complete_step(
    session: ReplaySession,
    step: Step,
    result: str,
    usage: dict[str, int] | None = None,
) -> Decision | None:
    """Emit a completed-step event carrying the model's response text.

    Called by the dispatcher *after* the response is materialised (cached or
    live) so the UI can show what the model actually returned before the
    developer chooses next/back/stop. A no-op when no channel is attached or
    the channel has no ``complete`` method (the in-process test channels
    don't implement it; only ``SSEApprovalChannel`` does).

    ``result`` is the assistant's textual response (extracted from the
    chat-completion payload by the dispatcher). Tools can pass their output
    serialised to a string.
    """
    channel = session.approval
    if channel is None:
        return None
    complete_fn = getattr(channel, "complete", None)
    if complete_fn is None:
        return None
    # ``complete`` may be async (SSEApprovalChannel) or sync. Duck-type via
    # iscoroutinefunction to avoid awaiting a sync return.
    import inspect  # pylint: disable=import-outside-toplevel

    ret = complete_fn(step, result, usage)
    if inspect.isawaitable(ret):
        awaited = await ret
        return awaited if isinstance(awaited, Decision) else None
    return ret if isinstance(ret, Decision) else None


def complete_step_sync(
    session: ReplaySession,
    step: Step,
    result: str,
    usage: dict[str, int] | None = None,
) -> Decision | None:
    """Sync counterpart for a worker-thread tool result.

    Browser-backed channels expose ``complete_sync`` so a wrapped sync tool
    can publish its output and wait for the developer's post-tool decision
    without blocking the server's asyncio event loop.
    """
    channel = session.approval
    if channel is None:
        return None
    complete_fn = getattr(channel, "complete_sync", None)
    if complete_fn is None:
        return None
    ret = complete_fn(step, result, usage)
    return ret if isinstance(ret, Decision) else None


# Type alias re-exported for test ergonomics: a callable approver is the
# shape tests inject into a bespoke channel. Keeping it here avoids tests
# reaching into asyncio internals.
ApproverFn = Callable[[Step], Awaitable[Decision]]
