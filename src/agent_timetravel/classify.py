"""Derive TimeTravel ``SpanKind`` from raw GenAI semconv attributes.

The classifier is the seam where TimeTravel maps OpenInference / OTel GenAI
semconv onto its closed ``SpanKind`` enum. It must be **defensive**: any span
we cannot classify is still preserved (as ``UNKNOWN``), never dropped — that's
the no-fidelity-loss contract.
"""

from __future__ import annotations

from typing import Any

from agent_timetravel.enums import SpanKind


def classify_span(span_name: str, attributes: dict[str, Any]) -> SpanKind:
    """Map a raw OTel span onto a TimeTravel ``SpanKind``.

    Heuristic, in priority order:

    1. ``openinference.span.kind`` (OpenInference-specific, most reliable when
       present): ``LLM``→LLM, ``TOOL``→TOOL, ``AGENT``→AGENT.
    2. ``gen_ai.system`` + presence of ``gen_ai.usage.*`` → ``LLM``.
    3. Span name / attribute keys mentioning ``tool`` or ``mcp`` →
       ``TOOL``/``MCP``.
    4. Fallback ``UNKNOWN`` (preserved, not dropped).
    """
    if not isinstance(attributes, dict):
        return SpanKind.UNKNOWN

    # 1. OpenInference's own kind tag — authoritative when present.
    oi_kind = attributes.get("openinference.span.kind")
    if isinstance(oi_kind, str):
        kind = _from_openinference_kind(oi_kind)
        if kind is not None:
            return kind

    # 2. GenAI semconv usage tokens => an LLM span.
    if any(k.startswith("gen_ai.usage.") for k in attributes):
        return SpanKind.LLM

    # 3. Tool / MCP by name or attribute keys.
    keys = " ".join([span_name, *attributes.keys()]).lower()
    if "mcp" in keys:
        return SpanKind.MCP
    if "tool" in keys:
        return SpanKind.TOOL

    # 4. Orchestrator-ish spans (ADK agent or LangGraph node) — best-effort.
    if "agent" in keys:
        return SpanKind.AGENT

    return SpanKind.UNKNOWN


def _from_openinference_kind(oi_kind: str) -> SpanKind | None:
    """Translate an ``openinference.span.kind`` value, or None if unrecognized.

    Split out of ``classify_span`` to keep that function within pylint's
    branch/return limits without weakening a security-relevant heuristic.
    """
    upper = oi_kind.upper()
    if upper == "LLM":
        return SpanKind.LLM
    if upper == "TOOL":
        return SpanKind.TOOL
    if upper == "AGENT":
        return SpanKind.AGENT
    return None


__all__ = ["classify_span"]
