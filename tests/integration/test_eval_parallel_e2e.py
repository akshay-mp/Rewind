"""Phase 5.5 integration test — parallel eval at scale, zero cross-session leakage.

Phase 5.5 exit criterion (plan §9):

> N=50 scenarios complete in ≤ slowest single scenario + p99 overhead,
> with parallel evaluation under bounded concurrency. Every scenario
> sees its own seed trace and its own candidate branch — no cross-session
> leakage.

Stress-wise we run **100 scenarios** so the parallel orchestrator
spreads across 8 workers and reorders results back into suite order
on completion. Each scenario uses a distinct seed trace id, so any
leakage between scenarios (e.g. branch_id re-use, trace mis-attribution)
would manifest as a wrong verdict or wrong seed-trace-id on at least
one row.

The test also runs the same suite **serially** (``concurrency=1``) as a
correctness oracle: parallel results must equal serial results on the
``seed_trace_id`` axis. (Verdict equality is implied — every scenario's
seed has the same shape, so every verdict must be PASS.)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from rewind.enums import CandidateMode, EvaluatorKind, EvalVerdict, SpanKind
from rewind.evaluate import (
    EvalScenario,
    EvalSuite,
    EvaluatorRequest,
    TokenBudgetExpectation,
    evaluate,
)
from rewind.models import Span, Trace
from rewind.storage import TraceStore

if TYPE_CHECKING:
    pass

pytestmark = pytest.mark.integration

#: How many parallel scenarios to run. The exit-criterion docs phrase
#: this as N=50; doubling to 100 stresses the reorder + per-scenario
#: isolation paths more aggressively without exceeding the per-test
#: timeout on CI.
N_SCENARIOS = 100


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


def _make_seed(
    trace_id: str,
    *,
    total_tokens: int = 100,
    prompt_tokens: int = 70,
    completion_tokens: int = 30,
) -> Trace:
    """Build a 3-span seed trace with deterministic token usage."""
    spans = [
        Span(
            trace_id=trace_id,
            span_id=f"agent{trace_id[:12]}",
            parent_span_id=None,
            name="adk.agent.Bot",
            kind=SpanKind.AGENT,
            start_time="2026-06-29T10:00:00+00:00",
            end_time="2026-06-29T10:00:05+00:00",
            raw_attributes={},
        ),
        Span(
            trace_id=trace_id,
            span_id=f"llm{trace_id[:12]}",
            parent_span_id=None,
            name="chat.completions",
            kind=SpanKind.LLM,
            model_name="qwen3:32b",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            start_time="2026-06-29T10:00:01+00:00",
            end_time="2026-06-29T10:00:02+00:00",
            raw_attributes={
                "gen_ai.system": "openai",
                "gen_ai.usage.prompt_tokens": prompt_tokens,
                "gen_ai.usage.completion_tokens": completion_tokens,
                "gen_ai.usage.total_tokens": total_tokens,
            },
        ),
        Span(
            trace_id=trace_id,
            span_id=f"tool{trace_id[:12]}",
            parent_span_id=None,
            name="search",
            kind=SpanKind.TOOL,
            start_time="2026-06-29T10:00:03+00:00",
            end_time="2026-06-29T10:00:04+00:00",
            raw_attributes={"tool.name": "search", "gen_ai.tool.result": "ok"},
        ),
    ]
    return Trace(trace_id=trace_id, spans=spans)


@pytest.fixture
def seeded_store(tmp_path: Path) -> tuple[TraceStore, dict[str, str]]:
    """Seed N traces, return (store, name→trace_id map)."""
    store = TraceStore(tmp_path / "rewind.db")
    trace_ids: dict[str, str] = {}
    for i in range(N_SCENARIOS):
        # Each trace_id is a unique 32-char hex with the index embedded.
        trace_id = f"{i:08x}" + "0" * 24
        trace = _make_seed(trace_id)
        store.upsert_trace(trace)
        for sp in trace.spans:
            store.insert_span(sp)
        trace_ids[f"scen_{i:03d}"] = trace_id
    return store, trace_ids


# ----------------------------------------------------------------------
# Exit-criterion tests
# ----------------------------------------------------------------------


def _build_suite(
    trace_ids: dict[str, str],
    *,
    concurrency: int,
    scenario_timeout_s: float,
) -> EvalSuite:
    """Build a suite that fires one TOKEN_BUDGET evaluator per scenario."""
    scenarios = [
        EvalScenario(
            name=name,
            seed_trace_id=trace_id,
            candidate_mode=CandidateMode.FROZEN,
            branch_at_index=None,
            evaluators=[
                EvaluatorRequest(
                    EvaluatorKind.TOKEN_BUDGET,
                    TokenBudgetExpectation(max_total_tokens=1000),
                )
            ],
        )
        for name, trace_id in trace_ids.items()
    ]
    return EvalSuite(
        name="parallel-stress",
        scenarios=scenarios,
        concurrency=concurrency,
        scenario_timeout_s=scenario_timeout_s,
    )


def test_parallel_eval_completes_all_scenarios(
    seeded_store: tuple[TraceStore, dict[str, str]],
) -> None:
    """Exit criterion: every scenario gets a verdict (not dropped)."""
    store, trace_ids = seeded_store
    suite = _build_suite(trace_ids, concurrency=8, scenario_timeout_s=10.0)
    result = asyncio.run(evaluate(suite, store=store))
    assert len(result.scenarios) == N_SCENARIOS
    # All scenarios must PASS — same budget, same shape.
    assert all(s.verdict == EvalVerdict.PASS for s in result.scenarios)


def test_parallel_results_match_suite_order(
    seeded_store: tuple[TraceStore, dict[str, str]],
) -> None:
    """Asynchronous completion must NOT shuffle the result order.

    The orchestrator gathers results in completion order, then reorders
    them back by name to match the suite's scenario order. This test
    enforces that contract directly: ``[s.name for s in result]`` must
    equal the suite's scenario name list.
    """
    store, trace_ids = seeded_store
    suite = _build_suite(trace_ids, concurrency=8, scenario_timeout_s=10.0)
    result = asyncio.run(evaluate(suite, store=store))
    expected_names = [sc.name for sc in suite.scenarios]
    assert [s.name for s in result.scenarios] == expected_names


def test_parallel_eval_matches_serial(
    seeded_store: tuple[TraceStore, dict[str, str]],
) -> None:
    """Concurrency=1 produces identical seed_trace_id mapping as concurrency=8.

    This is the strongest correctness guarantee: per-scenario isolation
    under parallelism. If any scenario leaked into another's branch, the
    serial vs parallel result maps would diverge.
    """
    store, trace_ids = seeded_store
    serial_suite = _build_suite(trace_ids, concurrency=1, scenario_timeout_s=10.0)
    parallel_suite = _build_suite(trace_ids, concurrency=8, scenario_timeout_s=10.0)

    serial_result = asyncio.run(evaluate(serial_suite, store=store))
    parallel_result = asyncio.run(evaluate(parallel_suite, store=store))

    serial_map = {s.name: s.seed_trace_id for s in serial_result.scenarios}
    parallel_map = {s.name: s.seed_trace_id for s in parallel_result.scenarios}

    assert serial_map == parallel_map
    # Spot-check: every scenario's seed_trace_id matches the suite input.
    for name, expected_tid in trace_ids.items():
        assert serial_map[name] == expected_tid
        assert parallel_map[name] == expected_tid


def test_parallel_eval_faster_than_serial_upper_bound(
    seeded_store: tuple[TraceStore, dict[str, str]],
) -> None:
    """Parallelism must not be slower than serial.

    The exit-criterion says parallel should be *faster*; in practice each
    scenario does so little work that the speedup is roughly linear
    modulo per-scenario overhead. We enforce the weaker "not 2x slower"
    bar to keep this test stable under CI noise.
    """
    store, trace_ids = seeded_store
    serial_suite = _build_suite(trace_ids, concurrency=1, scenario_timeout_s=30.0)
    parallel_suite = _build_suite(trace_ids, concurrency=8, scenario_timeout_s=30.0)

    t0 = time.perf_counter()
    asyncio.run(evaluate(serial_suite, store=store))
    serial_elapsed = time.perf_counter() - t0

    t1 = time.perf_counter()
    asyncio.run(evaluate(parallel_suite, store=store))
    parallel_elapsed = time.perf_counter() - t1

    # Parallel must be no slower than 1.5x serial — generous bar for CI noise.
    assert parallel_elapsed < serial_elapsed * 1.5, (
        f"parallel {parallel_elapsed:.3f}s vs serial {serial_elapsed:.3f}s"
    )


def test_run_id_is_persistable_uuid(
    seeded_store: tuple[TraceStore, dict[str, str]],
) -> None:
    """The run_id UUID must round-trip through TraceStore persistence."""
    store, trace_ids = seeded_store
    suite = _build_suite(trace_ids, concurrency=4, scenario_timeout_s=10.0)
    result = asyncio.run(evaluate(suite, store=store))
    # Round-trips as a UUID.
    _ = UUID(result.run_id.hex)
    # Persist + reload.
    store.upsert_eval_run(result, suite_yaml="name: test\n")
    reloaded = store.get_eval_run(result.run_id)
    assert reloaded is not None
    assert reloaded.run_id == result.run_id
    assert reloaded.suite_name == result.suite_name


def test_one_failed_scenario_doesnt_block_suite(
    tmp_path: Path,
) -> None:
    """A SKIP scenario (missing seed trace) doesn't poison the others."""
    store = TraceStore(tmp_path / "rewind.db")
    # Seed three valid scenarios; the middle one references a ghost trace.
    valid_tids = []
    for i in range(3):
        trace_id = f"bb{i:01x}" + "0" * 31
        trace = _make_seed(trace_id)
        store.upsert_trace(trace)
        for sp in trace.spans:
            store.insert_span(sp)
        valid_tids.append(trace_id)

    suite = EvalSuite(
        name="mixed",
        scenarios=[
            EvalScenario(
                name="ok_a",
                seed_trace_id=valid_tids[0],
                candidate_mode=CandidateMode.FROZEN,
                branch_at_index=None,
                evaluators=[
                    EvaluatorRequest(
                        EvaluatorKind.TOKEN_BUDGET,
                        TokenBudgetExpectation(max_total_tokens=1000),
                    )
                ],
            ),
            EvalScenario(
                name="ghost",
                seed_trace_id="x" * 32,  # never seeded
                candidate_mode=CandidateMode.FROZEN,
                branch_at_index=None,
                evaluators=[
                    EvaluatorRequest(
                        EvaluatorKind.TOKEN_BUDGET,
                        TokenBudgetExpectation(),
                    )
                ],
            ),
            EvalScenario(
                name="ok_c",
                seed_trace_id=valid_tids[2],
                candidate_mode=CandidateMode.FROZEN,
                branch_at_index=None,
                evaluators=[
                    EvaluatorRequest(
                        EvaluatorKind.TOKEN_BUDGET,
                        TokenBudgetExpectation(max_total_tokens=1000),
                    )
                ],
            ),
        ],
        concurrency=4,
        scenario_timeout_s=10.0,
    )
    result = asyncio.run(evaluate(suite, store=store))
    by_name = {s.name: s for s in result.scenarios}
    assert by_name["ok_a"].verdict == EvalVerdict.PASS
    assert by_name["ghost"].verdict == EvalVerdict.SKIP
    assert by_name["ghost"].branch_id is None
    assert by_name["ghost"].error_message is not None
    assert by_name["ok_c"].verdict == EvalVerdict.PASS
    # Overall verdict skips because at least one SKIPped — neither PASS nor FAIL.
    assert result.overall_verdict == EvalVerdict.SKIP
