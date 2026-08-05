"use client";

/**
 * SessionView — the interactive stepping layout.
 *
 * A resizable two-panel layout mirroring page.tsx's inspect view:
 *   - Left (28%): a step-history rail — one node + card per consumed step,
 *     with the currently-paused step highlighted.
 *   - Right (72%): the StepPanel when paused, or a status state (waiting /
 *     done / errored) otherwise.
 *
 * A progress header strip across the top shows status icon + step count +
 * live elapsed timer + a Progress bar — adapted from thinking-panel.tsx.
 */

import { useEffect, useState } from "react";
import {
  ResizablePanel,
  ResizableHandle,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Loader2,
  PauseCircle,
  Sparkles,
  AlertCircle,
  Circle,
  CheckCircle2,
} from "lucide-react";
import { useRewindStore } from "@/lib/rewind/store";
import { StepPanel } from "./step-panel";

function fmtElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function kindTint(kind: string): string {
  switch (kind) {
    case "llm": return "bg-sky-500";
    case "tool": return "bg-emerald-500";
    case "mcp": return "bg-violet-500";
    default: return "bg-muted-foreground";
  }
}

function decisionBadge(decision: string): { label: string; className: string } {
  switch (decision) {
    case "approve": return { label: "approved", className: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300" };
    case "edit": return { label: "edited", className: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300" };
    case "stop": return { label: "stopped", className: "bg-rose-100 text-rose-700 dark:bg-rose-950 dark:text-rose-300" };
    case "step_once": return { label: "stepped", className: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300" };
    default: return { label: decision, className: "bg-muted text-muted-foreground" };
  }
}

export function SessionView() {
  const { liveSession } = useRewindStore();
  // 500ms tick for the live elapsed timer.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 500);
    return () => clearInterval(id);
  }, []);

  if (!liveSession) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        No active session. Start one from the top bar.
      </div>
    );
  }

  const elapsed = Date.now() - liveSession.startedAt;
  const stepCount = liveSession.history.length + (liveSession.pausedStep ? 1 : 0);
  const isPaused = liveSession.status === "paused";
  const isDone = liveSession.status === "done";
  const isErrored = liveSession.status === "errored";

  return (
    <ResizablePanelGroup
      direction="horizontal"
      className="h-full rounded-lg border bg-background"
    >
      {/* Left: step-history rail */}
      <ResizablePanel defaultSize={28} minSize={20}>
        <div className="flex h-full flex-col overflow-hidden">
          <div className="border-b px-3 py-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Step history ({liveSession.history.length})
          </div>
          <ScrollArea className="flex-1">
            <div className="p-3">
              {/* Consumed steps */}
              {liveSession.history.map((h) => {
                const badge = decisionBadge(h.decision);
                return (
                  <div key={h.cursor} className="group relative mb-3 pl-5">
                    {/* rail line */}
                    <span className="absolute left-[7px] top-0 h-full w-px bg-border" />
                    {/* node */}
                    <span className={`absolute left-1 top-1.5 size-2.5 rounded-full ${kindTint(h.kind)}`} />
                    <div className="rounded-md border bg-card p-2 text-xs">
                      <div className="flex items-center gap-1.5">
                        <span className="font-mono text-muted-foreground">#{h.cursor}</span>
                        <Badge variant="secondary" className={`text-[10px] ${badge.className}`}>
                          <CheckCircle2 className="mr-1 size-2.5" /> {badge.label}
                        </Badge>
                      </div>
                      <p className="mt-1 line-clamp-2 text-muted-foreground">
                        {previewEntry(h)}
                      </p>
                    </div>
                  </div>
                );
              })}

              {/* Currently paused step (highlighted) */}
              {liveSession.pausedStep && (
                <div className="group relative mb-3 pl-5">
                  <span className="absolute left-[7px] top-0 h-full w-px bg-border" />
                  <span className="absolute left-[3px] top-1 size-3.5 animate-pulse rounded-full border-2 border-amber-500 bg-amber-500/30" />
                  <div className="rounded-md border-2 border-amber-400/60 bg-amber-50 p-2 text-xs dark:bg-amber-950/30">
                    <div className="flex items-center gap-1.5 font-medium">
                      <PauseCircle className="size-3.5 text-amber-500" />
                      <span className="font-mono">#{liveSession.pausedStep.cursor}</span>
                      <span className="text-muted-foreground">paused</span>
                    </div>
                    <p className="mt-1 line-clamp-2 text-muted-foreground">
                      {previewEntry(liveSession.pausedStep)}
                    </p>
                  </div>
                </div>
              )}

              {liveSession.history.length === 0 && !liveSession.pausedStep && (
                <p className="px-1 py-4 text-xs text-muted-foreground">
                  Waiting for the first step…
                </p>
              )}
            </div>
          </ScrollArea>
        </div>
      </ResizablePanel>

      <ResizableHandle withHandle />

      {/* Right: progress header + StepPanel / status */}
      <ResizablePanel defaultSize={72} minSize={40}>
        <div className="flex h-full flex-col overflow-hidden">
          {/* Progress header */}
          <div className="flex items-center gap-2 border-b bg-background/80 px-4 py-2 backdrop-blur">
            {liveSession.status === "running" && <Loader2 className="size-4 animate-spin text-violet-500" />}
            {isPaused && <PauseCircle className="size-4 text-amber-500" />}
            {isDone && <Sparkles className="size-4 text-emerald-500" />}
            {isErrored && <AlertCircle className="size-4 text-destructive" />}
            <span className="text-sm font-medium">
              {isDone ? "Session complete" : isErrored ? "Session errored" : isPaused ? "Paused" : "Running"}
            </span>
            <span className="text-xs text-muted-foreground">
              · step {stepCount}{liveSession.pausedStep ? ` (at #${liveSession.pausedStep.cursor})` : ""}
            </span>
            <span className="ml-auto font-mono text-xs text-muted-foreground">
              {fmtElapsed(elapsed)}
            </span>
            <Progress
              value={isDone ? 100 : (liveSession.history.length / Math.max(stepCount, 1)) * 100}
              className="ml-2 h-1.5 w-24"
            />
          </div>

          {/* Body */}
          <div className="flex-1 overflow-hidden">
            {liveSession.pausedStep ? (
              <StepPanel sessionId={liveSession.sessionId} step={liveSession.pausedStep} />
            ) : isDone ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <Sparkles className="size-8 text-emerald-500" />
                <p className="text-sm">
                  Session complete — {liveSession.history.length} step{liveSession.history.length === 1 ? "" : "s"}.
                </p>
                <p className="text-xs text-muted-foreground">
                  Start another session from the top bar.
                </p>
              </div>
            ) : isErrored ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
                <AlertCircle className="size-8 text-destructive" />
                <p className="text-sm font-medium text-destructive">Session errored</p>
                <pre className="max-w-md overflow-x-auto rounded-md bg-muted/40 p-3 text-left font-mono text-xs">
                  {liveSession.error}
                </pre>
              </div>
            ) : (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <Loader2 className="size-8 animate-spin text-violet-500" />
                <p className="text-sm text-muted-foreground">
                  Running — waiting for the agent to reach a step…
                </p>
                <p className="text-xs text-muted-foreground">
                  runner: <code className="font-mono">{liveSession.runnerRef}</code>
                </p>
              </div>
            )}
          </div>
        </div>
      </ResizablePanel>
    </ResizablePanelGroup>
  );
}

/** One-line preview of a step's payload or result for the rail cards. */
function previewEntry(entry: { payload?: unknown; result?: string | null }): string {
  // Prefer the model's response (the verify-loop result) over the input payload.
  if (entry.result && entry.result.trim()) {
    return entry.result.slice(0, 120);
  }
  const payload = entry.payload;
  if (!payload || typeof payload !== "object") return "";
  const p = payload as { model?: string; messages?: unknown[]; name?: string };
  if (p.name) return `tool: ${p.name}`;
  if (p.messages && Array.isArray(p.messages) && p.messages.length > 0) {
    const last = p.messages[p.messages.length - 1] as { content?: unknown };
    const c = typeof last?.content === "string" ? last.content : "";
    return c.slice(0, 120) || `${p.messages.length} message(s)`;
  }
  return p.model ?? "";
}
