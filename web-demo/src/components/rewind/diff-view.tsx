"use client";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
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
