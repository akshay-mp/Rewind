// Tiny typed fetch client for the read-only timeline API.
//
// All endpoints return JSON. Errors are normalised into one shape so the UI
// surfaces network failures, 4xx, and 5xx identically. The base path is "" —
// the SPA is served from the same origin as the API, so requests are
// relative. For dev mode Vite's proxy forwards /api → :8484.

import type {
  SearchResponse,
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

async function getJson<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      headers: { Accept: "application/json" },
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
};
