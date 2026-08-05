"""Property-ish tests for the model layer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rewind.enums import ReplayMode, SpanKind
from rewind.models import Branch, Span, hash_payload


def test_replay_modes_are_unique() -> None:
    values = {m.value for m in ReplayMode}
    assert values == {"frozen", "branch", "full", "interactive"}


def test_span_kind_enum_values_match_semconv() -> None:
    assert SpanKind.LLM.value == "gen_ai.llm"
    assert SpanKind.TOOL.value == "gen_ai.tool"
    assert SpanKind.MCP.value == "gen_ai.mcp"
    assert SpanKind.AGENT.value == "gen_ai.agent"


def test_span_rejects_empty_ids() -> None:
    with pytest.raises(ValidationError):
        Span(trace_id="", span_id="a" * 8, name="x")


def test_branch_rejects_negative_index() -> None:
    with pytest.raises(ValidationError):
        Branch(trace_id="t", branch_at_index=-1)


def test_hash_payload_is_deterministic() -> None:
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert hash_payload(a) == hash_payload(b)


def test_span_signature_match(llm_span: Span) -> None:
    """``Span.matches_signature`` lets the replay responder fixture-match."""
    messages = [{"role": "user", "content": "hi"}]
    llm_span2 = llm_span.model_copy(
        update={"messages_hash": hash_payload(messages), "model_name": "qwen3:32b"}
    )
    assert llm_span2.matches_signature("qwen3:32b", messages, None)


def test_extra_fields_forbidden(llm_span: Span) -> None:
    """Pydantic ``extra="forbid"`` keeps the wire format from drifting.

    We construct a fresh Span so validation runs (``model_copy`` with
    ``update=`` skips validation, which is why the original test failed).
    """
    with pytest.raises(ValidationError):
        Span(
            trace_id=llm_span.trace_id,
            span_id=llm_span.span_id,
            name=llm_span.name,
            bogus_field=1,  # type: ignore[call-arg]
        )
