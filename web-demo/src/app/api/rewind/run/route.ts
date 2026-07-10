/**
 * POST /api/rewind/run
 *   Body: { query: string }
 *
 * Captures a fresh deep-research trace as a STREAM of NDJSON events
 * (one JSON object per line). Every span is a live LLM call, streamed
 * token-by-token so the ThinkingPanel shows the model's reasoning live.
 *
 * Event sequence:
 *   {"type":"span_start",...}
 *   {"type":"reasoning_delta",...}  (zero or more)
 *   {"type":"content_delta",...}    (zero or more)
 *   {"type":"span_end",...}
 *   ... (repeated per span) ...
 *   {"type":"trace_end","trace":{...}}
 *
 * On error a single {"type":"error","message":...} line is emitted and the
 * stream closes.
 */

import { runTraceStream } from "@/lib/deep-research/agent";
import { DEFAULT_PROMPTS, DEFAULT_QUERY } from "@/lib/deep-research/prompts";
import type { StreamEvent } from "@/lib/rewind/types";

export const runtime = "nodejs";

export async function POST(req: Request) {
  let body: { query?: string } = {};
  try {
    body = await req.json();
  } catch {
    body = {};
  }
  const query = (body.query || DEFAULT_QUERY).trim();
  if (!query) {
    return Response.json({ error: "query is required" }, { status: 400 });
  }

  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const emit = (e: StreamEvent) => {
        controller.enqueue(encoder.encode(JSON.stringify(e) + "\n"));
      };
      try {
        await runTraceStream(query, emit, DEFAULT_PROMPTS);
      } catch (err) {
        const message = err instanceof Error ? err.message : "unknown error";
        emit({ type: "error", message });
      } finally {
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "application/x-ndjson; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no", // disable proxy buffering (nginx)
    },
  });
}
