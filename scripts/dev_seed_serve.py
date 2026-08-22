"""Spawn a uvicorn receiver against a seeded on-disk store.

Used for *manual* end-to-end UI verification during Phase 5 — the automated
test suite covers the API contract (``tests/integration/test_diff_api.py``)
but not the React render. Running this script + the Vite dev server lets
the operator click through the BranchTree / DiffView against real seeded
data. Not part of the test suite.

Run::

    python scripts/dev_seed_serve.py
    # in another terminal: cd web && pnpm dev
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid

# Allow ``python scripts/dev_seed_serve.py`` from the repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "src"))

import uvicorn  # noqa: E402

from agent_timetravel.enums import (  # noqa: E402
    CandidateMode,
    EvaluatorKind,
    SpanKind,
    SpanStatus,
)
from agent_timetravel.evaluate import (  # noqa: E402
    EvalScenario,
    EvalSuite,
    EvaluatorRequest,
    TokenBudgetExpectation,
    evaluate,
)
from agent_timetravel.models import Branch, Span, Trace  # noqa: E402
from agent_timetravel.receiver import create_app  # noqa: E402
from agent_timetravel.storage import TraceStore  # noqa: E402


def _seed(store: TraceStore) -> str:
    trace_id = "d" * 24 + "00000001"
    root_branch = Branch(
        trace_id=trace_id,
        parent_branch_id=None,
        branch_at_index=None,
        mode="frozen",
        label="root",
    )
    left_branch = Branch(
        trace_id=trace_id,
        parent_branch_id=root_branch.branch_id,
        branch_at_index=0,
        mode="frozen",
        label="left-variant",
    )
    right_branch = Branch(
        trace_id=trace_id,
        parent_branch_id=root_branch.branch_id,
        branch_at_index=0,
        mode="frozen",
        label="right-variant",
    )
    store.upsert_trace(Trace(trace_id=trace_id, spans=[]))
    store.insert_branch(root_branch)
    store.insert_branch(left_branch)
    store.insert_branch(right_branch)

    shared = Span(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex,
        parent_span_id=None,
        name="shared-llm",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        messages_hash="shared-hash",
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:00:01Z",
        status=SpanStatus.OK,
        raw_attributes={
            "gen_ai.response": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Hello! How can I help today?",
                        }
                    }
                ]
            }
        },
    )
    store.insert_span(shared, branch_id=None)

    left_only = Span(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex,
        parent_span_id=None,
        name="left-llm",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        messages_hash="left-hash",
        start_time="2026-01-01T00:00:02Z",
        end_time="2026-01-01T00:00:03Z",
        status=SpanStatus.OK,
        raw_attributes={
            "gen_ai.response": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I can write Python and Rust fluently.",
                        }
                    }
                ]
            }
        },
    )
    right_only = Span(
        trace_id=trace_id,
        span_id=uuid.uuid4().hex,
        parent_span_id=None,
        name="right-llm",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        messages_hash="right-hash",
        start_time="2026-01-01T00:00:02Z",
        end_time="2026-01-01T00:00:03Z",
        status=SpanStatus.OK,
        raw_attributes={
            "gen_ai.response": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I can write Python and Go fluently.",
                        }
                    }
                ]
            }
        },
    )
    store.insert_span(left_only, branch_id=left_branch.branch_id)
    store.insert_span(right_only, branch_id=right_branch.branch_id)
    return trace_id


def _eval_seed(store: TraceStore) -> list[str]:
    """Seed two eval runs against the existing trace.

    The first run is a "golden/baseline" — budget loose so all scenarios
    PASS. The second is a "candidate" capped tight so a couple of
    scenarios FAIL. Operators then click the "compare to baseline" button
    in the UI to see the diff.
    """
    trace_id = store.list_traces(limit=1, offset=0)[0][0].trace_id

    def build_suite(name: str, max_tokens: int | None) -> EvalSuite:
        scenarios = [
            EvalScenario(
                name=f"scen_{i:02d}",
                seed_trace_id=trace_id,
                candidate_mode=CandidateMode.FROZEN,
                branch_at_index=None,
                evaluators=[
                    EvaluatorRequest(
                        EvaluatorKind.TOKEN_BUDGET,
                        TokenBudgetExpectation(max_total_tokens=max_tokens),
                    )
                ],
            )
            for i in range(5)
        ]
        return EvalSuite(
            name=name,
            scenarios=scenarios,
            concurrency=4,
            scenario_timeout_s=10.0,
        )

    run_ids: list[str] = []
    for name, cap in [("golden", 100_000), ("candidate", 50)]:
        suite = build_suite(name, cap)
        result = asyncio.run(evaluate(suite, store=store))
        store.upsert_eval_run(result, suite_yaml=f"name: {name}\n")
        run_ids.append(str(result.run_id))
    return run_ids


def main() -> None:
    """Run the dev seed-and-serve."""
    db_path = os.path.join(tempfile.gettempdir(), "rewind-dev-seed.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    store = TraceStore(db_path)
    trace_id = _seed(store)
    print(f"[dev] seeded trace_id={trace_id} at {db_path}")
    eval_run_ids = _eval_seed(store)
    print(
        f"[dev] seeded eval runs: baseline={eval_run_ids[0]} "
        f"candidate={eval_run_ids[1]}"
    )
    app = create_app(store)
    uvicorn.run(app, host="127.0.0.1", port=8484, log_level="info")


if __name__ == "__main__":
    main()
