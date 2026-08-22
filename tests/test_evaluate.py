"""Unit tests for the Phase 5.5 deterministic evaluators and orchestrator.

Mirrors the density of ``tests/test_diff.py``: one test class per pure
evaluator with happy-path + edge-case coverage, plus a handful of
tests on the suite runner and serialization round-trips.

We construct :class:`Span` fixtures directly (no shared conftest
fixtures) because the eval engine cares about specific fields:
``kind``, ``raw_attributes`` keys, and per-SpanKind usage numbers.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from agent_timetravel.enums import CandidateMode, EvaluatorKind, EvalVerdict, SpanKind
from agent_timetravel.evaluate import (
    ConsistencyExpectation,
    EvalScenario,
    EvalSuite,
    EvalSuiteResult,
    EvaluatorOutcome,
    EvaluatorRequest,
    GoalCheckExpectation,
    NoHallucinationExpectation,
    ScenarioLatency,
    ScenarioResult,
    SuiteValidationError,
    TokenBudgetExpectation,
    TokenRollup,
    ToolCheckExpectation,
    _dedupe_consecutive,
    _final_response_text,
    _kind_sequence,
    _sum_tokens,
    _tokenize,
    _tool_names,
    _tool_result_text,
    evaluate,
    evaluate_consistency,
    evaluate_goal_check,
    evaluate_no_hallucination,
    evaluate_token_budget,
    evaluate_tool_check,
)
from agent_timetravel.models import Span

if TYPE_CHECKING:
    from agent_timetravel.storage import TraceStore


# ----------------------------------------------------------------------
# Span factory helpers — eval tests construct spans with hyper-specific
# shapes (usage numbers, response payloads, tool outputs).
# ----------------------------------------------------------------------


def _llm_span(
    *,
    response_text: str | None = None,
    prompt_tokens: int | None = 10,
    completion_tokens: int | None = 5,
    total_tokens: int | None = 15,
    response_dict: dict | None = None,
) -> Span:
    """Build a ``gen_ai.llm`` span with the given usage / response text."""
    raw: dict = {"gen_ai.system": "openai"}
    if response_dict is not None:
        raw["gen_ai.response"] = response_dict
    elif response_text is not None:
        raw["gen_ai.response"] = {
            "choices": [
                {"message": {"role": "assistant", "content": response_text}}
            ]
        }
    return Span(
        trace_id="t" * 32,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        name="chat.completions",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        start_time="2026-06-29T10:00:01+00:00",
        end_time="2026-06-29T10:00:02+00:00",
        raw_attributes=raw,
    )


def _tool_span(name: str, result: str | None = None) -> Span:
    raw: dict = {"tool.name": name}
    if result is not None:
        raw["gen_ai.tool.result"] = result
    return Span(
        trace_id="t" * 32,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        name=name,
        kind=SpanKind.TOOL,
        start_time="2026-06-29T10:00:03+00:00",
        end_time="2026-06-29T10:00:04+00:00",
        raw_attributes=raw,
    )


def _agent_span() -> Span:
    return Span(
        trace_id="t" * 32,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        name="adk.agent.Bot",
        kind=SpanKind.AGENT,
        start_time="2026-06-29T10:00:00+00:00",
        end_time="2026-06-29T10:00:05+00:00",
        raw_attributes={},
    )


# ======================================================================
# Tool-check evaluator
# ======================================================================


class TestToolCheck:
    """Exercises :func:`evaluate_tool_check`."""

    def test_all_expected_fires_pass(self) -> None:
        spans = [_tool_span("search"), _tool_span("lookup")]
        out = evaluate_tool_check(
            spans, ToolCheckExpectation(expected_tool_names=["search"])
        )
        assert out.verdict == EvalVerdict.PASS
        assert out.kind == EvaluatorKind.TOOL_CHECK
        assert "search" in out.metrics["observed_tools"]

    def test_missing_tool_fails(self) -> None:
        spans = [_tool_span("search")]
        out = evaluate_tool_check(
            spans,
            ToolCheckExpectation(expected_tool_names=["search", "lookup"]),
        )
        assert out.verdict == EvalVerdict.FAIL
        assert out.metrics["missing_tools"] == ["lookup"]

    def test_forbidden_tool_seen_fails(self) -> None:
        spans = [_tool_span("search"), _tool_span("delete")]
        out = evaluate_tool_check(
            spans, ToolCheckExpectation(forbidden_tool_names=["delete"])
        )
        assert out.verdict == EvalVerdict.FAIL
        assert out.metrics["forbidden_seen"] == ["delete"]

    def test_empty_expectations_pass(self) -> None:
        """No constraints — always passes, even on empty span list."""
        out = evaluate_tool_check([], ToolCheckExpectation())
        assert out.verdict == EvalVerdict.PASS

    def test_order_does_not_matter(self) -> None:
        """Set membership check: agents may call tools in any order."""
        spans = [_tool_span("b"), _tool_span("a")]
        out = evaluate_tool_check(
            spans, ToolCheckExpectation(expected_tool_names=["a", "b"])
        )
        assert out.verdict == EvalVerdict.PASS

    def test_tool_names_helper(self) -> None:
        """``_tool_names`` returns only TOOL spans, in order."""
        spans = [_agent_span(), _tool_span("search"), _llm_span(), _tool_span("lookup")]
        assert _tool_names(spans) == ["search", "lookup"]


# ======================================================================
# Goal-check evaluator
# ======================================================================


class TestGoalCheck:
    """Exercises :func:`evaluate_goal_check`."""

    def test_pattern_match_pass(self) -> None:
        spans = [_llm_span(response_text="The order ships Tuesday.")]
        out = evaluate_tool_check.__wrapped__ if False else evaluate_goal_check(
            spans, GoalCheckExpectation(pattern=r"ships \w+")
        )
        assert out.verdict == EvalVerdict.PASS

    def test_pattern_no_match_fails(self) -> None:
        spans = [_llm_span(response_text="I don't know.")]
        out = evaluate_goal_check(spans, GoalCheckExpectation(pattern=r"order \d+"))
        assert out.verdict == EvalVerdict.FAIL
        assert out.metrics["failed"] == ["pattern"]

    def test_must_be_exact_match_pass(self) -> None:
        spans = [_llm_span(response_text="42")]
        out = evaluate_goal_check(spans, GoalCheckExpectation(must_be="42"))
        assert out.verdict == EvalVerdict.PASS

    def test_must_be_wrong_fails(self) -> None:
        spans = [_llm_span(response_text="99")]
        out = evaluate_goal_check(spans, GoalCheckExpectation(must_be="42"))
        assert out.verdict == EvalVerdict.FAIL
        assert "must_be" in out.metrics["failed"][0]

    def test_pattern_is_case_insensitive(self) -> None:
        spans = [_llm_span(response_text="YES I CAN DO THAT")]
        out = evaluate_goal_check(spans, GoalCheckExpectation(pattern=r"yes i can"))
        assert out.verdict == EvalVerdict.PASS

    def test_pattern_is_multiline_dotall(self) -> None:
        """DOTALL so "." spans newlines in agent responses."""
        text = "Step 1\n\nThe customer\nis ready"
        spans = [_llm_span(response_text=text)]
        out = evaluate_goal_check(spans, GoalCheckExpectation(pattern=r"Step 1.*ready"))
        assert out.verdict == EvalVerdict.PASS

    def test_both_pattern_and_must_be_pass(self) -> None:
        spans = [_llm_span(response_text="The SKU-42 is in stock.")]
        out = evaluate_goal_check(
            spans,
            GoalCheckExpectation(pattern=r"SKU-\d+", must_be="The SKU-42 is in stock."),
        )
        assert out.verdict == EvalVerdict.PASS
        assert sorted(out.metrics["matched"]) == ["must_be", "pattern"]

    def test_no_llm_span_returns_empty_response(self) -> None:
        """Empty span list + still-passing pattern is a clean PASS."""
        assert _final_response_text([]) == ""
        out = evaluate_goal_check([], GoalCheckExpectation(pattern=r""))
        # Empty regex trivially matches empty string.
        assert out.verdict == EvalVerdict.PASS

    def test_legacy_raw_response_key(self) -> None:
        """Older schemas use ``raw_response`` not ``gen_ai.response``."""
        raw = {"raw_response": "legacy answer"}
        spans = [_llm_span(response_dict=None)]
        spans[0] = Span(
            trace_id="t" * 32,
            span_id=uuid4().hex[:16],
            parent_span_id=None,
            name="chat",
            kind=SpanKind.LLM,
            start_time="2026-06-29T10:00:01+00:00",
            end_time="2026-06-29T10:00:02+00:00",
            raw_attributes=raw,
        )
        assert _final_response_text(spans) == "legacy answer"


# ======================================================================
# Consistency evaluator
# ======================================================================


class TestConsistency:
    """Exercises :func:`evaluate_consistency`."""

    def test_strict_match_pass(self) -> None:
        spans = [_agent_span(), _llm_span(), _tool_span("x")]
        seed = [SpanKind.AGENT, SpanKind.LLM, SpanKind.TOOL]
        out = evaluate_consistency(spans, ConsistencyExpectation(seed_kind_sequence=seed))
        assert out.verdict == EvalVerdict.PASS

    def test_strict_divergence_fails(self) -> None:
        spans = [_agent_span(), _llm_span()]  # missing tool
        seed = [SpanKind.AGENT, SpanKind.LLM, SpanKind.TOOL]
        out = evaluate_consistency(spans, ConsistencyExpectation(seed_kind_sequence=seed))
        assert out.verdict == EvalVerdict.FAIL
        assert out.metrics["divergence_index"] == 2

    def test_loose_collapses_duplicates(self) -> None:
        """Loose mode: two consecutive LLM calls == one LLM call."""
        spans = [_agent_span(), _llm_span(), _llm_span(), _tool_span("x")]
        seed = [SpanKind.AGENT, SpanKind.LLM, SpanKind.TOOL]
        out = evaluate_consistency(
            spans,
            ConsistencyExpectation(seed_kind_sequence=seed, exact=False),
        )
        assert out.verdict == EvalVerdict.PASS

    def test_kind_sequence_helper(self) -> None:
        spans = [_agent_span(), _llm_span(), _tool_span("x")]
        assert _kind_sequence(spans) == [SpanKind.AGENT, SpanKind.LLM, SpanKind.TOOL]

    def test_dedupe_consecutive_helper(self) -> None:
        seq = [SpanKind.LLM, SpanKind.LLM, SpanKind.TOOL, SpanKind.LLM, SpanKind.LLM]
        assert _dedupe_consecutive(seq) == [SpanKind.LLM, SpanKind.TOOL, SpanKind.LLM]


# ======================================================================
# Token-budget evaluator
# ======================================================================


class TestTokenBudget:
    """Exercises :func:`evaluate_token_budget`."""

    def test_within_all_budgets_pass(self) -> None:
        spans = [_llm_span(prompt_tokens=10, completion_tokens=5, total_tokens=15)]
        out = evaluate_token_budget(
            spans,
            TokenBudgetExpectation(
                max_total_tokens=100,
                max_prompt_tokens=20,
                max_completion_tokens=10,
            ),
        )
        assert out.verdict == EvalVerdict.PASS
        assert out.metrics["total_tokens"] == 15

    def test_total_over_budget_fails(self) -> None:
        spans = [_llm_span(total_tokens=200)]
        out = evaluate_token_budget(
            spans, TokenBudgetExpectation(max_total_tokens=100)
        )
        assert out.verdict == EvalVerdict.FAIL
        assert "total" in out.detail

    def test_prompt_over_budget_fails(self) -> None:
        spans = [_llm_span(prompt_tokens=50)]
        out = evaluate_token_budget(
            spans, TokenBudgetExpectation(max_prompt_tokens=10)
        )
        assert out.verdict == EvalVerdict.FAIL
        assert "prompt" in out.detail

    def test_completion_over_budget_fails(self) -> None:
        spans = [_llm_span(completion_tokens=500)]
        out = evaluate_token_budget(
            spans, TokenBudgetExpectation(max_completion_tokens=10)
        )
        assert out.verdict == EvalVerdict.FAIL
        assert "completion" in out.detail

    def test_none_budgets_always_pass(self) -> None:
        """Unset ceilings => no check on that axis."""
        spans = [_llm_span(total_tokens=10_000_000)]
        out = evaluate_token_budget(spans, TokenBudgetExpectation())
        assert out.verdict == EvalVerdict.PASS
        # Still reports the rolled-up count.
        assert out.metrics["total_tokens"] == 10_000_000

    def test_sums_across_multiple_llm_spans(self) -> None:
        spans = [
            _llm_span(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            _llm_span(prompt_tokens=20, completion_tokens=5, total_tokens=25),
        ]
        assert _sum_tokens(spans) == (30, 10, 40)

    def test_none_tokens_contribute_zero(self) -> None:
        """Missing usage columns default to 0, not None."""
        spans = [_llm_span(prompt_tokens=None, completion_tokens=None, total_tokens=None)]
        out = evaluate_token_budget(spans, TokenBudgetExpectation(max_total_tokens=0))
        assert out.verdict == EvalVerdict.PASS


# ======================================================================
# No-hallucination evaluator
# ======================================================================


class TestNoHallucination:
    """Exercises :func:`evaluate_no_hallucination`."""

    def test_grounded_response_pass(self) -> None:
        spans = [
            _tool_span("search", result="Order 42 ships Tuesday"),
            _llm_span(response_text="Order 42 ships Tuesday"),
        ]
        out = evaluate_no_hallucination(spans, NoHallucinationExpectation())
        assert out.verdict == EvalVerdict.PASS

    def test_ungrounded_term_fails(self) -> None:
        """Response invents a word absent from tool outputs."""
        spans = [
            _tool_span("search", result="Order 42 ships Tuesday"),
            _llm_span(response_text="Order 42 ships Friday manufacture-date"),
        ]
        out = evaluate_no_hallucination(spans, NoHallucinationExpectation())
        assert out.verdict == EvalVerdict.FAIL
        assert "manufacture" in out.metrics["ungrounded_terms"]

    def test_required_term_in_tool_pass(self) -> None:
        # Response tokens are all grounded ("1234" + "sku"), and required
        # term is in tool result, so this is a clean PASS.
        spans = [
            _tool_span("lookup", result="1234 sku"),
            _llm_span(response_text="1234"),
        ]
        out = evaluate_no_hallucination(
            spans, NoHallucinationExpectation(required_grounding_terms=["sku"])
        )
        assert out.verdict == EvalVerdict.PASS

    def test_required_term_missing_fails(self) -> None:
        spans = [
            _tool_span("lookup", result="SKU-9999 found"),
            _llm_span(response_text="Found SKU-9999"),
        ]
        out = evaluate_no_hallucination(
            spans, NoHallucinationExpectation(required_grounding_terms=["SKU-1234"])
        )
        assert out.verdict == EvalVerdict.FAIL
        assert out.metrics["missing_required"] == ["SKU-1234"]

    def test_stopword_filter_skips_common_words(self) -> None:
        """Common English words don't trigger ungrounded-term flag."""
        spans = [
            _tool_span("lookup", result="number 42"),
            _llm_span(response_text="the number is 42"),
        ]
        out = evaluate_no_hallucination(spans, NoHallucinationExpectation())
        # 'the', 'is' are stopwords; '42' is grounded; should pass.
        assert out.verdict == EvalVerdict.PASS

    def test_stopword_filter_disabled(self) -> None:
        """Disabling the filter treats common words as content."""
        spans = [
            _tool_span("x", result="42"),
            _llm_span(response_text="the answer"),
        ]
        out = evaluate_no_hallucination(
            spans, NoHallucinationExpectation(stopword_filter=False)
        )
        assert out.verdict == EvalVerdict.FAIL

    def test_tokenize_helper(self) -> None:
        tokens = _tokenize("Hello, WORLD! It's-me 2026")
        assert tokens == ["hello", "world", "it", "s", "me", "2026"]

    def test_tool_result_text_helper_legacy_keys(self) -> None:
        """Older ``tool.output`` legacy key still surfaced."""
        spans = [_tool_span("x")]
        spans[0] = Span(
            trace_id="t" * 32,
            span_id=uuid4().hex[:16],
            parent_span_id=None,
            name="x",
            kind=SpanKind.TOOL,
            start_time="2026-06-29T10:00:03+00:00",
            end_time="2026-06-29T10:00:04+00:00",
            raw_attributes={"tool.output": "legacy output"},
        )
        assert "legacy output" in _tool_result_text(spans)


# ======================================================================
# Suite validation + orchestrator
# ======================================================================


class TestSuiteValidation:
    """Exercises :func:`validate_suite` + the orchestrator's pre-flight."""

    def test_empty_suite_name_rejected(self) -> None:
        from agent_timetravel.evaluate import validate_suite

        suite = _make_suite_with_name("")
        with pytest.raises(SuiteValidationError):
            validate_suite(suite)

    def test_empty_scenarios_rejected(self, tmp_path) -> None:
        from agent_timetravel.storage import TraceStore

        store = TraceStore(tmp_path / "agent_timetravel.db")
        suite = EvalSuite(name="x", scenarios=[])
        with pytest.raises(SuiteValidationError):
            asyncio.run(evaluate(suite, store=store))

    def test_scenario_without_evaluators_rejected(self, tmp_path) -> None:
        from agent_timetravel.storage import TraceStore

        store = TraceStore(tmp_path / "agent_timetravel.db")
        bad = EvalScenario(
            name="n",
            seed_trace_id="t" * 32,
            candidate_mode=CandidateMode.FROZEN,
            branch_at_index=None,
            evaluators=[],
        )
        suite = EvalSuite(name="s", scenarios=[bad])
        with pytest.raises(SuiteValidationError):
            asyncio.run(evaluate(suite, store=store))


def _make_suite_with_name(name: str) -> EvalSuite:
    """Tiny helper used by validation tests."""
    return EvalSuite(
        name=name,
        scenarios=[
            EvalScenario(
                name="s",
                seed_trace_id="t" * 32,
                candidate_mode=CandidateMode.FROZEN,
                branch_at_index=None,
                evaluators=[
                    EvaluatorRequest(
                        EvaluatorKind.TOOL_CHECK,
                        ToolCheckExpectation(),
                    )
                ],
            )
        ],
    )


class TestOrchestrator:
    """End-to-end tests against :func:`evaluate`.

    Seed-spans are written into a temp TraceStore; the orchestrator
    builds a frozen replay session and runs the evaluators.
    """

    def _seed_store(self, store: TraceStore, trace_id: str = "t" * 32) -> None:
        """Write a 3-span seed trace into the store."""
        from agent_timetravel.models import Trace

        spans = [_agent_span_trace(trace_id), _llm_span_trace(trace_id), _tool_span_trace(trace_id)]
        trace = Trace(trace_id=trace_id, spans=spans)
        store.upsert_trace(trace)
        for s in spans:
            store.insert_span(s)

    def test_pure_evaluator_dispatch_passes(self, tmp_path) -> None:
        from agent_timetravel.storage import TraceStore

        store = TraceStore(tmp_path / "agent_timetravel.db")
        self._seed_store(store)
        suite = EvalSuite(
            name="ok",
            scenarios=[
                EvalScenario(
                    name="s1",
                    seed_trace_id="t" * 32,
                    candidate_mode=CandidateMode.FROZEN,
                    branch_at_index=None,
                    evaluators=[
                        EvaluatorRequest(
                            EvaluatorKind.TOKEN_BUDGET,
                            TokenBudgetExpectation(max_total_tokens=1000),
                        )
                    ],
                )
            ],
        )
        result = asyncio.run(evaluate(suite, store=store))
        assert isinstance(result, EvalSuiteResult)
        assert result.overall_verdict == EvalVerdict.PASS
        assert len(result.scenarios) == 1
        assert result.scenarios[0].verdict == EvalVerdict.PASS

    def test_unknown_seed_trace_skips_scenario(self, tmp_path) -> None:
        from agent_timetravel.storage import TraceStore

        store = TraceStore(tmp_path / "agent_timetravel.db")
        # Don't seed — trace_id won't resolve; expect SKIP.
        suite = EvalSuite(
            name="miss",
            scenarios=[
                EvalScenario(
                    name="ghost",
                    seed_trace_id="x" * 32,
                    candidate_mode=CandidateMode.FROZEN,
                    branch_at_index=None,
                    evaluators=[
                        EvaluatorRequest(
                            EvaluatorKind.TOKEN_BUDGET,
                            TokenBudgetExpectation(),
                        )
                    ],
                )
            ],
        )
        result = asyncio.run(evaluate(suite, store=store))
        assert result.overall_verdict == EvalVerdict.SKIP
        assert result.scenarios[0].verdict == EvalVerdict.SKIP
        assert result.scenarios[0].branch_id is None
        assert result.scenarios[0].error_message is not None

    def test_scenario_order_preserved(self, tmp_path) -> None:
        """Scenarios must come back in suite-order, not async-completion order."""
        from agent_timetravel.storage import TraceStore

        store = TraceStore(tmp_path / "agent_timetravel.db")
        self._seed_store(store)
        names = [f"s{i}" for i in range(6)]
        suite = EvalSuite(
            name="order",
            scenarios=[
                EvalScenario(
                    name=n,
                    seed_trace_id="t" * 32,
                    candidate_mode=CandidateMode.FROZEN,
                    branch_at_index=None,
                    evaluators=[
                        EvaluatorRequest(
                            EvaluatorKind.TOKEN_BUDGET,
                            TokenBudgetExpectation(),
                        )
                    ],
                )
                for n in names
            ],
        )
        result = asyncio.run(evaluate(suite, store=store))
        assert [s.name for s in result.scenarios] == names

    def test_run_id_is_a_uuid(self, tmp_path) -> None:
        from uuid import UUID

        from agent_timetravel.storage import TraceStore

        store = TraceStore(tmp_path / "agent_timetravel.db")
        self._seed_store(store)
        suite = _make_suite_with_name("uuid-check")
        result = asyncio.run(evaluate(suite, store=store))
        # Raises ValueError if not a UUID.
        _ = UUID(result.run_id.hex) if hasattr(result.run_id, "hex") else None
        assert isinstance(result.run_id, UUID)

    def test_concurrency_limit_is_respected(self, tmp_path) -> None:
        """Low concurrency shouldn't break the harness, only slow it."""
        from agent_timetravel.storage import TraceStore

        store = TraceStore(tmp_path / "agent_timetravel.db")
        self._seed_store(store)
        suite = EvalSuite(
            name="parallel",
            concurrency=1,
            scenarios=[
                EvalScenario(
                    name=f"s{i}",
                    seed_trace_id="t" * 32,
                    candidate_mode=CandidateMode.FROZEN,
                    branch_at_index=None,
                    evaluators=[
                        EvaluatorRequest(
                            EvaluatorKind.TOKEN_BUDGET,
                            TokenBudgetExpectation(),
                        )
                    ],
                )
                for i in range(4)
            ],
        )
        result = asyncio.run(evaluate(suite, store=store))
        # All four scenarios processed.
        assert len(result.scenarios) == 4
        assert all(s.verdict == EvalVerdict.PASS for s in result.scenarios)


# ----------------------------------------------------------------------
# Serialization round-trip helpers
# ----------------------------------------------------------------------


class TestSerialization:
    """Round-trip the result dict helpers used for SQLite + API."""

    def test_evaluator_outcome_round_trip(self) -> None:
        from agent_timetravel.evaluate import (
            _evaluator_outcome_from_dict,
            _evaluator_outcome_to_dict,
        )

        original = EvaluatorOutcome(
            kind=EvaluatorKind.TOOL_CHECK,
            verdict=EvalVerdict.PASS,
            detail="ok",
            metrics={"a": 1},
        )
        round_tripped = _evaluator_outcome_from_dict(
            _evaluator_outcome_to_dict(original)
        )
        assert round_tripped == original

    def test_evaluator_outcome_rejects_skip_verdict(self) -> None:
        """Deserialization validates verdict is PASS/FAIL only."""
        from agent_timetravel.evaluate import (
            _evaluator_outcome_from_dict,
            _evaluator_outcome_to_dict,
        )

        data = _evaluator_outcome_to_dict(
            EvaluatorOutcome(
                kind=EvaluatorKind.TOOL_CHECK,
                verdict=EvalVerdict.PASS,
                detail="ok",
            )
        )
        data["verdict"] = "skip"
        with pytest.raises(ValueError, match=r"PASS|FAIL"):
            _evaluator_outcome_from_dict(data)

    def test_scenario_result_round_trip(self) -> None:
        from agent_timetravel.evaluate import scenario_result_from_dict, scenario_result_to_dict

        scen = ScenarioResult(
            name="s",
            seed_trace_id="t" * 32,
            branch_id=uuid4(),
            verdict=EvalVerdict.PASS,
            outcomes=[
                EvaluatorOutcome(
                    kind=EvaluatorKind.TOKEN_BUDGET,
                    verdict=EvalVerdict.PASS,
                    detail="ok",
                    metrics={"total_tokens": 10},
                )
            ],
            rollup=TokenRollup(
                prompt_tokens=5, completion_tokens=5, total_tokens=10, llm_call_count=1
            ),
            latency=ScenarioLatency(total_s=1.0, replay_s=0.5, evaluate_s=0.5),
        )
        rt = scenario_result_from_dict(scenario_result_to_dict(scen))
        assert rt == scen

    def test_scenario_result_with_branch_id_none_round_trips(self) -> None:
        from agent_timetravel.evaluate import scenario_result_from_dict, scenario_result_to_dict

        scen = ScenarioResult(
            name="s",
            seed_trace_id="t" * 32,
            branch_id=None,
            verdict=EvalVerdict.SKIP,
            outcomes=[],
            rollup=TokenRollup(
                prompt_tokens=0, completion_tokens=0, total_tokens=0, llm_call_count=0
            ),
            latency=ScenarioLatency(total_s=0.0, replay_s=0.0, evaluate_s=0.0),
            error_message="trace not found",
        )
        rt = scenario_result_from_dict(scenario_result_to_dict(scen))
        assert rt == scen
        assert rt.branch_id is None
        assert rt.error_message == "trace not found"


# ----------------------------------------------------------------------
# Per-scenario seed-trace-id helpers — independent spans so the seeded
# trace has consistent ids across tests.
# ----------------------------------------------------------------------


def _agent_span_trace(trace_id: str) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        name="adk.agent.Bot",
        kind=SpanKind.AGENT,
        start_time="2026-06-29T10:00:00+00:00",
        end_time="2026-06-29T10:00:05+00:00",
        raw_attributes={},
    )


def _llm_span_trace(trace_id: str) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        name="chat.completions",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        prompt_tokens=42,
        completion_tokens=7,
        total_tokens=49,
        start_time="2026-06-29T10:00:01+00:00",
        end_time="2026-06-29T10:00:02+00:00",
        raw_attributes={
            "gen_ai.system": "openai",
            "gen_ai.usage.prompt_tokens": 42,
            "gen_ai.usage.completion_tokens": 7,
            "gen_ai.usage.total_tokens": 49,
            "gen_ai.response": {
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ]
            },
        },
    )


def _tool_span_trace(trace_id: str) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=uuid4().hex[:16],
        parent_span_id=None,
        name="search",
        kind=SpanKind.TOOL,
        start_time="2026-06-29T10:00:03+00:00",
        end_time="2026-06-29T10:00:04+00:00",
        raw_attributes={"tool.name": "search", "gen_ai.tool.result": "result 42"},
    )
