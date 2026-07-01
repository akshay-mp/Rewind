// Tiny typed fetch client for the read-only timeline API.
//
// All endpoints return JSON. Errors are normalised into one shape so the UI
// surfaces network failures, 4xx, and 5xx identically. The base path is "" —
// the SPA is served from the same origin as the API, so requests are
// relative. For dev mode Vite's proxy forwards /api → :8484.

import type {
  BranchNodeView,
  CreateBranchRequest,
  CreateBranchResponse,
  EvalBaselineDiffView,
  EvalRunDetailView,
  EvalRunListResponse,
  MessageDiffView,
  SearchResponse,
  SpanDiffView,
  SpanView,
  TraceDetail,
  TraceListResponse,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { Accept: "application/json" },
      ...init,
    });
  } catch (err) {
    throw new ApiError(
      `network error reaching ${path}: ${(err as Error).message}`,
      0,
    );
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Body wasn't JSON; keep the status text.
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

async function getJson<T>(path: string): Promise<T> {
  return request<T>(path);
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function deleteJson<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

export interface ListTracesParams {
  limit?: number;
  offset?: number;
}

export const api = {
  listTraces(params: ListTracesParams = {}): Promise<TraceListResponse> {
    const search = new URLSearchParams();
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    const qs = search.toString();
    return getJson<TraceListResponse>(`/api/v1/traces${qs ? "?" + qs : ""}`);
  },

  getTrace(traceId: string): Promise<TraceDetail> {
    return getJson<TraceDetail>(`/api/v1/traces/${encodeURIComponent(traceId)}`);
  },

  getSpan(rewindId: string): Promise<SpanView> {
    return getJson<SpanView>(`/api/v1/spans/${encodeURIComponent(rewindId)}`);
  },

  search(
    query: string,
    filters: { kind?: string | undefined; model?: string | undefined; status?: string | undefined } = {},
  ): Promise<SearchResponse> {
    const search = new URLSearchParams({ q: query });
    if (filters.kind) search.set("kind", filters.kind);
    if (filters.model) search.set("model", filters.model);
    if (filters.status) search.set("status", filters.status);
    return getJson<SearchResponse>(`/api/v1/search?${search.toString()}`);
  },

  // ----- Phase 5: branching & diff ----------------------------------------
  //
  // ``getBranchTree`` and ``diffBranches`` take trace/branch ids and return
  // the structured payloads that drive the BranchTree and DiffView
  // components. ``createBranch`` forks from a parent at an index; the
  // server resolves the parent to the trace root when omitted.

  getBranchTree(traceId: string): Promise<BranchNodeView> {
    return getJson<BranchNodeView>(
      `/api/v1/traces/${encodeURIComponent(traceId)}/branches`,
    );
  },

  diffBranches(
    traceId: string,
    leftBranchId: string,
    rightBranchId: string,
  ): Promise<SpanDiffView> {
    // Backend binds ``left`` / ``right`` query params (see timeline.diff_branches).
    const qs = new URLSearchParams({
      left: leftBranchId,
      right: rightBranchId,
    });
    return getJson<SpanDiffView>(
      `/api/v1/traces/${encodeURIComponent(traceId)}/diff?${qs.toString()}`,
    );
  },

  messageDiff(rewindId: string, otherRewindId: string): Promise<MessageDiffView> {
    const qs = new URLSearchParams({ other: otherRewindId });
    return getJson<MessageDiffView>(
      `/api/v1/spans/${encodeURIComponent(rewindId)}/message-diff?${qs.toString()}`,
    );
  },

  createBranch(
    traceId: string,
    body: CreateBranchRequest,
  ): Promise<CreateBranchResponse> {
    return postJson<CreateBranchResponse>(
      `/api/v1/traces/${encodeURIComponent(traceId)}/branches`,
      body,
    );
  },

  // ----- Phase 5.5: eval harness ------------------------------------------
  //
  // The eval API is mounted alongside the timeline API on the same origin.
  // ``listEvalRuns`` returns the newest-first summary page. ``getEvalRun``
  // pulls the full detail (including per-scenario outcomes). ``compareBaseline``
  // diffs a candidate run against a golden run; the backend resolves both
  // run ids to their stored verdicts.

  listEvalRuns(params: ListTracesParams = {}): Promise<EvalRunListResponse> {
    const search = new URLSearchParams();
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    const qs = search.toString();
    return getJson<EvalRunListResponse>(`/api/v1/evals${qs ? "?" + qs : ""}`);
  },

  getEvalRun(runId: string): Promise<EvalRunDetailView> {
    return getJson<EvalRunDetailView>(
      `/api/v1/evals/${encodeURIComponent(runId)}`,
    );
  },

  compareEvalBaseline(
    candidateRunId: string,
    baselineRunId: string,
  ): Promise<EvalBaselineDiffView> {
    const qs = new URLSearchParams({ baseline_run_id: baselineRunId });
    return getJson<EvalBaselineDiffView>(
      `/api/v1/evals/${encodeURIComponent(candidateRunId)}/baseline?${qs.toString()}`,
    );
  },

  deleteEvalRun(runId: string): Promise<{ deleted: string }> {
    return deleteJson<{ deleted: string }>(
      `/api/v1/evals/${encodeURIComponent(runId)}`,
    );
  },
};
