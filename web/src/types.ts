// Wire-shape types mirroring `src/rewind/timeline.py`.
//
// These are the source of truth for what the timeline UI understands. Keep
// them in lock-step with the Pydantic models on the Python side: every field
// here must exist on the JSON response, and every new field there should land
// here before it's consumed in the UI.

export type SpanKind =
  | "gen_ai.llm"
  | "gen_ai.tool"
  | "gen_ai.mcp"
  | "gen_ai.agent"
  | "rewind.unknown";

export type SpanStatus = "OK" | "ERROR" | "UNSET";

export interface TraceSummary {
  trace_id: string;
  root_branch_id: string;
  created_at: string;
  span_count: number;
  span_count_by_kind: Record<string, number>;
  model_names: string[];
  has_error: boolean;
}

export interface TraceListResponse {
  items: TraceSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface SpanView {
  rewind_id: string;
  span_id: string;
  parent_span_id: string | null;
  branch_id: string | null;
  name: string;
  kind: SpanKind;
  start_time: string;
  end_time: string;
  status: SpanStatus;
  status_message: string | null;
  model_name: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  messages_hash: string | null;
  tools_hash: string | null;
  raw_attributes: Record<string, unknown>;
}

export interface TraceDetail {
  trace_id: string;
  root_branch_id: string;
  created_at: string;
  spans: SpanView[];
}

export interface SpanSearchHit {
  trace_id: string;
  rewind_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: SpanKind;
  status: SpanStatus;
  model_name: string | null;
  start_time: string;
  snippet: string;
}

export interface SearchResponse {
  items: SpanSearchHit[];
  total: number;
  limit: number;
  offset: number;
}

// ----- Phase 5: branching & diff -------------------------------------------

/** Recursive branch node — one row per `rewind.models.Branch`. */
export interface BranchNodeView {
  branch_id: string;
  trace_id: string;
  parent_branch_id: string | null;
  /** Index within the parent branch where this branch diverged. */
  branch_at_index: number | null;
  mode: string;
  label: string;
  created_at: string;
  children: BranchNodeView[];
}

/** One row of a side-by-side span diff. */
export interface SpanPairView {
  index: number;
  /** Span as it appears on the left branch; ``null`` if added on right. */
  left: SpanView | null;
  /** Span as it appears on the right branch; ``null`` if removed. */
  right: SpanView | null;
  status: "equal" | "added" | "removed" | "changed";
  /**
   * ``true`` for the first divergent row — the UI uses this to render the
   * "branch point" marker on the timeline (Phase 5 exit criterion).
   */
  is_first_divergence: boolean;
}

export interface SpanDiffView {
  pairs: SpanPairView[];
  /** Index into ``pairs`` where the two branches first diverged. */
  first_divergence_index: number | null;
  left_count: number;
  right_count: number;
  identical: boolean;
}

/** One fragment of a token-level message diff. */
export interface MessageFragmentView {
  text: string;
  /** ``equal``/``added``/``removed``/``changed`` — same vocab as span status. */
  kind: "equal" | "added" | "removed" | "changed";
}

export interface MessageDiffView {
  left: string;
  right: string;
  fragments: MessageFragmentView[];
  added_tokens: number;
  removed_tokens: number;
  identical: boolean;
}

/** POST body for ``POST /api/v1/traces/{traceId}/branches``. */
export interface CreateBranchRequest {
  trace_id: string;
  /** Branch to fork from; defaults to the trace root when omitted. */
  parent_branch_id: string | null;
  /** Index within the parent where the fork cuts off (inclusive of parent). */
  branch_at_index: number;
  mode: string;
  label: string;
}

export interface CreateBranchResponse {
  branch_id: string;
  trace_id: string;
  parent_branch_id: string | null;
  branch_at_index: number;
  mode: string;
  label: string;
  created_at: string;
}

// ----- Phase 5.5: eval harness ---------------------------------------------
//
// Wire shape mirrors the pydantic view models in ``src/rewind/eval_api.py``.
// The eval namespace rides the same origin as the timeline API — no CORS.

export type EvalVerdict = "PASS" | "FAIL" | "SKIP" | "ERROR";

export type EvaluatorKind =
  | "tool_check"
  | "goal_check"
  | "consistency"
  | "token_budget"
  | "no_hallucination";

export interface EvaluatorOutcomeView {
  kind: EvaluatorKind;
  verdict: EvalVerdict;
  detail: string;
  metrics: Record<string, unknown>;
}

export interface TokenRollupView {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  llm_call_count: number;
}

export interface ScenarioLatencyView {
  total_s: number;
  replay_s: number;
  evaluate_s: number;
}

export interface ScenarioResultView {
  name: string;
  seed_trace_id: string;
  branch_id: string | null;
  verdict: EvalVerdict;
  outcomes: EvaluatorOutcomeView[];
  rollup: TokenRollupView;
  latency: ScenarioLatencyView;
  error_message: string | null;
}

export interface EvalRunSummaryView {
  run_id: string;
  suite_name: string;
  started_at: string;
  finished_at: string;
  overall_verdict: EvalVerdict;
}

export interface EvalRunDetailView {
  run_id: string;
  suite_name: string;
  started_at: string;
  finished_at: string;
  overall_verdict: EvalVerdict;
  scenarios: ScenarioResultView[];
}

export interface EvalRunListResponse {
  items: EvalRunSummaryView[];
  total: number;
  limit: number;
  offset: number;
}

export interface BaselineScenarioDiffView {
  scenario_name: string;
  baseline_verdict: EvalVerdict;
  candidate_verdict: EvalVerdict;
  baseline_detail: string;
  candidate_detail: string;
  changed: boolean;
}

export interface EvalBaselineDiffView {
  baseline_run_id: string;
  candidate_run_id: string;
  overall_changed: boolean;
  scenarios: BaselineScenarioDiffView[];
}
