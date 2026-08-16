"""Phase 6 — HuggingFace SmolAgents adapter contract tests.

Skipped unless ``smolagents`` is importable. Exercises:

* FROZEN replay returns the recorded payload with zero outbound calls.
* No active session → the wrapper is transparent.

Install the extra to run them::

    pip install rewind-debugger[smolagents]
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from rewind.adapters.smolagents import _smol_chat_message, _smol_chat_message_to_text
from rewind.enums import ReplayMode, SpanKind, SpanStatus
from rewind.models import Span, Trace, hash_payload
from rewind.replay import (
    replay as replay_ctx,
)
from rewind.storage import TraceStore

_HAS_SMOL = importlib.util.find_spec("smolagents") is not None
pytestmark = pytest.mark.skipif(not _HAS_SMOL, reason="smolagents not installed")


_MESSAGES = [{"role": "user", "content": "hello"}]


def _recorded_llm_span(trace_id: str, *, content: str = "recorded") -> Span:
    return Span(
        trace_id=trace_id,
        span_id="a" * 16,
        parent_span_id=None,
        name="smolagents.HfApiModel",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name="smol-test",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        messages_hash=hash_payload(_MESSAGES),
        raw_attributes={
            "gen_ai.request.model": "smol-test",
            "gen_ai.response": {
                "choices": [{"message": {"role": "assistant", "content": content}}],
            },
        },
    )


def _wrapped_model() -> tuple[Any, list[Any]]:
    from rewind.adapters.smolagents import replay_model

    calls: list[Any] = []

    class _Wrapped:  # pylint: disable=too-few-public-methods
        model_id = "smol-test"

        def __call__(self, messages: list[Any], **_kwargs: Any) -> Any:
            calls.append(messages)
            return _smol_chat_message("live-text", tools_to_call_from=None)

    return replay_model(_Wrapped()), calls


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    return TraceStore(str(tmp_path / "smol.db"))


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
        result = wrapped.__call__([{"role": "user", "content": "hello"}])
    assert _smol_chat_message_to_text(result) == "recorded-text"
    assert calls == [], "FROZEN replay must make zero outbound calls"


def test_no_session_delegates_to_wrapped(
    seeded: tuple[TraceStore, Span],
) -> None:
    wrapped, calls = _wrapped_model()
    wrapped.__call__([{"role": "user", "content": "hello"}])
    assert len(calls) == 1
