import { useCallback, useEffect, useState } from "react";
import { api } from "@/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { useTimeTravelStore } from "@/lib/timetravel/store";
import { spanCost } from "@/lib/timetravel/diff";
import { resumeSession, startAgentSession } from "@/lib/timetravel/session-client";
import { AgentStartDialog } from "@/components/AgentStartDialog";
import type { AgentView } from "@/types";
import { SpanTimeline } from "@/components/timetravel/span-timeline";
import { SpanDetail } from "@/components/timetravel/span-detail";
import { DiffView } from "@/components/timetravel/diff-view";
import { ThinkingPanel } from "@/components/timetravel/thinking-panel";
import { SessionView } from "@/components/timetravel/session-view";
import {
  ChevronDown,
  GitCompare,
  Loader2,
  Play,
  RotateCcw,
  Sparkles,
  Zap,
  Database,
  PauseCircle,
  Undo2,
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
    reset,
    setLastEvent,
    stepUp,
    stepDown,
    setDiff,
    mode,
    exitBranchMode,
    uiView,
    setUIView,
  } = useTimeTravelStore();

  const [sessStarting, setSessStarting] = useState(false);
  const [agents, setAgents] = useState<AgentView[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [agentsError, setAgentsError] = useState<string | null>(null);
  const [selectedAgentRef, setSelectedAgentRef] = useState("");
  const [dialogAgent, setDialogAgent] = useState<AgentView | null>(null);

  const loadAgents = useCallback(async (): Promise<AgentView[]> => {
    setAgentsLoading(true);
    try {
      const response = await api.listAgents();
      setAgents(response.items);
      setAgentsError(null);
      setSelectedAgentRef((current) => current && response.items.some((agent) => agent.ref === current)
        ? current
        : response.items.length === 1 ? response.items[0].ref : response.items.find((agent) => agent.available)?.ref ?? response.items[0]?.ref ?? "");
      return response.items;
    } catch (error) {
      setAgentsError(error instanceof Error ? error.message : String(error));
      return [];
    } finally {
      setAgentsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAgents();
  }, [loadAgents]);

  const selected = selectedBranchId ? traces[selectedBranchId] : null;
  const canStep = !!selected && mode === "inspect";

  const selectedAgent = agents.find((agent) => agent.ref === selectedAgentRef)
    ?? (agents.length === 1 ? agents[0] : null);
  const availableAgentCount = agents.filter((agent) => agent.available).length;
  const unavailableReason = selectedAgent?.availability_reason || "This agent is unavailable; configure its capabilities first.";

  const openStartDialog = useCallback(async () => {
    const freshAgents = await loadAgents();
    const agent = freshAgents.find((item) => item.ref === selectedAgentRef)
      ?? (freshAgents.length === 1 ? freshAgents[0] : freshAgents.find((item) => item.available) ?? null);
    if (agent?.available) setDialogAgent(agent);
  }, [loadAgents, selectedAgentRef]);

  const startAgent = useCallback(async (inputs: Record<string, unknown>) => {
    if (!dialogAgent) return;
    setLastEvent(`Starting ${dialogAgent.name} — step-by-step interactive mode armed...`);
    setSessStarting(true);
    try {
      await startAgentSession(dialogAgent.ref, {
        inputs,
      });
      setDialogAgent(null);
      setUIView("session");
    } catch (error) {
      useTimeTravelStore.setState({ runError: error instanceof Error ? error.message : String(error) });
      throw error;
    } finally {
      setSessStarting(false);
    }
  }, [dialogAgent, setLastEvent, setUIView]);

  const compareWithOriginal = useCallback(() => {
    if (!rootBranchId || !selectedBranchId || selectedBranchId === rootBranchId)
      return;
    setDiff(rootBranchId, selectedBranchId);
  }, [rootBranchId, selectedBranchId, setDiff]);

  const totalCost = selected ? spanCost(selected.spans) : null;

  return (
    <header className="border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="flex flex-col gap-3 px-4 py-3">
        {/* Row 1 — Branding + Start Agent + View Controls */}
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <div className="flex size-8 items-center justify-center rounded-lg bg-emerald-500/10 ring-1 ring-emerald-500/20 text-emerald-500 font-bold text-sm">
              ««
            </div>
            <div>
              <div className="text-sm font-semibold leading-tight flex items-center gap-2">
                Agent Timetravel Debugger
                <Badge variant="outline" className="text-[10px] py-0 border-emerald-500/30 text-emerald-400">
                  Step-by-Step
                </Badge>
              </div>
              <div className="text-[10px] text-muted-foreground leading-tight">
                Time-travel agent execution · step-by-step control · edit prompts &amp; step back
              </div>
            </div>
          </div>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {/* Primary "Start Agent" Button */}
            {agents.length > 1 && (
              <select
                aria-label="Agent"
                value={selectedAgent?.ref ?? ""}
                onChange={(event) => setSelectedAgentRef(event.target.value)}
                className="h-8 max-w-52 rounded-md border bg-background px-2 text-xs"
                disabled={agentsLoading}
              >
                {agents.map((agent) => (
                  <option key={agent.ref} value={agent.ref} disabled={!agent.available}>
                    {agent.name}{agent.available ? "" : ` · unavailable${agent.availability_reason ? ` (${agent.availability_reason})` : " (not configured)"}`}
                  </option>
                ))}
              </select>
            )}
            <Button
              size="sm"
              onClick={() => void openStartDialog()}
              disabled={isRunning || sessStarting || agentsLoading || !selectedAgent || !selectedAgent.available}
              title={selectedAgent && !selectedAgent.available ? unavailableReason : undefined}
              className="bg-emerald-600 hover:bg-emerald-500 text-white font-medium shadow-sm"
            >
              {isRunning || sessStarting || agentsLoading ? (
                <Loader2 className="size-4 animate-spin mr-1.5" />
              ) : (
                <Play className="size-4 mr-1.5 fill-current" />
              )}
              Start Agent
            </Button>

            {/* View Switcher: Interactive Workbench vs Sessions List */}
            <div className="flex items-center rounded-md border bg-muted/40 p-0.5">
              <button
                type="button"
                onClick={() => setUIView("demo")}
                className={cn(
                  "flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium transition-colors",
                  uiView === "demo"
                    ? "bg-background shadow-sm text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <PauseCircle className="size-3.5" /> Workbench
              </button>
              <button
                type="button"
                onClick={() => setUIView("session")}
                className={cn(
                  "flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium transition-colors",
                  uiView === "session"
                    ? "bg-background shadow-sm text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                <Database className="size-3.5" /> Sessions
              </button>
            </div>

            {selectedBranchId && selectedBranchId !== rootBranchId && (
              <Button
                size="sm"
                variant="outline"
                onClick={compareWithOriginal}
                title="Diff this branch against the original run"
              >
                <GitCompare className="size-4 mr-1" /> Compare Diffs
              </Button>
            )}

            {selectedBranchId && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  if (confirm("Reset the session? All traces will be cleared.")) {
                    reset();
                  }
                }}
              >
                <RotateCcw className="size-4" /> Reset
              </Button>
            )}
          </div>
        </div>

        {/* Row 2 — Stepping Controls & Metrics */}
        {selectedBranchId && (
          <div className="flex flex-wrap items-center gap-3 border-t pt-2.5">
            <div className="flex items-center gap-1">
              <Button
                size="sm"
                variant="outline"
                onClick={stepDown}
                disabled={!canStep || cursor <= 0}
                title="Step back (timetravel to previous span)"
                className="gap-1"
              >
                <Undo2 className="size-3.5" /> Step Back
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={stepUp}
                disabled={!canStep || cursor >= (selected?.spans.length ?? 0) - 1}
                title="Step forward (advance to next span)"
                className="gap-1"
              >
                <ChevronDown className="size-3.5" /> Next Step
              </Button>
              <span className="ml-2 text-xs text-muted-foreground">
                Span{" "}
                <span className="font-mono text-foreground font-semibold">
                  #{cursor + 1}
                </span>{" "}
                of {selected?.spans.length}
              </span>
            </div>

            {totalCost && (
              <div className="flex items-center gap-2 text-xs ml-auto">
                <Badge className="bg-sky-500/10 text-sky-400 border border-sky-500/20">
                  <Zap className="mr-1 size-3" /> {totalCost.liveCalls} live calls · {totalCost.totalLatencyMs}ms
                </Badge>
                <Badge className="bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                  <Database className="mr-1 size-3" /> {totalCost.cachedCalls} cached
                </Badge>
              </div>
            )}

            {mode === "branch" && (
              <Badge className="bg-violet-500/10 text-violet-300 border border-violet-500/30">
                <Sparkles className="mr-1 size-3" /> Branching mode active
                <button
                  className="ml-2 underline hover:text-white"
                  onClick={exitBranchMode}
                >
                  exit
                </button>
              </Badge>
            )}
          </div>
        )}

        {runError && (
          <div className="rounded-md bg-destructive/15 border border-destructive/30 px-3 py-1.5 text-xs text-destructive flex items-center justify-between">
            <span>{runError}</span>
            <button onClick={() => useTimeTravelStore.setState({ runError: null })} className="underline">dismiss</button>
          </div>
        )}
        {!agentsLoading && agents.length === 0 && !agentsError && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
            <strong>No agents registered.</strong> Register an agent with the backend, then <button type="button" className="underline" onClick={() => void loadAgents()}>refresh the agent list</button> to start a session.
          </div>
        )}
        {agentsError && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            Could not load the agent registry: {agentsError} <button type="button" className="ml-1 underline" onClick={() => void loadAgents()}>retry</button>
          </div>
        )}
        {!agentsLoading && agents.length > 0 && availableAgentCount === 0 && (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
            Registered agents are unavailable. {selectedAgent?.availability_reason || "Configure at least one agent capability before starting a session."}
          </div>
        )}
      </div>
      <AgentStartDialog
        agent={dialogAgent}
        open={dialogAgent !== null}
        submitting={sessStarting}
        onOpenChange={(open) => { if (!open && !sessStarting) setDialogAgent(null); }}
        onStart={startAgent}
      />
    </header>
  );
}

export default function App() {
  const { uiView, diff, liveSession, setUIView } = useTimeTravelStore();

  useEffect(() => {
    if (liveSession || typeof window === "undefined") return;
    const sessionId = window.localStorage.getItem("timetravel-active-session");
    if (!sessionId) return;
    void resumeSession(sessionId)
      .then(() => setUIView("session"))
      .catch(() => window.localStorage.removeItem("timetravel-active-session"));
  }, [liveSession, setUIView]);

  return (
    <div className="flex h-screen flex-col bg-background text-foreground dark">
      <TopBar />

      <main className="min-h-0 flex-1 overflow-hidden">
        {uiView === "session" || liveSession ? (
          <SessionView />
        ) : (
          <ResizablePanelGroup direction="horizontal" className="h-full">
            {/* Left Rail: Span Timeline */}
            <ResizablePanel defaultSize={30} minSize={20} maxSize={45}>
              <div className="h-full border-r bg-muted/20">
                <SpanTimeline />
              </div>
            </ResizablePanel>

            <ResizableHandle withHandle />

            {/* Right Main Workbench: Span Inspector & Local Model Thinking */}
            <ResizablePanel defaultSize={70}>
              <ResizablePanelGroup direction="vertical">
                {/* Top Section: Local Model Reasoning Accordion (<think>) */}
                <ResizablePanel defaultSize={30} minSize={15}>
                  <div className="h-full border-b bg-muted/10">
                    <ThinkingPanel />
                  </div>
                </ResizablePanel>

                <ResizableHandle withHandle />

                {/* Bottom Section: Prompt Editor & Step Inspector */}
                <ResizablePanel defaultSize={70}>
                  <div className="h-full">
                    <SpanDetail />
                  </div>
                </ResizablePanel>
              </ResizablePanelGroup>
            </ResizablePanel>
          </ResizablePanelGroup>
        )}
      </main>

      {/* Side-by-Side Branch Diff Modal */}
      {diff && <DiffView />}
    </div>
  );
}
