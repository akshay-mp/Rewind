/**
 * POST /api/rewind/branch
 *   Body: {
 *     parent: Trace,
 *     branchAtSpanIndex: number,
 *     editedSystemPrompt: string,
 *     label?: string,
 *     note?: string,
 *   }
 *
 * Creates a new branch as a STREAM of NDJSON events (same shape as
 * /api/rewind/run). FROZEN-replays spans [0, branchAtSpanIndex) from the
 * parent recording (emitted instantly as span_start+span_end), then runs live
 * LLM calls from branchAtSpanIndex onward using the edited system prompt at
 * branchAtSpanIndex and the original prompts (with rebuilt context) for every
 * subsequent span — streamed token-by-token so the ThinkingPanel shows the
 * model's reasoning live for the divergent tail.
 */

import { runBranchStream } from "@/lib/deep-research/agent";
import { DEFAULT_PROMPTS } from "@/lib/deep-research/prompts";
import type { StreamEvent, Trace } from "@/lib/rewind/types";

export const runtime = "nodejs";

export async function POST(req: Request) {
  let body: {
    parent?: Trace;
    branchAtSpanIndex?: number;
    editedSystemPrompt?: string;
    label?: string;
    note?: string;
  } = {};
  try {
    body = await req.json();
  } catch {
    body = {};
  }
  if (!body.parent || typeof body.branchAtSpanIndex !== "number") {
    return Response.json(
      { error: "parent and branchAtSpanIndex are required" },
      { status: 400 },
    );
  }
  if (
    body.branchAtSpanIndex < 0 ||
    body.branchAtSpanIndex >= body.parent.spans.length
  ) {
    return Response.json(
      { error: "branchAtSpanIndex out of range" },
      { status: 400 },
    );
  }
  if (!body.editedSystemPrompt || !body.editedSystemPrompt.trim()) {
    return Response.json(
      { error: "editedSystemPrompt is required" },
      { status: 400 },
    );
  }

  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const emit = (e: StreamEvent) => {
        controller.enqueue(encoder.encode(JSON.stringify(e) + "\n"));
      };
      try {
        await runBranchStream(
          {
            parent: body.parent!,
            branchAtSpanIndex: body.branchAtSpanIndex!,
            editedSystemPrompt: body.editedSystemPrompt!,
            label: body.label,
            note: body.note,
            prompts: DEFAULT_PROMPTS,
          },
          emit,
        );
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
      "X-Accel-Buffering": "no",
    },
  });
}
