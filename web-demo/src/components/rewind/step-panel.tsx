"use client";

/**
 * StepPanel — the verify-then-navigate step viewer for interactive stepping.
 *
 * The stepping loop is: pause BEFORE the call → developer approves → call
 * executes → result surfaces HERE → developer verifies and chooses
 * Next / Step back / Stop. This component renders both the pending call
 * (messages/model/params) and, once the model responds, the result text.
 *
 * Decision hierarchy (shadcn button variants):
 *   - default (solid)  → Next step (approve + continue)
 *   - outline          → Edit (toggles edit mode) / Apply edit & continue
 *   - outline          → Step back (restart-from a prior cursor)
 *   - destructive      → Stop (inline AlertDialog confirm)
 */

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  PauseCircle,
  PlayCircle,
  Pencil,
  ChevronUp,
  Square,
  Clock,
  Wrench,
  MessageSquare,
  SlidersHorizontal,
  Loader2,
  Sparkles,
  CornerUpLeft,
} from "lucide-react";
import type { PausedStep } from "@/lib/rewind/types";
import { postDecision } from "@/lib/rewind/session-client";

interface StepPanelProps {
  sessionId: string;
  step: PausedStep;
}

function kindMeta(kind: string): { label: string; className: string } {
  switch (kind) {
    case "llm":
      return { label: "LLM", className: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-300" };
    case "tool":
      return { label: "Tool", className: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300" };
    case "mcp":
      return { label: "MCP", className: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300" };
    default:
      return { label: kind, className: "bg-muted text-muted-foreground" };
  }
}

function fmtElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

export function StepPanel({ sessionId, step }: StepPanelProps) {
  const [editing, setEditing] = useState(false);
  const [editedMessages, setEditedMessages] = useState("");
  const [editedModel, setEditedModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 500);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const msgs = step.payload.messages;
    setEditedMessages(msgs ? JSON.stringify(msgs, null, 2) : "");
    setEditedModel(step.payload.model ?? "");
    setEditing(false);
    setError(null);
  }, [step]);

  const meta = kindMeta(step.kind);
  const elapsed = Date.now() - step.pausedAt;
  const hasResult = step.result !== null && step.result !== "";
  const waitingForModel = !hasResult && !editing;

  const decide = async (kind: "approve" | "edit" | "stop" | "step_once", body?: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await postDecision(sessionId, { kind, ...body });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const handleApplyEdit = () => {
    let messages: unknown[] | undefined;
    if (editedMessages.trim()) {
      try {
        messages = JSON.parse(editedMessages) as unknown[];
      } catch (e) {
        setError(`invalid messages JSON: ${e instanceof Error ? e.message : String(e)}`);
        return;
      }
    }
    void decide("edit", {
      ...(messages ? { messages } : {}),
      ...(editedModel.trim() ? { model: editedModel.trim() } : {}),
    });
    setEditing(false);
  };

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 border-b px-4 py-3">
        {waitingForModel ? (
          <Loader2 className="size-5 animate-spin text-violet-500" />
        ) : hasResult ? (
          <Sparkles className="size-5 text-emerald-500" />
        ) : (
          <PauseCircle className="size-5 text-amber-500" />
        )}
        <span className="text-sm font-semibold">
          {waitingForModel
            ? `Executing step #${step.cursor}…`
            : hasResult
              ? `Step #${step.cursor} result`
              : `Paused at step #${step.cursor}`}
        </span>
        <Badge variant="secondary" className={meta.className}>{meta.label}</Badge>
        <span className="ml-auto flex items-center gap-1 font-mono text-xs text-muted-foreground">
          <Clock className="size-3" />
          {fmtElapsed(elapsed)}
        </span>
      </div>

      {/* Body */}
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {error && (
          <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
            {error}
          </div>
        )}

        {/* The model's RESPONSE — shown once the step has executed (verify loop) */}
        {hasResult && !editing && (
          <Card className="border-emerald-300/50 bg-emerald-50/40 dark:bg-emerald-950/20">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-emerald-700 dark:text-emerald-400">
                <Sparkles className="size-3.5" /> Model response
              </CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="whitespace-pre-wrap break-words bg-background/60 p-2 text-xs leading-relaxed">
                {step.result}
              </pre>
            </CardContent>
          </Card>
        )}

        {/* "generating…" placeholder while waiting for the model */}
        {waitingForModel && (
          <Card>
            <CardContent className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin text-violet-500" />
              Waiting for the model to respond…
            </CardContent>
          </Card>
        )}

        {/* The pending call — messages/model/params (shown when editing or no result yet) */}
        {(!hasResult || editing) && step.payload.model && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                <MessageSquare className="size-3.5" /> Model
              </CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="whitespace-pre-wrap break-all bg-muted/40 p-2 font-mono text-xs">
                {step.payload.model}
              </pre>
            </CardContent>
          </Card>
        )}

        {/* Edit mode */}
        {editing && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Edit messages (JSON)
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">Model override</label>
                <Input
                  value={editedModel}
                  onChange={(e) => setEditedModel(e.target.value)}
                  placeholder="(leave unchanged)"
                  className="h-8 font-mono text-xs"
                />
              </div>
              <Textarea
                value={editedMessages}
                onChange={(e) => setEditedMessages(e.target.value)}
                className="min-h-[200px] font-mono text-xs"
                spellCheck={false}
              />
            </CardContent>
          </Card>
        )}

        {/* Messages (read-only, when not editing and no result or has result) */}
        {!editing && step.payload.messages && step.payload.messages.length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                <MessageSquare className="size-3.5" /> Messages sent
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {step.payload.messages.map((m, i) => (
                <MessageRow key={i} message={m} />
              ))}
            </CardContent>
          </Card>
        )}

        {/* Params */}
        {!editing && step.payload.params && Object.keys(step.payload.params).length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                <SlidersHorizontal className="size-3.5" /> Params
              </CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="overflow-x-auto bg-muted/40 p-2 font-mono text-xs">
                {JSON.stringify(step.payload.params, null, 2)}
              </pre>
            </CardContent>
          </Card>
        )}

        {/* Tools */}
        {!editing && step.payload.tools && step.payload.tools.length > 0 && (
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                <Wrench className="size-3.5" /> Tools ({step.payload.tools.length})
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {step.payload.tools.map((t, i) => (
                <pre key={i} className="overflow-x-auto bg-muted/40 p-2 font-mono text-xs">
                  {JSON.stringify(t, null, 2)}
                </pre>
              ))}
            </CardContent>
          </Card>
        )}
      </div>

      {/* Decision footer — reframed for the verify loop */}
      <div className="flex items-center gap-2 border-t bg-muted/30 px-4 py-3">
        {!editing ? (
          <>
            {/* Primary: Next step (only enabled once the result is in) */}
            <Button
              size="sm"
              onClick={() => void decide("approve")}
              disabled={busy || waitingForModel}
              title={waitingForModel ? "Wait for the model to respond first" : "Approve and continue to the next step"}
            >
              <PlayCircle className="mr-1 size-4" /> Next step
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(true)} disabled={busy || waitingForModel}>
              <Pencil className="mr-1 size-4" /> Edit &amp; rerun
            </Button>
            <Button size="sm" variant="outline" onClick={() => void decide("step_once")} disabled={busy || waitingForModel} title="Approve this step and run the rest without pausing">
              <ChevronUp className="mr-1 size-4" /> Run to end
            </Button>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button size="sm" variant="destructive" disabled={busy} className="ml-auto">
                  <Square className="mr-1 size-4" /> Stop
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Stop the agent run?</AlertDialogTitle>
                  <AlertDialogDescription>
                    This terminates the session. The captured spans under this
                    branch are preserved, but the agent will not continue.
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={() => void decide("stop")}
                    className="bg-destructive text-white hover:bg-destructive/90"
                  >
                    Stop run
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          </>
        ) : (
          <>
            <Button size="sm" onClick={handleApplyEdit} disabled={busy}>
              <PlayCircle className="mr-1 size-4" /> Apply edit &amp; continue
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)} disabled={busy}>
              <CornerUpLeft className="mr-1 size-4" /> Cancel
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

function MessageRow({ message }: { message: unknown }) {
  if (typeof message === "string") {
    return <pre className="whitespace-pre-wrap bg-muted/40 p-2 font-mono text-xs">{message}</pre>;
  }
  if (message !== null && typeof message === "object") {
    const m = message as { role?: unknown; content?: unknown };
    return (
      <div className="space-y-1">
        {typeof m.role === "string" && (
          <Badge variant="outline" className="text-[10px] uppercase">{m.role}</Badge>
        )}
        <pre className="whitespace-pre-wrap bg-muted/40 p-2 font-mono text-xs">
          {typeof m.content === "string"
            ? m.content
            : JSON.stringify(m.content ?? message, null, 2)}
        </pre>
      </div>
    );
  }
  return <pre className="whitespace-pre-wrap bg-muted/40 p-2 font-mono text-xs">{String(message)}</pre>;
}
