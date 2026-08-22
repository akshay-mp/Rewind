"""Phase 3 — LangGraph / langchain-core chat-model adapter.

Track 3B.1 of plan §6 Phase 3. Provides :func:`replay_chat_model`, a
factory that wraps a real ``langchain_core`` ``BaseChatModel`` so that
during a :func:`timetravel.replay` context, its ``_generate`` / ``_agenerate``
consult the active :class:`~timetravel.replay.ReplaySession`:

* Active + matching recorded LLM span at cursor → return the recorded
  :class:`~langchain_core.outputs.ChatResult` (zero outbound traffic).
* Active + divergence in BRANCH / FULL_RERUN → forward to the wrapped
  model and capture the new span.
* Active + divergence in FROZEN → raise :class:`~timetravel.replay.ReplayError`.
* No active session → delegate to the wrapped model verbatim.

The factory pattern (rather than a top-level class) means **TimeTravel works
on machines without ``langchain_core`` installed** — the import only fires
when the adapter is actually requested. This keeps ``timetravel --version``
fast and the package dependency-light.

Per Phase 3 exit criterion: *at least one framework adapter passes all
replay tests without the generic monkey-patch fallback*. This module is
that adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_timetravel.openai_intercept import extract_signature

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import ChatResult

    from agent_timetravel.replay import ReplaySession

__all__ = ["AdapterError", "replay_chat_model"]


class AdapterError(RuntimeError):
    """Raised when the LangGraph adapter cannot satisfy a replay contract."""


def replay_chat_model(
    wrapped: BaseChatModel,
    *,
    trace_id: str | None = None,
) -> BaseChatModel:
    """Wrap a real ``BaseChatModel`` so replay consults the active session.

    Parameters
    ----------
    wrapped:
        The live model to delegate to when no replay is active or when
        branch/full mode authorises a forward.
    trace_id:
        Optional explicit trace id. Defaults to the active session's
        ``trace_id`` (looked up per call) so a single wrapper follows the
        session across forks.

    Returns
    -------
    BaseChatModel
        A subclass instance whose ``_generate`` / ``_agenerate`` route
        through TimeTravel. The original model is preserved as ``._timetravel_wrapped``.
    """
    # Subclassing BaseChatModel *requires* calling its protected _generate /
    # _agenerate / _llm_type surface — there is no public composition API.
    # The inner class is closure-bound to `wrapped`; extracting it would
    # require passing seven fields through __init__, defeating the laziness.
    # pylint: disable=too-many-statements,protected-access
    # pylint: disable=import-outside-toplevel
    try:
        from langchain_core.language_models.chat_models import (
            BaseChatModel,
        )
        from langchain_core.messages import (
            AIMessage,
        )
        from langchain_core.outputs import (
            ChatGeneration,
            ChatResult,
        )
    except ImportError as exc:  # pragma: no cover - exercised only without langchain
        raise AdapterError(
            "agent_timetravel.adapters.langgraph requires `langchain-core`; install it or use "
            "the generic OpenAI monkey-patch (timetravel.openai_intercept.patch)."
        ) from exc
    # pylint: enable=import-outside-toplevel

    class _ReplayChatModel(BaseChatModel):  # type: ignore[misc]
        """Subclass created at factory-call time so imports resolve lazily."""

        _timetravel_wrapped: BaseChatModel = wrapped
        _timetravel_trace_id: str | None = trace_id

        # ------------------------------------------------------------------
        # langchain_core contract
        # ------------------------------------------------------------------
        @property
        def _llm_type(self) -> str:
            return f"timetravel-replay({self._timetravel_wrapped._llm_type})"

        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> ChatResult:
            session = self._active_session()
            if session is None:
                return self._timetravel_wrapped._generate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            return self._dispatch_sync(messages, session, stop, run_manager, **kwargs)

        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any = None,
            **kwargs: Any,
        ) -> ChatResult:
            session = self._active_session()
            if session is None:
                return await self._timetravel_wrapped._agenerate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
            return await self._dispatch_async(messages, session, stop, run_manager, **kwargs)

        # ------------------------------------------------------------------
        # Dispatch
        # ------------------------------------------------------------------
        def _dispatch_sync(
            self,
            messages: list[BaseMessage],
            session: ReplaySession,
            stop: list[str] | None,
            run_manager: Any,
            **kwargs: Any,
        ) -> ChatResult:
            signature = self._signature(messages, **kwargs)
            recorded = session.respond_or_forward(signature)
            if recorded is None:
                self._assert_not_frozen(session)
                result = self._timetravel_wrapped._generate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
                self._capture_live_span(session, messages, result, signature.model)
                return result
            return self._materialise(recorded)

        async def _dispatch_async(
            self,
            messages: list[BaseMessage],
            session: ReplaySession,
            stop: list[str] | None,
            run_manager: Any,
            **kwargs: Any,
        ) -> ChatResult:
            signature = self._signature(messages, **kwargs)
            recorded = session.respond_or_forward(signature)
            if recorded is None:
                self._assert_not_frozen(session)
                result = await self._timetravel_wrapped._agenerate(
                    messages, stop=stop, run_manager=run_manager, **kwargs
                )
                self._capture_live_span(session, messages, result, signature.model)
                return result
            return self._materialise(recorded)

        # ------------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------------
        def _signature(self, messages: list[BaseMessage], **kwargs: Any) -> Any:
            payload_messages = [m.model_dump() for m in messages]
            return extract_signature(
                model=str(kwargs.get("model") or self._timetravel_wrapped._llm_type),
                messages=payload_messages,
                tools=kwargs.get("tools") or None,
            )

        def _materialise(self, recorded: Any) -> ChatResult:
            payload = recorded.payload or {}
            response = (
                payload.get("gen_ai.response")
                or payload.get("raw_response")
                or payload.get("response")
                or {}
            )
            choices = response.get("choices") or [{}]
            first_choice = choices[0] if isinstance(choices, list) else {}
            content = (
                (first_choice.get("message") or {}).get("content")
                if isinstance(first_choice, dict)
                else ""
            )
            message = AIMessage(content=content or "")
            generation = ChatGeneration(message=message)
            usage = response.get("usage") or {}
            output = {
                "token_usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
            } if isinstance(usage, dict) else {}
            result: ChatResult = ChatResult(generations=[generation], llm_output=output)
            return result

        def _capture_live_span(
            self,
            session: ReplaySession,
            messages: list[BaseMessage],
            result: ChatResult,
            model_name: str,
        ) -> None:
            # pylint: disable=import-outside-toplevel
            from secrets import token_hex

            from agent_timetravel.enums import SpanKind, SpanStatus
            from agent_timetravel.models import Span, hash_payload
            # pylint: enable=import-outside-toplevel

            payload_messages = [m.model_dump() for m in messages]
            content = ""
            if result.generations:
                content = result.generations[0].text or ""
            raw = {
                "gen_ai.request.model": model_name,
                "gen_ai.response": {
                    "choices": [{"message": {"role": "assistant", "content": content}}]
                },
            }
            span = Span(
                trace_id=session.trace_id,
                span_id=token_hex(8),
                parent_span_id=None,
                name=f"langchain.{self._timetravel_wrapped._llm_type}",
                kind=SpanKind.LLM,
                status=SpanStatus.OK,
                model_name=model_name,
                messages_hash=hash_payload(payload_messages),
                raw_attributes=raw,
            )
            session.record_new(span)

        def _assert_not_frozen(self, session: ReplaySession) -> None:
            # pylint: disable=import-outside-toplevel
            from agent_timetravel.enums import ReplayMode
            from agent_timetravel.replay import ReplayError
            # pylint: enable=import-outside-toplevel

            if session.mode is ReplayMode.FROZEN:
                raise ReplayError(
                    "frozen LangGraph replay diverged at cursor="
                    f"{session.cursor}; no recorded fixture to serve"
                )

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

    instance = _ReplayChatModel()
    return instance
