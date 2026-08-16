// Tiny typed fetch client for the read-only timeline API.
//
// All endpoints return JSON. Errors are normalised into one shape so the UI
// surfaces network failures, 4xx, and 5xx identically. The base path is "" —
// the SPA is served from the same origin as the API, so requests are
// relative. For dev mode Vite's proxy forwards /api → :8484.

import type {
  AgentListResponse,
  AgentSessionRequest,
  BranchNodeView,
  CreateBranchRequest,
  CreateBranchResponse,
  DecisionRequest,
  EvalBaselineDiffView,
  EvalRunDetailView,
  EvalRunListResponse,
  MessageDiffView,
  SearchResponse,
  SessionDetailView,
  SessionListResponse,
  SpanDiffView,
  SpanView,
  StartSessionRequest,
  StartSessionResponse,
  StepEvent,
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
  listAgents(): Promise<AgentListResponse> {
    return getJson<AgentListResponse>("/api/v1/agents");
  },

  startAgentSession(agentRef: string, body: AgentSessionRequest): Promise<StartSessionResponse> {
    return postJson<StartSessionResponse>(
      `/api/v1/agents/${encodeURIComponent(agentRef)}/sessions`,
      body,
    );
  },

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

  // ----- Phase 9: interactive stepping -------------------------------------
  //
  // Sessions are the server-side step-through debug runs. ``startSession``
  // spawns a background task; progress flows out via ``streamSession``.
  // ``decide`` posts a Decision to resume the blocked agent. ``deleteSession``
  // cancels the task and removes the row (captured spans are preserved).

  listSessions(params: ListTracesParams = {}): Promise<SessionListResponse> {
    const search = new URLSearchParams();
    if (params.limit !== undefined) search.set("limit", String(params.limit));
    if (params.offset !== undefined) search.set("offset", String(params.offset));
    const qs = search.toString();
    return getJson<SessionListResponse>(`/api/v1/sessions${qs ? "?" + qs : ""}`);
  },

  getSession(sessionId: string): Promise<SessionDetailView> {
    return getJson<SessionDetailView>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    );
  },

  startSession(body: StartSessionRequest): Promise<StartSessionResponse> {
    return postJson<StartSessionResponse>("/api/v1/sessions", body);
  },

  decide(sessionId: string, body: DecisionRequest): Promise<{ status: string; decision: string }> {
    return postJson<{ status: string; decision: string }>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/decide`,
      body,
    );
  },

  deleteSession(sessionId: string): Promise<void> {
    return deleteJson<void>(`/api/v1/sessions/${encodeURIComponent(sessionId)}`);
  },

  /** Phase 5.1 — execution DAG. */
  getDag(traceId: string): Promise<unknown[]> {
    return getJson(`/api/v1/traces/${encodeURIComponent(traceId)}/dag`);
  },

  /** Phase 1.2 — server-owned run-control intent. */
  getRunControl(
    sessionId: string,
  ): Promise<{ pause_after_current: boolean; run_until_breakpoint: boolean; breakpoints: unknown[] }> {
    return getJson(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/run-control`,
    );
  },

  patchRunControl(
    sessionId: string,
    body: { pause_after_current: boolean; run_until_breakpoint: boolean; breakpoints?: unknown[] },
  ): Promise<{ pause_after_current: boolean; run_until_breakpoint: boolean; breakpoints: unknown[] }> {
    return request(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/run-control`,
      {
        method: "PATCH",
        body: JSON.stringify(body),
        headers: { "Content-Type": "application/json" },
      },
    );
  },

  /** Phase 2.1 — durable prompt-version experiment records. */
  listPromptVersions(traceId: string, cursor: number): Promise<unknown[]> {
    return getJson(
      `/api/v1/traces/${encodeURIComponent(traceId)}/prompt-versions?cursor=${cursor}`,
    );
  },

  createPromptVersion(traceId: string, body: Record<string, unknown>): Promise<unknown> {
    return postJson(
      `/api/v1/traces/${encodeURIComponent(traceId)}/prompt-versions`,
      body,
    );
  },

  savePromptVersionResult(
    versionId: string,
    body: { result: string; usage?: Record<string, number>; latency_ms?: number },
  ): Promise<unknown> {
    return request(
      `/api/v1/prompt-versions/${encodeURIComponent(versionId)}/result`,
      {
        method: "PUT",
        body: JSON.stringify(body),
        headers: { "Content-Type": "application/json" },
      },
    );
  },

  /** Phase 2.1 — reusable assertion profiles. */
  listAssertionProfiles(): Promise<unknown[]> {
    return getJson(`/api/v1/assertion-profiles`);
  },

  createAssertionProfile(body: Record<string, unknown>): Promise<unknown> {
    return postJson(`/api/v1/assertion-profiles`, body);
  },

  /** Phase 2.1 — durable step reviews. */
  listReviews(traceId: string): Promise<unknown[]> {
    return getJson(`/api/v1/traces/${encodeURIComponent(traceId)}/reviews`);
  },

  saveReview(
    traceId: string,
    body: {
      trace_id: string;
      cursor_index: number;
      review_note?: string;
      review_verdict?: string;
    },
  ): Promise<unknown> {
    return postJson(
      `/api/v1/traces/${encodeURIComponent(traceId)}/reviews`,
      body,
    );
  },

  /**
   * Subscribe to a session's SSE event stream.
   *
   * Returns an unsubscribe function. The browser's ``EventSource`` API opens
   * the connection and invokes ``onEvent`` for each parsed event. We do NOT
   * use the existing ``request()`` wrapper — that's JSON-only and consumes
   * the whole body; SSE needs incremental parsing.
   *
   * EventSource auto-reconnects on connection loss; the server replays the
   * current state on reconnect because each event is self-contained (the
   * session row's status is the source of truth, queryable via
   * ``getSession``).
   */
  streamSession(
    sessionId: string,
    onEvent: (event: StepEvent) => void,
    onError?: (err: Event) => void,
  ): () => void {
    const es = new EventSource(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/stream`,
    );
    es.onmessage = (e: MessageEvent<string>) => {
      try {
        const parsed = JSON.parse(e.data) as StepEvent;
        onEvent(parsed);
      } catch {
        // Malformed event — ignore rather than kill the whole stream. The
        // next well-formed event will re-sync the UI.
      }
    };
    if (onError !== undefined) {
      es.onerror = onError;
    }
    return () => {
      es.close();
    };
  },
};
