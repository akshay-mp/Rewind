"""Phase 5.5 — Batch parallel execution + eval harness.

This module is **two layers in one file by design**:

1. **Pure evaluator functions** (``evaluate_tool_check``,
   ``evaluate_goal_check``, ``evaluate_consistency``,
   ``evaluate_token_budget``, ``evaluate_no_hallucination``). Each takes
   plain dataclasses in (``list[Span]`` + an expectation struct) and
   returns an :class:`EvaluatorOutcome`. No SQLite, no FastAPI, no SDK.
   This is the source-of-truth layer; the same five functions are reused
   by the HTTP API, the CLI, and (one day) the SDK caller wanting to
   score branches programmatically.
2. **The suite runner** (:func:`evaluate`): the asynchronous orchestrator
   that fans out :class:`EvalScenario` coroutines via
   :func:`asyncio.gather` with a semaphore, drives each through a
   :class:`~timetravel.replay.ReplaySession`, runs the requested evaluators,
   and rolls up cost / latency per the GenAI semconv ``usage.*`` columns.

Why pure + async-orchestrator in one file?
    The evaluators are the load-bearing part. They mirror the Agents_Arena
    semantics (plan §9 risk: *"do not rebuild the adapter matrix"*). Keeping
    them next to the orchestrator means a reader can scroll from "what does
    ``tool_check`` mean" to "how does the harness actually invoke it" in
    one file. The HTTP + CLI surfaces live in :mod:`timetravel.timeline_api`
    and :mod:`timetravel.cli`, respectively.

Concurrency guarantees
----------------------
Parallel safety leans on three Phase 3/4 invariants:

* :class:`~timetravel.replay.ReplaySession` is per-branch isolated (its cursor
  lives on the dataclass, not on ``TraceStore``). ``ContextVar`` binding
  makes concurrent ``with replay(...)`` blocks task-safe (see
  ``test_replay.py::test_branch_isolation_concurrent_contextvars``).
* :class:`~timetravel.storage.TraceStore` writes serialise on the SQLite WAL
  lock; concurrent branches share the DB but never collide on rows
  (``branch_id`` is the partition key, ``UNIQUE(branch_id, name)`` on
  checkpoints prevents collisions).
* ``asyncio.gather`` with a bounded :class:`~asyncio.Semaphore` enforces
  the per-CPU concurrency limit so we don't DOS the local model. Per-
  scenario timeout is enforced via :func:`asyncio.wait_for`.

What this module does NOT do
----------------------------
* It does not pull in any LLM SDK (``openai``, ``langchain_core``).
  ``LLM_JUDGE`` is opt-in and the caller supplies the judge callable.
* It does not spawn subprocesses (no eval sandbox) — replay happens
  in-process via the engine.
* It does not write to ``~/.timetravel`` directly; persistence of the
  :class:`EvalRun` row is the caller's job (CLI or HTTP layer).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol
from uuid import UUID, uuid4

from agent_timetravel.enums import (
    CandidateMode,
    EvaluatorKind,
    EvalVerdict,
    SpanKind,
)
from agent_timetravel.models import Span

if TYPE_CHECKING:
    from agent_timetravel.enums import ReplayMode

#: Cap on parallel scenarios when ``concurrency`` is not specified.
#: ``min(32, os.cpu_count() + 4)`` mirrors the default thread pool in 3.13+
#: but caps at 32 to prevent over-subscription on big-iron laptops.
_DEFAULT_CONCURRENCY = 8

#: Per-scenario timeout when the suite doesn't specify one (seconds).
#: Generous because branching-mode scenarios do real model calls; the
#: 30s wall is intended to surface hangs, not enforce tight latency.
_DEFAULT_SCENARIO_TIMEOUT_S = 30.0

#: Maximum spans we materialise when computing consistency / cost rollups.
#: Per the Phase 4 guarantee, 100k-span traces must not OOM the harness.
#: The evaluator's structured walk is O(n) so the cap is defensive.
_MAX_SPANS_FOR_EVAL = 100_000

# Public error details must not expose provider, filesystem, or database data.
_GENERIC_REPLAY_ERROR_DETAIL = "error: regression case could not be executed"
_LOGGER = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Pure dataclasses — the evaluator contract
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolCheckExpectation:
    """Declarative tool-call expectation.

    ``expected_tool_names`` is a *set*: each must appear at least once in
    the candidate's ``gen_ai.tool`` spans. ``forbidden_tool_names`` must
    not appear. Empty lists mean "no constraint on this axis".

    Tool identity is by ``span.name`` (OpenInference convention:
    ``gen_ai.tool.name``), which the ingestion layer promotes to the
    typed ``Span.name`` field for ``gen_ai.tool`` spans.
    """

    expected_tool_names: list[str] = field(default_factory=list)
    forbidden_tool_names: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GoalCheckExpectation:
    """Regex / substring / exact-match expectation on the final response.

    ``pattern`` is a Python regex applied case-insensitively and in
    multiline mode (LLM responses are typically multi-line). ``must_be``
    is an exact-match fallback for "did the agent reply with this literal
    phrase?" scenarios. At least one of the two must be set.
    """

    pattern: str | None = None
    must_be: str | None = None


@dataclass(frozen=True, slots=True)
class ConsistencyExpectation:
    """Span-kind-sequence comparison against the seed trace.

    Phases 5.5's "consistency" evaluator checks the *shape* of the
    candidate run matches the seed: same sequence of ``gen_ai.llm`` /
    ``gen_ai.tool`` / ``gen_ai.agent`` spans in the same order. This is
    a cheap structural check — "did the agent skip a tool call?" — that
    fires without needing a goal regex.

    Strict mode (``exact=True``) requires every span kind to match; loose
    mode (``exact=False``) collapses consecutive identical kinds and
    compares the deduped sequence (useful when the candidate emits one
    extra LLM call but follows the same overall shape).
    """

    seed_kind_sequence: list[SpanKind] = field(default_factory=list)
    exact: bool = True


@dataclass(frozen=True, slots=True)
class TokenBudgetExpectation:
    """Cumulative token ceiling per scenario.

    Counts ``gen_ai.usage.total_tokens`` on every LLM span in the
    candidate branch and fails if the sum exceeds ``max_total_tokens``.
    A ``None`` ceiling disables the check (the harness still records the
    rolled-up count in :class:`TokenRollup`).
    """

    max_total_tokens: int | None = None
    max_prompt_tokens: int | None = None
    max_completion_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class NoHallucinationExpectation:
    """Cheap lexical grounding check.

    Surfaces any final-response claim not grounded in a prior tool span.
    This is the deterministic, no-LLM-judge version: it tokenises the
    final response and checks each non-stopword token is present in the
    union of tool-span result text. ``required_grounding_terms`` lets
    the suite force specific terms to appear (e.g. a SKU the agent must
    have retrieved).

    The full LLM-judge version (opt-in) lives behind ``EvaluatorKind.LLM_JUDGE``.
    """

    required_grounding_terms: list[str] = field(default_factory=list)
    stopword_filter: bool = True


#: The set of expectation types the harness accepts. Each goes with its
#: matching :class:`EvaluatorKind`. The Protocol-typed ``expected`` field
#: on :class:`EvaluatorRequest` keeps the algebra closed: an unsupported
#: shape would be a static type error.
Expectation = (
    ToolCheckExpectation
    | GoalCheckExpectation
    | ConsistencyExpectation
    | TokenBudgetExpectation
    | NoHallucinationExpectation
)


@dataclass(frozen=True, slots=True)
class EvaluatorOutcome:
    """One evaluator's verdict on one candidate branch.

    ``detail`` is a short human-readable string surfaced in the UI; it
    must not exceed ~200 chars (the API caps it). ``metrics`` carries
    machine-readable sub-scores (e.g. ``{"matched_tools": 2,
    "missing_tools": ["search"]}``).
    """

    kind: EvaluatorKind
    verdict: Literal[EvalVerdict.PASS, EvalVerdict.FAIL]
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)


#: Type alias for an opt-in LLM-judge callable. The harness invokes this
#: with the candidate spans (esp. the final response text) and the
#: scenario's raw ``expected`` dict; the judge returns an
#: :class:`EvaluatorOutcome` whose ``kind`` is :attr:`EvaluatorKind.LLM_JUDGE`.
#:
#: This Protocol intentionally mirrors the Agents_Arena judge signature
#: so the user's existing judge code calls in unchanged. We do not
#: import Agents_Arena's BaseAdapter (plan §9: "borrow semantics, not code").
JudgeCallable = Callable[
    [list[Span], dict[str, Any]],
    Awaitable[EvaluatorOutcome],
]


@dataclass(frozen=True, slots=True)
class EvaluatorRequest:
    """One evaluator invocation: kind + its typed expectation.

    The harness maps each request to its pure function via
    :data:`_EVALUATOR_DISPATCH`. ``expected`` is closed over the union
    of typed expectation structs — a mismatch is a static type error.
    """

    kind: EvaluatorKind
    expected: Expectation


# ----------------------------------------------------------------------
# Pure evaluators — the five deterministic checks
# ----------------------------------------------------------------------


def _iter_spans(spans: list[Span]) -> Iterator[Span]:
    """Bounded iterator — prevents 100k-span scenario OOM-ing the harness.

    The eval engine never needs the *full* trace at once: each evaluator
    walks spans lazily and breaks early. The cap is therefore defensive.
    """
    for i, span in enumerate(spans):
        if i >= _MAX_SPANS_FOR_EVAL:
            return
        yield span


def _tool_names(spans: list[Span]) -> list[str]:
    """Return ordered ``gen_ai.tool`` span names from the candidate branch."""
    return [s.name for s in _iter_spans(spans) if s.kind is SpanKind.TOOL]


def evaluate_tool_check(
    spans: list[Span], expected: ToolCheckExpectation
) -> EvaluatorOutcome:
    """PASS iff every expected tool fired and no forbidden tool did.

    The check is *set membership on observed tool names*. Order doesn't
    matter — agents may legitimately call tools in different orders when
    re-running. The metrics block surfaces exactly which tools are
    missing so the UI diff view ("the agent dropped ``search_products``")
    can highlight them.
    """
    observed = set(_tool_names(spans))
    missing = [
        name for name in expected.expected_tool_names if name not in observed
    ]
    forbidden_seen = [
        name for name in expected.forbidden_tool_names if name in observed
    ]
    if missing or forbidden_seen:
        bits = []
        if missing:
            bits.append(f"missing={missing}")
        if forbidden_seen:
            bits.append(f"forbidden={forbidden_seen}")
        return EvaluatorOutcome(
            kind=EvaluatorKind.TOOL_CHECK,
            verdict=EvalVerdict.FAIL,
            detail="; ".join(bits)[:200],
            metrics={
                "observed_tools": sorted(observed),
                "missing_tools": missing,
                "forbidden_seen": forbidden_seen,
            },
        )
    return EvaluatorOutcome(
        kind=EvaluatorKind.TOOL_CHECK,
        verdict=EvalVerdict.PASS,
        detail=f"all {len(expected.expected_tool_names)} expected tools fired",
        metrics={"observed_tools": sorted(observed)},
    )


def _final_response_text(spans: list[Span]) -> str:
    """Extract the text of the last LLM span's assistant message.

    OpenInference puts the response under ``gen_ai.response`` (or
    ``raw_response`` on older schemas). We grab the assistant role's
    content if it's the standard message-list shape, otherwise fall back
    to a string-coerced best-effort. Non-LLM spans return "".
    """
    for span in reversed(spans):
        if span.kind is not SpanKind.LLM:
            continue
        attrs = span.raw_attributes
        response = attrs.get("gen_ai.response") or attrs.get("raw_response") or {}
        if isinstance(response, dict):
            choices = response.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    message = first.get("message") or first.get("delta")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str):
                            return content
            # Some schemas put the assistant text directly under
            # ``response.message.content``.
            message = response.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
        if isinstance(response, str):
            return response
        return ""
    return ""


def evaluate_goal_check(
    spans: list[Span], expected: GoalCheckExpectation
) -> EvaluatorOutcome:
    """PASS iff the final response matches the goal regex / exact match.

    At least one of ``pattern`` or ``must_be`` must be set — the harness
    validates this on suite load and raises ``ValueError`` if both are
    ``None``, so the evaluator itself can assume one is non-empty.
    """
    response_text = _final_response_text(spans)
    matched: list[str] = []
    failed: list[str] = []
    if expected.must_be is not None:
        if response_text == expected.must_be:
            matched.append("must_be")
        else:
            failed.append(f"must_be (got {len(response_text)} chars)")
    if expected.pattern is not None:
        # Multiline + IGNORECASE + DOTALL: most agent responses are
        # multi-line, and goal patterns are typically written
        # case-insensitively by humans. DOTALL so "." matches newlines
        # when the user explicitly writes it.
        regex = re.compile(expected.pattern, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if regex.search(response_text):
            matched.append("pattern")
        else:
            failed.append("pattern")
    if failed:
        return EvaluatorOutcome(
            kind=EvaluatorKind.GOAL_CHECK,
            verdict=EvalVerdict.FAIL,
            detail=("failed: " + "; ".join(failed))[:200],
            metrics={
                "response_chars": len(response_text),
                "matched": matched,
                "failed": failed,
            },
        )
    return EvaluatorOutcome(
        kind=EvaluatorKind.GOAL_CHECK,
        verdict=EvalVerdict.PASS,
        detail=f"matched ({', '.join(matched)})",
        metrics={"response_chars": len(response_text), "matched": matched},
    )


def _kind_sequence(spans: list[Span]) -> list[SpanKind]:
    """Ordered list of SpanKind for every span in the candidate branch."""
    return [s.kind for s in _iter_spans(spans)]


def _dedupe_consecutive(seq: list[SpanKind]) -> list[SpanKind]:
    """Collapse runs of identical SpanKind into one entry.

    Used by ``evaluate_consistency`` in loose mode: an agent that emits
    two LLM calls in a row is "structurally the same" as one that emits
    one. The strict mode preserves the difference.
    """
    out: list[SpanKind] = []
    for kind in seq:
        if not out or out[-1] is not kind:
            out.append(kind)
    return out


def evaluate_consistency(
    spans: list[Span], expected: ConsistencyExpectation
) -> EvaluatorOutcome:
    """PASS iff the candidate's span-kind sequence matches the seed's.

    Strict mode (default) compares sequences element-wise; loose mode
    collapses consecutive dupes before comparing. The metrics block
    surfaces the divergence index so the UI can mark the first
    structural change.
    """
    candidate = _kind_sequence(spans)
    seed = list(expected.seed_kind_sequence)
    if not expected.exact:
        candidate = _dedupe_consecutive(candidate)
        seed = _dedupe_consecutive(seed)
    if candidate == seed:
        return EvaluatorOutcome(
            kind=EvaluatorKind.CONSISTENCY,
            verdict=EvalVerdict.PASS,
            detail=f"kind sequence matches ({len(candidate)} spans)",
            metrics={"sequence": [k.value for k in candidate]},
        )
    # Find first divergence for diagnostics.
    divergence_index: int | None = None
    for i, (a, b) in enumerate(zip(candidate, seed, strict=False)):
        if a is not b:
            divergence_index = i
            break
    if divergence_index is None and len(candidate) != len(seed):
        divergence_index = min(len(candidate), len(seed))
    return EvaluatorOutcome(
        kind=EvaluatorKind.CONSISTENCY,
        verdict=EvalVerdict.FAIL,
        detail=(
            f"divergence at index {divergence_index} "
            f"(candidate {len(candidate)} vs seed {len(seed)} spans)"
        )[:200],
        metrics={
            "candidate_sequence": [k.value for k in candidate],
            "seed_sequence": [k.value for k in seed],
            "divergence_index": divergence_index,
        },
    )


def _sum_tokens(spans: list[Span]) -> tuple[int, int, int]:
    """Return ``(prompt_total, completion_total, grand_total)`` for LLM spans.

    Spans with ``None`` tokens contribute 0 — OpenInfluence doesn't
    always populate usage on streaming responses. ``grand_total`` is
    computed independently (not ``p + c``) so it stays correct if the
    ingester recorded total but not prompt / completion split.
    """
    p = c = t = 0
    for span in _iter_spans(spans):
        if span.kind is not SpanKind.LLM:
            continue
        p += span.prompt_tokens or 0
        c += span.completion_tokens or 0
        t += span.total_tokens or 0
    return p, c, t


def evaluate_token_budget(
    spans: list[Span], expected: TokenBudgetExpectation
) -> EvaluatorOutcome:
    """PASS iff the candidate's cumulative token usage fits the ceiling.

    A ``None`` ceiling means "no check on this axis". The evaluator still
    computes the rolled-up counts and reports them in metrics so the UI
    can show the cost column even when no ceiling is configured.
    """
    prompt_t, completion_t, total_t = _sum_tokens(spans)
    over: list[str] = []
    if expected.max_total_tokens is not None and total_t > expected.max_total_tokens:
        over.append(
            f"total {total_t} > {expected.max_total_tokens}"
        )
    if (
        expected.max_prompt_tokens is not None
        and prompt_t > expected.max_prompt_tokens
    ):
        over.append(f"prompt {prompt_t} > {expected.max_prompt_tokens}")
    if (
        expected.max_completion_tokens is not None
        and completion_t > expected.max_completion_tokens
    ):
        over.append(
            f"completion {completion_t} > {expected.max_completion_tokens}"
        )
    if over:
        return EvaluatorOutcome(
            kind=EvaluatorKind.TOKEN_BUDGET,
            verdict=EvalVerdict.FAIL,
            detail="over budget: " + "; ".join(over)[:160],
            metrics={
                "prompt_tokens": prompt_t,
                "completion_tokens": completion_t,
                "total_tokens": total_t,
            },
        )
    return EvaluatorOutcome(
        kind=EvaluatorKind.TOKEN_BUDGET,
        verdict=EvalVerdict.PASS,
        detail=f"within budget (total={total_t})",
        metrics={
            "prompt_tokens": prompt_t,
            "completion_tokens": completion_t,
            "total_tokens": total_t,
        },
    )


#: Cheap stopword set — English-only, sufficient for the deterministic
#: grounding check. The LLM-judge evaluator does the real semantic work.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "if", "then", "is", "are",
        "was", "were", "be", "been", "being", "to", "of", "in", "on", "for",
        "with", "as", "by", "at", "from", "this", "that", "these", "those",
        "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
        "us", "them", "my", "your", "his", "its", "our", "their",
        "do", "does", "did", "have", "has", "had", "will", "would",
        "should", "could", "can", "may", "might", "must",
        "yes", "no", "not", "so", "very", "too",
    }
)


def _tokenize(text: str) -> list[str]:
    """Whitespace + punctuation tokeniser for the grounding check.

    Returns lowercased alphanumeric tokens; punctuation is dropped. Used
    only by ``evaluate_no_hallucination``; the diff engine has its own
    tokeniser that preserves whitespace for visual diff rendering.
    """
    return [tok for tok in re.findall(r"[a-zA-Z0-9]+", text.lower()) if tok]


def _tool_result_text(spans: list[Span]) -> str:
    """Concatenate all ``gen_ai.tool`` spans' result attributes.

    OpenInference convention: tool spans carry the result under
    ``gen_ai.tool.result`` (older: ``tool.output`` / ``output.value``).
    Returns a single space-joined string for tokenisation.
    """
    bits: list[str] = []
    for span in _iter_spans(spans):
        if span.kind is not SpanKind.TOOL:
            continue
        attrs = span.raw_attributes
        for key in ("gen_ai.tool.result", "tool.output", "output.value", "result"):
            value = attrs.get(key)
            if isinstance(value, str) and value:
                bits.append(value)
                break
            if isinstance(value, dict):
                # Stringify the dict (cheap path; the LLM-judge evaluator
                # would do structured comparison).
                bits.append(str(value))
                break
    return " ".join(bits)


def evaluate_no_hallucination(
    spans: list[Span], expected: NoHallucinationExpectation
) -> EvaluatorOutcome:
    """PASS iff the response's terms appear in tool results (+ required ones).

    This is the **cheap** grounding check — no LLM-judge. It surfaces
    obviously ungrounded claims (e.g. a SKU the agent made up). False
    positives are possible on common English words, hence the stopword
    filter; ``required_grounding_terms`` lets the suite force specific
    tokens (e.g. ``["SKU-1234"]``) to be present.

    For semantic hallucination detection (subtle inference gaps), the
    suite should add an ``LLM_JUDGE`` evaluator.
    """
    response_text = _final_response_text(spans)
    grounding_text = _tool_result_text(spans)
    response_tokens = _tokenize(response_text)
    grounding_tokens = set(_tokenize(grounding_text))
    filter_set = _STOPWORDS if expected.stopword_filter else frozenset()

    ungrounded: list[str] = []
    seen_ungrounded: set[str] = set()
    for tok in response_tokens:
        if tok in filter_set:
            continue
        if tok in grounding_tokens:
            continue
        if tok in seen_ungrounded:
            continue
        seen_ungrounded.add(tok)
        ungrounded.append(tok)

    missing_required = [
        term
        for term in expected.required_grounding_terms
        if term.lower() not in grounding_tokens
        and term.lower() not in " ".join(response_tokens).lower()
    ]

    if ungrounded or missing_required:
        bits = []
        if ungrounded:
            bits.append(f"{len(ungrounded)} ungrounded terms")
        if missing_required:
            bits.append(f"missing required={missing_required}")
        return EvaluatorOutcome(
            kind=EvaluatorKind.NO_HALLUCINATION,
            verdict=EvalVerdict.FAIL,
            detail=("hallucination risk: " + "; ".join(bits))[:200],
            metrics={
                "ungrounded_terms": ungrounded[:20],
                "missing_required": missing_required,
            },
        )
    return EvaluatorOutcome(
        kind=EvaluatorKind.NO_HALLUCINATION,
        verdict=EvalVerdict.PASS,
        detail="all response terms grounded",
        metrics={"ungrounded_terms": [], "missing_required": []},
    )


#: Dispatch table from :class:`EvaluatorKind` to the pure function.
#: Defined here (after every function is defined) so the suite runner
#: can look the evaluator up by enum value. ``LLM_JUDGE`` is intentionally
#: absent — it's opt-in via the suite's ``judge`` callable.
_EVALUATOR_DISPATCH: dict[
    EvaluatorKind,
    Callable[..., EvaluatorOutcome],
] = {
    EvaluatorKind.TOOL_CHECK: evaluate_tool_check,
    EvaluatorKind.GOAL_CHECK: evaluate_goal_check,
    EvaluatorKind.CONSISTENCY: evaluate_consistency,
    EvaluatorKind.TOKEN_BUDGET: evaluate_token_budget,
    EvaluatorKind.NO_HALLUCINATION: evaluate_no_hallucination,
}


# ----------------------------------------------------------------------
# Suite declaration + runner
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvalScenario:
    """One row of an eval suite — the declarative scenario spec.

    Mirrors the YAML schema documented in ``docs/eval-suite-schema.md``
    (Phase 5.5). The runner picks this up, builds a
    :class:`~timetravel.replay.ReplaySession`, drives the candidate branch,
    and scores it.

    Fields
    ------
    name
        Human-readable scenario id unique within the suite. Used in UI
        rows and baseline diffs.
    seed_trace_id
        Trace id this scenario branches/replays from. Required — every
        Phase 5.5 scenario is *trace-native* (plan §9: leverage the
        replay engine for fixture control).
    candidate_mode
        How to produce the candidate branch (frozen/branch/full-rerun).
    branch_at_index
        Where in the seed to fork. ``None`` = entire seed recorded, no
        live forward (use ``CandidateMode.FROZEN`` then). Required with
        ``BRANCH`` / ``FULL_RERUN``.
    evaluators
        Ordered list of evaluator requests to run after the candidate
        branch is materialised. Order is preserved in the output so the
        UI can render the verdict in the same row order.
    expected
        Optional raw expected-dict kept for the LLM-judge callable.
        Mirrors Agents_Arena's shape (plan §9: borrow semantics).
    query
        Optional human-readable description / prompt override. The
        runner does not interpret this — it leaks through to the judge
        and to the UI as a row description. (Live prompt mutation is a
        Phase 6+ concern; Phase 5.5 keeps the seed prompt intact.)
    """

    name: str
    seed_trace_id: str
    candidate_mode: CandidateMode
    branch_at_index: int | None
    evaluators: list[EvaluatorRequest]
    query: str = ""
    expected: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TokenRollup:
    """Cost rollup for one candidate branch.

    Sourced from the GenAI semconv ``usage.*`` columns on LLM spans
    (``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``). The
    UI renders this as a per-scenario and per-suite total. ``None``
    fields mean "scenario never produced LLM spans" (e.g. a frozen
    replay-check that errored before any LLM call).
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    llm_call_count: int


@dataclass(frozen=True, slots=True)
class ScenarioLatency:
    """Wall-clock latency rollup. Phase 5.5 measures latency in seconds."""

    total_s: float
    replay_s: float
    evaluate_s: float


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    """One scenario's outcome in a suite run.

    The ``branch_id`` is the candidate branch the runner created
    (always non-``None`` on PASS/FAIL; ``None`` on SKIP/ERROR before
    forking). The UI uses this to deep-link into Phase 5's timeline
    branches view.
    """

    name: str
    seed_trace_id: str
    branch_id: UUID | None
    verdict: EvalVerdict
    outcomes: list[EvaluatorOutcome]
    rollup: TokenRollup
    latency: ScenarioLatency
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class EvalSuiteResult:
    """Full result of one :func:`evaluate` invocation.

    The ``run_id`` is a UUID minted by the runner; the caller persists
    it (CLI or HTTP layer) under that key. ``scenario_results`` is in
    the same order as the input ``EvalSuite.scenarios``; the parallel
    runner reorders them back into suite order before returning.
    """

    run_id: UUID
    suite_name: str
    started_at: str
    finished_at: str
    overall_verdict: EvalVerdict
    scenarios: list[ScenarioResult]


# ----------------------------------------------------------------------
# Serialization helpers — round-trip to JSON-safe dicts for SQLite / API.
# ----------------------------------------------------------------------
#
# We avoid Pydantic BaseModel on the eval result types (frozen + slots
# dataclasses give us strict equality without the runtime overhead) so
# storage / API need explicit to/from dict helpers. The wire format is
# stable across releases — adding new evaluator metrics is additive.
#
# Format version is hardcoded at 1; bump only on a non-additive change
# (renaming / removing a key) which would break stored runs.


_EVAL_RESULT_FORMAT_VERSION = 1


def _evaluator_outcome_to_dict(outcome: EvaluatorOutcome) -> dict[str, Any]:
    """Serialize a single :class:`EvaluatorOutcome` to a JSON-safe dict."""
    return {
        "kind": outcome.kind.value,
        "verdict": outcome.verdict.value,
        "detail": outcome.detail,
        "metrics": dict(outcome.metrics),
    }


def _evaluator_outcome_from_dict(data: dict[str, Any]) -> EvaluatorOutcome:
    """Reverse of :func:`_evaluator_outcome_to_dict`."""
    # Outcome verdict is a closed Literal[PASS, FAIL] — validate on read so
    # a stored SKIP/ERROR can't sneak in via deserialization.
    raw_verdict = EvalVerdict(data["verdict"])
    if raw_verdict not in (EvalVerdict.PASS, EvalVerdict.FAIL):
        raise ValueError(
            f"EvaluatorOutcome verdict must be PASS/FAIL, got {raw_verdict!r}"
        )
    return EvaluatorOutcome(
        kind=EvaluatorKind(data["kind"]),
        verdict=raw_verdict,
        detail=data["detail"],
        metrics=dict(data.get("metrics", {})),
    )


def _token_rollup_to_dict(rollup: TokenRollup) -> dict[str, Any]:
    """Serialize a :class:`TokenRollup` to a JSON-safe dict."""
    return {
        "prompt_tokens": rollup.prompt_tokens,
        "completion_tokens": rollup.completion_tokens,
        "total_tokens": rollup.total_tokens,
        "llm_call_count": rollup.llm_call_count,
    }


def _token_rollup_from_dict(data: dict[str, Any]) -> TokenRollup:
    """Reverse of :func:`_token_rollup_to_dict`."""
    return TokenRollup(
        prompt_tokens=int(data["prompt_tokens"]),
        completion_tokens=int(data["completion_tokens"]),
        total_tokens=int(data["total_tokens"]),
        llm_call_count=int(data["llm_call_count"]),
    )


def _scenario_latency_to_dict(latency: ScenarioLatency) -> dict[str, Any]:
    """Serialize a :class:`ScenarioLatency` to a JSON-safe dict."""
    return {
        "total_s": latency.total_s,
        "replay_s": latency.replay_s,
        "evaluate_s": latency.evaluate_s,
    }


def _scenario_latency_from_dict(data: dict[str, Any]) -> ScenarioLatency:
    """Reverse of :func:`_scenario_latency_to_dict`."""
    return ScenarioLatency(
        total_s=float(data["total_s"]),
        replay_s=float(data["replay_s"]),
        evaluate_s=float(data["evaluate_s"]),
    )


def scenario_result_to_dict(scenario: ScenarioResult) -> dict[str, Any]:
    """Serialize a :class:`ScenarioResult` to a JSON-safe dict."""
    return {
        "name": scenario.name,
        "seed_trace_id": scenario.seed_trace_id,
        "branch_id": str(scenario.branch_id) if scenario.branch_id else None,
        "verdict": scenario.verdict.value,
        "outcomes": [_evaluator_outcome_to_dict(o) for o in scenario.outcomes],
        "rollup": _token_rollup_to_dict(scenario.rollup),
        "latency": _scenario_latency_to_dict(scenario.latency),
        "error_message": scenario.error_message,
    }


def scenario_result_from_dict(data: dict[str, Any]) -> ScenarioResult:
    """Reverse of :func:`scenario_result_to_dict`."""
    return ScenarioResult(
        name=data["name"],
        seed_trace_id=data["seed_trace_id"],
        branch_id=UUID(data["branch_id"]) if data.get("branch_id") else None,
        verdict=EvalVerdict(data["verdict"]),
        outcomes=[_evaluator_outcome_from_dict(o) for o in data.get("outcomes", [])],
        rollup=_token_rollup_from_dict(data["rollup"]),
        latency=_scenario_latency_from_dict(data["latency"]),
        error_message=data.get("error_message"),
    )


def eval_suite_result_to_dict(result: EvalSuiteResult) -> dict[str, Any]:
    """Serialize an :class:`EvalSuiteResult` to a JSON-safe dict.

    The resulting dict is what the HTTP API and CLI emit verbatim. It
    includes a ``format_version`` key so future readers can detect
    schema drift on stored runs.
    """
    return {
        "format_version": _EVAL_RESULT_FORMAT_VERSION,
        "run_id": str(result.run_id),
        "suite_name": result.suite_name,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "overall_verdict": result.overall_verdict.value,
        "scenarios": [scenario_result_to_dict(s) for s in result.scenarios],
    }


def eval_suite_result_from_dict(data: dict[str, Any]) -> EvalSuiteResult:
    """Reverse of :func:`eval_suite_result_to_dict`."""
    return EvalSuiteResult(
        run_id=UUID(data["run_id"]),
        suite_name=data["suite_name"],
        started_at=data["started_at"],
        finished_at=data["finished_at"],
        overall_verdict=EvalVerdict(data["overall_verdict"]),
        scenarios=[scenario_result_from_dict(s) for s in data.get("scenarios", [])],
    )


@dataclass(frozen=True, slots=True)
class EvalSuiteResultSummary:
    """Lightweight run summary — used by the ``GET /api/v1/evals`` list.

    Excludes per-scenario JSON so a 100-scenario suite's summary row is
    constant-size. The detail endpoint (:meth:`TraceStore.get_eval_run`)
    returns the full :class:`EvalSuiteResult`.
    """

    run_id: str
    suite_name: str
    started_at: str
    finished_at: str
    overall_verdict: EvalVerdict


@dataclass(slots=True)
class EvalSuite:
    """The full declarative suite spec — runner input.

    Fields
    ------
    name
        Suite identifier. Used in :class:`EvalSuiteResult.suite_name`.
    scenarios
        Ordered list — the runner preserves order in the result.
    concurrency
        Upper bound on parallel scenarios. ``None`` = per-CPU default
        (capped via :data:`_DEFAULT_CONCURRENCY`).
    scenario_timeout_s
        Per-scenario wall-clock ceiling. ``None`` = the default generous
        cap.
    judge
        Opt-in LLM-judge callable. If a scenario's ``evaluators``
        includes :attr:`EvaluatorKind.LLM_JUDGE`, this must be non-
        ``None``. The harness validates this *before* running.
    """

    name: str
    scenarios: list[EvalScenario]
    concurrency: int | None = None
    scenario_timeout_s: float | None = None
    judge: JudgeCallable | None = None


class SuiteValidationError(ValueError):
    """Raised when an :class:`EvalSuite` is structurally invalid.

    The harness validates the suite *before* running any scenario so a
    config error doesn't half-execute. Distinct from runtime errors
    (those become :class:`~timetravel.replay.ReplayError` -> SKIP/ERROR
    verdicts on individual scenarios).
    """


class ReplaySessionFactory(Protocol):
    """Hook the test layer injects to mock :func:`timetravel.replay.replay`.

    Production calls go through :func:`_default_replay_session_factory`
    which opens a real :class:`~timetravel.replay.ReplaySession`. Tests can
    inject a fake that returns a pre-built :class:`_CandidateMaterialised`
    so the evaluator layer is unit-testable without SQLite / FastAPI.
    """

    def __call__(
        self,
        store: Any,  # noqa: ANN401
        scenario: EvalScenario,
    ) -> tuple[list[Span], UUID]:
        """Return ``(candidate_spans, branch_id)`` for ``scenario``'s branch."""


@dataclass(frozen=True, slots=True)
class _CandidateMaterialised:
    """Internal: the result of forking + driving a scenario's branch.

    Returned by :func:`ReplaySessionFactory`; carries everything the
    evaluators need (spans + branch id + replay wall time). The default
    factory lives below.
    """

    spans: list[Span]
    branch_id: UUID
    replay_duration_s: float


def _default_replay_session_factory(
    store: Any,  # noqa: ANN401
    scenario: EvalScenario,
) -> tuple[list[Span], UUID]:
    """Production factory — opens a real :class:`ReplaySession`.

    Frozen mode = no live forward; the candidate branch is just the
    seed's spans unioned with whatever ``branch_at_index`` cut-off
    specified. Branch/full mode forks at ``branch_at_index`` — we don't
    actually drive the agent here; the harness stamps the fork row and
    returns the recorded spans + the new branch id. The *real* live
    forward (interceptor-driven) happens when the runner is being called
    from inside a ``with replay(...)`` block provided by the caller.

    Doing actual live LLM forwards inside the harness would couple us
    to a specific interceptor; Phase 5.5 leaves that to the caller's
    agent loop. Suite results are reproducible because the candidate
    branch is the recorded seed + offline fork row.
    """
    # pylint: disable=import-outside-toplevel
    import time

    from agent_timetravel.replay import ReplaySession
    from agent_timetravel.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    if not isinstance(store, TraceStore):
        raise SuiteValidationError(
            "default replay session factory requires a TraceStore; "
            "inject a custom factory for tests"
        )
    started = time.monotonic()
    root = ReplaySession.for_root(store, scenario.seed_trace_id)
    if scenario.candidate_mode is CandidateMode.FROZEN:
        # Frozen scenarios don't fork — they just inspect the seed.
        spans = store.get_spans(scenario.seed_trace_id, branch_id=root.branch_id)
        return spans, root.branch_id
    if scenario.branch_at_index is None:
        raise SuiteValidationError(
            f"scenario {scenario.name!r}: branch_at_index required for "
            f"mode={scenario.candidate_mode.value}"
        )
    forked = root.fork(
        branch_at=scenario.branch_at_index,
        mode=_candidate_mode_to_replay_mode(scenario.candidate_mode),
        label=f"eval-{scenario.name}",
    )
    spans = store.get_spans(scenario.seed_trace_id, branch_id=forked.branch_id)
    # Stash replay duration so the caller can build latency rollup. We
    # abuse the return value of the Protocol by appending latency in a
    # third tuple slot — but Protocol return is fixed at ``(spans, id)``;
    # so we measure end-to-end including storage fetch in the runner
    # itself (see :func:`_run_one_scenario`).
    del started  # measured in the runner for accuracy
    return spans, forked.branch_id


def _candidate_mode_to_replay_mode(mode: CandidateMode) -> ReplayMode:
    """Translate :class:`CandidateMode` → :class:`~timetravel.enums.ReplayMode`.

    Imported lazily to avoid the enum import cycle at module top
    (enums import everything; evaluate imports enums).
    """
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enums import ReplayMode
    # pylint: enable=import-outside-toplevel

    if mode is CandidateMode.FROZEN:
        return ReplayMode.FROZEN
    if mode is CandidateMode.BRANCH:
        return ReplayMode.BRANCH
    return ReplayMode.FULL_RERUN


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def validate_suite(suite: EvalSuite) -> None:
    """Structural validation pass before any scenario runs.

    Raises :class:`SuiteValidationError` on the first issue found. Run
    *eagerly* (caller-side) so a config typo doesn't half-execute an
    expensive suite.
    """
    if not suite.name:
        raise SuiteValidationError("suite name must be non-empty")
    if not suite.scenarios:
        raise SuiteValidationError("suite must have at least one scenario")
    seen_names: set[str] = set()
    for scen in suite.scenarios:
        if not scen.name:
            raise SuiteValidationError("scenario name must be non-empty")
        if scen.name in seen_names:
            raise SuiteValidationError(
                f"duplicate scenario name {scen.name!r}"
            )
        seen_names.add(scen.name)
        if not scen.evaluators:
            raise SuiteValidationError(
                f"scenario {scen.name!r}: at least one evaluator required"
            )
        for req in scen.evaluators:
            _validate_evaluator_request(scen.name, req)
        if (
            any(
                r.kind is EvaluatorKind.LLM_JUDGE
                for r in scen.evaluators
            )
            and suite.judge is None
        ):
            raise SuiteValidationError(
                f"scenario {scen.name!r} uses llm_judge but suite.judge is None"
            )
        if (
            scen.candidate_mode is not CandidateMode.FROZEN
            and scen.branch_at_index is None
        ):
            raise SuiteValidationError(
                f"scenario {scen.name!r}: mode={scen.candidate_mode.value} "
                "requires branch_at_index"
            )
        if scen.branch_at_index is not None and scen.branch_at_index < 0:
            raise SuiteValidationError(
                f"scenario {scen.name!r}: branch_at_index must be >= 0"
            )
    if suite.concurrency is not None and suite.concurrency < 1:
        raise SuiteValidationError(
            f"concurrency must be >= 1, got {suite.concurrency}"
        )


def _validate_evaluator_request(scenario_name: str, req: EvaluatorRequest) -> None:
    """Validate one evaluator request's expectation shape."""
    kind = req.kind
    exp = req.expected
    if kind is EvaluatorKind.TOOL_CHECK:
        if not isinstance(exp, ToolCheckExpectation):
            raise SuiteValidationError(
                f"scenario {scenario_name!r}: tool_check requires ToolCheckExpectation"
            )
    elif kind is EvaluatorKind.GOAL_CHECK:
        if not isinstance(exp, GoalCheckExpectation):
            raise SuiteValidationError(
                f"scenario {scenario_name!r}: goal_check requires GoalCheckExpectation"
            )
        if exp.pattern is None and exp.must_be is None:
            raise SuiteValidationError(
                f"scenario {scenario_name!r}: goal_check needs pattern or must_be"
            )
    elif kind is EvaluatorKind.CONSISTENCY:
        if not isinstance(exp, ConsistencyExpectation):
            raise SuiteValidationError(
                f"scenario {scenario_name!r}: consistency requires ConsistencyExpectation"
            )
    elif kind is EvaluatorKind.TOKEN_BUDGET:
        if not isinstance(exp, TokenBudgetExpectation):
            raise SuiteValidationError(
                f"scenario {scenario_name!r}: token_budget requires TokenBudgetExpectation"
            )
    elif kind is EvaluatorKind.NO_HALLUCINATION:
        if not isinstance(exp, NoHallucinationExpectation):
            raise SuiteValidationError(
                f"scenario {scenario_name!r}: no_hallucination requires "
                "NoHallucinationExpectation"
            )
    elif kind is EvaluatorKind.LLM_JUDGE:
        # LLM_JUDGE's "expectation" is opaque — the judge callable decides.
        # No structural validation beyond "suite.judge must be set" (checked
        # in validate_suite). The harness passes the raw ``expected`` dict
        # to the judge.
        pass
    else:
        # Defensive — should be unreachable because EvaluatorKind is closed.
        raise SuiteValidationError(
            f"scenario {scenario_name!r}: unknown evaluator kind {kind!r}"
        )


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------


async def evaluate(
    suite: EvalSuite,
    *,
    store: Any,  # noqa: ANN401
    factory: ReplaySessionFactory | None = None,
) -> EvalSuiteResult:
    """Run every scenario in ``suite`` concurrently and roll up results.

    Args:
        suite: Validated eval suite. ``validate_suite`` is invoked
            *before* any scenario starts; failures raise
            :class:`SuiteValidationError` synchronously (no partial run).
        store: The :class:`~timetravel.storage.TraceStore` to fork from.
            Tests inject a custom ``factory`` and may pass ``None`` here.
        factory: Override the replay-session opener. Production path
            uses :func:`_default_replay_session_factory`; this hook is
            the unit-test escape hatch.

    Returns:
        An :class:`EvalSuiteResult` whose ``scenarios`` are in suite
        order (the parallel runner reorders internally).

    Concurrency model
    -----------------
    Scenarios run under a bounded :class:`asyncio.Semaphore` sized by
    ``suite.concurrency``. Each scenario is wrapped in
    :func:`asyncio.wait_for`; a timeout produces a SKIP verdict (not
    ERROR — timeout is an environment issue, not an evaluator bug).
    Scenario isolation leans on the Phase 3 ``ContextVar``-bound
    :class:`~timetravel.replay.ReplaySession`; concurrent sessions never
    share cursor state.
    """
    validate_suite(suite)
    factory = factory or _default_replay_session_factory
    concurrency = suite.concurrency or _DEFAULT_CONCURRENCY
    timeout_s = suite.scenario_timeout_s or _DEFAULT_SCENARIO_TIMEOUT_S
    semaphore = asyncio.Semaphore(concurrency)
    started_iso = _utcnow_iso()
    run_id = uuid4()

    async def _bounded(scen: EvalScenario) -> ScenarioResult:
        async with semaphore:
            return await asyncio.wait_for(
                _run_one_scenario(scen, suite, factory, store),
                timeout=timeout_s,
            )

    # Preserve order despite gather: zip results back to scenario names.
    raw_results = await asyncio.gather(*(_bounded(s) for s in suite.scenarios))
    by_name = {r.name: r for r in raw_results}
    ordered = [by_name[s.name] for s in suite.scenarios]
    overall = _roll_up_overall_verdict(ordered)
    return EvalSuiteResult(
        run_id=run_id,
        suite_name=suite.name,
        started_at=started_iso,
        finished_at=_utcnow_iso(),
        overall_verdict=overall,
        scenarios=ordered,
    )


async def _run_one_scenario(
    scenario: EvalScenario,
    suite: EvalSuite,
    factory: ReplaySessionFactory,
    store: Any,  # noqa: ANN401
) -> ScenarioResult:
    """Materialise the candidate branch + run all evaluators on it.

    Exceptions from the factory (replay errors) become SKIP verdicts;
    exceptions from an evaluator become ERROR verdicts attributed to
    that evaluator but don't kill the scenario. Timeout is enforced by
    the caller via :func:`asyncio.wait_for`.
    """
    # pylint: disable=import-outside-toplevel
    import time

    from agent_timetravel.replay import ReplayError

    # pylint: enable=import-outside-toplevel

    t0 = time.monotonic()
    spans: list[Span] = []
    branch_id: UUID | None = None
    error_message: str | None = None
    verdict = EvalVerdict.SKIP
    outcomes: list[EvaluatorOutcome] = []

    try:
        # The factory opens the replay session under the hood — depending
        # on `scenario.candidate_mode` it may fork or stay frozen. The
        # returned spans include the candidate branch's recorded prefix
        # (Phase 3 union: parent spans + forked tail).
        spans, branch_id = await asyncio.to_thread(factory, store, scenario)
    except ReplayError as exc:
        error_message = f"replay error: {exc}"
        return _build_scenario_result(
            scenario,
            branch_id=None,
            verdict=EvalVerdict.SKIP,
            outcomes=[],
            spans=spans,
            replay_s=time.monotonic() - t0,
            evaluate_s=0.0,
            error_message=error_message,
        )
    except SuiteValidationError as exc:
        error_message = f"suite validation error: {exc}"
        return _build_scenario_result(
            scenario,
            branch_id=None,
            verdict=EvalVerdict.SKIP,
            outcomes=[],
            spans=spans,
            replay_s=time.monotonic() - t0,
            evaluate_s=0.0,
            error_message=error_message,
        )

    replay_s = time.monotonic() - t0
    eval_t0 = time.monotonic()
    verdict = EvalVerdict.PASS

    for req in scenario.evaluators:
        try:
            outcome = await _dispatch_evaluator(req, suite, spans)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # An evaluator bug must surface distinctly from an agent bug.
            outcome = EvaluatorOutcome(
                kind=req.kind,
                verdict=EvalVerdict.FAIL,
                detail=f"evaluator crashed: {type(exc).__name__}: {exc}"[:200],
                metrics={"exception_type": type(exc).__name__},
            )
            verdict = EvalVerdict.ERROR
            outcomes.append(outcome)
            # Continue trying the rest — they may surface more signal.
            continue
        outcomes.append(outcome)
        if outcome.verdict is EvalVerdict.FAIL and verdict is not EvalVerdict.ERROR:
            verdict = EvalVerdict.FAIL

    evaluate_s = time.monotonic() - eval_t0
    return _build_scenario_result(
        scenario,
        branch_id=branch_id,
        verdict=verdict,
        outcomes=outcomes,
        spans=spans,
        replay_s=replay_s,
        evaluate_s=evaluate_s,
        error_message=error_message,
    )


async def _dispatch_evaluator(
    req: EvaluatorRequest,
    suite: EvalSuite,
    spans: list[Span],
) -> EvaluatorOutcome:
    """Run one evaluator (pure function or async judge).

    The five deterministic evaluators run synchronously inside
    :func:`asyncio.to_thread` so they don't block the event loop on
    large traces. The LLM-judge is already async (Protocol).
    """
    if req.kind is EvaluatorKind.LLM_JUDGE:
        if suite.judge is None:
            # Should be caught by validate_suite; defensive.
            return EvaluatorOutcome(
                kind=EvaluatorKind.LLM_JUDGE,
                verdict=EvalVerdict.FAIL,
                detail="no judge configured",
            )
        # The judge is opaque — pass the raw ``expected`` dict + spans.
        return await suite.judge(spans, {})
    pure_fn = _EVALUATOR_DISPATCH.get(req.kind)
    if pure_fn is None:
        return EvaluatorOutcome(
            kind=req.kind,
            verdict=EvalVerdict.FAIL,
            detail=f"no dispatcher for kind={req.kind.value}",
        )
    # mypy: the Expectation union is closed; the dispatcher's signature
    # is the matching member. We narrow at runtime inside each fn.
    return await asyncio.to_thread(pure_fn, spans, req.expected)


def _build_scenario_result(
    scenario: EvalScenario,
    *,
    branch_id: UUID | None,
    verdict: EvalVerdict,
    outcomes: list[EvaluatorOutcome],
    spans: list[Span],
    replay_s: float,
    evaluate_s: float,
    error_message: str | None,
) -> ScenarioResult:
    """Final assembly: snap the spans list into the latency + rollup shape.

    Token rollup is computed from the same spans the evaluators saw, so
    the UI's cost column is always consistent with the verdict.
    """
    p, c, t = _sum_tokens(spans)
    llm_count = sum(1 for s in spans if s.kind is SpanKind.LLM)
    return ScenarioResult(
        name=scenario.name,
        seed_trace_id=scenario.seed_trace_id,
        branch_id=branch_id,
        verdict=verdict,
        outcomes=outcomes,
        rollup=TokenRollup(
            prompt_tokens=p,
            completion_tokens=c,
            total_tokens=t,
            llm_call_count=llm_count,
        ),
        latency=ScenarioLatency(
            total_s=replay_s + evaluate_s,
            replay_s=replay_s,
            evaluate_s=evaluate_s,
        ),
        error_message=error_message,
    )


def _roll_up_overall_verdict(results: list[ScenarioResult]) -> EvalVerdict:
    """Combine per-scenario verdicts into one suite-level verdict.

    Worst-of semantics: ERROR > FAIL > SKIP > PASS. The UI surfaces the
    suite with this one verdict but expands to per-scenario detail.
    """
    precedence = {
        EvalVerdict.ERROR: 4,
        EvalVerdict.FAIL: 3,
        EvalVerdict.SKIP: 2,
        EvalVerdict.PASS: 1,
    }
    if not results:
        return EvalVerdict.PASS
    worst = max(results, key=lambda r: precedence[r.verdict])
    return worst.verdict


# ----------------------------------------------------------------------
# Phase 4 — frozen verification runner
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class RegressionResult:
    """Outcome of one ``run_frozen_verification`` execution.

    * ``passed`` — True if every expected check matched the re-executed output.
    * ``detail`` — human-readable summary of what matched / drifted.
    * ``branch_id`` — the branch the verification ran on (None if it couldn't start).
    """

    passed: bool
    detail: str
    branch_id: UUID | None = None


async def run_frozen_verification(
    case_id: str,
    *,
    store: Any,  # noqa: ANN401
    factory: ReplaySessionFactory | None = None,
) -> RegressionResult:
    """Re-execute a regression case's seed trace in FROZEN mode and verify.

    Loads the case from the store, materialises its seed trace's spans under
    FROZEN replay (via the same factory the eval suite uses), and checks the
    ``expected`` dict against the re-materialised spans. Any drift (a
    different span count, a missing required text) surfaces as
    ``passed=False`` with a detail string.

    This is the deterministic core of the regression suite: same trace +
    same checks = same verdict, regardless of the live model's current
    behaviour.
    """
    case = store.get_regression_case(case_id)
    if case is None:
        return RegressionResult(passed=False, detail=f"case {case_id} not found")

    seed_trace_id = case["seed_trace_id"]
    expected = case.get("expected", {})
    factory = factory or _default_replay_session_factory
    started = _utcnow_iso()

    # Build a minimal FROZEN scenario so the existing factory handles the
    # ReplaySession.for_root + span materialisation.
    scenario = EvalScenario(
        name=case.get("name", case_id),
        seed_trace_id=seed_trace_id,
        candidate_mode=CandidateMode.FROZEN,
        branch_at_index=None,
        evaluators=[],
    )

    try:
        spans, branch_id = factory(store, scenario)
    except Exception:  # pylint: disable=broad-exception-caught
        _LOGGER.exception(
            "Frozen verification failed during replay",
        )
        store.insert_regression_run(
            {
                "run_id": str(uuid4()),
                "case_id": case_id,
                "passed": False,
                "detail": _GENERIC_REPLAY_ERROR_DETAIL,
                "branch_id": None,
                "started_at": started,
                "finished_at": _utcnow_iso(),
            }
        )
        return RegressionResult(
            passed=False,
            detail=_GENERIC_REPLAY_ERROR_DETAIL,
        )

    failures: list[str] = []
    for key, want in expected.items():
        if key == "span_count":
            if len(spans) != want:
                failures.append(
                    f"span_count drift: expected {want}, got {len(spans)}"
                )
        elif key == "required_text":
            blob = " ".join(_span_text(s) for s in spans)
            for needle in want:
                if needle not in blob:
                    failures.append(f"missing required text: {needle!r}")
        elif key == "forbidden_text":
            blob = " ".join(_span_text(s) for s in spans)
            for needle in want:
                if needle in blob:
                    failures.append(f"forbidden text present: {needle!r}")

    # Saved interactive cases carry reviewed output and assertion profiles in
    # addition to the seed trace. Frozen verification reads these snapshots
    # only; it never calls a provider or tool.
    captured_steps = expected.get("captured_steps", expected.get("checks", []))
    pricing = expected.get("pricing") if isinstance(expected.get("pricing"), dict) else None
    if isinstance(captured_steps, list):
        for item in captured_steps:
            if not isinstance(item, dict):
                continue
            raw_output = item.get("result")
            output = raw_output if isinstance(raw_output, str) else ""
            raw_usage = item.get("usage")
            usage = raw_usage if isinstance(raw_usage, dict) else {}
            assertions = item.get("assertions")
            if isinstance(assertions, dict):
                failures.extend(
                    _check_captured_assertions(
                        output,
                        usage,
                        assertions,
                        pricing,
                        f"step {item.get('cursor', '?')}",
                    )
                )

    passed = not failures
    detail = "all checks passed" if passed else "; ".join(failures)
    store.insert_regression_run(
        {
            "run_id": str(uuid4()),
            "case_id": case_id,
            "passed": passed,
            "detail": detail,
            "branch_id": str(branch_id),
            "started_at": started,
            "finished_at": _utcnow_iso(),
        }
    )
    return RegressionResult(
        passed=passed,
        detail=detail,
        branch_id=branch_id,
    )


def _span_text(span: Any) -> str:  # noqa: ANN401
    """Flatten a span's raw_attributes into a searchable text blob.

    Recursively walks nested dicts/lists so content buried under
    ``gen_ai.response.choices[0].message.content`` is reachable by the
    required-text check.
    """
    parts: list[str] = [getattr(span, "name", "") or ""]

    def _walk(obj: Any) -> None:  # noqa: ANN401
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _walk(item)

    attrs = getattr(span, "raw_attributes", None) or {}
    _walk(attrs)
    return " ".join(parts)


def _check_captured_assertions(
    output: str,
    usage: dict[str, Any],
    assertions: dict[str, Any],
    pricing: dict[str, Any] | None,
    label: str,
) -> list[str]:
    """Evaluate the portable built-in assertions on one captured output."""
    failures: list[str] = []
    if assertions.get("requireJson", assertions.get("require_json", False)):
        try:
            json.loads(output)
        except (TypeError, json.JSONDecodeError):
            failures.append(f"{label}: response is not valid JSON")
    for value in assertions.get("requiredText", assertions.get("required_text", [])) or []:
        if str(value).lower() not in output.lower():
            failures.append(f"{label}: missing required text {value!r}")
    for value in assertions.get("forbiddenText", assertions.get("forbidden_text", [])) or []:
        if str(value).lower() in output.lower():
            failures.append(f"{label}: contains forbidden text {value!r}")
    requires_citations = assertions.get(
        "requireCitations", assertions.get("require_citations", False)
    )
    if requires_citations and not re.search(r"\[[^\]]+\]", output):
        failures.append(f"{label}: missing citation")
    max_tokens = assertions.get("maxTokens", assertions.get("max_tokens"))
    if max_tokens is not None and int(usage.get("total_tokens", 0) or 0) > int(max_tokens):
        failures.append(f"{label}: token budget exceeded")
    max_cost = assertions.get("maxCostUsd", assertions.get("max_cost_usd"))
    if max_cost is not None:
        if pricing is None:
            failures.append(f"{label}: saved pricing is required for cost assertion")
        elif _usage_cost_usd(usage, pricing) > float(max_cost):
            failures.append(f"{label}: cost budget exceeded")
    return failures


def _usage_cost_usd(usage: dict[str, Any], pricing: dict[str, Any]) -> float:
    """Estimate saved output cost with the browser's pricing snapshot."""
    input_total = max(0.0, float(usage.get("input_tokens", 0) or 0))
    cached_input = min(
        input_total,
        max(0.0, float(usage.get("cached_input_tokens", 0) or 0)),
    )
    uncached_input = input_total - cached_input
    final_tokens = max(
        0.0,
        float(usage.get("final_tokens", usage.get("output_tokens", 0)) or 0),
    )
    thinking_tokens = max(0.0, float(usage.get("thinking_tokens", 0) or 0))

    def rate(camel: str, snake: str) -> float:
        return float(pricing.get(camel, pricing.get(snake, 0)) or 0)

    return (
        uncached_input * rate("inputPerMillion", "input_per_million")
        + cached_input * rate("cachedInputPerMillion", "cached_input_per_million")
        + final_tokens * rate("outputPerMillion", "output_per_million")
        + thinking_tokens * rate("thinkingPerMillion", "thinking_per_million")
    ) / 1_000_000


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _utcnow_iso() -> str:
    """ISO-8601 timestamp helper. Mirrors :mod:`timetravel.models._utcnow_iso`.

    Re-implemented here to avoid a private-API import (models.py's helper
    is underscore-prefixed and could change).
    """
    # pylint: disable=import-outside-toplevel
    from datetime import UTC, datetime
    # pylint: enable=import-outside-toplevel

    return datetime.now(tz=UTC).isoformat()


__all__ = [
    "CandidateMode",
    "ConsistencyExpectation",
    "EvalScenario",
    "EvalSuite",
    "EvalSuiteResult",
    "EvalSuiteResultSummary",
    "EvalVerdict",
    "EvaluatorKind",
    "EvaluatorOutcome",
    "EvaluatorRequest",
    "Expectation",
    "GoalCheckExpectation",
    "JudgeCallable",
    "NoHallucinationExpectation",
    "RegressionResult",
    "ReplaySessionFactory",
    "ScenarioLatency",
    "ScenarioResult",
    "SuiteValidationError",
    "TokenBudgetExpectation",
    "TokenRollup",
    "ToolCheckExpectation",
    "eval_suite_result_from_dict",
    "eval_suite_result_to_dict",
    "evaluate",
    "evaluate_consistency",
    "evaluate_goal_check",
    "evaluate_no_hallucination",
    "evaluate_token_budget",
    "evaluate_tool_check",
    "run_frozen_verification",
    "scenario_result_from_dict",
    "scenario_result_to_dict",
    "validate_suite",
]
