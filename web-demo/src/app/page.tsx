"use client";

import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { useRewindStore } from "@/lib/rewind/store";
import { spanCost } from "@/lib/rewind/diff";
import { streamRewind } from "@/lib/rewind/stream-client";
import { SpanTimeline } from "@/components/rewind/span-timeline";
import { SpanDetail } from "@/components/rewind/span-detail";
import { DiffView } from "@/components/rewind/diff-view";
import { ThinkingPanel } from "@/components/rewind/thinking-panel";
import { DEFAULT_QUERY, PROMPT_SUGGESTIONS } from "@/lib/deep-research/prompts";
import {
  ChevronDown,
  ChevronUp,
  GitCompare,
  Loader2,
  Play,
  RotateCcw,
  Sparkles,
  Zap,
  Database,
} from "lucide-react";
import { cn } from "@/lib/utils";

function TopBar() {
  const {
    traces,
    rootBranchId,
    selectedBranchId,
    cursor,
    isRunning,
    runError,
    lastEvent,
    reset,
    setLastEvent,
    stepUp,
    stepDown,
    setDiff,
    mode,
    exitBranchMode,
  } = useRewindStore();

  const [query, setQuery] = useState(DEFAULT_QUERY);

  const selected = selectedBranchId ? traces[selectedBranchId] : null;
  const canStep = !!selected && mode === "inspect";

  const runDemo = useCallback(async () => {
    setLastEvent("Researching — streaming the model's thinking live…");
    await streamRewind("/api/rewind/run", { query }, "run");
  }, [query, setLastEvent]);

  const compareWithOriginal = useCallback(() => {
    if (!rootBranchId || !selectedBranchId || selectedBranchId === rootBranchId)
      return;
    setDiff(rootBranchId, selectedBranchId);
  }, [rootBranchId, selectedBranchId, setDiff]);

  const totalCost = selected ? spanCost(selected.spans) : null;

  return (
    <header className="border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex flex-col gap-3 px-4 py-3">
        {/* row 1 — branding + actions */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <div className="flex size-8 items-center justify-center rounded-lg bg-primary/10 ring-1 ring-primary/20">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src="/rewind.svg" alt="Rewind" className="size-5" />
            </div>
            <div>
              <div className="text-sm font-semibold leading-tight">
                Rewind × Deep Research
              </div>
              <div className="text-[10px] text-muted-foreground leading-tight">
                Time-travel debugging for LangChain deep research · step down → fix
                prompt → step up
              </div>
            </div>
          </div>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="flex items-center gap-1.5">
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Research query…"
                className="h-8 w-72 text-xs"
                disabled={isRunning}
              />
              <Button
                size="sm"
                onClick={runDemo}
                disabled={isRunning || !query.trim()}
              >
                {isRunning && !selectedBranchId ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Play className="size-4" />
                )}
                {selectedBranchId ? "Re-run" : "Run demo"}
              </Button>
            </div>

            {selectedBranchId && selectedBranchId !== rootBranchId && (
              <Button
                size="sm"
                variant="outline"
                onClick={compareWithOriginal}
                title="Diff this branch against the original run"
              >
                <GitCompare className="size-4" /> Compare
              </Button>
            )}

            {selectedBranchId && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  if (
                    confirm("Reset the session? All traces will be discarded.")
                  ) {
                    reset();
                  }
                }}
              >
                <RotateCcw className="size-4" /> Reset
              </Button>
            )}
          </div>
        </div>

        {/* row 2 — step controls + cost + status */}
        {selectedBranchId && (
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-1">
              <Button
                size="sm"
                variant="outline"
                onClick={stepDown}
                disabled={!canStep || cursor <= 0}
                title="Step down (rewind through recorded spans)"
              >
                <ChevronUp className="size-4" /> Step down
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={stepUp}
                disabled={
                  !canStep || cursor >= (selected?.spans.length ?? 0) - 1
                }
                title="Step up (forward through recorded spans)"
              >
                <ChevronDown className="size-4" /> Step up
              </Button>
              <span className="ml-1 text-xs text-muted-foreground">
                span{" "}
                <span className="font-mono text-foreground">
                  #{cursor + 1}
                </span>{" "}
                / {selected?.spans.length}
              </span>
            </div>

            {totalCost && (
              <div className="flex items-center gap-2 text-xs">
                <Badge className="bg-amber-100 text-amber-900 hover:bg-amber-100 dark:bg-amber-900/40 dark:text-amber-200">
                  <Zap className="mr-1 size-3" /> {totalCost.liveCalls} live ·{" "}
                  {totalCost.totalLatencyMs} ms
                </Badge>
                <Badge className="bg-emerald-100 text-emerald-900 hover:bg-emerald-100 dark:bg-emerald-900/40 dark:text-emerald-200">
                  <Database className="mr-1 size-3" /> {totalCost.cachedCalls}{" "}
                  cached · 0 tok
                </Badge>
              </div>
            )}

            {mode === "branch" && (
              <Badge className="bg-fuchsia-100 text-fuchsia-900 hover:bg-fuchsia-100 dark:bg-fuchsia-900/40 dark:text-fuchsia-200">
                <Sparkles className="mr-1 size-3" /> branch mode — editing prompt
                <button
                  className="ml-1 underline"
                  onClick={exitBranchMode}
                  aria-label="exit branch mode"
                >
                  exit
                </button>
              </Badge>
            )}

            {lastEvent && (
              <div className="ml-auto max-w-md truncate text-xs text-muted-foreground">
                {lastEvent}
              </div>
            )}
          </div>
        )}

        {runError && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
            {runError}
          </div>
        )}
      </div>
    </header>
  );
}

function EmptyState() {
  const { isRunning, runError, setLastEvent } = useRewindStore();
  const [query, setQuery] = useState(DEFAULT_QUERY);

  const runDemo = useCallback(async () => {
    setLastEvent("Researching — streaming the model's thinking live…");
    await streamRewind("/api/rewind/run", { query }, "run");
  }, [query, setLastEvent]);

  const error = runError;

  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="mx-auto max-w-3xl space-y-6">
        <div className="text-center">
          <h1 className="text-3xl font-semibold tracking-tight">
            Rewind × Deep Research
          </h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Time-travel debugging for a LangChain deep-research agent. Capture a
            run, step down through the recorded spans to find where it went
            wrong, edit the prompt, and step up — only the divergent tail calls
            the live model.
          </p>
        </div>

        <Card>
          <CardContent className="space-y-3">
            <label className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Research query
            </label>
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="e.g. Compare RLHF vs DPO for aligning LLMs, with citations."
              disabled={isRunning}
            />
            <Button
              onClick={runDemo}
              disabled={isRunning || !query.trim()}
              className="w-full"
            >
              {isRunning ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Play className="size-4" />
              )}
              Run deep research · capture 8 spans
            </Button>
            {error && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
                {error}
              </div>
            )}
          </CardContent>
        </Card>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="rounded-lg border bg-card p-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <span className="flex size-6 items-center justify-center rounded bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
                1
              </span>
              Capture
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Run the 8-span deep research flow (clarify → brief → supervisor →
              research ×2 → complete → final report). Every span is recorded.
            </p>
          </div>
          <div className="rounded-lg border bg-card p-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <span className="flex size-6 items-center justify-center rounded bg-violet-100 text-violet-900 dark:bg-violet-900/40 dark:text-violet-200">
                2
              </span>
              Step down
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Rewind through the recorded spans in FROZEN mode. Find the span
              where the agent took a wrong turn — zero LLM calls while inspecting.
            </p>
          </div>
          <div className="rounded-lg border bg-card p-3">
            <div className="flex items-center gap-2 text-sm font-medium">
              <span className="flex size-6 items-center justify-center rounded bg-fuchsia-100 text-fuchsia-900 dark:bg-fuchsia-900/40 dark:text-fuchsia-200">
                3
              </span>
              Step up
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Edit the prompt and branch forward live. Only the divergent tail
              calls the model — diff the new timeline against the original.
            </p>
          </div>
        </div>

        <Card>
          <CardContent>
            <div className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              <Sparkles className="size-3.5 text-amber-500" />
              Try one of these prompt fixes after capturing
            </div>
            <ul className="space-y-2">
              {PROMPT_SUGGESTIONS.map((s, i) => (
                <li key={i} className="text-xs">
                  <span className="font-medium">#{s.spanIndex + 1} · {s.title}</span>
                  <span className="text-muted-foreground"> — {s.rationale}</span>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

export default function Home() {
  const { selectedBranchId, diff, liveRun } = useRewindStore();

  // While a run is streaming, the live ThinkingPanel is the focus — the trace
  // isn't committed yet so the timeline would be empty. The panel dismisses
  // itself on trace_end (finishLiveRun clears liveRun), restoring the inspect
  // view with the committed trace selected.
  if (liveRun) {
    return (
      <main className="flex min-h-screen flex-col bg-muted/30">
        <TopBar />
        <div className="flex-1 overflow-hidden p-3">
          <Card className="h-[calc(100vh-7rem)] overflow-hidden">
            <CardContent className="h-full p-0">
              <ThinkingPanel />
            </CardContent>
          </Card>
        </div>
      </main>
    );
  }

  if (!selectedBranchId) {
    return (
      <main className="min-h-screen bg-muted/30">
        <EmptyState />
      </main>
    );
  }

  return (
    <main className="flex min-h-screen flex-col bg-muted/30">
      <TopBar />
      <div className="flex-1 overflow-hidden p-3">
        {diff ? (
          <Card className="h-[calc(100vh-7rem)] overflow-hidden">
            <CardContent className="h-full p-0">
              <DiffView />
            </CardContent>
          </Card>
        ) : (
          <ResizablePanelGroup
            direction="horizontal"
            className="h-[calc(100vh-7rem)] rounded-lg border bg-background"
          >
            <ResizablePanel defaultSize={28} minSize={20}>
              <div className="flex h-full flex-col overflow-hidden">
                <div className="border-b px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Trace timeline
                </div>
                <div className="flex-1 overflow-hidden">
                  <SpanTimeline />
                </div>
              </div>
            </ResizablePanel>
            <ResizableHandle withHandle />
            <ResizablePanel defaultSize={72} minSize={40}>
              <SpanDetail />
            </ResizablePanel>
          </ResizablePanelGroup>
        )}
      </div>
    </main>
  );
}
