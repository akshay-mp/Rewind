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


@verify(UNIQUE)
class EvaluatorKind(StrEnum):
    """The built-in evaluators a Phase 5.5 suite can request.

    These mirror the Agents_Arena evaluator *semantics* — see plan.md §9
    ("Eval harness overlaps with Agents_Arena"). Rewind implements each
    one against its own span model; it does not reuse Agents_Arena's code.

    The five deterministic checks are always available. ``LLM_JUDGE`` is
    opt-in per-suite (deterministic-by-default policy) and requires the
    caller to provide a judge callable.
    """

    TOOL_CHECK = "tool_check"
    GOAL_CHECK = "goal_check"
    CONSISTENCY = "consistency"
    TOKEN_BUDGET = "token_budget"  # noqa: S105 - not a password
    NO_HALLUCINATION = "no_hallucination"
    LLM_JUDGE = "llm_judge"


@verify(UNIQUE)
class EvalVerdict(StrEnum):
    """Per-scenario outcome the harness assigns after running evaluators.

    * ``PASS`` — all enabled evaluators returned pass.
    * ``FAIL`` — at least one evaluator returned fail (deterministic,
      debuggable — the UI hands the user the failing scenario as a branch).
    * ``SKIP`` — the scenario could not run (missing seed, replay error
      before evaluators fired). Skipped scenarios do not affect baseline
      diffs.
    * ``ERROR`` — an evaluator itself crashed. Surfaced distinctly from
      ``FAIL`` so a buggy evaluator doesn't masquerade as an agent regression.
    """

    PASS = "pass"  # noqa: S105 - not a password
    FAIL = "fail"
    SKIP = "skip"
    ERROR = "error"


@verify(UNIQUE)
class CandidateMode(StrEnum):
    """How a Phase 5.5 scenario produces its candidate branch.

    * ``FROZEN`` — serve recorded spans only, no egress. Used to validate
      that a recorded trace still meets its expected (a regression on the
      *evaluator* itself, or a stored-span corruption check).
    * ``BRANCH`` — fork at the seed cursor and forward live from there.
      The default: "run this query against the live model and see what
      changed vs. the seed".
    * ``FULL_RERUN`` — re-execute every span live. The strictest
      reproducibility check.
    """

    FROZEN = "frozen"
    BRANCH = "branch"
    FULL_RERUN = "full"


__all__ = [
    "CandidateMode",
    "EvalVerdict",
    "EvaluatorKind",
    "ReplayMode",
    "SpanKind",
    "SpanStatus",
]
