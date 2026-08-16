import { expect, test } from "bun:test";

import { parseBranchRequestBody, POST } from "./route";

const branchUrl = "http://127.0.0.1:3000/api/rewind/branch";

function requestFor(body: unknown, headers: Record<string, string> = {}): Request {
  return new Request(branchUrl, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

test("accepts a complete trace and preserves prompt whitespace", () => {
  const editedSystemPrompt = "\n  preserve this developer text exactly  \n";
  const parsed = parseBranchRequestBody({
    parent: {
      id: "trace-1",
      branchId: "main",
      parentBranchId: null,
      branchAtSpanIndex: null,
      query: "a bounded query",
      label: "Original run",
      note: "",
      createdAt: 1_700_000_000_000,
      spans: [
        {
          id: "span-1",
          index: 0,
          name: "clarify_with_user",
          kind: "clarify_with_user",
          type: "llm",
          model: "local-model",
          systemPrompt: "original system prompt",
          userInput: "original input",
          output: "PROCEED",
          latencyMs: 12,
          tokensIn: 4,
          tokensOut: 2,
          source: "live",
        },
      ],
    },
    branchAtSpanIndex: 0,
    editedSystemPrompt,
  });

  expect(parsed).not.toBeNull();
  expect(parsed?.editedSystemPrompt).toBe(editedSystemPrompt);
});

test("rejects non-loopback and cross-origin requests", async () => {
  const nonLoopback = await POST(
    new Request("https://example.test/api/rewind/branch", { method: "POST" }),
  );
  expect(nonLoopback.status).toBe(403);

  const crossOrigin = await POST(
    requestFor({}, { origin: "http://localhost:3000" }),
  );
  expect(crossOrigin.status).toBe(403);
});

test("rejects an oversized request body before JSON parsing", async () => {
  const oversized = "x".repeat(1_048_577);
  const response = await POST(
    new Request(branchUrl, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: oversized,
    }),
  );

  expect(response.status).toBe(413);
});

test("rejects a malformed parent trace", async () => {
  const response = await POST(
    requestFor({
      parent: {},
      branchAtSpanIndex: 0,
      editedSystemPrompt: "debug prompt",
    }),
  );

  expect(response.status).toBe(400);
});
