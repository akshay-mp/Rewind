"use client";

/**
 * Live "Thinking" panel — the centerpiece of the streaming UX.
 *
 * Mirrors the Unsloth Studio thinking display: while a research run streams
 * in, each span is a collapsible section that shows the model's
 * chain-of-thought. The currently-generating span auto-expands and shows
 * reasoning live (muted/italic, distinct from the answer prose); finished
 * spans collapse to a "Thought for Xs" summary. A progress header tracks the
 * overall run.
 *
 * Reads `liveRun` from the store (populated by the streaming run/branch
 * hook in page.tsx). When the run completes, finishLiveRun() commits the
 * Trace and clears liveRun, which unmounts this panel and restores the
 * normal 2-panel inspect view.
 */

import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { useRewindStore } from "@/lib/rewind/store";
import { MarkdownPreview } from "@/lib/rewind/markdown";
import type { LiveSpan, SpanKind } from "@/lib/rewind/types";
import {
  AlertCircle,
  Brain,
  ChevronDown,
  GitBranch,
  Loader2,
  PenLine,
  Play,
  Sparkles,
} from "lucide-react";
import { cn } from "@/lib/utils";

const KIND_LABEL: Record<SpanKind, string> = {
  clarify_with_user: "Clarify",
  write_research_brief: "Research brief",
  supervisor_think: "Supervisor",
  conduct_research: "Researcher",
  research_complete: "Synthesize",
  final_report: "Final report",
};

/** Format an elapsed duration in seconds as "Xs" / "Xm Ys". */
function fmtSec(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

function ThinkingSpanRow({ span, isActive }: { span: LiveSpan; isActive: boolean }) {
  // The active span is always expanded; the user can toggle the others.
  const [userOpen, setUserOpen] = useState(false);
  const open = isActive || userOpen;
  const reasoningRef = useRef<HTMLDivElement>(null);

  // Auto-scroll the reasoning to the bottom as it streams.
  useEffect(() => {
    if (open && isActive && reasoningRef.current) {
      reasoningRef.current.scrollTop = reasoningRef.current.scrollHeight;
    }
  }, [span.reasoning, open, isActive]);

  const elapsed = (span.endedAt ?? Date.now()) - span.startedAt;
  const hasReasoning = span.reasoning.trim().length > 0;
  const hasOutput = span.output.trim().length > 0;

  const statusIcon =
    span.status === "thinking" ? (
      <Loader2 className="h-3.5 w-3.5 animate-spin text-violet-500" />
    ) : span.status === "answering" ? (
      <PenLine className="h-3.5 w-3.5 text-sky-500" />
    ) : (
      <Sparkles className="h-3.5 w-3.5 text-emerald-500" />
    );

  return (
    <Collapsible open={open} onOpenChange={setUserOpen} className="group">
      <div
        className={cn(
          "rounded-lg border bg-card transition-colors",
          isActive && "border-violet-300 ring-1 ring-violet-200 dark:border-violet-700 dark:ring-violet-800/40",
        )}
      >
        <CollapsibleTrigger className="flex w-full items-center gap-2.5 px-3 py-2.5 text-left hover:bg-accent/40 rounded-lg">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-muted text-xs font-semibold text-muted-foreground">
            {span.index + 1}
          </span>
          {statusIcon}
          <span className="text-sm font-medium">{span.name}</span>
          <Badge variant="secondary" className="h-5 px-1.5 text-[10px] font-normal text-muted-foreground">
            {KIND_LABEL[span.kind]}
          </Badge>
          <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
            {/* Collapsed summary: Unsloth-style "Thought for Xs" */}
            {span.status === "done" ? (
              hasReasoning ? (
                <span className="flex items-center gap-1 italic">
                  <Brain className="h-3 w-3" />
                  Thought for {fmtSec(elapsed)}
                </span>
              ) : (
                <span className="italic">No thinking</span>
              )
            ) : span.status === "thinking" ? (
              <span className="flex items-center gap-1 italic text-violet-500">
                <Brain className="h-3 w-3" />
                Thinking…
              </span>
            ) : (
              <span className="flex items-center gap-1 italic text-sky-500">
                <PenLine className="h-3 w-3" />
                Writing…
              </span>
            )}
            <ChevronDown
              className={cn(
                "h-3.5 w-3.5 transition-transform",
                open && "rotate-180",
              )}
            />
          </span>
        </CollapsibleTrigger>

        <CollapsibleContent>
          <AnimatePresence initial={false}>
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.18, ease: "easeOut" }}
              className="overflow-hidden"
            >
              <div className="border-t px-3 py-2.5">
                {/* Reasoning block — muted/italic, visually distinct from answer */}
                {hasReasoning || span.status === "thinking" ? (
                  <div className="mb-3">
                    <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-slate-400">
                      <Brain className="h-3 w-3" />
                      Reasoning
                      {isActive && span.status === "thinking" && (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      )}
                    </div>
                    <div
                      ref={reasoningRef}
                      className="max-h-72 overflow-y-auto rounded-md bg-slate-50 px-3 py-2 dark:bg-slate-900/40"
                    >
                      {hasReasoning ? (
                        <MarkdownPreview text={span.reasoning} variant="thinking" />
                      ) : (
                        <p className="text-[13px] italic text-slate-400">
                          The model is gathering its thoughts…
                        </p>
                      )}
                    </div>
                  </div>
                ) : null}

                {/* Answer block */}
                <div>
                  <div className="mb-1 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    <PenLine className="h-3 w-3" />
                    Answer
                  </div>
                  {hasOutput ? (
                    <MarkdownPreview text={span.output} />
                  ) : (
                    <p className="text-sm italic text-muted-foreground">
                      Waiting for the model to answer…
                    </p>
                  )}
                </div>
              </div>
            </motion.div>
          </AnimatePresence>
        </CollapsibleContent>
      </div>
    </Collapsible>
  );
}

export function ThinkingPanel() {
  const liveRun = useRewindStore((s) => s.liveRun);
  // Tick every 500ms so elapsed timers stay live during long runs.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!liveRun || liveRun.status !== "running") return;
    const id = setInterval(() => setTick((t) => t + 1), 500);
    return () => clearInterval(id);
  }, [liveRun?.status]);

  // Auto-scroll to the newest span as they appear.
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [liveRun?.spans.length]);

  if (!liveRun) return null;

  const total = 8; // DEFAULT_PROMPTS length
  const done = liveRun.spans.filter((s) => s.status === "done").length;
  const progress = liveRun.status === "done" ? 100 : (done / total) * 100;
  const elapsed = Date.now() - liveRun.startedAt;

  return (
    <div className="mx-auto flex h-full w-full max-w-3xl flex-col">
      {/* Progress header */}
      <div className="border-b bg-background/80 px-4 py-3 backdrop-blur">
        <div className="mb-2 flex items-center gap-2">
          {liveRun.status === "running" ? (
            <Loader2 className="h-4 w-4 animate-spin text-violet-500" />
          ) : liveRun.status === "error" ? (
            <AlertCircle className="h-4 w-4 text-destructive" />
          ) : (
            <Sparkles className="h-4 w-4 text-emerald-500" />
          )}
          <h2 className="text-sm font-semibold">
            {liveRun.kind === "branch" ? (
              <span className="flex items-center gap-1.5">
                <GitBranch className="h-3.5 w-3.5" /> Branching
              </span>
            ) : (
              <span className="flex items-center gap-1.5">
                <Play className="h-3.5 w-3.5" /> Researching
              </span>
            )}
          </h2>
          <span className="text-sm text-muted-foreground">
            step {Math.min(done + (liveRun.status === "running" ? 1 : 0), total)} of {total}
            {liveRun.currentIndex !== null && liveRun.spans[liveRun.currentIndex] && (
              <> · {liveRun.spans[liveRun.currentIndex].name}</>
            )}
          </span>
          <span className="ml-auto font-mono text-xs text-muted-foreground">
            {fmtSec(elapsed)}
          </span>
        </div>
        <Progress value={progress} className="h-1.5" />
        <p className="mt-1.5 truncate text-xs text-muted-foreground">
          {liveRun.query}
        </p>
      </div>

      {/* Streaming spans */}
      <ScrollArea className="flex-1">
        <div className="space-y-2.5 p-4">
          {liveRun.spans.map((span) => (
            <ThinkingSpanRow
              key={span.index}
              span={span}
              isActive={liveRun.currentIndex === span.index && liveRun.status === "running"}
            />
          ))}
          {liveRun.status === "error" && (
            <div className="rounded-lg border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
              {liveRun.error}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </ScrollArea>
    </div>
  );
}
