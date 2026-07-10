"use client";

import { useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useRewindStore } from "@/lib/rewind/store";
import { MarkdownPreview } from "@/lib/rewind/markdown";
import { streamRewind } from "@/lib/rewind/stream-client";
import { PROMPT_SUGGESTIONS } from "@/lib/deep-research/prompts";
import {
  ArrowLeftFromLine,
  Brain,
  ChevronDown,
  Database,
  Lightbulb,
  Loader2,
  PlayCircle,
  RotateCcw,
  Sparkles,
  X,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Collapsible "Thinking" card shown in inspect mode for any span that captured
 * reasoning. Collapses to a Unsloth-style "Thought for Xs" summary by default
 * (the reasoning is verbose); expands to show the full chain-of-thought in the
 * muted/italic thinking style.
 */
function ThinkingCard({
  reasoning,
  latencyMs,
}: {
  reasoning: string;
  latencyMs: number;
}) {
  const [open, setOpen] = useState(false);
  const seconds = Math.max(1, Math.round(latencyMs / 1000));
  return (
    <Card>
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger asChild>
          <CardHeader className="cursor-pointer pb-2 hover:bg-accent/40">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Brain className="size-4 text-violet-500" />
              Thinking
              <span className="ml-auto flex items-center gap-1.5 text-xs font-normal italic text-muted-foreground">
                Thought for {seconds}s
                <ChevronDown
                  className={cn("size-3.5 transition-transform", open && "rotate-180")}
                />
              </span>
            </CardTitle>
          </CardHeader>
        </CollapsibleTrigger>
        <CollapsibleContent>
          <CardContent className="pt-0">
            <div className="rounded-md bg-slate-50 p-3 dark:bg-slate-900/40">
              <MarkdownPreview text={reasoning} variant="thinking" />
            </div>
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}

export function SpanDetail() {
  const {
    traces,
    selectedBranchId,
    cursor,
    mode,
    draftSystemPrompt,
    draftLabel,
    draftNote,
    isRunning,
    runError,
    enterBranchMode,
    exitBranchMode,
    setDraftSystemPrompt,
    setDraftLabel,
    setDraftNote,
    setLastEvent,
  } = useRewindStore();

  const trace = selectedBranchId ? traces[selectedBranchId] : null;
  const span = trace ? trace.spans[cursor] : null;

  // Pre-fill the draft with the selected span's system prompt whenever we
  // enter branch mode.
  useEffect(() => {
    if (mode === "branch" && span) {
      setDraftSystemPrompt(span.systemPrompt);
    }
  }, [mode, cursor, selectedBranchId, span, setDraftSystemPrompt]);

  const suggestionsForThisSpan = useMemo(
    () => (span ? PROMPT_SUGGESTIONS.filter((s) => s.spanIndex === span.index) : []),
    [span],
  );

  if (!trace || !span) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Select a span to inspect.
      </div>
    );
  }

  async function runBranch() {
    const s = useRewindStore.getState();
    if (!s.selectedBranchId) return;
    const parent = s.traces[s.selectedBranchId];
    const spanIndex = s.cursor;
    setLastEvent(
      `Branching from “${parent.label}” @ #${spanIndex + 1} — streaming the divergent tail live…`,
    );
    // The cached prefix + live tail stream into the ThinkingPanel. On
    // trace_end the committed branch replaces the live view (page.tsx
    // switches off liveRun and renders the inspect view with it selected).
    await streamRewind(
      "/api/rewind/branch",
      {
        parent,
        branchAtSpanIndex: spanIndex,
        editedSystemPrompt: s.draftSystemPrompt,
        label: s.draftLabel || `Branch @ #${spanIndex + 1}`,
        note: s.draftNote,
      },
      "branch",
    );
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* header */}
      <div className="flex items-start justify-between gap-3 border-b p-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="font-mono">
              #{span.index + 1}
            </Badge>
            <span className="truncate text-base font-semibold">{span.name}</span>
            {span.source === "cached" ? (
              <Badge className="bg-emerald-100 text-emerald-900 hover:bg-emerald-100 dark:bg-emerald-900/40 dark:text-emerald-200">
                <Database className="mr-1 size-3" /> cached · 0 tok
              </Badge>
            ) : (
              <Badge className="bg-amber-100 text-amber-900 hover:bg-amber-100 dark:bg-amber-900/40 dark:text-amber-200">
                <Zap className="mr-1 size-3" /> live · {span.tokensOut} tok
              </Badge>
            )}
            {trace.branchAtSpanIndex === span.index && (
              <Badge className="bg-fuchsia-100 text-fuchsia-900 hover:bg-fuchsia-100 dark:bg-fuchsia-900/40 dark:text-fuchsia-200">
                <Sparkles className="mr-1 size-3" /> branch point
              </Badge>
            )}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {trace.label} · model {span.model} · {span.latencyMs} ms
          </div>
        </div>
        {mode === "inspect" && (
          <Button size="sm" onClick={enterBranchMode}>
            <ArrowLeftFromLine className="size-4" />
            Branch from here
          </Button>
        )}
        {mode === "branch" && (
          <Button size="sm" variant="ghost" onClick={exitBranchMode}>
            <X className="size-4" /> Cancel
          </Button>
        )}
      </div>

      {/* scroll body */}
      <div className="flex-1 overflow-y-auto p-4">
        {mode === "inspect" ? (
          <div className="space-y-4">
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">System prompt</CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="whitespace-pre-wrap rounded-md bg-muted/40 p-3 font-mono text-xs leading-relaxed">
                  {span.systemPrompt}
                </pre>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">User input</CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="whitespace-pre-wrap rounded-md bg-muted/40 p-3 font-mono text-xs leading-relaxed">
                  {span.userInput}
                </pre>
              </CardContent>
            </Card>
            {span.reasoning && span.reasoning.trim() && (
              <ThinkingCard reasoning={span.reasoning} latencyMs={span.latencyMs} />
            )}
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Output</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="rounded-md border bg-background p-3">
                  <MarkdownPreview text={span.output} />
                </div>
              </CardContent>
            </Card>
          </div>
        ) : (
          <div className="space-y-4">
            <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-3 text-sm dark:border-emerald-700 dark:bg-emerald-950/40">
              <div className="flex items-center gap-2 font-medium text-emerald-900 dark:text-emerald-200">
                <Sparkles className="size-4" />
                Branch mode — fix the prompt and run the divergent tail live
              </div>
              <p className="mt-1 text-xs text-emerald-900/80 dark:text-emerald-200/80">
                Spans #1–#{span.index + 1} will be FROZEN-replayed from the recording
                (zero LLM calls). Span #{span.index + 1} onward will run live with your
                edited prompt — that&apos;s the only part you pay for.
              </p>
            </div>

            {/* suggestions */}
            {suggestionsForThisSpan.length > 0 && (
              <Card>
                <CardHeader className="pb-2">
                  <CardTitle className="flex items-center gap-2 text-sm">
                    <Lightbulb className="size-4 text-amber-500" />
                    Suggested prompt fix
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  {suggestionsForThisSpan.map((s, i) => (
                    <div
                      key={i}
                      className="rounded-md border bg-background p-3 text-sm"
                    >
                      <div className="font-medium">{s.title}</div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {s.rationale}
                      </p>
                      <Button
                        size="sm"
                        variant="outline"
                        className="mt-2"
                        onClick={() => setDraftSystemPrompt(s.newSystemPrompt)}
                      >
                        Apply this fix
                      </Button>
                    </div>
                  ))}
                </CardContent>
              </Card>
            )}

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Edited system prompt</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <Textarea
                  value={draftSystemPrompt}
                  onChange={(e) => setDraftSystemPrompt(e.target.value)}
                  className="min-h-[180px] font-mono text-xs"
                  placeholder="Edit the system prompt for this span…"
                />
                <div className="grid grid-cols-2 gap-3">
                  <div className="space-y-1">
                    <Label htmlFor="label" className="text-xs">
                      Branch label
                    </Label>
                    <Input
                      id="label"
                      value={draftLabel}
                      onChange={(e) => setDraftLabel(e.target.value)}
                      className="h-8 text-xs"
                    />
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="note" className="text-xs">
                      What are you testing?
                    </Label>
                    <Input
                      id="note"
                      value={draftNote}
                      onChange={(e) => setDraftNote(e.target.value)}
                      className="h-8 text-xs"
                      placeholder="e.g. force 3 non-overlapping topics"
                    />
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">
                  Original system prompt (for reference)
                </CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="whitespace-pre-wrap rounded-md bg-muted/40 p-3 font-mono text-xs leading-relaxed text-muted-foreground">
                  {span.systemPrompt}
                </pre>
              </CardContent>
            </Card>

            {runError && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                {runError}
              </div>
            )}

            <div className="flex items-center justify-end gap-2">
              <Button variant="ghost" size="sm" onClick={exitBranchMode}>
                <RotateCcw className="size-4" /> Reset
              </Button>
              <Button onClick={runBranch} disabled={isRunning}>
                {isRunning ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <PlayCircle className="size-4" />
                )}
                Run branch live
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
