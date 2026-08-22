"""Phase 6 — CrewAI adapter contract tests (gated on `crewai`).

Skipped unless ``crewai`` is importable. Exercises:

* FROZEN replay returns the recorded payload with zero outbound calls.
* BRANCH replay forwards divergent calls and records a new span.
* No active session → the wrapper is transparent.

Install CrewAI separately to run them::

    pip install crewai
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from agent_timetravel.enums import ReplayMode, SpanKind, SpanStatus
from agent_timetravel.models import Span, Trace, hash_payload
from agent_timetravel.replay import (
    replay as replay_ctx,
)
from agent_timetravel.storage import TraceStore

_HAS_CREWAI = importlib.util.find_spec("crewai") is not None
pytestmark = pytest.mark.skipif(not _HAS_CREWAI, reason="crewai not installed")


_MESSAGES = [{"role": "user", "content": "hello"}]


def _recorded_llm_span(trace_id: str, *, content: str = "recorded") -> Span:
    return Span(
        trace_id=trace_id,
        span_id="a" * 16,
        parent_span_id=None,
        name="crewai.litellm",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name="crewai-test",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        messages_hash=hash_payload(_MESSAGES),
        raw_attributes={
            "gen_ai.request.model": "crewai-test",
            "gen_ai.response": {
                "choices": [{"message": {"role": "assistant", "content": content}}],
            },
        },
    )


def _wrapped_llm() -> tuple[Any, list[Any]]:
    """Build a wrapped CrewAI BaseLLM stand-in plus an outbound-call log."""
    from agent_timetravel.adapters.crewai import replay_llm

    calls: list[Any] = []

    class _Wrapped:  # pylint: disable=too-few-public-methods
        def __init__(self) -> None:
            self.model = "crewai-test"

        def call(self, messages: list[Any], **_kwargs: Any) -> str:
            calls.append(messages)
            return "live-text"

        async def call_async(self, messages: list[Any], **_kwargs: Any) -> str:
            calls.append(messages)
            return "live-text"

        def supports_function_calling(self) -> bool:
            return False

    return replay_llm(_Wrapped()), calls


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    return TraceStore(str(tmp_path / "crewai.db"))


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
    wrapped, calls = _wrapped_llm()
    with replay_ctx(store, trace_id, mode=ReplayMode.FROZEN):
        result = wrapped.call(list(_MESSAGES))
    assert result == "recorded-text"
    assert calls == [], "FROZEN replay must make zero outbound calls"


def test_branch_replay_forwards_divergent_call(
    seeded: tuple[TraceStore, Span], trace_id: str
) -> None:
    store, _span = seeded
    wrapped, calls = _wrapped_llm()
    with replay_ctx(store, trace_id, mode=ReplayMode.BRANCH):
        # Recorded message set: serve from fixture.
        frozen = wrapped.call(list(_MESSAGES))
        assert frozen == "recorded-text"
        assert calls == []
        # Divergence: a new message set never matches a recorded span.
        divergent = wrapped.call([{"role": "user", "content": "a different turn"}])
        assert calls, "BRANCH divergence must forward to the wrapped model"
        assert divergent == "live-text"


def test_no_session_delegates_to_wrapped(
    seeded: tuple[TraceStore, Span],
) -> None:
    """Without an active session, the wrapper is transparent."""
    wrapped, calls = _wrapped_llm()
    wrapped.call(list(_MESSAGES))
    assert len(calls) == 1
