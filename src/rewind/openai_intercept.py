"""Phase 3 — OpenAI Chat Completions interceptor.

Track 3B.2 of plan §6 Phase 3. Patches ``openai.resources.chat.completions.
Completions.create`` and ``AsyncCompletions.create`` (non-streaming) so
that during a :func:`rewind.replay` context:

* Calls matching a recorded LLM span ``<= cursor`` are served from the
  recorded ``raw_attributes`` payload (zero outbound traffic).
* Calls *beyond* the cursor (``BRANCH`` / ``FULL_RERUN`` only) are
  forwarded live and the new span captured under the replay branch.

The interceptor never imports ``openai`` at module load — it does so lazily
on :func:`patch` so projects without ``openai`` installed can still use
the rest of Rewind. It degrades to a no-op when no replay is active.

Reentrancy: the interceptor consults :func:`rewind.replay.active_session`,
a :class:`contextvars.ContextVar`, so concurrent replay sessions in the
Phase 5.5 eval harness are isolated per task. Install/uninstall is also
idempotent — nested ``with patch():`` calls do not double-restore.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rewind.replay import CallSignature, ReplaySession

__all__ = ["InterceptError", "extract_signature", "patch"]


class InterceptError(RuntimeError):
    """Raised when the interceptor cannot satisfy a frozen-replay contract."""


# ----------------------------------------------------------------------
# Signature extraction
# ----------------------------------------------------------------------
def extract_signature(**kwargs: Any) -> CallSignature:
    """Build a :class:`~rewind.replay.CallSignature` from a Chat Completions call.

    Matches the SDK call style ``create(model=..., messages=..., tools=...)``.
    """
    # pylint: disable=import-outside-toplevel
    from rewind.models import hash_payload
    from rewind.replay import CallSignature
    # pylint: enable=import-outside-toplevel

    model = str(kwargs.get("model", ""))
    messages = _to_jsonable(kwargs.get("messages") or [])
    tools_raw = kwargs.get("tools") or None
    tools_jsonable = _to_jsonable(tools_raw) if tools_raw is not None else None

    return CallSignature(
        model=model,
        messages_hash=hash_payload(messages),
        tools_hash=hash_payload(tools_jsonable) if tools_jsonable is not None else None,
    )


def _to_jsonable(value: Any) -> Any:
    """Recursively coerce untyped inputs (pydantic, dataclasses) to plain JSON."""
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return value


# ----------------------------------------------------------------------
# Frozen response re-materialisation
# ----------------------------------------------------------------------
def _materialise_chat_completion(payload: dict[str, Any], sdk_module: Any) -> Any:
    """Re-build an SDK ``ChatCompletion`` from a stored raw payload.

    Recorded ``raw_attributes`` carries the full ``gen_ai.completion`` JSON
    under ``gen_ai.response`` (OpenInference convention) or ``raw_response``
    (older exporters). Falls back to a minimal but valid response shape so
    privacy-skinned exporters still replay.
    """
    response_json = (
        payload.get("gen_ai.response")
        or payload.get("raw_response")
        or payload.get("response")
        or _minimal_response(payload)
    )
    if sdk_module is None:
        return response_json
    construct = getattr(sdk_module, "model_validate", None)
    if construct is None:
        return response_json
    return construct(response_json)


def _minimal_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal but valid Chat Completion payload from typed fields."""
    return {
        "id": "rewind-replay",
        "object": "chat.completion",
        "created": 0,
        "model": payload.get("gen_ai.response.model", "rewind-replay"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": payload.get("gen_ai.usage.prompt_tokens", 0),
            "completion_tokens": payload.get("gen_ai.usage.completion_tokens", 0),
            "total_tokens": payload.get("gen_ai.usage.total_tokens", 0),
        },
    }


# ----------------------------------------------------------------------
# Live span capture
# ----------------------------------------------------------------------
def _capture_live_span(
    session: ReplaySession,
    *,
    kwargs: dict[str, Any],
    response: Any,
    signature_model: str,
) -> None:
    """Build a :class:`~rewind.models.Span` for a live-forwarded call.

    Persisted under ``session.branch_id`` so the live tail queries as a
    distinct branch timeline.
    """
    # pylint: disable=import-outside-toplevel
    from rewind.enums import SpanKind, SpanStatus
    from rewind.models import Span, hash_payload
    # pylint: enable=import-outside-toplevel

    raw = _response_to_raw(response, kwargs)
    span = Span(
        trace_id=session.trace_id,
        span_id=_gen_span_id_hex(),
        parent_span_id=None,
        name="chat.completions.create",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name=signature_model,
        prompt_tokens=raw.get("gen_ai.usage.prompt_tokens"),
        completion_tokens=raw.get("gen_ai.usage.completion_tokens"),
        total_tokens=raw.get("gen_ai.usage.total_tokens"),
        messages_hash=hash_payload(_to_jsonable(kwargs.get("messages") or [])),
        tools_hash=(
            hash_payload(_to_jsonable(kwargs.get("tools")))
            if kwargs.get("tools")
            else None
        ),
        raw_attributes=raw,
    )
    session.record_new(span)


def _response_to_raw(response: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Flatten a live SDK ChatCompletion into the Rewind raw_attributes shape."""
    payload = _to_jsonable(response)
    raw: dict[str, Any] = {}
    raw["gen_ai.request.model"] = str(kwargs.get("model", ""))
    if isinstance(payload, dict):
        raw["gen_ai.response"] = payload
        model = payload.get("model")
        if isinstance(model, str):
            raw["gen_ai.response.model"] = model
        usage = payload.get("usage") or {}
        if isinstance(usage, dict):
            for side in ("prompt_tokens", "completion_tokens", "total_tokens"):
                val = usage.get(side)
                if isinstance(val, int):
                    raw[f"gen_ai.usage.{side}"] = val
    return raw


def _gen_span_id_hex() -> str:
    """Generate a fresh, valid OTel 16-hex-char span id."""
    # pylint: disable=import-outside-toplevel
    from secrets import token_hex
    # pylint: enable=import-outside-toplevel

    return token_hex(8)


# ----------------------------------------------------------------------
# Patch installation
# ----------------------------------------------------------------------
@contextmanager
def patch() -> Iterator[None]:
    """Monkey-patch ``openai`` Chat Completions for the duration of the block.

    Restores the original methods on exit even if the body raises. Installing
    a second time while one is already active is a no-op (so nested replays
    don't double-restore).
    """
    # pylint: disable=import-outside-toplevel
    try:
        import openai.resources.chat.completions as completions_mod
    except ImportError as exc:  # pragma: no cover - exercised only without openai
        raise InterceptError(
            "rewind.replay requires the `openai` package; install it or use the "
            "adapter path (rewind.adapters.<framework>)."
        ) from exc

    from rewind.replay import active_session
    # pylint: enable=import-outside-toplevel

    CompletionsCls = completions_mod.Completions
    AsyncCompletionsCls = getattr(completions_mod, "AsyncCompletions", None)

    if getattr(CompletionsCls.create, "__rewind_patched__", False):
        yield
        return

    orig_sync_create = CompletionsCls.create
    orig_async_create = (
        AsyncCompletionsCls.create if AsyncCompletionsCls is not None else None
    )

    def patched_sync_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        session = active_session()
        if session is None:
            return orig_sync_create(self, *args, **kwargs)
        return _dispatch_sync(self, session, orig_sync_create, args, kwargs)

    async def patched_async_create(self: Any, *args: Any, **kwargs: Any) -> Any:
        session = active_session()
        if session is None:
            return await orig_async_create(self, *args, **kwargs)  # type: ignore[misc]
        return await _dispatch_async(self, session, orig_async_create, args, kwargs)

    patched_sync_create.__rewind_patched__ = True  # type: ignore[attr-defined]
    patched_async_create.__rewind_patched__ = True  # type: ignore[attr-defined]
    CompletionsCls.create = patched_sync_create  # type: ignore[method-assign]
    if AsyncCompletionsCls is not None:
        AsyncCompletionsCls.create = patched_async_create

    try:
        yield
    finally:
        CompletionsCls.create = orig_sync_create  # type: ignore[method-assign]
        if AsyncCompletionsCls is not None and orig_async_create is not None:
            AsyncCompletionsCls.create = orig_async_create


# ----------------------------------------------------------------------
# Dispatch (the actual frozen vs. forward logic)
# ----------------------------------------------------------------------
def _dispatch_sync(
    self: Any,
    session: ReplaySession,
    orig_create: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Decide serve-vs-forward for a sync ``create`` call.

    Streaming frozen replay fails closed — streaming-chunk caching lands
    in Phase 5 polish. ``mode=branch`` forwards live (then captures).
    """
    # pylint: disable=import-outside-toplevel
    from rewind.enums import ReplayMode
    from rewind.replay import ReplayError
    # pylint: enable=import-outside-toplevel

    if kwargs.get("stream") and session.mode is ReplayMode.FROZEN:
        raise ReplayError(
            "frozen streaming replay not yet supported (Phase 5); "
            "use non-streaming calls or mode=branch"
        )
    signature = extract_signature(**kwargs)
    recorded = session.respond_or_forward(signature)
    if recorded is not None:
        return _materialise_chat_completion(recorded.payload, _chat_completion_module())
    response = orig_create(self, *args, **kwargs)
    _capture_live_span(
        session, kwargs=kwargs, response=response, signature_model=signature.model
    )
    return response


async def _dispatch_async(
    self: Any,
    session: ReplaySession,
    orig_create: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Async dual of :func:`_dispatch_sync`."""
    # pylint: disable=import-outside-toplevel
    from rewind.enums import ReplayMode
    from rewind.replay import ReplayError
    # pylint: enable=import-outside-toplevel

    if kwargs.get("stream") and session.mode is ReplayMode.FROZEN:
        raise ReplayError(
            "frozen streaming replay not yet supported (Phase 5); "
            "use non-streaming calls or mode=branch"
        )
    signature = extract_signature(**kwargs)
    recorded = session.respond_or_forward(signature)
    if recorded is not None:
        return _materialise_chat_completion(recorded.payload, _chat_completion_module())
    response = await orig_create(self, *args, **kwargs)
    _capture_live_span(
        session, kwargs=kwargs, response=response, signature_model=signature.model
    )
    return response


def _chat_completion_module() -> Any:
    """Return the SDK module hosting ``ChatCompletion`` (typed or None)."""
    # pylint: disable=import-outside-toplevel
    try:
        from openai.types.chat import ChatCompletion as _cc
    except ImportError:
        return None
    return _cc
    # pylint: enable=import-outside-toplevel
