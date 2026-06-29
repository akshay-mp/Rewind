"""Core domain enums for Rewind.

Mirrors the OpenTelemetry GenAI semantic conventions span `gen_ai.system`
values plus Rewind's internal classification of span kinds. We keep an eye on
`open-telemetry/semantic-conventions-genai` and pin to a known shape; the
mapper in ``models`` reads attributes defensively.
"""

from __future__ import annotations

from enum import UNIQUE, StrEnum, verify


@verify(UNIQUE)
class SpanKind(StrEnum):
    """Discrete classification of an OTel span within an agent trace.

    We intentionally use a *small* closed enum rather than the full OTel
    ``SpanKind`` enum (CLIENT/SERVER/...). Rewind cares about the *agent
    semantics* of a span (was this an LLM call? a tool call? orchestration?),
    which we derive from GenAI semconv attributes on the span.
    """

    #: A model invocation. Carries prompt messages, params, response tokens.
    LLM = "gen_ai.llm"
    #: A tool / function call invoked by the agent.
    TOOL = "gen_ai.tool"
    #: A Model Context Protocol server call (a specialized tool, per semconv).
    MCP = "gen_ai.mcp"
    #: Framework-level orchestration node (e.g. an ADK agent, a LangGraph node).
    AGENT = "gen_ai.agent"
    #: Any span we could not classify. Preserved verbatim, never dropped.
    UNKNOWN = "rewind.unknown"


@verify(UNIQUE)
class ReplayMode(StrEnum):
    """How the replay engine answers a model call during a re-run.

    ``FROZEN``
        Serve the recorded response. Deterministic. Used for stepping
        backward, inspection, and the "no egress" guarantee.
    ``BRANCH``
        Serve recorded responses up to the cursor, then make a *live* call to
        the model from there. Creates a divergent branch.
    ``FULL_RERUN``
        Re-execute everything live. Answers "is this run reproducible?"
        Explicitly non-deterministic.
    """

    FROZEN = "frozen"
    BRANCH = "branch"
    FULL_RERUN = "full"


@verify(UNIQUE)
class SpanStatus(StrEnum):
    """Lifecycle status of a span as observed from OTel.

    OTel uses an ``StatusCode`` enum (``UNSET``/``OK``/``ERROR``) plus an
    optional ``status.message``. We collapse into three Rewind-facing states.
    """

    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


__all__ = ["ReplayMode", "SpanKind", "SpanStatus"]
