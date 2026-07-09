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
import type { BranchDiff, LiveRun, SpanKind, Trace } from "./types";
import { diffBranches } from "./diff";

export type UIMode = "inspect" | "branch";

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
}));
