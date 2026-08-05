"use client";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useRewindStore } from "@/lib/rewind/store";
import type { DiffToken, SpanDiffPair } from "@/lib/rewind/types";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

function DiffRender({ tokens }: { tokens: DiffToken[] }) {
  return (
    <div className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed">
      {tokens.length === 0 ? (
        <span className="text-muted-foreground">(no content)</span>
      ) : (
        tokens.map((t, i) => {
          if (t.type === "equal")
            return <span key={i}>{t.value}</span>;
          if (t.type === "add")
            return (
              <span
                key={i}
                className="bg-emerald-200/70 text-emerald-950 dark:bg-emerald-500/30 dark:text-emerald-100"
              >
                {t.value}
              </span>
            );
          return (
            <span
              key={i}
              className="bg-rose-200/70 text-rose-950 line-through dark:bg-rose-500/30 dark:text-rose-100"
            >
              {t.value}
            </span>
          );
        })
      )}
    </div>
  );
}

function PairRow({ pair }: { pair: SpanDiffPair }) {
  return (
    <Card
      className={cn(
        "overflow-hidden",
        pair.diverged && "border-fuchsia-300 dark:border-fuchsia-700",
      )}
    >
      <CardHeader className="pb-2">
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="font-mono">
            #{pair.index + 1}
          </Badge>
          <CardTitle className="text-sm">{pair.name}</CardTitle>
          {pair.diverged ? (
            <Badge className="bg-fuchsia-100 text-fuchsia-900 hover:bg-fuchsia-100 dark:bg-fuchsia-900/40 dark:text-fuchsia-200">
              diverged
            </Badge>
          ) : (
            <Badge className="bg-emerald-100 text-emerald-900 hover:bg-emerald-100 dark:bg-emerald-900/40 dark:text-emerald-200">
              identical
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {pair.diverged ? (
          <div className="space-y-3">
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                Output diff (left = original · right = new branch)
              </div>
              <div className="rounded-md border bg-background p-3">
                <DiffRender tokens={pair.outputDiff} />
              </div>
            </div>
            {pair.systemPromptDiff.some((t) => t.type !== "equal") && (
              <div>
                <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  System prompt diff
                </div>
                <div className="rounded-md border bg-muted/30 p-3">
                  <DiffRender tokens={pair.systemPromptDiff} />
                </div>
              </div>
            )}
          </div>
        ) : (
          <div className="rounded-md border bg-muted/20 p-3 text-xs text-muted-foreground">
            Both branches produced identical output for this span.
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export function DiffView() {
  const {
    diff,
    diffLeftBranchId,
    diffRightBranchId,
    traces,
    setDiff,
  } = useRewindStore();

  if (!diff || !diffLeftBranchId || !diffRightBranchId) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground">
        Capture a branch to see the side-by-side diff against the original.
      </div>
    );
  }

  const left = traces[diffLeftBranchId];
  const right = traces[diffRightBranchId];
  if (!left || !right) return null;

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b p-4">
        <div className="min-w-0">
          <div className="text-base font-semibold">Branch diff</div>
          <div className="mt-1 text-xs text-muted-foreground">
            <span className="font-medium text-foreground">{left.label}</span>
            {" → "}
            <span className="font-medium text-fuchsia-700 dark:text-fuchsia-300">
              {right.label}
            </span>
            {diff.firstDivergenceIndex !== null && (
              <>
                {" · first divergence at "}
                <span className="font-mono">
                  #{diff.firstDivergenceIndex + 1}
                </span>
              </>
            )}
          </div>
        </div>
        <Button size="sm" variant="ghost" onClick={() => setDiff(null, null)}>
          <X className="size-4" /> Close
        </Button>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {diff.pairs.map((p) => (
          <PairRow key={p.index} pair={p} />
        ))}
        {diff.firstDivergenceIndex === null && (
          <div className="rounded-md border bg-muted/20 p-3 text-sm text-muted-foreground">
            No divergence — the edited prompt did not change any span output.
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Phase 3.2 — token-level message diff panel.
 *
 * Uses the existing ``GET /api/v1/spans/{id}/message-diff`` API to render a
 * word/token-aligned diff between two assistant messages. Unlike the branch
 * ``DiffView`` (which compares span *sequences*), this compares the *content*
 * of two specific LLM responses.
 */
export function MessageDiffPanel({
  fragments,
  left,
  right,
}: {
  fragments: { text: string; kind: string }[];
  left: string;
  right: string;
}) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Message diff (token-level)</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="mb-2 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
          <span className="text-rose-600 dark:text-rose-400">removed</span>
          {" → "}
          <span className="text-emerald-600 dark:text-emerald-400">added</span>
        </div>
        <div className="rounded-md border bg-background p-3">
          <div className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed">
            {fragments.length === 0 ? (
              <span className="text-muted-foreground">(no diff)</span>
            ) : (
              fragments.map((f, i) => {
                if (f.kind === "equal") return <span key={i}>{f.text}</span>;
                if (f.kind === "add")
                  return (
                    <span
                      key={i}
                      className="bg-emerald-200/70 text-emerald-950 dark:bg-emerald-500/30 dark:text-emerald-100"
                    >
                      {f.text}
                    </span>
                  );
                return (
                  <span
                    key={i}
                    className="bg-rose-200/70 text-rose-950 line-through dark:bg-rose-500/30 dark:text-rose-100"
                  >
                    {f.text}
                  </span>
                );
              })
            )}
          </div>
        </div>
        <div className="mt-2 flex gap-4 text-[10px] text-muted-foreground">
          <span>left: {left.length} chars</span>
          <span>right: {right.length} chars</span>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * Phase 3.5 — multi-variant comparison matrix.
 *
 * Renders N prompt variants in a row so the developer can scan outputs,
 * token counts, and pass/fail side by side. Capped at 10 columns (per the
 * plan's open question Q2) to avoid extremely wide tables.
 */
const MAX_MATRIX_VARIANTS = 10;

export function VariantMatrix({
  variants,
}: {
  variants: {
    id: string;
    model?: string;
    result?: string | null;
    totalTokens?: number;
    passed?: boolean;
  }[];
}) {
  const capped = variants.slice(0, MAX_MATRIX_VARIANTS);
  if (capped.length === 0) {
    return (
      <div className="rounded-md border bg-muted/20 p-3 text-sm text-muted-foreground">
        No variants to compare yet.
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="border-b">
            <th className="p-2 text-left text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              Metric
            </th>
            {capped.map((v, i) => (
              <th key={v.id} className="p-2 text-left font-mono text-[10px]">
                V{i + 1}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr className="border-b">
            <td className="p-2 text-muted-foreground">Model</td>
            {capped.map((v) => (
              <td key={v.id} className="p-2 font-mono">
                {v.model || "—"}
              </td>
            ))}
          </tr>
          <tr className="border-b">
            <td className="p-2 text-muted-foreground">Tokens</td>
            {capped.map((v) => (
              <td key={v.id} className="p-2 font-mono">
                {v.totalTokens ?? "—"}
              </td>
            ))}
          </tr>
          <tr className="border-b">
            <td className="p-2 text-muted-foreground">Checks</td>
            {capped.map((v) => (
              <td key={v.id} className="p-2">
                {v.passed === undefined ? (
                  "—"
                ) : v.passed ? (
                  <Badge className="bg-emerald-100 text-emerald-900 hover:bg-emerald-100 dark:bg-emerald-900/40 dark:text-emerald-200">
                    pass
                  </Badge>
                ) : (
                  <Badge className="bg-rose-100 text-rose-900 hover:bg-rose-100 dark:bg-rose-900/40 dark:text-rose-200">
                    fail
                  </Badge>
                )}
              </td>
            ))}
          </tr>
          <tr>
            <td className="p-2 text-muted-foreground">Output</td>
            {capped.map((v) => (
              <td key={v.id} className="max-w-xs p-2 align-top">
                <div className="max-h-24 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed">
                  {v.result || "(waiting…)"}
                </div>
              </td>
            ))}
          </tr>
        </tbody>
      </table>
      {variants.length > MAX_MATRIX_VARIANTS && (
        <div className="mt-2 text-[10px] text-muted-foreground">
          Showing {MAX_MATRIX_VARIANTS} of {variants.length} variants.
        </div>
      )}
    </div>
  );
}
