"""Phase 6 — SmolAgents replay adapter.

Plan §6 Phase 6 — extends adapter-first replay to **HuggingFace SmolAgents**.

SmolAgents' pluggable model slot is ``smolagents.models.Model`` (an
abstract base), with concrete implementations like
:class:`smolagents.HfApiModel` and :class:`smolagents.OpenAIModel`
(default-async via :class:`smolagents.OpenAIServerModel` on recent
versions). Subclasses implement ``__call__(messages, …)`` returning a
``ChatMessage`` / ``List[_MESSAGE]`` (depending on version).

This module's :func:`replay_model` factory returns a fresh ``Model``
subclass that delegates inbound calls to the wrapped SmolAgents model
while consulting the active :class:`~timetravel.replay.ReplaySession`:

* Active + recorded LLM span ``<= cursor`` → return a recorded
  ``ChatMessage`` (zero egress).
* Active + divergence in ``BRANCH`` / ``FULL_RERUN`` → forward to the
  wrapped model and capture the new span under the replay branch.
* Active + divergence in ``FROZEN`` → raise
  :class:`~timetravel.replay.ReplayError`.
* No active session → delegate to the wrapped model verbatim.

Lazy import inside the factory keeps ``agent-timetravel --version`` fast without
``smolagents`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_timetravel.adapters._common import assert_not_frozen, build_live_span
from agent_timetravel.openai_intercept import extract_signature

if TYPE_CHECKING:
    from agent_timetravel.replay import ReplaySession

__all__ = ["AdapterError", "replay_model"]


class AdapterError(RuntimeError):
    """Raised when the SmolAgents adapter cannot satisfy a replay contract."""


def replay_model(
    wrapped: Any,
    *,
    trace_id: str | None = None,
) -> Any:
    """Wrap a SmolAgents ``Model`` so replay consults the active session.

    Parameters
    ----------
    wrapped:
        The live SmolAgents Model to delegate to when no replay is active
        or when branch / full mode authorises a forward
        (e.g. :class:`smolagents.HfApiModel`,
        :class:`smolagents.OpenAIServerModel`…).
    trace_id:
        Optional explicit trace id. Defaults to the active session's
        ``trace_id`` (resolved per call) so one wrapper follows the
        session across forks.

    Returns
    -------
    Model
        A subclass instance whose ``__call__`` routes through TimeTravel. The
        wrapped model is preserved as ``._timetravel_wrapped``.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from smolagents.models import Model
        # pylint: enable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - exercised only without smolagents
        raise AdapterError(
            "agent_timetravel.adapters.smolagents requires `smolagents`; install it via "
            "`pip install agent-timetravel[smolagents]` or use the generic OpenAI "
            "monkey-patch (agent_timetravel.openai_intercept.patch)."
        ) from exc

    class _ReplayModel(Model):  # type: ignore[misc]
        """Subclass created at factory-call time so imports resolve lazily."""

        _timetravel_wrapped: Any = wrapped
        _timetravel_trace_id: str | None = trace_id

        # ------------------------------------------------------------------
        # SmolAgents Model contract
        # ------------------------------------------------------------------
        @property
        def model_id(self) -> str:
            inner = getattr(self._timetravel_wrapped, "model_id", "smolagents")
            return f"timetravel-replay({inner})"

        @model_id.setter
        def model_id(self, value: str) -> None:
            # SmolAgents 1.26 assigns model_id during Model.__init__.
            self._smolagents_model_id = value

        def __call__(
            self,
            messages: list[Any],
            stop_sequences: Any = None,
            tools_to_call_from: Any = None,
            **kwargs: Any,
        ) -> Any:
            session = self._active_session()
            if session is None:
                return self._timetravel_wrapped(
                    messages,
                    stop_sequences=stop_sequences,
                    tools_to_call_from=tools_to_call_from,
                    **kwargs,
                )
            signature = self._signature(messages, kwargs)
            recorded = session.respond_or_forward(signature)
            if recorded is None:
                assert_not_frozen(session)
                result = self._timetravel_wrapped(
                    messages,
                    stop_sequences=stop_sequences,
                    tools_to_call_from=tools_to_call_from,
                    **kwargs,
                )
                self._capture_live_span(messages, session, result,
                                        self._model_id())
                return result
            return self._materialise(recorded, tools_to_call_from)

        async def astream(
            self,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """SmolAgents streaming surface — collapse to one-chunk stream.

            Mirrors the PydanticAI adapter's approach: a recorded payload is
            not chunked so we route it through non-streamed ``__call__`` and
            yield once.
            """
            result = self(*args, **kwargs)
            return _wrap_one_shot_stream(result)

        def generate(
            self,
            messages: list[Any],
            stop: Any = None,
            **kwargs: Any,
        ) -> Any:
            """Older ``generate`` entrypoint still used by some smolagents versions."""
            return self(messages, stop, **kwargs)

        # ------------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------------
        def _model_id(self) -> str:
            return str(
                getattr(self._timetravel_wrapped, "model_id", "smolagents-replay")
                or "smolagents-replay"
            )

        def _signature(self, messages: list[Any], kwargs: Any) -> Any:
            return extract_signature(
                model=self._model_id(),
                messages=_smol_messages_to_jsonable(messages),
                tools=kwargs.get("tools_to_call_from") or kwargs.get("tools") or None,
            )

        def _materialise(self, recorded: Any, tools_to_call_from: Any) -> Any:
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
            return _smol_chat_message(content, tools_to_call_from)

        def _capture_live_span(
            self,
            messages: list[Any],
            session: ReplaySession,
            result: Any,
            model_name: str,
        ) -> None:
            content = _smol_chat_message_to_text(result)
            span = build_live_span(
                session,
                model_name=model_name,
                messages=_smol_messages_to_jsonable(messages),
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
# SmolAgents shape helpers
# ----------------------------------------------------------------------
def _smol_messages_to_jsonable(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        if hasattr(msg, "model_dump"):
            out.append(msg.model_dump())
        elif isinstance(msg, dict):
            out.append(msg)
        else:
            role = getattr(msg, "role", "user")
            content = getattr(msg, "content", "")
            out.append({"role": role, "content": str(content or "")})
    return out


def _smol_chat_message(content: str, tools_to_call_from: Any) -> Any:
    """Build a SmolAgents ``ChatMessage``-shaped object from recorded text.

    SmolAgents' ChatMessage has ``role`` and ``content`` attributes and
    optionally ``tool_calls``. We construct it via the canonical import
    path so callers' downstream attribute access works regardless of
    version-specific constructor signatures. If construction fails
    (different versions have slightly different kwargs), we fall back to a
    minimal duck-typed SimpleNamespace so the recorded payload is still
    reflected into the agent's loop.
    """
    # pylint: disable=import-outside-toplevel
    try:
        from smolagents.messages import ChatMessage
    except ImportError:
        try:
            from smolagents.models import ChatMessage
        except ImportError as exc:  # pragma: no cover - tolerance for shape drift
            raise AdapterError(
                "agent_timetravel.adapters.smolagents couldn't locate `ChatMessage`; "
                "install smolagents with `pip install agent-timetravel[smolagents]`."
            ) from exc
    # pylint: enable=import-outside-toplevel

    try:
        return ChatMessage(role="assistant", content=content)
    # SmolAgents ChatMessage shape drifts across versions (constructor kwargs
    # for tool-calls are not stable). Fall back to a duck-typed object rather
    # than chasing the upstream signature churn.
    except (TypeError, ValueError):
        # Late import so the type namespace is defined on the happy path.
        # pylint: disable=import-outside-toplevel
        from types import SimpleNamespace
        # pylint: enable=import-outside-toplevel
        _ = tools_to_call_from
        return SimpleNamespace(role="assistant", content=content, tool_calls=None)


def _smol_chat_message_to_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, list):
        # SmolAgents sometimes returns a list of messages; concatenate.
        return "\n".join(_smol_chat_message_to_text(m) for m in result)
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    if content is None:
        content = getattr(result, "text", None)
    return str(content or "")


def _wrap_one_shot_stream(result: Any) -> Any:
    """Yield a recorded result as a single-chunk async iterator."""

    async def _stream() -> Any:
        yield result

    return _stream()
