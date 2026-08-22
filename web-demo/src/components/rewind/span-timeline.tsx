"use client";

import { cn } from "@/lib/utils";
import { useRewindStore } from "@/lib/rewind/store";
import type { Span, Trace } from "@/lib/rewind/types";
import { spanCost } from "@/lib/rewind/diff";
import {
  ChevronDown,
  ChevronUp,
  CircleDot,
  Database,
  GitBranch,
  Zap,
} from "lucide-react";

const KIND_META: Record<
  Span["kind"],
  { label: string; tint: string; ring: string }
> = {
  clarify_with_user: {
    label: "Clarify",
    tint: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
    ring: "ring-amber-300",
  },
  write_research_brief: {
    label: "Brief",
    tint: "bg-violet-100 text-violet-900 dark:bg-violet-900/40 dark:text-violet-200",
    ring: "ring-violet-300",
  },
  supervisor_think: {
    label: "Supervisor",
    tint: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
    ring: "ring-emerald-300",
  },
  conduct_research: {
    label: "Researcher",
    tint: "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200",
    ring: "ring-sky-300",
  },
  research_complete: {
    label: "Complete",
    tint: "bg-rose-100 text-rose-900 dark:bg-rose-900/40 dark:text-rose-200",
    ring: "ring-rose-300",
  },
  final_report: {
    label: "Final report",
    tint:
      "bg-fuchsia-100 text-fuchsia-900 dark:bg-fuchsia-900/40 dark:text-fuchsia-200",
    ring: "ring-fuchsia-300",
  },
};

function BranchHeader({
  trace,
  isRoot,
  isSelected,
  onClick,
}: {
  trace: Trace;
  isRoot: boolean;
  isSelected: boolean;
  onClick: () => void;
}) {
  const cost = spanCost(trace.spans);
  return (
    <button
      onClick={onClick}
      className={cn(
        "group flex w-full items-center gap-2 rounded-md border px-2 py-1.5 text-left transition-colors",
        isSelected
          ? "border-primary bg-primary/5"
          : "border-transparent hover:bg-muted/60",
      )}
    >
      {isRoot ? (
        <CircleDot className="size-3.5 shrink-0 text-primary" />
      ) : (
        <GitBranch className="size-3.5 shrink-0 text-emerald-600" />
      )}
      <span className="flex-1 truncate text-xs font-medium">
        {trace.label || (isRoot ? "Original run" : `Branch ${trace.branchId}`)}
      </span>
      <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
        <Zap className="size-3 text-amber-500" />
        {cost.liveCalls}
        <Database className="ml-1 size-3 text-emerald-500" />
        {cost.cachedCalls}
      </span>
    </button>
  );
}

function SpanRow({
  span,
  isCursor,
  isBranchPoint,
  onClick,
  onStepUp,
  onStepDown,
  canStepUp,
  canStepDown,
}: {
  span: Span;
  isCursor: boolean;
  isBranchPoint: boolean;
  onClick: () => void;
  onStepUp: () => void;
  onStepDown: () => void;
  canStepUp: boolean;
  canStepDown: boolean;
}) {
  const meta = KIND_META[span.kind];
  return (
    <div className="group relative pl-5">
      {/* vertical rail */}
      <span className="absolute left-[7px] top-0 h-full w-px bg-border" />
      {/* node */}
      <span
        className={cn(
          "absolute left-1 top-3 size-3 rounded-full border-2 bg-background",
          isCursor ? "border-primary" : "border-border",
          isBranchPoint && "ring-2 ring-emerald-400 ring-offset-1",
        )}
      />
      <button
        onClick={onClick}
        className={cn(
          "relative ml-3 mb-1 w-[calc(100%-1.5rem)] rounded-md border px-2.5 py-2 text-left transition-all",
          isCursor
            ? "border-primary bg-primary/5 shadow-sm"
            : "border-border bg-card hover:border-foreground/20",
        )}
      >
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-muted-foreground">
            #{span.index + 1}
          </span>
          <span
            className={cn(
              "rounded px-1.5 py-0.5 text-[10px] font-medium",
              meta.tint,
            )}
          >
            {meta.label}
          </span>
          {span.source === "cached" ? (
            <span className="ml-auto flex items-center gap-0.5 text-[10px] text-emerald-600 dark:text-emerald-400">
              <Database className="size-2.5" />
              cached
            </span>
          ) : (
            <span className="ml-auto flex items-center gap-0.5 text-[10px] text-amber-600 dark:text-amber-400">
              <Zap className="size-2.5" />
              live
            </span>
          )}
        </div>
        <div className="mt-1 line-clamp-2 text-[11px] text-muted-foreground">
          {span.output.slice(0, 140) || "(empty)"}
          {span.output.length > 140 ? "…" : ""}
        </div>
        <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground">
          <span>{span.latencyMs > 0 ? `${span.latencyMs} ms` : "0 ms"}</span>
          <span>·</span>
          <span>
            {span.source === "cached"
              ? "0 tok (free)"
              : `${span.tokensIn}+${span.tokensOut} tok`}
          </span>
          {isBranchPoint && (
            <>
              <span>·</span>
              <span className="font-medium text-emerald-600 dark:text-emerald-400">
                ◆ branch point
              </span>
            </>
          )}
        </div>
      </button>
      {/* step controls, only on the cursor row */}
      {isCursor && (
        <div className="absolute -left-1 top-1/2 -translate-y-1/2 flex flex-col gap-0.5">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onStepDown();
            }}
            disabled={!canStepDown}
            className="rounded border border-border bg-card p-0.5 text-muted-foreground shadow-sm transition-colors hover:bg-muted disabled:opacity-30"
            title="Step down through recorded spans"
            aria-label="Step down"
          >
            <ChevronUp className="size-3" />
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              onStepUp();
            }}
            disabled={!canStepUp}
            className="rounded border border-border bg-card p-0.5 text-muted-foreground shadow-sm transition-colors hover:bg-muted disabled:opacity-30"
            title="Step up (forward)"
            aria-label="Step up"
          >
            <ChevronDown className="size-3" />
          </button>
        </div>
      )}
    </div>
  );
}

export function SpanTimeline() {
  const {
    traces,
    rootBranchId,
    selectedBranchId,
    cursor,
    selectBranch,
    setCursor,
    stepUp,
    stepDown,
  } = useRewindStore();

  if (!rootBranchId || !selectedBranchId) {
    return (
      <div className="flex h-full items-center justify-center p-6 text-center text-sm text-muted-foreground">
        Run the demo to capture a trace.
      </div>
    );
  }

  // Show root first, then all branches in creation order.
  const all = Object.values(traces).sort((a, b) => {
    if (a.branchId === "main") return -1;
    if (b.branchId === "main") return 1;
    return a.createdAt - b.createdAt;
  });
  const selected = traces[selectedBranchId];

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto p-3">
      {all.map((trace) => {
        const isRoot = trace.branchId === rootBranchId;
        const isSelected = trace.branchId === selectedBranchId;
        return (
          <div key={trace.branchId} className="rounded-lg border bg-background">
            <div className="px-2 pt-2">
              <BranchHeader
                trace={trace}
                isRoot={isRoot}
                isSelected={isSelected}
                onClick={() => selectBranch(trace.branchId)}
              />
            </div>
            {isSelected && (
              <div className="space-y-0 pb-3 pt-1">
                {trace.spans.map((span) => (
                  <SpanRow
                    key={span.id}
                    span={span}
                    isCursor={span.index === cursor}
                    isBranchPoint={
                      trace.branchAtSpanIndex === span.index
                    }
                    onClick={() => setCursor(span.index)}
                    onStepUp={stepUp}
                    onStepDown={stepDown}
                    canStepUp={cursor < trace.spans.length - 1}
                    canStepDown={cursor > 0}
                  />
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
