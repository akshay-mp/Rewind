"""FastAPI mountable routes for the Phase 5.5 eval harness.

Audit directive (§5): put eval routes in their own module rather than
bloating :mod:`timetravel.timeline`. The harness has its own lifecycle
(async, long-running, may persist YAML) and a different access pattern
(submit-and-poll rather than read-only). Mounting alongside the timeline
gives the UI one origin to talk to (matches Phase 2's CORS-free design).

Routes
------
* ``GET  /api/v1/evals``                       - list runs (newest first)
* ``POST /api/v1/evals``                       - submit a suite YAML, run + persist
* ``GET  /api/v1/evals/{run_id}``              - full run with scenarios
* ``GET  /api/v1/evals/{run_id}/baseline``     - diff this run vs a golden run
* ``DELETE /api/v1/evals/{run_id}``            - remove a run (and its scenarios)

Concurrency
-----------
``POST /api/v1/evals`` runs the suite through :func:`timetravel.evaluate.evaluate`
which uses ``asyncio.gather`` + ``Semaphore`` for parallel scenario execution.
The HTTP handler is sync (FastAPI threadpools it) so the runner uses
``asyncio.run`` to drive the coroutine to completion. Long-running suites
should be moved to a queue later; for now the route is fine for dev/UI smoke
workloads (<=50 scenarios, ~=30s ceiling).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, TypeAlias
from uuid import UUID

import yaml
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_timetravel.enums import CandidateMode, EvaluatorKind, EvalVerdict
from agent_timetravel.evaluate import (
    ConsistencyExpectation,
    EvalScenario,
    EvalSuite,
    EvalSuiteResult,
    EvalSuiteResultSummary,
    EvaluatorOutcome,
    EvaluatorRequest,
    GoalCheckExpectation,
    NoHallucinationExpectation,
    ReplaySessionFactory,
    ScenarioResult,
    SuiteValidationError,
    TokenBudgetExpectation,
    ToolCheckExpectation,
    evaluate,
    validate_suite,
)
from agent_timetravel.storage import TraceStore

#: Max suite-YAML size we accept on POST. 256 KiB is enough for a 100-scenario
#: suite with rich expected dictionaries; anything bigger is likely a misuse.
_MAX_SUITE_YAML_BYTES = 256 * 1024

#: Default page size for list endpoint — small because each summary row is
#: constant-size, but we want the UI to render the first page quickly.
_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 500

# Optional server-side factories let a local integration register a custom
# materialisation policy while keeping the default frozen runner available.
_REGRESSION_FACTORIES: dict[str, ReplaySessionFactory] = {}


def register_regression_factory(ref: str, factory: ReplaySessionFactory) -> None:
    """Register a regression materialisation factory for ``factory_ref``."""
    if not ref:
        raise ValueError("regression factory ref must be non-empty")
    _REGRESSION_FACTORIES[ref] = factory


def get_regression_factory(ref: str) -> ReplaySessionFactory | None:
    """Resolve a registered regression factory, if present."""
    return _REGRESSION_FACTORIES.get(ref)

#: Closed union of expectation types. We use a TypeAlias (not ``Any``) so the
#: YAML loader stays type-safe and mypy can narrow on the dispatcher.
_Expectation: TypeAlias = (
    ToolCheckExpectation
    | GoalCheckExpectation
    | ConsistencyExpectation
    | TokenBudgetExpectation
    | NoHallucinationExpectation
)


# ---------------------------------------------------------------------------
# View models — wire shape for the HTTP layer. These are intentionally
# pydantic BaseModel (not the eval dataclasses) so FastAPI renders them
# directly via response_model. The eval dataclasses stay framework-free.
# ---------------------------------------------------------------------------


class EvaluatorOutcomeView(BaseModel):
    """One evaluator's verdict on one candidate."""

    kind: EvaluatorKind
    verdict: EvalVerdict
    detail: str
    metrics: dict[str, Any] = Field(default_factory=dict)

    model_config = {"use_enum_values": False}


class TokenRollupView(BaseModel):
    """Cost rollup for one scenario."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    llm_call_count: int


class ScenarioLatencyView(BaseModel):
    """Latency rollup (all values in seconds)."""

    total_s: float
    replay_s: float
    evaluate_s: float


class ScenarioResultView(BaseModel):
    """One scenario row inside an :class:`EvalRunDetailView`."""

    name: str
    seed_trace_id: str
    branch_id: str | None
    verdict: EvalVerdict
    outcomes: list[EvaluatorOutcomeView]
    rollup: TokenRollupView
    latency: ScenarioLatencyView
    error_message: str | None = None


class EvalRunSummaryView(BaseModel):
    """Lightweight row for the list endpoint."""

    run_id: str
    suite_name: str
    started_at: str
    finished_at: str
    overall_verdict: EvalVerdict


class EvalRunDetailView(BaseModel):
    """Full run, including every scenario's outcomes."""

    run_id: str
    suite_name: str
    started_at: str
    finished_at: str
    overall_verdict: EvalVerdict
    scenarios: list[ScenarioResultView]


class EvalRunListResponse(BaseModel):
    """Paginated list of eval-run summaries."""

    items: list[EvalRunSummaryView]
    total: int
    limit: int
    offset: int


class BaselineScenarioDiffView(BaseModel):
    """Per-scenario diff entry comparing a candidate run to a golden run."""

    scenario_name: str
    baseline_verdict: EvalVerdict
    candidate_verdict: EvalVerdict

    baseline_detail: str
    candidate_detail: str
    changed: bool


class EvalBaselineDiffView(BaseModel):
    """Full baseline comparison: overall deltas + per-scenario rows."""

    baseline_run_id: str
    candidate_run_id: str
    overall_changed: bool
    scenarios: list[BaselineScenarioDiffView]


# --- Phase 4: regression case + run views ---------------------------------


class RegressionCaseView(BaseModel):
    """A regression case: golden trace + expected checks."""

    case_id: str
    name: str
    seed_trace_id: str
    expected: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class CreateRegressionCaseRequest(BaseModel):
    """Body for ``POST /api/v1/regression-cases``."""

    case_id: str = Field(..., description="Client-generated unique id.")
    name: str
    seed_trace_id: str
    expected: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class RegressionRunView(BaseModel):
    """One regression run result."""

    run_id: str
    case_id: str
    passed: bool
    detail: str | None = None
    branch_id: str | None = None
    started_at: str
    finished_at: str


class RunRegressionRequest(BaseModel):
    """Body for ``POST /api/v1/regression-cases/{id}/run``."""

    factory_ref: str | None = Field(
        default=None,
        description="Optional registered factory ref. Defaults to the built-in "
        "frozen-replay factory.",
    )


class CreateEvalRunRequest(BaseModel):
    """Body for ``POST /api/v1/evals`` — a YAML-serialized suite."""

    suite_yaml: str = Field(
        ..., description="YAML-serialized EvalSuite (see docs/phases/phase-5.5.md)."
    )
    suite_name: str | None = Field(
        default=None,
        description=(
            "Optional name override; if absent the YAML's ``name`` key is used."
        ),
    )


class CreateEvalRunResponse(BaseModel):
    """Returned after submitting a run — the persisted run id + initial view."""

    run_id: str
    run: EvalRunDetailView


# ---------------------------------------------------------------------------
# Mappers (eval dataclass → pydantic view model).
# ---------------------------------------------------------------------------


def _outcome_to_view(out: EvaluatorOutcome) -> EvaluatorOutcomeView:
    """Project an ``EvaluatorOutcome`` dataclass to its view model."""
    return EvaluatorOutcomeView(
        kind=out.kind,
        verdict=out.verdict,
        detail=out.detail,
        metrics=dict(out.metrics),
    )


def _scenario_to_view(scen: ScenarioResult) -> ScenarioResultView:
    """Project a ``ScenarioResult`` dataclass to its view model."""
    return ScenarioResultView(
        name=scen.name,
        seed_trace_id=scen.seed_trace_id,
        branch_id=str(scen.branch_id) if scen.branch_id else None,
        verdict=scen.verdict,
        outcomes=[_outcome_to_view(o) for o in scen.outcomes],
        rollup=TokenRollupView(
            prompt_tokens=scen.rollup.prompt_tokens,
            completion_tokens=scen.rollup.completion_tokens,
            total_tokens=scen.rollup.total_tokens,
            llm_call_count=scen.rollup.llm_call_count,
        ),
        latency=ScenarioLatencyView(
            total_s=scen.latency.total_s,
            replay_s=scen.latency.replay_s,
            evaluate_s=scen.latency.evaluate_s,
        ),
        error_message=scen.error_message,
    )


def _run_to_detail_view(run: EvalSuiteResult) -> EvalRunDetailView:
    """Project an ``EvalSuiteResult`` dataclass to its detail view model."""
    return EvalRunDetailView(
        run_id=str(run.run_id),
        suite_name=run.suite_name,
        started_at=run.started_at,
        finished_at=run.finished_at,
        overall_verdict=run.overall_verdict,
        scenarios=[_scenario_to_view(s) for s in run.scenarios],
    )


def _run_to_summary_view(run: EvalSuiteResultSummary) -> EvalRunSummaryView:
    """Project an ``EvalSuiteResultSummary`` dataclass to its summary view model."""
    return EvalRunSummaryView(
        run_id=run.run_id,
        suite_name=run.suite_name,
        started_at=run.started_at,
        finished_at=run.finished_at,
        overall_verdict=run.overall_verdict,
    )


# ---------------------------------------------------------------------------
# YAML suite loader — shared by HTTP + CLI paths.
# ---------------------------------------------------------------------------


def _expectation_from_dict(
    kind: EvaluatorKind, data: dict[str, object]
) -> _Expectation:
    """Build the typed expectation for ``kind`` from a YAML dict.

    The mapping is closed: each evaluator kind has exactly one expectation
    type, and an unexpected kind here surfaces as a ``400 Bad Request`` in
    the HTTP layer or a ``SuiteValidationError`` in the CLI.
    """
    if kind is EvaluatorKind.TOOL_CHECK:
        return ToolCheckExpectation(**data)  # type: ignore[arg-type]
    if kind is EvaluatorKind.GOAL_CHECK:
        return GoalCheckExpectation(**data)  # type: ignore[arg-type]
    if kind is EvaluatorKind.CONSISTENCY:
        return ConsistencyExpectation(**data)  # type: ignore[arg-type]
    if kind is EvaluatorKind.TOKEN_BUDGET:
        return TokenBudgetExpectation(**data)  # type: ignore[arg-type]
    if kind is EvaluatorKind.NO_HALLUCINATION:
        return NoHallucinationExpectation(**data)  # type: ignore[arg-type]
    raise SuiteValidationError(
        f"kind {kind!r} has no expectation loader (LLM_JUDGE expected via suite "
        "config, not inline)."
    )


def _evaluator_request_from_dict(item: dict[str, Any]) -> EvaluatorRequest:
    """Parse a single ``evaluators`` list entry from YAML."""
    if "kind" not in item:
        raise SuiteValidationError("each evaluator entry needs a 'kind' key")
    try:
        kind = EvaluatorKind(item["kind"])
    except ValueError as exc:
        valid = [k.value for k in EvaluatorKind]
        raise SuiteValidationError(
            f"unknown evaluator kind {item['kind']!r}; valid: {valid}"
        ) from exc
    expected_dict = dict(item.get("expected", {}))
    expected = _expectation_from_dict(kind, expected_dict)
    return EvaluatorRequest(kind=kind, expected=expected)


def _scenario_from_dict(scen: dict[str, Any]) -> EvalScenario:
    """Parse a single scenario entry from YAML."""
    if "name" not in scen:
        raise SuiteValidationError("each scenario needs a 'name' key")
    if "seed_trace_id" not in scen:
        raise SuiteValidationError(f"scenario {scen.get('name')!r} needs 'seed_trace_id'")
    if "evaluators" not in scen or not scen["evaluators"]:
        raise SuiteValidationError(
            f"scenario {scen['name']!r} needs a non-empty 'evaluators' list"
        )
    evaluators = [_evaluator_request_from_dict(e) for e in scen["evaluators"]]
    # candidate_mode defaults to frozen — cheapest mode, recommended baseline.
    mode_raw = scen.get("candidate_mode", "frozen")
    try:
        mode = CandidateMode(mode_raw)
    except ValueError as exc:
        valid = [k.value for k in CandidateMode]
        raise SuiteValidationError(
            f"unknown candidate_mode {mode_raw!r}; valid: {valid}"
        ) from exc
    branch_at_index = scen.get("branch_at_index")
    return EvalScenario(
        name=scen["name"],
        seed_trace_id=scen["seed_trace_id"],
        candidate_mode=mode,
        branch_at_index=branch_at_index,
        evaluators=evaluators,
        query=scen.get("query", ""),
        expected=dict(scen.get("expected", {})),
    )


def parse_suite_from_yaml(yaml_text: str) -> EvalSuite:
    """Parse an :class:`EvalSuite` from raw YAML text.

    The YAML contract (see ``docs/phases/phase-5.5.md``) is:

    .. code-block:: yaml

        name: my-suite
        concurrency: 8                       # optional, default 8
        scenario_timeout_s: 30.0             # optional, default 30s
        scenarios:
          - name: search-and-summarise
            seed_trace_id: 0123-...
            candidate_mode: branch           # frozen | branch | full_rerun
            branch_at_index: 5               # required if mode != frozen
            query: "what is X?"              # optional context
            expected:                        # optional, passed to judge
              domain: physics
            evaluators:
              - kind: tool_check
                expected:
                  required_tools: ["search"]
                  forbidden_tools: []
              - kind: goal_check
                expected:
                  regex: "X = .*\\\\d+.*"
                  must_be: null              # optional exact-match
              - kind: token_budget
                expected:
                  budget_tokens: 4000
        # judge:                             # optional, only used by llm_judge
        #   module: my_judge
        #   factory: build_judge
    """
    try:
        # yaml.safe_load raises yaml.YAMLError on any syntax issue; we wrap
        # to a domain-typed error so callers don't need to import yaml.
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise SuiteValidationError(f"invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise SuiteValidationError(
            f"top-level YAML must be a mapping, got {type(data).__name__}"
        )
    if "name" not in data:
        raise SuiteValidationError("suite YAML needs a top-level 'name' key")
    if "scenarios" not in data or not data["scenarios"]:
        raise SuiteValidationError("suite YAML needs a non-empty 'scenarios' list")

    scenarios = [_scenario_from_dict(s) for s in data["scenarios"]]
    suite = EvalSuite(
        name=str(data["name"]),
        scenarios=scenarios,
        concurrency=data.get("concurrency"),
        scenario_timeout_s=data.get("scenario_timeout_s"),
        judge=None,  # judge wiring is configured out-of-band (callers set this)
    )
    # Pre-flight validation: catches dup names, bad mode/index combos, etc.
    validate_suite(suite)
    return suite


# ---------------------------------------------------------------------------
# Baseline diff — compares a candidate run to a golden baseline.
# ---------------------------------------------------------------------------


def _scenario_detail_for_baseline(
    scen: ScenarioResult,
) -> tuple[str, EvalVerdict, str]:
    """Helper for :func:`_build_baseline_diff` — extract (name, verdict, detail)."""
    # Pick the most informative detail string: error_message first, then any
    # evaluator's detail (no deterministic priority between evaluators).
    detail = scen.error_message or ""
    if not detail and scen.outcomes:
        detail = scen.outcomes[0].detail
    return scen.name, scen.verdict, detail


def _build_baseline_diff(
    baseline: EvalSuiteResult, candidate: EvalSuiteResult
) -> EvalBaselineDiffView:
    """Diff two runs (full :class:`EvalSuiteResult`s) for the baseline view."""
    base_by_name = {
        name: (verdict, det)
        for name, verdict, det in (
            _scenario_detail_for_baseline(s) for s in baseline.scenarios
        )
    }
    cand_by_name = {
        name: (verdict, det)
        for name, verdict, det in (
            _scenario_detail_for_baseline(s) for s in candidate.scenarios
        )
    }
    # Union preserves the suite's scenario order: iterate the candidate first
    # (newer), then any stragglers that exist only in the baseline.
    ordered_names: list[str] = [s.name for s in candidate.scenarios]
    for s in baseline.scenarios:
        if s.name not in cand_by_name:
            ordered_names.append(s.name)

    rows: list[BaselineScenarioDiffView] = []
    overall_changed = False
    for name in ordered_names:
        b_verdict, b_detail = base_by_name.get(
            name, (EvalVerdict.SKIP, "(scenario absent from baseline)")
        )
        c_verdict, c_detail = cand_by_name.get(
            name, (EvalVerdict.SKIP, "(scenario absent from candidate)")
        )
        changed = b_verdict != c_verdict
        if changed:
            overall_changed = True
        rows.append(
            BaselineScenarioDiffView(
                scenario_name=name,
                baseline_verdict=b_verdict,
                candidate_verdict=c_verdict,
                baseline_detail=b_detail,
                candidate_detail=c_detail,
                changed=changed,
            )
        )
    return EvalBaselineDiffView(
        baseline_run_id=str(baseline.run_id),
        candidate_run_id=str(candidate.run_id),
        overall_changed=overall_changed,
        scenarios=rows,
    )


# ---------------------------------------------------------------------------
# Mount
# ---------------------------------------------------------------------------


def mount_eval(app: FastAPI) -> None:
    """Register the eval-harness API routes on ``app``.

    Mirrors :func:`timetravel.timeline.mount_timeline` — same app.state.store
    accessor, same exception conventions. Should be called after
    :func:`~timetravel.timeline.mount_timeline` so the UI can deep-link into
    branches (the timeline owns ``GET /api/v1/traces/{trace_id}/branches``).
    """

    @app.get("/api/v1/evals", tags=["eval"])
    def list_eval_runs(
        request: Request,
        limit: int = Query(_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> EvalRunListResponse:
        """List eval runs, newest-first by ``started_at``."""
        store: TraceStore = request.app.state.store
        summaries, total = store.list_eval_runs(limit=limit, offset=offset)
        return EvalRunListResponse(
            items=[_run_to_summary_view(s) for s in summaries],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.post("/api/v1/evals", tags=["eval"], status_code=status.HTTP_201_CREATED)
    def create_eval_run(
        request: Request,
        body: CreateEvalRunRequest,
    ) -> CreateEvalRunResponse:
        """Parse suite YAML, run it, persist the result, and return the detail view.

        Long-running suites (>30s) work but block this thread; treat as
        fire-and-wait for now. Validation errors come back as ``400``.
        """
        if len(body.suite_yaml.encode("utf-8")) > _MAX_SUITE_YAML_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"suite_yaml exceeds {_MAX_SUITE_YAML_BYTES} bytes; "
                    "split into smaller runs."
                ),
            )
        try:
            suite = parse_suite_from_yaml(body.suite_yaml)
        except SuiteValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"suite validation failed: {exc}",
            ) from exc
        if body.suite_name is not None:
            suite = EvalSuite(
                name=body.suite_name,
                scenarios=suite.scenarios,
                concurrency=suite.concurrency,
                scenario_timeout_s=suite.scenario_timeout_s,
                judge=suite.judge,
            )

        store: TraceStore = request.app.state.store
        try:
            result = asyncio.run(evaluate(suite, store=store))
        except SuiteValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"suite validation failed: {exc}",
            ) from exc
        store.upsert_eval_run(result, suite_yaml=body.suite_yaml)
        return CreateEvalRunResponse(
            run_id=str(result.run_id),
            run=_run_to_detail_view(result),
        )

    @app.get("/api/v1/evals/{run_id}", tags=["eval"])
    def get_eval_run(request: Request, run_id: str) -> EvalRunDetailView:
        """Return the full run, including every scenario's outcomes."""
        if not _is_valid_uuid(run_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="run_id must be a UUID",
            )
        store: TraceStore = request.app.state.store
        result = store.get_eval_run(run_id)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"eval run {run_id} not found",
            )
        return _run_to_detail_view(result)

    @app.get("/api/v1/evals/{run_id}/baseline", tags=["eval"])
    def get_eval_baseline(
        request: Request,
        run_id: str,
        baseline_run_id: str = Query(
            ...,
            description="Golden/good run to diff against. UUID required.",
        ),
    ) -> EvalBaselineDiffView:
        """Compare this run (candidate) against ``baseline_run_id`` (golden).

        The diff is purely on stored verdicts — no re-execution. Each scenario
        row reports the two verdicts and a short detail per side.
        """
        if not _is_valid_uuid(run_id) or not _is_valid_uuid(baseline_run_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="run_id and baseline_run_id must both be UUIDs",
            )
        store: TraceStore = request.app.state.store
        candidate = store.get_eval_run(run_id)
        if candidate is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"candidate eval run {run_id} not found",
            )
        baseline = store.get_eval_run(baseline_run_id)
        if baseline is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"baseline eval run {baseline_run_id} not found",
            )
        return _build_baseline_diff(baseline, candidate)

    @app.delete("/api/v1/evals/{run_id}", tags=["eval"])
    def delete_eval_run(request: Request, run_id: str) -> dict[str, str]:
        """Delete a run and its scenario rows (cascade via FK ON DELETE)."""
        if not _is_valid_uuid(run_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="run_id must be a UUID",
            )
        store: TraceStore = request.app.state.store
        existing = store.get_eval_run(run_id)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"eval run {run_id} not found",
            )
        store.delete_eval_run(run_id)
        return {"status": "deleted", "run_id": run_id}

    # --- Phase 4: regression cases + runs --------------------------------
    @app.get("/api/v1/regression-cases", tags=["eval", "regression"])
    def list_regression_cases(request: Request) -> list[RegressionCaseView]:
        """List all regression cases, newest-first."""
        store: TraceStore = request.app.state.store
        return [RegressionCaseView(**r) for r in store.list_regression_cases()]

    @app.post(
        "/api/v1/regression-cases",
        tags=["eval", "regression"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_regression_case(
        request: Request,
        body: CreateRegressionCaseRequest,
    ) -> RegressionCaseView:
        """Create or update a regression case (upsert by case_id)."""
        store: TraceStore = request.app.state.store
        if store.get_trace(body.seed_trace_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"seed trace {body.seed_trace_id} not found",
            )
        store.upsert_regression_case(body.model_dump())
        return RegressionCaseView(**body.model_dump())

    @app.get(
        "/api/v1/regression-cases/{case_id}",
        tags=["eval", "regression"],
    )
    def get_regression_case(
        request: Request, case_id: str
    ) -> RegressionCaseView:
        """Return one regression case by id."""
        store: TraceStore = request.app.state.store
        case = store.get_regression_case(case_id)
        if case is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"regression case {case_id} not found",
            )
        return RegressionCaseView(**case)

    @app.delete(
        "/api/v1/regression-cases/{case_id}",
        tags=["eval", "regression"],
    )
    def delete_regression_case(
        request: Request, case_id: str
    ) -> dict[str, str]:
        """Delete a regression case (cascades to runs)."""
        store: TraceStore = request.app.state.store
        if not store.delete_regression_case(case_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"regression case {case_id} not found",
            )
        return {"status": "deleted", "case_id": case_id}

    @app.post(
        "/api/v1/regression-cases/{case_id}/run",
        tags=["eval", "regression"],
    )
    async def run_regression_case(
        request: Request,
        case_id: str,
        body: RunRegressionRequest,
    ) -> RegressionRunView:
        """Run ``run_frozen_verification`` for one case and persist the result."""
        # pylint: disable=import-outside-toplevel

        from agent_timetravel.evaluate import run_frozen_verification
        # pylint: enable=import-outside-toplevel

        store: TraceStore = request.app.state.store
        if store.get_regression_case(case_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"regression case {case_id} not found",
            )
        factory = None
        if body.factory_ref is not None:
            factory = get_regression_factory(body.factory_ref)
            if factory is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"regression factory {body.factory_ref!r} not found",
                )
        await run_frozen_verification(case_id, store=store, factory=factory)
        runs = store.list_regression_runs(case_id)
        latest = runs[0] if runs else None
        if latest is None:  # pragma: no cover - insert always lands a row
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="regression run persisted but not readable",
            )
        return RegressionRunView(**latest)

    @app.get(
        "/api/v1/regression-cases/{case_id}/runs",
        tags=["eval", "regression"],
    )
    def list_regression_runs(
        request: Request, case_id: str
    ) -> list[RegressionRunView]:
        """List regression runs for a case, newest-first."""
        store: TraceStore = request.app.state.store
        if store.get_regression_case(case_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"regression case {case_id} not found",
            )
        return [RegressionRunView(**r) for r in store.list_regression_runs(case_id)]

    @app.post(
        "/api/v1/regression-suites/stream",
        tags=["eval", "regression"],
    )
    async def stream_regression_suite(
        request: Request,
        case_ids: list[str],
    ) -> StreamingResponse:
        """Run a suite of regression cases and stream progress via SSE.

        The request body is a JSON array of case ids. The response is an SSE
        stream of ``suite_started`` / ``case_done`` / ``suite_finished``
        events (see :class:`timetravel.suite_runner.SuiteRunner`).
        """
        # pylint: disable=import-outside-toplevel
        from agent_timetravel.suite_runner import SuiteRunner
        # pylint: enable=import-outside-toplevel

        if not case_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="case_ids must contain at least one regression case",
            )

        store: TraceStore = request.app.state.store

        async def _stream() -> AsyncIterator[str]:
            runner = SuiteRunner(store, case_ids=case_ids)
            async for event in runner.run():
                yield f"data: {json.dumps(event, default=str)}\n\n"

        return StreamingResponse(_stream(), media_type="text/event-stream")


def _is_valid_uuid(value: str) -> bool:
    """Cheap UUID-format check (not a strict UUID parse — fast pre-validation)."""
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


__all__ = [
    "BaselineScenarioDiffView",
    "CreateEvalRunRequest",
    "CreateEvalRunResponse",
    "CreateRegressionCaseRequest",
    "EvalBaselineDiffView",
    "EvalRunDetailView",
    "EvalRunListResponse",
    "EvalRunSummaryView",
    "EvaluatorOutcomeView",
    "RegressionCaseView",
    "RegressionRunView",
    "RunRegressionRequest",
    "ScenarioLatencyView",
    "ScenarioResultView",
    "TokenRollupView",
    "get_regression_factory",
    "mount_eval",
    "parse_suite_from_yaml",
    "register_regression_factory",
]
