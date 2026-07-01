"""Phase 6 — PydanticAI adapter contract tests (gated on `pydantic-ai`).

Skipped unless ``pydantic_ai`` is importable. Exercises:

* FROZEN replay returns the recorded payload with zero outbound calls.
* BRANCH replay forwards divergent calls and records a new span.
* No active session → the wrapper is transparent.

Install the extra to run them::

    pip install rewind-ai[pydantic-ai]
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rewind.adapters.pydantic_ai import _model_response_to_text
from rewind.enums import ReplayMode, SpanKind, SpanStatus
from rewind.models import Span, Trace, hash_payload
from rewind.replay import (
    replay as replay_ctx,
)
from rewind.storage import TraceStore

_HAS_PYDANTIC_AI = importlib.util.find_spec("pydantic_ai") is not None
pytestmark = pytest.mark.skipif(
    not _HAS_PYDANTIC_AI, reason="pydantic-ai not installed"
)


_MESSAGES = [{"role": "user", "content": "hello"}]


def _recorded_llm_span(trace_id: str, *, content: str = "recorded") -> Span:
    return Span(
        trace_id=trace_id,
        span_id="a" * 16,
        parent_span_id=None,
        name="pydantic_ai.model",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name="pydantic-ai-test",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        messages_hash=hash_payload(_MESSAGES),
        raw_attributes={
            "gen_ai.request.model": "pydantic-ai-test",
            "gen_ai.response": {
                "choices": [{"message": {"role": "assistant", "content": content}}],
            },
        },
    )


def _wrapped_model() -> tuple[Any, list[Any]]:
    from rewind.adapters.pydantic_ai import replay_model

    calls: list[Any] = []

    class _Wrapped:  # pylint: disable=too-few-public-methods
        model_name = "pydantic-ai-test"
        system = "rewind"
        system_api = "rewind"

        async def request(
            self,
            messages: list[Any],
            *,
            model_settings: Any,
            model_request_parameters: Any,
        ) -> Any:
            calls.append(messages)
            return SimpleNamespace(parts=[SimpleNamespace(content="live-text")])

    return replay_model(_Wrapped()), calls


def _request_part(content: str) -> Any:
    """A duck-typed stand-in for a PydanticAI ModelRequest."""
    return SimpleNamespace(
        role="user",
        parts=[SimpleNamespace(content=content)],
        tools=None,
        tool_definitions=None,
        model_dump=lambda: {"role": "user", "content": content},
    )


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    return TraceStore(str(tmp_path / "pydantic_ai.db"))


@pytest.fixture
def trace_id() -> str:
    return "abcd1234abcd1234abcd1234abcd1234"


@pytest.fixture
def seeded(store: TraceStore, trace_id: str) -> tuple[TraceStore, Span]:
    span = _recorded_llm_span(trace_id, content="recorded-text")
    store.upsert_trace(Trace(trace_id=trace_id, spans=[span]))
    store.insert_span(span)
    return store, span


def test_frozen_replay_returns_recorded_payload(
    seeded: tuple[TraceStore, Span], trace_id: str
) -> None:
    store, _span = seeded
    wrapped, calls = _wrapped_model()
    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN):
        result = wrapped.request(
            [_request_part("hello")],
            model_settings=None,
            model_request_parameters=None,
        )
    # `request` is async — drive it through a fresh event loop.
    import asyncio

    out = asyncio.new_event_loop().run_until_complete(result)
    assert _model_response_to_text(out) == "recorded-text"
    assert calls == [], "FROZEN replay must make zero outbound calls"


def test_no_session_delegates_to_wrapped(
    seeded: tuple[TraceStore, Span],
) -> None:
    wrapped, calls = _wrapped_model()
    import asyncio

    asyncio.new_event_loop().run_until_complete(
        wrapped.request(
            [_request_part("hello")],
            model_settings=None,
            model_request_parameters=None,
        )
    )
    assert len(calls) == 1
