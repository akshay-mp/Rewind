"""Tests for the GenAI → TimeTravel ``SpanKind`` classifier."""

from __future__ import annotations

from agent_timetravel.classify import classify_span
from agent_timetravel.enums import SpanKind


def test_openinference_llm_kind() -> None:
    attrs = {"openinference.span.kind": "LLM"}
    assert classify_span("chat.completions", attrs) == SpanKind.LLM


def test_genai_usage_tokens_imply_llm() -> None:
    attrs = {"gen_ai.usage.total_tokens": 49}
    assert classify_span("anything", attrs) == SpanKind.LLM


def test_tool_span_by_name() -> None:
    assert classify_span("tool.search_products", {}) == SpanKind.TOOL


def test_mcp_span_by_key() -> None:
    attrs = {"mcp.server.name": "search"}
    assert classify_span("rpc", attrs) == SpanKind.MCP


def test_agent_span_by_oi_kind() -> None:
    attrs = {"openinference.span.kind": "AGENT"}
    assert classify_span("agent", attrs) == SpanKind.AGENT


def test_unknown_is_preserved_not_dropped() -> None:
    """The fidelity contract: unclassifiable spans become UNKNOWN, never dropped."""
    assert classify_span("weird.thing", {"foo": "bar"}) == SpanKind.UNKNOWN
