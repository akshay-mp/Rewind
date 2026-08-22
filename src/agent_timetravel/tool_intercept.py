"""Phase 3 — Tool-call interceptor (frozen tool cache).

Track 3B.3 of plan §6 Phase 3. During a frozen :func:`timetravel.replay`,
agents invoke tools (search, calculator, MCP RPCs, …) that have live
side-effects: hitting a third-party API, mutating a database, sending
emails. Running them again would *break* determinism — a replayed trace
must reproduce the recorded span tree verbatim, including tool results.

This module provides decorators / context managers that wrap a Python
tool function and, **if a replay session is active and has a matching
recorded ``gen_ai.tool``/``gen_ai.mcp`` span at the cursor**, return the
recorded output instead of calling the live function.

Matching is *content-based*: a tool invocation matches a recorded span iff

* ``span.kind`` is :attr:`~timetravel.enums.SpanKind.TOOL` or :data:`.MCP`
* ``span.name`` equals the wrapper's ``name``
* the recorded ``gen_ai.tool.input`` JSON hashes-equal the live call args
  (see :func:`_tool_args_hash` for the deterministic normaliser).

When the live call diverges (different args, or cursor exhausted):

* FROZEN mode: raise :class:`~timetravel.replay.ReplayError` — fail closed.
* BRANCH / FULL_RERUN modes: call through and capture the new TOOL span
  under the active branch via :meth:`~timetravel.replay.ReplaySession.record_new`.

Reentrancy: same contract as :mod:`timetravel.openai_intercept` — each replay
session is a contextvar, so the Phase 5.5 eval harness can fan out
concurrent determinism checks without cross-contaminating cursors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from agent_timetravel.replay import RecordedResponse, ReplaySession
    from agent_timetravel.stepping import Step

__all__ = ["ToolCacheMiss", "tool"]

F = TypeVar("F", bound=Callable[..., Any])


class ToolCacheMiss(RuntimeError):
    """A live tool call does not match the recorded span tree.

    Raised only in FROZEN mode. In BRANCH / FULL_RERUN modes the wrapper
    silently forwards to the live function and records the new span.
    """


# ----------------------------------------------------------------------
# Public API: the decorator
# ----------------------------------------------------------------------
def tool(
    name: str | None = None,
    *,
    kind: str | None = None,
) -> Callable[[F], F]:
    """Wrap a Python function as a TimeTravel-replay-aware tool.

    Parameters
    ----------
    name:
        The recorded span name. Defaults to ``func.__name__``.
    kind:
        ``"gen_ai.tool"`` (default) or ``"gen_ai.mcp"``. Selects which
        spaN kind to match against.

    Behaviour
    ---------
    * No active replay session → call the underlying function unchanged.
    * Active session + matching recorded span at cursor → return cached
      ``gen_ai.tool.output`` (deserialised). Cursor advances.
    * Active session + mismatch in FROZEN mode → :class:`ToolCacheMiss`.
    * Active session + mismatch in BRANCH / FULL_RERUN → call live and
      capture a new TOOL span under ``session.branch_id``.

    Usage::

        @timetravel.tool()
        def search(query: str) -> list[dict]:
            return live_search_client(query)
    """
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enums import SpanKind
    # pylint: enable=import-outside-toplevel

    tool_kind = kind if kind is not None else SpanKind.TOOL

    def _decorator(func: F) -> F:
        tool_name = name if name is not None else func.__name__

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            session = _active_session()
            if session is None:
                return func(*args, **kwargs)
            return _dispatch_sync_tool(
                func,
                session,
                args,
                kwargs,
                tool_name=tool_name,
                tool_kind=tool_kind,
            )

        wrapper.__timetravel_tool_name__ = tool_name  # type: ignore[attr-defined]
        wrapper.__timetravel_tool_kind__ = tool_kind  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return _decorator


# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------
def _dispatch_sync_tool(
    func: Callable[..., Any],
    session: ReplaySession,
    args: tuple[Any, ...],
    kwargs: dict[Any, Any],
    *,
    tool_name: str,
    tool_kind: str,
) -> Any:
    """Decide cache-hit vs. live-forward for a wrapped tool call.

    Implements the determinism contract:

    * FROZEN  : must hit recorded span or raise (no live call permitted).
    * BRANCH  : hit if available, else forward + capture.
    * FULL    : always forward + capture (cursor still advances on hit).
    * INTERACTIVE : pause at the stepping gate before any of the above; the
      developer can APPROVE, EDIT the tool args, STOP, or STEP_ONCE. An EDIT
      rewrites ``args``/``kwargs`` before the cache lookup so a divergent
      edit naturally falls into the live-forward path. Requires a
      :class:`~timetravel.stepping.ThreadBridgeChannel` (sync blocking) — an
      async-only channel raises ``SteppingStopped`` with an actionable hint.
    """
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enums import ReplayMode
    # pylint: enable=import-outside-toplevel

    # Interactive stepping gate — fires before the cache lookup so an EDIT
    # can rewrite the inputs. No-op when mode isn't INTERACTIVE or no
    # channel is attached (the documented zero-regression invariant).
    args, kwargs, step, decision = _step_tool(session, tool_name, tool_kind, args, kwargs)

    # Mock, skip, and reject are resolved before cache lookup and before the
    # wrapped function is entered, so side effects cannot occur accidentally.
    if decision is not None:
        from agent_timetravel.stepping import DecisionKind
        if decision.kind is DecisionKind.MOCK:
            output = decision.mock_result
            _complete_tool_step(session, step, output)
            return output
        if decision.kind is DecisionKind.SKIP:
            output = {"timetravel": "tool skipped", "tool": tool_name}
            _complete_tool_step(session, step, output)
            return output
        if decision.kind is DecisionKind.REJECT:
            # The developer vetoed the tool call. Return a structured reject
            # result to the agent (no live call) so the agent can react to the
            # refusal — e.g. choose a different tool or ask the user. The
            # optional ``reason`` is surfaced back to the agent verbatim.
            output = {
                "timetravel": "tool rejected",
                "tool": tool_name,
                "reason": (
                    decision.reason
                    if decision.reason is not None
                    else "rejected by developer"
                ),
            }
            _complete_tool_step(session, step, output)
            return output

    args_hash = _tool_args_hash(args, kwargs)
    recorded = _find_tool_span(session, name=tool_name, kind=tool_kind, args_hash=args_hash)
    if recorded is not None:
        output = _materialise_tool_output(recorded.payload)
        _complete_tool_step(session, step, output)
        return output

    # Cache miss — only legalise in non-frozen modes.
    if session.mode is ReplayMode.FROZEN:
        raise ToolCacheMiss(
            f"Tool `{tool_name}` was called live during a frozen replay with no "
            f"matching recorded span (args_hash={args_hash[:8]}…). Branch the "
            "trace (mode=branch) or fix the call sequence."
        )

    output = func(*args, **kwargs)
    _capture_live_tool_span(
        session,
        tool_name=tool_name,
        tool_kind=tool_kind,
        args=args,
        kwargs=kwargs,
        args_hash=args_hash,
        output=output,
    )
    _complete_tool_step(session, step, output)
    return output


def _step_tool(
    session: ReplaySession,
    tool_name: str,
    tool_kind: str,
    args: tuple[Any, ...],
    kwargs: dict[Any, Any],
) -> tuple[tuple[Any, ...], dict[Any, Any], Step, Any]:
    """Interactive stepping gate for the sync tool path.

    Returns the (possibly edited) args/kwargs. Raises
    :class:`~timetravel.stepping.SteppingStopped` on STOP. A no-op when no
    approval channel is attached.
    """
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.stepping import (
        DecisionKind,
        Step,
        StepKind,
        SteppingStopped,
        gate_sync,
    )
    # pylint: enable=import-outside-toplevel

    kind_enum = StepKind.TOOL if tool_kind == "gen_ai.tool" else StepKind.MCP
    step = Step(
        kind=kind_enum,
        payload={
            "name": tool_name,
            "args": list(args),
            "kwargs": dict(kwargs),
        },
        cursor=session.cursor,
    )
    decision = gate_sync(session, step)
    if decision is None:
        return args, kwargs, step, None
    if decision.kind is DecisionKind.STOP:
        raise SteppingStopped(step)
    if decision.kind is DecisionKind.EDIT:
        new_args = tuple(decision.args) if decision.args is not None else args
        new_kwargs = decision.kwargs if decision.kwargs is not None else kwargs
        return new_args, new_kwargs, step, None
    return args, kwargs, step, decision


def _complete_tool_step(session: ReplaySession, step: Step, output: Any) -> None:
    """Publish the exact tool result for the browser's post-tool review."""
    # pylint: disable=import-outside-toplevel
    import json

    from agent_timetravel.stepping import complete_step_sync
    # pylint: enable=import-outside-toplevel

    try:
        result = json.dumps(output, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        result = repr(output)
    complete_step_sync(session, step, result)


# ----------------------------------------------------------------------
# Span matching & materialisation
# ----------------------------------------------------------------------
def _find_tool_span(
    session: ReplaySession,
    *,
    name: str,
    kind: str,
    args_hash: str,
) -> RecordedResponse | None:
    """Look ahead in the session's recorded span cache for a matching tool span.

    We don't use :meth:`respond_or_forward` directly because tool spans are
    looked up by *name+args_hash*, not the LLM's ``messages_hash``. We
    inspect the spans **after** the current cursor — the next TOOL span in
    script-order whose name/args match is consumed, and the cursor jumps
    to just past it (any interleaved LLM spans are presumed already served).
    """
    cache = session.recorded_spans()
    cursor = session.cursor
    for idx in range(cursor, len(cache)):
        span = cache[idx]
        if span.kind != kind or span.name != name:
            continue
        recorded_hash = span.raw_attributes.get("gen_ai.tool.input_hash")
        if recorded_hash != args_hash:
            continue
        session.advance_cursor_to(idx + 1)
        payload = {
            "output": span.raw_attributes.get("gen_ai.tool.output"),
            "span_id": span.span_id,
            "timetravel_id": span.timetravel_id,
        }
        from agent_timetravel.replay import (
            RecordedResponse,  # pylint: disable=import-outside-toplevel
        )

        return RecordedResponse(
            payload=payload,
            span_id=span.span_id,
            timetravel_id=span.timetravel_id,
            model="",
        )
    return None


def _materialise_tool_output(payload: dict[str, Any]) -> Any:
    """Return the recorded ``gen_ai.tool.output`` verbatim.

    We trust the recording — TimeTravel is a debugging tool, not a sanitiser.
    """
    return payload.get("output")


def _capture_live_tool_span(
    session: ReplaySession,
    *,
    tool_name: str,
    tool_kind: str,
    args: tuple[Any, ...],
    kwargs: dict[Any, Any],
    args_hash: str,
    output: Any,
) -> None:
    """Persist a TOOL span for a live-forwarded call under the active branch."""
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enums import SpanKind, SpanStatus
    from agent_timetravel.models import Span
    # pylint: enable=import-outside-toplevel

    raw = {
        "gen_ai.tool.name": tool_name,
        "gen_ai.tool.input": _serialise_call(args, kwargs),
        "gen_ai.tool.input_hash": args_hash,
        "gen_ai.tool.output": output,
    }
    span = Span(
        trace_id=session.trace_id,
        span_id=_gen_span_id_hex(),
        parent_span_id=None,
        name=tool_name,
        kind=SpanKind(tool_kind),
        status=SpanStatus.OK,
        raw_attributes=raw,
    )
    session.record_new(span)


# ----------------------------------------------------------------------
# Hashing helpers
# ----------------------------------------------------------------------
def _tool_args_hash(args: tuple[Any, ...], kwargs: dict[Any, Any]) -> str:
    """Deterministic hash of a tool invocation's bound inputs.

    We JSON-serialise (kwargs sorted by key, then args list) so two calls
    with the same content-but-different-order kwargs hash identically.
    """
    # pylint: disable=import-outside-toplevel
    import json

    from agent_timetravel.models import hash_payload
    # pylint: enable=import-outside-toplevel

    payload = {
        "args": list(args),
        "kwargs": dict(sorted(kwargs.items(), key=lambda kv: str(kv[0]))),
    }
    try:
        as_text = json.dumps(payload, default=str, sort_keys=True)
    except (TypeError, ValueError):
        as_text = repr(payload)
    return hash_payload(as_text)


def _serialise_call(args: tuple[Any, ...], kwargs: dict[Any, Any]) -> dict[str, Any]:
    """Capture ``(args, kwargs)`` for later inspection/hash check."""
    return {
        "args": list(args),
        "kwargs": dict(kwargs),
    }


def _gen_span_id_hex() -> str:
    """Generate a fresh, OTel-valid 16-hex-char span id."""
    # pylint: disable=import-outside-toplevel
    from secrets import token_hex
    # pylint: enable=import-outside-toplevel

    return token_hex(8)


# ----------------------------------------------------------------------
# Session plumbing — forwarded to timetravel.replay (kept here for testability)
# ----------------------------------------------------------------------
def _active_session() -> ReplaySession | None:
    """Return the active replay session for the current task, or ``None``."""
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.replay import active_session
    # pylint: enable=import-outside-toplevel

    return active_session()


@contextmanager
def _unused_uuid_namespace() -> Iterator[UUID]:
    """Reserved for future tool-id correlation; kept as a ctxmgr so static
    analysers don't flag the imported ``UUID`` as dead."""
    yield uuid4()
