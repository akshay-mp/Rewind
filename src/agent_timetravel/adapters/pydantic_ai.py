"""Phase 6 — PydanticAI replay adapter.

Plan §6 Phase 6 — extends adapter-first replay to **PydanticAI**.

PydanticAI exposes :class:`pydantic_ai.models.Model` as the pluggable
slot agents pass to :class:`pydantic_ai.Agent(model=…)` (or override via
:attr:`agent.model`). Subclasses implement ``request([...])`` /
``request_stream([...])`` returning a
:class:`pydantic_ai.messages.ModelResponse` (or a stream). PydanticAI
ships first-party :class:`OpenAIModel`, :class:`GeminiModel` and offers a
:class:`pydantic_ai.models.wrapper.WrappedModel` composition helper.

This module's :func:`replay_model` factory returns a fresh ``Model``
subclass that delegates inbound calls to the wrapped model while
consulting the active :class:`~timetravel.replay.ReplaySession`:

* Active + recorded LLM span ``<= cursor`` → return a recorded
  :class:`TextPart`-shaped :class:`ModelResponse` (zero egress).
* Active + divergence in ``BRANCH`` / ``FULL_RERUN`` → forward to the
  wrapped model and capture the new span under the replay branch.
* Active + divergence in ``FROZEN`` → raise
  :class:`~timetravel.replay.ReplayError`.
* No active session → delegate to the wrapped model verbatim.

The factory pattern mirrors the other adapters: ``Model`` is lazily
imported inside the factory so ``agent-timetravel --version`` stays fast without
``pydantic-ai`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_timetravel.adapters._common import assert_not_frozen, build_live_span
from agent_timetravel.openai_intercept import extract_signature

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.models import Model, ModelRequestParameters, ModelSettings

    from agent_timetravel.replay import ReplaySession

__all__ = ["AdapterError", "replay_model"]


class AdapterError(RuntimeError):
    """Raised when the PydanticAI adapter cannot satisfy a replay contract."""


# ----------------------------------------------------------------------
# Factory
# ----------------------------------------------------------------------
def replay_model(
    wrapped: Model,
    *,
    trace_id: str | None = None,
) -> Model:
    """Wrap a PydanticAI ``Model`` so replay consults the active session.

    Parameters
    ----------
    wrapped:
        The live PydanticAI Model to delegate to when no replay is active
        or when branch / full mode authorises a forward (e.g. an
        ``OpenAIModel``, ``GeminiModel``, or ``AnthropicModel``).
    trace_id:
        Optional explicit trace id. Defaults to the active session's
        ``trace_id`` (resolved per call) so one wrapper follows the
        session across forks.

    Returns
    -------
    Model
        A subclass instance whose ``request`` / ``request_stream`` route
        through TimeTravel. The wrapped model is preserved as
        ``._timetravel_wrapped``.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from pydantic_ai.models import Model
        # pylint: enable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - exercised only without pydantic-ai
        raise AdapterError(
            "agent_timetravel.adapters.pydantic_ai requires `pydantic-ai`; install it via "
            "`pip install agent-timetravel[pydantic-ai]` or use the generic OpenAI "
            "monkey-patch (agent_timetravel.openai_intercept.patch)."
        ) from exc

    class _ReplayModel(Model):  # type: ignore[misc]
        """Subclass created at factory-call time so imports resolve lazily."""

        _timetravel_wrapped: Model = wrapped
        _timetravel_trace_id: str | None = trace_id

        # ------------------------------------------------------------------
        # PydanticAI Model contract
        # ------------------------------------------------------------------
        @property
        def system(self) -> str:
            wrapped_system = getattr(self._timetravel_wrapped, "system", "timetravel")
            return f"timetravel-replay({wrapped_system})"

        @property
        def model_name(self) -> Any:
            return getattr(self._timetravel_wrapped, "model_name", "timetravel-replay")

        @property
        def system_api(self) -> str:
            return getattr(self._timetravel_wrapped, "system_api", "timetravel")

        async def request(
            self,
            messages: list[ModelMessage],
            *,
            model_settings: ModelSettings | None,
            model_request_parameters: ModelRequestParameters,
        ) -> ModelResponse:
            session = self._active_session()
            if session is None:
                return await self._timetravel_wrapped.request(
                    messages,
                    model_settings=model_settings,
                    model_request_parameters=model_request_parameters,
                )
            # Interactive stepping gate: surface the pending call before the
            # dispatch decision. An EDIT decision rewrites the outbound
            # messages in place; STOP raises SteppingStopped. A no-op when no
            # approval channel is attached (see stepping.gate_async).
            messages = await self._step(messages, session)
            signature = self._signature(messages)
            recorded = session.respond_or_forward(signature)
            if recorded is None:
                assert_not_frozen(session)
                result = await self._timetravel_wrapped.request(
                    messages,
                    model_settings=model_settings,
                    model_request_parameters=model_request_parameters,
                )
                self._capture_live_span(messages, session, result, str(self.model_name))
                return result
            return self._materialise(recorded)

        async def request_stream(
            self,
            messages: list[ModelMessage],
            *,
            model_settings: ModelSettings | None,
            model_request_parameters: ModelRequestParameters,
        ) -> Any:
            # Replay semantics for streaming are intentionally simple: we
            # collapse a recorded response to a one-chunk stream by going
            # through the non-streaming ``request``. This is correct for
            # frozen replay (recorded payloads are not chunked) and for
            # branch-mode forwarding (we already capture one span per call,
            # not per chunk). Frameworks that need true streamed chunking
            # can still bypass via the wrapped model directly when no
            # session is active — which is the no-op path above.
            non_stream = await self.request(
                messages,
                model_settings=model_settings,
                model_request_parameters=model_request_parameters,
            )
            return _wrap_as_stream_response(non_stream)

        # ------------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------------
        def _signature(self, messages: list[ModelMessage]) -> Any:
            payload = _messages_to_jsonable(messages)
            return extract_signature(
                model=str(self.model_name),
                messages=payload,
                tools=_extract_tools(messages),
            )

        async def _step(
            self,
            messages: list[ModelMessage],
            session: ReplaySession,
        ) -> list[ModelMessage]:
            """Interactive stepping gate for the async ``request`` path.

            Returns the (possibly edited) message list. Raises
            :class:`~agent_timetravel.stepping.SteppingStopped` on STOP. A pure no-op
            when no approval channel is attached — the common path stays
            unchanged.
            """
            # pylint: disable=import-outside-toplevel
            from agent_timetravel.stepping import (
                DecisionKind,
                Step,
                StepKind,
                SteppingStopped,
                gate_async,
            )
            # pylint: enable=import-outside-toplevel

            step = Step(
                kind=StepKind.LLM,
                payload={
                    "model": str(self.model_name),
                    "messages": _messages_to_jsonable(messages),
                    "tools": _extract_tools(messages),
                },
                cursor=session.cursor,
            )
            decision = await gate_async(session, step)
            if decision is None:
                return messages
            if decision.kind is DecisionKind.STOP:
                raise SteppingStopped(step)
            if decision.kind is DecisionKind.EDIT and decision.messages is not None:
                # Edited messages arrive as plain dicts; PydanticAI's
                # ModelMessage coercion accepts dict payloads, so we hand
                # them straight through. The signature is recomputed below
                # on the edited list, so a divergent edit naturally falls
                # into the live-forward branch.
                return decision.messages
            return messages

        def _materialise(self, recorded: Any) -> ModelResponse:
            payload = recorded.payload or {}
            response = (
                payload.get("gen_ai.response")
                or payload.get("raw_response")
                or payload.get("response")
                or {}
            )
            choices = response.get("choices") or [{}]
            first = choices[0] if isinstance(choices, list) and choices else {}
            content = (
                (first.get("message") or {}).get("content")
                if isinstance(first, dict)
                else ""
            ) or ""
            return _text_model_response(content)

        def _capture_live_span(
            self,
            messages: list[ModelMessage],
            session: ReplaySession,
            result: ModelResponse,
            model_name: str,
        ) -> None:
            content = _model_response_to_text(result)
            span = build_live_span(
                session,
                model_name=model_name,
                messages=_messages_to_jsonable(messages),
                content=content,
            )
            session.record_new(span)

        def _active_session(self) -> ReplaySession | None:
            # pylint: disable=import-outside-toplevel
            from agent_timetravel.replay import active_session
            # pylint: enable=import-outside-toplevel

            session = active_session()
            if session is None:
                return None
            if (
                self._timetravel_trace_id is not None
                and session.trace_id != self._timetravel_trace_id
            ):
                return None
            return session

    instance = _ReplayModel()
    return instance


# ----------------------------------------------------------------------
# PydanticAI message-shape helpers
# ----------------------------------------------------------------------
def _messages_to_jsonable(messages: list[Any]) -> list[dict[str, Any]]:
    """Flatten PydanticAI's typed ModelMessage objects to plain dicts."""
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        if hasattr(msg, "model_dump"):
            out.append(msg.model_dump())
        elif isinstance(msg, dict):
            out.append(msg)
        else:
            role = getattr(msg, "role", getattr(msg, "kind", "user"))
            content = _model_response_to_text(msg)
            out.append({"role": role, "content": content})
    return out


def _extract_tools(messages: list[Any]) -> Any:
    """PydanticAI carries tool definitions in ``ModelRequest`` ``tools``.

    We return them verbatim through ``extract_signature`` which tolerates
    pydantic objects via ``_to_jsonable``.
    """
    for msg in reversed(messages or []):
        tools = getattr(msg, "tools", None)
        if tools:
            return list(tools)
        tools_field = getattr(msg, "tool_definitions", None)
        if tools_field:
            return list(tools_field)
    return None


def _text_model_response(content: str) -> Any:
    """Build a PydanticAI ``ModelResponse`` with a single ``TextPart``."""
    # pylint: disable=import-outside-toplevel
    try:
        from pydantic_ai.messages import ModelResponse, TextPart
    except ImportError as exc:  # pragma: no cover - exercised only without pydantic-ai
        raise AdapterError(
            "agent_timetravel.adapters.pydantic_ai requires `pydantic-ai`; install it via "
            "`pip install agent-timetravel[pydantic-ai]`."
        ) from exc
    # pylint: enable=import-outside-toplevel

    return ModelResponse(parts=[TextPart(content=content)])


def _model_response_to_text(result: Any) -> str:
    """Best-effort text extraction from a PydanticAI response / message."""
    parts = getattr(result, "parts", None) or []
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            chunks.append(str(part.get("content") or part.get("text") or ""))
        elif hasattr(part, "content"):
            chunks.append(str(part.content or ""))
        elif hasattr(part, "text"):
            chunks.append(str(part.text or ""))
    return "\n".join(c for c in chunks if c)


def _wrap_as_stream_response(non_stream: Any) -> Any:
    """Yield a recorded response as a single-chunk async iterator.

    PydanticAI's stream contract expects an async generator of
    ``PartStartEvent`` / ``FinalResultEvent`` shapes today. We emit one
    part-start event followed by ``None`` so the caller's
    ``async for`` loop converges; this is intentionally minimal — agents
    that need token-level streamed replay should turn streaming off and
    use :meth:`request` directly so the recorded payload maps back to a
    single non-stream span (which is what OpenInference records today).
    """
    async def _stream() -> Any:
        yield non_stream

    return _stream()
