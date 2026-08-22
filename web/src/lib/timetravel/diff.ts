/**
 * TimeTravel diff engine — mirrors src/timetravel/diff.py from akshay-mp/TimeTravel.
 *
 * Two layers:
 *   1. wordDiff(a, b)  — token/word-level diff between two strings.
 *      Produces a list of DiffTokens ({equal, add, remove}) used to render
 *      side-by-side `<del>...</del><ins>...</ins>` markup.
 *   2. diffBranches(left, right) — span-level diff between two branches.
 *      Walks both span lists in order, runs wordDiff on each span pair, and
 *      marks the first index where the two timelines diverge (the "first
 *      divergence index" — the headline metric TimeTravel reports).
 *
 * The word-diff is a standard LCS-based algorithm. Good enough for prompt /
 * output prose; runs in O(n*m) which is fine for our short LLM outputs.
 */

import type {
  BranchDiff,
  DiffToken,
  Span,
  SpanDiffPair,
  Trace,
} from "./types";

/** Split a string into word tokens while preserving whitespace runs. */
function tokenize(text: string): string[] {
  if (!text) return [];
  // Split on word boundaries but keep the whitespace between words as its own
  // token so re-joining the tokens reconstructs the original string exactly.
  return text.match(/\s+|\S+/g) ?? [text];
}

/** Compute a word-level diff between two strings using LCS. */
export function wordDiff(a: string, b: string): DiffToken[] {
  const at = tokenize(a);
  const bt = tokenize(b);
  const n = at.length;
  const m = bt.length;

  // dp[i][j] = length of LCS of at[i:] and bt[j:]
  const dp: number[][] = Array.from({ length: n + 1 }, () =>
    new Array(m + 1).fill(0),
  );
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] =
        at[i] === bt[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  // Walk forward, emitting equal/add/remove tokens.
  const out: DiffToken[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (at[i] === bt[j]) {
      pushEqual(out, at[i]);
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      pushRemove(out, at[i]);
      i++;
    } else {
      pushAdd(out, bt[j]);
      j++;
    }
  }
  while (i < n) pushRemove(out, at[i++]);
  while (j < m) pushAdd(out, bt[j++]);

  // Coalesce adjacent tokens of the same type so the renderer doesn't emit
  // one <span> per word.
  return coalesce(out);
}

function pushEqual(out: DiffToken[], v: string) {
  out.push({ type: "equal", value: v });
}
function pushAdd(out: DiffToken[], v: string) {
  out.push({ type: "add", value: v });
}
function pushRemove(out: DiffToken[], v: string) {
  out.push({ type: "remove", value: v });
}

function coalesce(tokens: DiffToken[]): DiffToken[] {
  const out: DiffToken[] = [];
  for (const t of tokens) {
    const last = out[out.length - 1];
    if (last && last.type === t.type) {
      last.value += t.value;
    } else {
      out.push({ ...t });
    }
  }
  return out;
}

/** Span-level diff between two branches (traces). */
export function diffBranches(
  left: Trace,
  right: Trace,
): BranchDiff {
  const maxLen = Math.max(left.spans.length, right.spans.length);
  const pairs: SpanDiffPair[] = [];
  let firstDivergenceIndex: number | null = null;

  for (let idx = 0; idx < maxLen; idx++) {
    const l = left.spans[idx] ?? null;
    const r = right.spans[idx] ?? null;
    const kind = (l ?? r)!.kind;
    const name = (l ?? r)!.name;

    // A span is considered diverged if either side is missing, or if the
    // outputs differ. We deliberately do NOT treat latency/tokens as
    // divergence — only the textual content matters (mirrors timetravel.diff).
    const diverged =
      !l || !r || l.output !== r.output || l.systemPrompt !== r.systemPrompt;

    if (diverged && firstDivergenceIndex === null) {
      firstDivergenceIndex = idx;
    }

    pairs.push({
      index: idx,
      kind,
      name,
      left: l,
      right: r,
      diverged,
      outputDiff: l && r ? wordDiff(l.output, r.output) : [],
      systemPromptDiff:
        l && r ? wordDiff(l.systemPrompt, r.systemPrompt) : [],
    });
  }

  return {
    leftBranchId: left.branchId,
    rightBranchId: right.branchId,
    firstDivergenceIndex,
    pairs,
  };
}

/** Aggregate stats for a span — used by the timeline badges. */
export function spanCost(spans: Span[]): {
  liveCalls: number;
  cachedCalls: number;
  totalLatencyMs: number;
  totalTokensOut: number;
} {
  let liveCalls = 0;
  let cachedCalls = 0;
  let totalLatencyMs = 0;
  let totalTokensOut = 0;
  for (const s of spans) {
    if (s.source === "live") {
      liveCalls++;
      totalLatencyMs += s.latencyMs;
      totalTokensOut += s.tokensOut;
    } else {
      cachedCalls++;
    }
  }
  return { liveCalls, cachedCalls, totalLatencyMs, totalTokensOut };
}
