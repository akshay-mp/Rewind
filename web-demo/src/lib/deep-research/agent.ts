/**
 * Deep Research agent runner — server-only.
 *
 * Mirrors src/open_deep_research/deep_researcher.py from langchain-ai/open_deep_research
 * (supervisor ⇄ researcher subgraph loop), but flattened to a linear sequence
 * of LLM spans for clarity in the demo. Each span is one chat completion call
 * against the local model server (Unsloth / Ollama / OpenAI-compatible).
 *
 * Two entry points:
 *   - runTrace(query, prompts)            — capture a fresh trace (all spans live)
 *   - runBranch(parent, branchAt, edit)   — FROZEN replay [0, branchAt) from
 *     the parent's recording, then live LLM calls from branchAt onward with
 *     the user's edited prompt at branchAt and the original prompts (with new
 *     context) for every subsequent span. This is exactly the BRANCH mode
 *     from akshay-mp/Rewind: only the divergent tail pays for LLM calls.
 *
 * Model backend: the OpenAI-compatible client pointed at a local server.
 * Configured via env vars:
 *   OPENAI_BASE_URL — default http://localhost:8888/v1 (Unsloth Studio)
 *   OPENAI_API_KEY  — default "local"
 *   REWIND_MODEL    — default "unsloth/Qwen3.6-27B-MTP-GGUF"
 *
 * Each live LLM call is ALSO mirrored into Rewind's OTLP receiver
 * (OTEL_EXPORTER_OTLP_ENDPOINT, default http://127.0.0.1:4318) so the Python
 * engine + timeline UI see the same spans. The mirror is best-effort: if the
 * receiver is down, the demo UI still works (it has its own in-memory trace).
 */

import { execSync } from "child_process";
import OpenAI from "openai";
import { v4 as uuid } from "uuid";
import type { Span, SpanKind, StreamEvent, Trace } from "../rewind/types";
import { DEFAULT_PROMPTS, type SpanPrompt } from "./prompts";

/**
 * Resolve the model backend config — robustly.
 *
 * Priority:
 *   1. Explicit env vars (OPENAI_BASE_URL / OPENAI_API_KEY / REWIND_MODEL).
 *      Use this for Ollama, real OpenAI, or a pinned port.
 *   2. Auto-discover the llama-server subprocess that Unsloth Studio spawned.
 *      Unsloth runs `llama-server --port <N>` on a RANDOM port each launch,
 *      so we read it from the process table. This is the endpoint that
 *      actually generates correctly (the Studio proxy on :8888 is buggy).
 *   3. Fall back to the Studio proxy on :8888 (works only when Studio's
 *      GGUF→OpenAI translation layer isn't erroring).
 *
 * The model id is also auto-resolved: llama-server accepts ANY model string
 * (it ignores it — the model is already loaded), so we don't need the exact
 * HF repo name. We default to the Qwen3.6 tag but it's cosmetic.
 */
function resolveLlamaServerPort(): number | null {
  try {
    // Find the llama-server process and extract its --port flag.
    const out = execSync(
      "ps aux | grep 'llama-server' | grep -v grep | grep -oE -- '--port [0-9]+' | head -1",
      { encoding: "utf8", timeout: 3000, stdio: ["pipe", "pipe", "ignore"] },
    ).trim();
    const m = out.match(/(\d+)/);
    return m ? Number(m[1]) : null;
  } catch {
    return null;
  }
}

function resolveConfig(): { baseUrl: string; apiKey: string; model: string } {
  const envUrl = process.env.OPENAI_BASE_URL;
  const envKey = process.env.OPENAI_API_KEY;
  const model = process.env.REWIND_MODEL || "unsloth/Qwen3.6-27B-MTP-GGUF";

  // 1. Explicit env always wins.
  if (envUrl) {
    return { baseUrl: envUrl, apiKey: envKey || "local", model };
  }
  // 2. Auto-discover the llama-server subprocess.
  const port = resolveLlamaServerPort();
  if (port) {
    return { baseUrl: `http://localhost:${port}/v1`, apiKey: "unused", model };
  }
  // 3. Fall back to the Studio proxy.
  return { baseUrl: "http://localhost:8888/v1", apiKey: envKey || "local", model };
}

const { baseUrl: OPENAI_BASE_URL, apiKey: OPENAI_API_KEY, model: MODEL_ID } =
  resolveConfig();

const REWIND_OTLP_ENDPOINT =
  process.env.OTEL_EXPORTER_OTLP_ENDPOINT || "http://127.0.0.1:4318";

// Log the resolved config once at module load so it's visible in the server log.
console.log(
  `[rewind-demo] model backend → ${OPENAI_BASE_URL} (model=${MODEL_ID})`,
);

// One client instance reused across all calls in the process.
const client = new OpenAI({
  baseURL: OPENAI_BASE_URL,
  apiKey: OPENAI_API_KEY,
});

/** Rough token estimate — ~4 chars per token. Good enough for the demo UI. */
function estimateTokens(text: string): number {
  return Math.max(1, Math.ceil((text || "").length / 4));
}

function fillTemplate(
  template: string,
  query: string,
  outputsByIndex: Record<number, string>,
): string {
  let out = template.replace(/\{query\}/g, query);
  out = out.replace(/\{output:(\d+)\}/g, (_m, idx) => {
    const v = outputsByIndex[Number(idx)];
    return v !== undefined ? v : `[output:${idx} not yet available]`;
  });
  return out;
}

/**
 * One streaming LLM call through the OpenAI-compatible server.
 *
 * Thinking is ENABLED (the opposite of the old non-streaming callLlm) so the
 * model's chain-of-thought is surfaced in the ThinkingPanel. Qwen3.6 /
 * llama-server emits reasoning as a separate `reasoning_content` delta field;
 * some servers instead inline `<think>…</think>` into `content`, so we parse
 * that as a fallback. `onDelta` is invoked for each token so the UI updates
 * live — {kind:"reasoning"} while the model thinks, {kind:"content"} for the
 * final answer.
 *
 * max_tokens is bumped to 2048 (from 600) so the thinking + the answer both
 * fit — at 600 the model spent the whole budget reasoning and truncated before
 * answering (finish_reason:"length").
 */
async function callLlmStream(
  systemPrompt: string,
  userInput: string,
  onDelta: (d: { kind: "reasoning" | "content"; chunk: string }) => void,
): Promise<{ output: string; reasoning: string; latencyMs: number }> {
  const start = Date.now();
  // llama-server accepts `chat_template_kwargs.enable_thinking` as a top-level
  // request-body field (confirmed via direct curl). The OpenAI JS SDK's types
  // don't know this key, so we cast to forward it through verbatim — the SDK
  // serializes the whole object as JSON. This is the same field Unsloth Studio
  // / llama.cpp gate the <think> reasoning blocks on.
  const stream = await client.chat.completions.create({
    model: MODEL_ID,
    messages: [
      { role: "system", content: systemPrompt },
      { role: "user", content: userInput },
    ],
    max_tokens: 2048,
    temperature: 0.3,
    stream: true,
    chat_template_kwargs: { enable_thinking: true },
  } as Parameters<typeof client.chat.completions.create>[0]);

  let output = "";
  let reasoning = "";
  // Fallback accumulator for servers that inline <think>…</think> into content.
  let rawContent = "";
  let inThinkBlock = false;

  // `stream: true` is set above, so the SDK returns an async-iterable Stream;
  // the cast only appeases TS (the union return type from the param cast).
  for await (const chunk of stream as AsyncIterable<{
    choices?: { delta?: { reasoning_content?: string; content?: string } }[];
  }>) {
    const delta = chunk.choices?.[0]?.delta;
    if (!delta) continue;

    // Primary path: explicit reasoning_content field (llama-server / Qwen3).
    if (delta.reasoning_content) {
      reasoning += delta.reasoning_content;
      onDelta({ kind: "reasoning", chunk: delta.reasoning_content });
      continue;
    }

    // Answer content. Strip any inline <think> blocks some servers emit.
    if (delta.content) {
      rawContent += delta.content;
      let piece = delta.content;
      // Handle <think>…</think> inlined into content deltas.
      if (piece.includes("<think>")) {
        inThinkBlock = true;
        piece = piece.replace(/<think>/g, "");
      }
      if (inThinkBlock) {
        if (piece.includes("</think>")) {
          inThinkBlock = false;
          const [thinkPart, ...rest] = piece.split("</think>");
          reasoning += thinkPart;
          onDelta({ kind: "reasoning", chunk: thinkPart });
          piece = rest.join("</think>");
        } else {
          reasoning += piece;
          onDelta({ kind: "reasoning", chunk: piece });
          continue;
        }
      }
      if (piece) {
        output += piece;
        onDelta({ kind: "content", chunk: piece });
      }
    }
  }

  // If the server inlined thinking into content but never closed the tag,
  // treat everything accumulated as reasoning and clear the output.
  if (inThinkBlock && !output) {
    reasoning = rawContent.replace(/<think>/g, "");
    output = "";
  }

  return { output, reasoning, latencyMs: Date.now() - start };
}

/**
 * Best-effort mirror of a span into Rewind's OTLP receiver as a gen_ai.llm
 * span. Fire-and-forget: we don't await it and we swallow errors so a missing
 * receiver never breaks the demo UI. When the receiver IS up, the same spans
 * that drive this UI also land in ~/.rewind/rewind.db and are queryable by
 * the Python engine / `rewind ui` timeline.
 *
 * We send OTLP/HTTP JSON (the format Rewind's receiver accepts on /v1/traces).
 */
function mirrorToRewind(span: Span): void {
  // Defer to the next tick so it never blocks the response.
  setImmediate(async () => {
    try {
      const traceId = uuid().replace(/-/g, "").padEnd(32, "0").slice(0, 32);
      const spanId = uuid().replace(/-/g, "").slice(0, 16);
      const now = Date.now();
      const startTime = `${now}000000`;
      const endTime = `${now + span.latencyMs}000000`;
      const body = {
        resourceSpans: [
          {
            resource: { attributes: [{ key: "service.name", value: { stringValue: "rewind-web-demo" } }] },
            scopeSpans: [
              {
                scope: { name: "rewind-demo" },
                spans: [
                  {
                    traceId,
                    spanId,
                    name: "ChatCompletion",
                    kind: "SPAN_KIND_INTERNAL",
                    startTimeUnixNano: startTime,
                    endTimeUnixNano: endTime,
                    attributes: [
                      { key: "gen_ai.system", value: { stringValue: "unsloth" } },
                      { key: "gen_ai.request.model", value: { stringValue: span.model } },
                      { key: "gen_ai.response.model", value: { stringValue: span.model } },
                      {
                        key: "gen_ai.response",
                        value: {
                          stringValue: JSON.stringify({
                            model: span.model,
                            choices: [
                              {
                                message: {
                                  role: "assistant",
                                  content: span.output,
                                  reasoning_content: span.reasoning ?? "",
                                },
                              },
                            ],
                            usage: {
                              prompt_tokens: span.tokensIn,
                              completion_tokens: span.tokensOut,
                              total_tokens: span.tokensIn + span.tokensOut,
                            },
                          }),
                        },
                      },
                    ],
                  },
                ],
              },
            ],
          },
        ],
      };
      await fetch(`${REWIND_OTLP_ENDPOINT}/v1/traces`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch {
      /* best-effort: receiver down is fine */
    }
  });
}

function makeSpan(args: {
  index: number;
  prompt: SpanPrompt;
  systemPrompt: string;
  userInput: string;
  output: string;
  reasoning?: string;
  latencyMs: number;
  source: "live" | "cached";
}): Span {
  const span: Span = {
    id: uuid(),
    index: args.index,
    name: args.prompt.name,
    kind: args.prompt.kind as SpanKind,
    type: "llm",
    model: MODEL_ID,
    systemPrompt: args.systemPrompt,
    userInput: args.userInput,
    output: args.output,
    reasoning: args.reasoning ?? "",
    latencyMs: args.latencyMs,
    tokensIn: estimateTokens(args.systemPrompt + args.userInput),
    tokensOut: estimateTokens(args.output + (args.reasoning ?? "")),
    source: args.source,
  };
  if (args.source === "live") {
    mirrorToRewind(span);
  }
  return span;
}

/**
 * StreamEvent emitter — the run/branch route turns these into NDJSON lines.
 * Kept as a plain function type (not imported from types.ts) so the agent
 * stays server-only and doesn't pull client type baggage.
 */
export type Emit = (e: StreamEvent) => void;

/**
 * Capture a fresh trace with LIVE streaming. Every span is a real LLM call,
 * streamed token-by-token through `emit`. Returns the complete Trace at the
 * end (branchId === "main", parentBranchId === null).
 *
 * The emit sequence per span is:
 *   span_start → reasoning_delta* → content_delta* → span_end
 * followed by a single trace_end. The caller (route handler) flushes each
 * event to the client immediately.
 */
export async function runTraceStream(
  query: string,
  emit: Emit,
  prompts: SpanPrompt[] = DEFAULT_PROMPTS,
): Promise<Trace> {
  const outputsByIndex: Record<number, string> = {};
  const spans: Span[] = [];

  for (let i = 0; i < prompts.length; i++) {
    const p = prompts[i];
    const userInput = fillTemplate(p.userInputTemplate, query, outputsByIndex);
    emit({ type: "span_start", index: i, name: p.name, kind: p.kind as SpanKind });
    const { output, reasoning, latencyMs } = await callLlmStream(
      p.systemPrompt,
      userInput,
      (d) =>
        emit(
          d.kind === "reasoning"
            ? { type: "reasoning_delta", index: i, chunk: d.chunk }
            : { type: "content_delta", index: i, chunk: d.chunk },
        ),
    );
    outputsByIndex[i] = output;
    const span = makeSpan({
      index: i,
      prompt: p,
      systemPrompt: p.systemPrompt,
      userInput,
      output,
      reasoning,
      latencyMs,
      source: "live",
    });
    spans.push(span);
    emit({ type: "span_end", index: i, span });
  }

  const trace: Trace = {
    id: uuid(),
    branchId: "main",
    parentBranchId: null,
    branchAtSpanIndex: null,
    query,
    label: "Original run",
    note: "",
    spans,
    createdAt: Date.now(),
  };
  emit({ type: "trace_end", trace });
  return trace;
}

/**
 * Branch from a parent trace, streaming the live tail:
 *   - spans [0, branchAtSpanIndex) are copied verbatim from the parent and
 *     marked source === "cached" (FROZEN replay — no LLM call, no tokens, no cost).
 *     These emit a span_end each (no deltas) so the ThinkingPanel still shows them.
 *   - span at branchAtSpanIndex runs LIVE with the user's edited system prompt.
 *   - spans (branchAtSpanIndex, end) run live with the original prompts, but
 *     their userInput is rebuilt from the NEW (branched) prior outputs, so the
 *     edited prompt's effect propagates forward through the rest of the flow.
 *
 * This is the headline Rewind value: only the divergent tail pays for LLM calls.
 */
export async function runBranchStream(
  args: {
    parent: Trace;
    branchAtSpanIndex: number;
    editedSystemPrompt: string;
    label?: string;
    note?: string;
    prompts?: SpanPrompt[];
  },
  emit: Emit,
): Promise<Trace> {
  const {
    parent,
    branchAtSpanIndex,
    editedSystemPrompt,
    label = "",
    note = "",
    prompts = DEFAULT_PROMPTS,
  } = args;

  const outputsByIndex: Record<number, string> = {};
  const spans: Span[] = [];

  // Phase 1 — FROZEN replay of the cached prefix (instant, no deltas).
  for (let i = 0; i < branchAtSpanIndex; i++) {
    const parentSpan = parent.spans[i];
    outputsByIndex[i] = parentSpan.output;
    const cached: Span = {
      ...parentSpan,
      id: uuid(),
      source: "cached",
      latencyMs: 0,
      tokensIn: 0,
      tokensOut: 0,
    };
    spans.push(cached);
    emit({ type: "span_start", index: i, name: parentSpan.name, kind: parentSpan.kind });
    emit({ type: "span_end", index: i, span: cached });
  }

  // Phase 2 — live divergent tail (streamed).
  for (let i = branchAtSpanIndex; i < prompts.length; i++) {
    const p = prompts[i];
    const systemPrompt = i === branchAtSpanIndex ? editedSystemPrompt : p.systemPrompt;
    const userInput = fillTemplate(p.userInputTemplate, parent.query, outputsByIndex);
    emit({ type: "span_start", index: i, name: p.name, kind: p.kind as SpanKind });
    const { output, reasoning, latencyMs } = await callLlmStream(
      systemPrompt,
      userInput,
      (d) =>
        emit(
          d.kind === "reasoning"
            ? { type: "reasoning_delta", index: i, chunk: d.chunk }
            : { type: "content_delta", index: i, chunk: d.chunk },
        ),
    );
    outputsByIndex[i] = output;
    const span = makeSpan({
      index: i,
      prompt: p,
      systemPrompt,
      userInput,
      output,
      reasoning,
      latencyMs,
      source: "live",
    });
    spans.push(span);
    emit({ type: "span_end", index: i, span });
  }

  const branchId = uuid().slice(0, 8);
  const trace: Trace = {
    id: uuid(),
    branchId,
    parentBranchId: parent.branchId,
    branchAtSpanIndex,
    query: parent.query,
    label: label || `Branch @ #${branchAtSpanIndex + 1}`,
    note,
    spans,
    createdAt: Date.now(),
  };
  emit({ type: "trace_end", trace });
  return trace;
}
