"use client";

/**
 * Client-side helper that POSTs to a streaming timetravel endpoint and dispatches
 * store actions for each NDJSON event. Shared by the run trigger (page.tsx)
 * and the branch trigger (span-detail.tsx) so both surface the model's
 * thinking live in the ThinkingPanel.
 *
 * The endpoint streams one JSON object per line; the final line is a
 * `trace_end` event whose `trace` we commit via finishLiveRun (which clears
 * the live view and selects the committed branch).
 */
import type { StreamEvent } from "./types";
import { useTimeTravelStore } from "./store";

export async function streamTimeTravel(
  endpoint: string,
  body: unknown,
  kind: "run" | "branch",
): Promise<void> {
  const {
    startLiveRun,
    beginSpan,
    appendReasoning,
    appendOutput,
    finishSpan,
    finishLiveRun,
    failLiveRun,
  } = useTimeTravelStore.getState();

  const query =
    kind === "run"
      ? String((body as { query?: string }).query ?? "")
      : String((body as { parent?: { query?: string } }).parent?.query ?? "");
  startLiveRun(query, kind);

  try {
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) {
      const err = await res.json().catch(() => ({}));
      throw new Error(
        (err as { error?: string }).error || `HTTP ${res.status}`,
      );
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        let evt: StreamEvent;
        try {
          evt = JSON.parse(trimmed) as StreamEvent;
        } catch {
          continue;
        }
        switch (evt.type) {
          case "span_start":
            beginSpan(evt.index, evt.name, evt.kind);
            break;
          case "reasoning_delta":
            appendReasoning(evt.index, evt.chunk);
            break;
          case "content_delta":
            appendOutput(evt.index, evt.chunk);
            break;
          case "span_end":
            finishSpan(evt.index);
            break;
          case "trace_end":
            finishLiveRun(evt.trace);
            break;
          case "error":
            throw new Error(evt.message);
        }
      }
    }
  } catch (e) {
    failLiveRun(e instanceof Error ? e.message : String(e));
  }
}
