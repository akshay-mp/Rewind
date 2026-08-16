"""Phase 6 — CrewAI replay adapter.

Plan §6 Phase 6 — extends adapter-first replay to **CrewAI**.

CrewAI's pluggable LLM slot is :class:`crewai.LLM` (subclassing
:class:`crewai.llms.base_llm.BaseLLM`). Each agent takes ``agent.llm`` as
an LLM instance, and subclasses implement ``call(messages=…)`` and
``call_async(messages=…)`` returning the raw chat output (string or dict
matching the OpenAI ChatCompletion shape, since CrewAI uses LiteLLM
under the hood).

This module's :func:`replay_llm` factory returns a fresh ``LLM`` subclass
that delegates inbound calls to the wrapped CrewAI LLM while consulting
the active :class:`~rewind.replay.ReplaySession`:

* Active + recorded LLM span ``<= cursor`` → return the recorded response
  (zero egress).
* Active + divergence in ``BRANCH`` / ``FULL_RERUN`` → forward to the
  wrapped LLM and capture the new span under the replay branch.
* Active + divergence in ``FROZEN`` → raise
  :class:`~rewind.replay.ReplayError`.
* No active session → delegate to the wrapped LLM verbatim.

Lazy import inside the factory keeps ``rewind --version`` fast without
``crewai`` installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rewind.adapters._common import assert_not_frozen, build_live_span
from rewind.openai_intercept import extract_signature

if TYPE_CHECKING:
    from crewai.llms.base_llm import BaseLLM

    from rewind.replay import ReplaySession

__all__ = ["AdapterError", "replay_llm"]


class AdapterError(RuntimeError):
    """Raised when the CrewAI adapter cannot satisfy a replay contract."""


def replay_llm(
    wrapped: BaseLLM,
    *,
    trace_id: str | None = None,
) -> BaseLLM:
    """Wrap a CrewAI ``LLM`` / ``BaseLLM`` so replay consults the active session.

    Parameters
    ----------
    wrapped:
        The live CrewAI LLM to delegate to when no replay is active or when
        branch / full mode authorises a forward.
    trace_id:
        Optional explicit trace id. Defaults to the active session's
        ``trace_id`` (resolved per call) so one wrapper follows the
        session across forks.

    Returns
    -------
    BaseLLM
        A subclass instance whose ``call`` / ``call_async`` route through
        Rewind. The wrapped LLM is preserved as ``._rewind_wrapped``.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from crewai.llms.base_llm import BaseLLM
        # pylint: enable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - exercised only without crewai
        raise AdapterError(
            "rewind.adapters.crewai requires `crewai`; install it via "
            "`pip install rewind-debugger[crewai]` or use the generic OpenAI "
            "monkey-patch (rewind.openai_intercept.patch)."
        ) from exc

    class _ReplayLLM(BaseLLM):  # type: ignore[misc]
        """Subclass created at factory-call time so imports resolve lazily."""

        _rewind_wrapped: BaseLLM = wrapped
        _rewind_trace_id: str | None = trace_id

        # ------------------------------------------------------------------
        # CrewAI BaseLLM contract
        # ------------------------------------------------------------------
        @property
        def model(self) -> str:
            return f"rewind-replay({getattr(self._rewind_wrapped, 'model', 'crewai')})"

        @property
        def _llm_type(self) -> str:
            return getattr(self._rewind_wrapped, "_llm_type", "crewai")

        def get_response(self, *args: Any, **kwargs: Any) -> Any:
            """CrewAI's newer sync entrypoint routes through BaseLLM.

            The signature varies across versions (CrewAI 0.70+ accepts
            the tool-calling completion through ``get_response``);
            delegate to ``call`` for the replay-decisión since signatures
            are computed once per message exchange in any case.
            """
            return self.call(*args, **kwargs)

        async def aget_response(self, *args: Any, **kwargs: Any) -> Any:
            return await self.call_async(*args, **kwargs)

        def call(self, messages: list[Any], **kwargs: Any) -> Any:
            session = self._active_session()
            if session is None:
                return self._rewind_wrapped.call(messages, **kwargs)
            signature = self._signature(messages, **kwargs)
            recorded = session.respond_or_forward(signature)
            if recorded is None:
                assert_not_frozen(session)
                result = self._rewind_wrapped.call(messages, **kwargs)
                self._capture_live_span(messages, session, result,
                                        self._model_name(**kwargs))
                return result
            return self._materialise(recorded)

        async def call_async(self, messages: list[Any], **kwargs: Any) -> Any:
            session = self._active_session()
            async_call = getattr(self._rewind_wrapped, "call_async", None)
            if session is None:
                if async_call is not None:
                    return await async_call(messages, **kwargs)
                return self._rewind_wrapped.call(messages, **kwargs)
            signature = self._signature(messages, **kwargs)
            recorded = session.respond_or_forward(signature)
            if recorded is None:
                assert_not_frozen(session)
                if async_call is not None:
                    result = await async_call(messages, **kwargs)
                else:
                    result = self._rewind_wrapped.call(messages, **kwargs)
                self._capture_live_span(messages, session, result,
                                        self._model_name(**kwargs))
                return result
            return self._materialise(recorded)

        def supports_function_calling(self) -> bool:
            return bool(getattr(self._rewind_wrapped, "supports_function_calling", lambda: False)())

        # ------------------------------------------------------------------
        # Helpers
        # ------------------------------------------------------------------
        def _model_name(self, **kwargs: Any) -> str:
            requested = kwargs.get("model")
            if requested:
                return str(requested)
            return str(
                getattr(self._rewind_wrapped, "model", "crewai-replay")
                or "crewai-replay"
            )

        def _signature(self, messages: list[Any], **kwargs: Any) -> Any:
            return extract_signature(
                model=self._model_name(**kwargs),
                messages=messages,
                tools=kwargs.get("tools") or None,
            )

        def _materialise(self, recorded: Any) -> Any:
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
            )
            if not content and isinstance(response, str):
                return response
            # CrewAI's BaseLLM.call usually returns a string (text content);
            # we return the bare content so the framework's downstream parser
            # gets the same shape it does on a live LiteLLM call.
            return content or ""

        def _capture_live_span(
            self,
            messages: list[Any],
            session: ReplaySession,
            result: Any,
            model_name: str,
        ) -> None:
            content = result if isinstance(result, str) else _crewai_result_text(result)
            span = build_live_span(
                session,
                model_name=model_name,
                messages=_crewai_messages_to_jsonable(messages),
                content=content,
            )
            session.record_new(span)

        def _active_session(self) -> ReplaySession | None:
            # pylint: disable=import-outside-toplevel
            from rewind.replay import active_session
            # pylint: enable=import-outside-toplevel

            session = active_session()
            if session is None:
                return None
            if self._rewind_trace_id is not None and session.trace_id != self._rewind_trace_id:
                return None
            return session

    instance = _ReplayLLM()
    return instance


# ----------------------------------------------------------------------
# CrewAI message / result helpers
# ----------------------------------------------------------------------
def _crewai_messages_to_jsonable(messages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        if hasattr(msg, "model_dump"):
            out.append(msg.model_dump())
        elif isinstance(msg, dict):
            out.append(msg)
        else:
            role = getattr(msg, "role", "user")
            content = getattr(msg, "content", getattr(msg, "text", ""))
            out.append({"role": role, "content": str(content or "")})
    return out


def _crewai_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return str(result.get("content") or result.get("response") or "")
    return str(getattr(result, "content", "") or "")
