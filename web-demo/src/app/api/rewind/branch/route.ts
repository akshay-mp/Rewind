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
import type { Span, SpanKind, StreamEvent, Trace } from "@/lib/rewind/types";

export const runtime = "nodejs";

// Keep prompt-debugging flexible while bounding request memory and processing.
const MAX_EDITED_SYSTEM_PROMPT_LENGTH = 32_000;
const MAX_BRANCH_REQUEST_BYTES = 1_048_576;
const MAX_QUERY_LENGTH = 8_000;
const MAX_PARENT_SPANS = 64;
const MAX_ID_LENGTH = 256;
const MAX_SPAN_STRING_LENGTH = 64_000;
const MAX_LABEL_LENGTH = 256;
const MAX_NOTE_LENGTH = 4_000;

const SPAN_KINDS = new Set<SpanKind>([
  "clarify_with_user",
  "write_research_brief",
  "supervisor_think",
  "conduct_research",
  "research_complete",
  "final_report",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isBoundedString(value: unknown, maxLength: number): value is string {
  return typeof value === "string" && value.length <= maxLength;
}

function isFiniteNonNegativeInteger(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    Number.isInteger(value) &&
    value >= 0
  );
}

function isFiniteNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isValidSpan(value: unknown, expectedIndex: number): value is Span {
  if (!isRecord(value)) return false;
  if (
    !isBoundedString(value.id, MAX_ID_LENGTH) ||
    !value.id ||
    !isFiniteNonNegativeInteger(value.index) ||
    value.index !== expectedIndex ||
    !isBoundedString(value.name, MAX_SPAN_STRING_LENGTH) ||
    !value.name ||
    typeof value.kind !== "string" ||
    !SPAN_KINDS.has(value.kind as SpanKind) ||
    value.type !== "llm" ||
    !isBoundedString(value.model, MAX_SPAN_STRING_LENGTH) ||
    !value.model ||
    !isBoundedString(value.systemPrompt, MAX_SPAN_STRING_LENGTH) ||
    !isBoundedString(value.userInput, MAX_SPAN_STRING_LENGTH) ||
    !isBoundedString(value.output, MAX_SPAN_STRING_LENGTH) ||
    !isFiniteNonNegativeNumber(value.latencyMs) ||
    !isFiniteNonNegativeInteger(value.tokensIn) ||
    !isFiniteNonNegativeInteger(value.tokensOut) ||
    (value.source !== "live" && value.source !== "cached")
  ) {
    return false;
  }
  return value.reasoning === undefined || isBoundedString(value.reasoning, MAX_SPAN_STRING_LENGTH);
}

function isValidBranchParent(value: unknown): value is Trace {
  if (!isRecord(value)) return false;
  if (
    !isBoundedString(value.id, MAX_ID_LENGTH) ||
    !value.id ||
    !isBoundedString(value.branchId, MAX_ID_LENGTH) ||
    !value.branchId ||
    (value.parentBranchId !== null &&
      (!isBoundedString(value.parentBranchId, MAX_ID_LENGTH) || !value.parentBranchId)) ||
    (value.branchAtSpanIndex !== null &&
      !isFiniteNonNegativeInteger(value.branchAtSpanIndex)) ||
    (value.branchAtSpanIndex !== null &&
      Array.isArray(value.spans) &&
      value.branchAtSpanIndex >= value.spans.length) ||
    !isBoundedString(value.query, MAX_QUERY_LENGTH) ||
    !value.query.trim() ||
    !isBoundedString(value.label, MAX_LABEL_LENGTH) ||
    !isBoundedString(value.note, MAX_NOTE_LENGTH) ||
    !Array.isArray(value.spans) ||
    value.spans.length === 0 ||
    value.spans.length > MAX_PARENT_SPANS ||
    !isFiniteNonNegativeNumber(value.createdAt)
  ) {
    return false;
  }
  return value.spans.every((span, index) => isValidSpan(span, index));
}

export function parseBranchRequestBody(body: unknown): {
  parent: Trace;
  branchAtSpanIndex: number;
  editedSystemPrompt: string;
  label?: string;
  note?: string;
} | null {
  if (!isRecord(body)) return null;

  const parent = body.parent;
  const branchAtSpanIndex = body.branchAtSpanIndex;
  if (
    !isValidBranchParent(parent) ||
    typeof branchAtSpanIndex !== "number" ||
    !Number.isFinite(branchAtSpanIndex) ||
    !Number.isInteger(branchAtSpanIndex) ||
    branchAtSpanIndex < 0 ||
    branchAtSpanIndex >= parent.spans.length ||
    branchAtSpanIndex >= DEFAULT_PROMPTS.length
  ) {
    return null;
  }

  const editedSystemPrompt = body.editedSystemPrompt;
  if (
    typeof editedSystemPrompt !== "string" ||
    !editedSystemPrompt.trim() ||
    editedSystemPrompt.length > MAX_EDITED_SYSTEM_PROMPT_LENGTH ||
    (body.label !== undefined && !isBoundedString(body.label, MAX_LABEL_LENGTH)) ||
    (body.note !== undefined && !isBoundedString(body.note, MAX_NOTE_LENGTH))
  ) {
    return null;
  }

  return {
    parent,
    branchAtSpanIndex,
    editedSystemPrompt,
    label: body.label as string | undefined,
    note: body.note as string | undefined,
  };
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = hostname.toLowerCase().replace(/^\[|\]$/g, "");
  return normalized === "localhost" || normalized === "127.0.0.1" || normalized === "::1";
}

function acceptsLocalRequest(req: Request): boolean {
  let requestUrl: URL;
  try {
    requestUrl = new URL(req.url);
  } catch {
    return false;
  }
  if (!isLoopbackHostname(requestUrl.hostname)) return false;

  const origin = req.headers.get("origin");
  if (origin === null) return true;
  try {
    return new URL(origin).origin === requestUrl.origin;
  } catch {
    return false;
  }
}

async function readBoundedBody(
  req: Request,
): Promise<{ ok: true; text: string } | { ok: false }> {
  const contentLength = req.headers.get("content-length");
  if (contentLength !== null) {
    const parsedLength = Number(contentLength);
    if (Number.isFinite(parsedLength) && parsedLength > MAX_BRANCH_REQUEST_BYTES) {
      return { ok: false };
    }
  }

  if (!req.body) return { ok: true, text: "" };
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_BRANCH_REQUEST_BYTES) {
      await reader.cancel();
      return { ok: false };
    }
    chunks.push(value);
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return { ok: true, text: new TextDecoder().decode(bytes) };
}

export async function POST(req: Request) {
  if (!acceptsLocalRequest(req)) {
    return Response.json({ error: "local requests only" }, { status: 403 });
  }

  const boundedBody = await readBoundedBody(req);
  if (!boundedBody.ok) {
    return Response.json({ error: "request body too large" }, { status: 413 });
  }

  let body: unknown;
  try {
    body = JSON.parse(boundedBody.text);
  } catch {
    body = null;
  }
  if (!isRecord(body)) {
    return Response.json(
      { error: "parent and branchAtSpanIndex are required" },
      { status: 400 },
    );
  }

  const parsedBody = parseBranchRequestBody(body);
  if (!parsedBody) {
    return Response.json({ error: "invalid branch request" }, { status: 400 });
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
            ...parsedBody,
            prompts: DEFAULT_PROMPTS,
          },
          emit,
        );
      } catch (err) {
        console.error("[rewind-demo] branch execution failed", err);
        emit({ type: "error", message: "branch execution failed" });
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
