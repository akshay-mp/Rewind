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
