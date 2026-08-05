/**
 * Client-side session state for the Rewind × Deep Research demo.
 *
 * The store owns:
 *   - traces:      every Trace the user has captured or branched, keyed by branchId.
 *   - rootBranchId: branchId of the original run (always "main" once captured).
 *   - selectedBranchId: which branch is shown in the timeline + span detail.
 *   - cursor:      index of the currently-selected span in the selected branch.
 *                  "Step Down" decrements this; "Step Up" increments it.
 *   - mode:        "inspect" (FROZEN — just reading the recording) or
 *                  "branch"  (editing the system prompt to fork a new branch).
 *   - draftSystemPrompt: the edited prompt being staged before running a branch.
 *   - isRunning:   true while an API call is in flight.
 *   - diff:        optional side-by-side diff between two branches.
 */

"use client";

import { create } from "zustand";
import type {
  BranchDiff,
  LiveRun,
  LiveSession,
  PausedStep,
  SpanKind,
  Trace,
} from "./types";
import { diffBranches } from "./diff";

export type UIMode = "inspect" | "branch";

/**
 * Top-level view discriminator (separate from UIMode). The "demo" view is the
 * bundled-agent trace/branch experience; "session" is the Phase 9 interactive
 * stepping view driven by the Python stepping server.
 */
export type UIView = "demo" | "session";

interface RewindState {
  traces: Record<string, Trace>;
  rootBranchId: string | null;
  selectedBranchId: string | null;
  cursor: number;
  mode: UIMode;
  draftSystemPrompt: string;
  draftLabel: string;
  draftNote: string;
  isRunning: boolean;
  runError: string | null;
  diff: BranchDiff | null;
  diffLeftBranchId: string | null;
  diffRightBranchId: string | null;
  lastEvent: string | null;
  /**
   * A run in progress, shown in the ThinkingPanel. null when no run is
   * streaming. Set by startLiveRun, mutated by the delta actions as
   * StreamEvents arrive, and cleared by finishLiveRun once the Trace commits.
   */
  liveRun: LiveRun | null;

  /**
   * Top-level view: "demo" (bundled agent) or "session" (stepping server).
   * Defaults to "demo" so the existing experience is unchanged.
   */
  uiView: UIView;
  /**
   * A stepping session in progress, parallel to liveRun. null when no session
   * is active. Mutated by the session-client as SSE events arrive.
   */
  liveSession: LiveSession | null;

  // actions
  setRunning: (v: boolean) => void;
  setRunError: (e: string | null) => void;
  addTrace: (t: Trace) => void;
  selectBranch: (branchId: string) => void;
  setCursor: (i: number) => void;
  stepDown: () => void;
  stepUp: () => void;
  enterBranchMode: () => void;
  exitBranchMode: () => void;
  setDraftSystemPrompt: (s: string) => void;
  setDraftLabel: (s: string) => void;
  setDraftNote: (s: string) => void;
  setDiff: (
    leftBranchId: string | null,
    rightBranchId: string | null,
  ) => void;
  reset: () => void;
  setLastEvent: (s: string | null) => void;

  // live-run actions
  startLiveRun: (query: string, kind: "run" | "branch") => void;
  beginSpan: (index: number, name: string, kind: SpanKind) => void;
  appendReasoning: (index: number, chunk: string) => void;
  appendOutput: (index: number, chunk: string) => void;
  finishSpan: (index: number) => void;
  failLiveRun: (message: string) => void;
  /** Commit the finished trace and dismiss the live view. */
  finishLiveRun: (trace: Trace) => void;
  clearLiveRun: () => void;

  // session (stepping) actions — additive, do not touch liveRun/traces/mode
  setUIView: (v: UIView) => void;
  startLiveSession: (
    sessionId: string,
    traceId: string,
    branchId: string,
    runnerRef: string,
  ) => void;
  pauseAtStep: (step: PausedStep) => void;
  /** Attach the model's response text to the current paused step (verify loop). */
  completeStep: (cursor: number, result: string) => void;
  resumeAfterStep: (decision: string) => void;
  finishSession: () => void;
  failSession: (message: string) => void;
  clearLiveSession: () => void;
}

export const useRewindStore = create<RewindState>((set, get) => ({
  traces: {},
  rootBranchId: null,
  selectedBranchId: null,
  cursor: 0,
  mode: "inspect",
  draftSystemPrompt: "",
  draftLabel: "",
  draftNote: "",
  isRunning: false,
  runError: null,
  diff: null,
  diffLeftBranchId: null,
  diffRightBranchId: null,
  lastEvent: null,
  liveRun: null,
  uiView: "demo",
  liveSession: null,

  setRunning: (v) => set({ isRunning: v }),
  setRunError: (e) => set({ runError: e }),
  addTrace: (t) =>
    set((s) => {
      const traces = { ...s.traces, [t.branchId]: t };
      const rootBranchId = s.rootBranchId ?? t.branchId;
      return {
        traces,
        rootBranchId,
        selectedBranchId: t.branchId,
        cursor: 0,
        mode: "inspect",
        draftSystemPrompt: "",
        draftLabel: "",
        draftNote: "",
        runError: null,
        diff: null,
        diffLeftBranchId: null,
        diffRightBranchId: null,
        lastEvent: t.parentBranchId
          ? `Branch “${t.label}” captured — ${t.spans.filter((x) => x.source === "cached").length} spans served from cache, ${t.spans.filter((x) => x.source === "live").length} live LLM calls.`
          : `Original trace captured — ${t.spans.length} live LLM spans.`,
      };
    }),

  selectBranch: (branchId) =>
    set((s) => {
      const t = s.traces[branchId];
      if (!t) return s;
      const clamped = Math.min(s.cursor, t.spans.length - 1);
      return {
        selectedBranchId: branchId,
        cursor: Math.max(0, clamped),
        mode: "inspect",
        draftSystemPrompt: "",
        draftLabel: "",
        draftNote: "",
      };
    }),

  setCursor: (i) => set({ cursor: i }),

  stepDown: () =>
    set((s) => ({
      cursor: Math.max(0, s.cursor - 1),
      mode: "inspect",
      lastEvent: `Stepped down to span #${Math.max(0, s.cursor - 1) + 1}.`,
    })),

  stepUp: () => {
    const s = get();
    const t = s.traces[s.selectedBranchId!];
    if (!t) return;
    const max = t.spans.length - 1;
    set({
      cursor: Math.min(max, s.cursor + 1),
      mode: "inspect",
      lastEvent: `Stepped up to span #${Math.min(max, s.cursor + 1) + 1}.`,
    });
  },

  enterBranchMode: () =>
    set((s) => {
      const t = s.traces[s.selectedBranchId!];
      if (!t) return s;
      const span = t.spans[s.cursor];
      return {
        mode: "branch",
        draftSystemPrompt: span.systemPrompt,
        draftLabel: `Branch @ #${s.cursor + 1}`,
        draftNote: "",
      };
    }),

  exitBranchMode: () =>
    set({
      mode: "inspect",
      draftSystemPrompt: "",
      draftLabel: "",
      draftNote: "",
    }),

  setDraftSystemPrompt: (s2) => set({ draftSystemPrompt: s2 }),
  setDraftLabel: (s2) => set({ draftLabel: s2 }),
  setDraftNote: (s2) => set({ draftNote: s2 }),

  setDiff: (leftBranchId, rightBranchId) =>
    set((s) => {
      if (!leftBranchId || !rightBranchId) {
        return {
          diff: null,
          diffLeftBranchId: null,
          diffRightBranchId: null,
        };
      }
      const left = s.traces[leftBranchId];
      const right = s.traces[rightBranchId];
      if (!left || !right) {
        return {
          diff: null,
          diffLeftBranchId: null,
          diffRightBranchId: null,
        };
      }
      const diff = diffBranches(left, right);
      return {
        diff,
        diffLeftBranchId: leftBranchId,
        diffRightBranchId: rightBranchId,
      };
    }),

  reset: () =>
    set({
      traces: {},
      rootBranchId: null,
      selectedBranchId: null,
      cursor: 0,
      mode: "inspect",
      draftSystemPrompt: "",
      draftLabel: "",
      draftNote: "",
      isRunning: false,
      runError: null,
      diff: null,
      diffLeftBranchId: null,
      diffRightBranchId: null,
      lastEvent: "Session reset.",
      liveRun: null,
    }),

  setLastEvent: (s2) => set({ lastEvent: s2 }),

  // --- live-run actions -----------------------------------------------------
  // These rebuild the liveRun object each call so Zustand sees a new ref and
  // the ThinkingPanel re-renders. Deltas arrive frequently but each is a small
  // string append, so the cost is negligible for an 8-span demo.
  startLiveRun: (query, kind) =>
    set({
      isRunning: true,
      runError: null,
      liveRun: {
        query,
        kind,
        spans: [],
        currentIndex: null,
        status: "running",
        error: null,
        startedAt: Date.now(),
      },
    }),

  beginSpan: (index, name, kind) =>
    set((s) => {
      if (!s.liveRun) return s;
      const spans = [...s.liveRun.spans];
      spans[index] = {
        index,
        name,
        kind,
        reasoning: "",
        output: "",
        status: "thinking",
        startedAt: Date.now(),
        endedAt: null,
      };
      return { liveRun: { ...s.liveRun, spans, currentIndex: index } };
    }),

  appendReasoning: (index, chunk) =>
    set((s) => {
      if (!s.liveRun || !s.liveRun.spans[index]) return s;
      const span = s.liveRun.spans[index];
      const spans = [...s.liveRun.spans];
      spans[index] = { ...span, reasoning: span.reasoning + chunk };
      return { liveRun: { ...s.liveRun, spans } };
    }),

  appendOutput: (index, chunk) =>
    set((s) => {
      if (!s.liveRun || !s.liveRun.spans[index]) return s;
      const span = s.liveRun.spans[index];
      const spans = [...s.liveRun.spans];
      spans[index] = {
        ...span,
        output: span.output + chunk,
        status: "answering",
      };
      return { liveRun: { ...s.liveRun, spans } };
    }),

  finishSpan: (index) =>
    set((s) => {
      if (!s.liveRun || !s.liveRun.spans[index]) return s;
      const span = s.liveRun.spans[index];
      const spans = [...s.liveRun.spans];
      spans[index] = { ...span, status: "done", endedAt: Date.now() };
      return { liveRun: { ...s.liveRun, spans } };
    }),

  failLiveRun: (message) =>
    set((s) => ({
      isRunning: false,
      runError: message,
      liveRun: s.liveRun
        ? { ...s.liveRun, status: "error", error: message }
        : null,
    })),

  finishLiveRun: (trace) =>
    set((s) => {
      const traces = { ...s.traces, [trace.branchId]: trace };
      const rootBranchId = s.rootBranchId ?? trace.branchId;
      return {
        traces,
        rootBranchId,
        selectedBranchId: trace.branchId,
        cursor: 0,
        mode: "inspect",
        draftSystemPrompt: "",
        draftLabel: "",
        draftNote: "",
        runError: null,
        diff: null,
        diffLeftBranchId: null,
        diffRightBranchId: null,
        isRunning: false,
        liveRun: null,
        lastEvent: trace.parentBranchId
          ? `Branch “${trace.label}” captured — ${trace.spans.filter((x) => x.source === "cached").length} spans served from cache, ${trace.spans.filter((x) => x.source === "live").length} live LLM calls.`
          : `Original trace captured — ${trace.spans.length} live LLM spans.`,
      };
    }),

  clearLiveRun: () => set({ liveRun: null, isRunning: false }),

  // --- session (stepping) actions ----------------------------------------
  // All additive: none of these touch liveRun, traces, mode, or cursor.
  // They mutate only liveSession, the parallel state object for the Phase 9
  // interactive stepping view. session-client.ts drives these as SSE events
  // arrive from the Python stepping server.
  setUIView: (v) => set({ uiView: v }),

  startLiveSession: (sessionId, traceId, branchId, runnerRef) =>
    set({
      liveSession: {
        sessionId,
        traceId,
        branchId,
        runnerRef,
        status: "running",
        error: null,
        pausedStep: null,
        history: [],
        startedAt: Date.now(),
      },
    }),

  pauseAtStep: (step) =>
    set((s) => {
      if (!s.liveSession) return s;
      return {
        liveSession: {
          ...s.liveSession,
          status: "paused",
          pausedStep: step,
        },
      };
    }),

  completeStep: (cursor, result) =>
    set((s) => {
      if (!s.liveSession || !s.liveSession.pausedStep) return s;
      if (s.liveSession.pausedStep.cursor !== cursor) return s;
      return {
        liveSession: {
          ...s.liveSession,
          pausedStep: { ...s.liveSession.pausedStep, result },
        },
      };
    }),

  resumeAfterStep: (decision) =>
    set((s) => {
      if (!s.liveSession || !s.liveSession.pausedStep) return s;
      const paused = s.liveSession.pausedStep;
      const entry = {
        cursor: paused.cursor,
        kind: paused.kind,
        decision,
        payload: paused.payload,
        result: paused.result,
        resolvedAt: Date.now(),
      };
      return {
        liveSession: {
          ...s.liveSession,
          status: "running",
          pausedStep: null,
          history: [...s.liveSession.history, entry],
        },
      };
    }),

  finishSession: () =>
    set((s) => ({
      liveSession: s.liveSession
        ? { ...s.liveSession, status: "done", pausedStep: null }
        : null,
    })),

  failSession: (message) =>
    set((s) => ({
      liveSession: s.liveSession
        ? { ...s.liveSession, status: "errored", error: message, pausedStep: null }
        : null,
    })),

  clearLiveSession: () => set({ liveSession: null }),
}));
