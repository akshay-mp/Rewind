"use client";

/**
 * Client-side helper for the interactive stepping server (Phase 9).
 *
 * Sibling to streamRewind, but for the bidirectional SSE + decision protocol.
 * The Python stepping server emits SSE events (paused/resumed/done/errored)
 * over GET /api/v1/sessions/{id}/stream; the browser POSTs decisions to
 * /api/v1/sessions/{id}/decide on a SEPARATE connection while the stream
 * stays open. EventSource + fetch multiplex naturally in a real browser.
 *
 * All endpoints are relative ("/api/v1/sessions*") and proxied to the Python
 * backend by next.config.ts rewrites — no CORS, no env-var URL in the browser.
 */

import { useRewindStore } from "./store";
import type {
  DecisionBody,
  PausedStep,
  StartSessionBody,
  StartSessionResponse,
  StepEvent,
} from "./types";

/**
 * Start a stepping session: POST /api/v1/sessions, seed the store's
 * liveSession, then open the SSE stream. Returns the session id.
 *
 * The SSE stream is opened as a side effect (streamSessionDecisions) so the
 * caller can `await startSession(...)` and immediately have events flowing.
 */
export async function startSession(body: StartSessionBody): Promise<string> {
  const {
    startLiveSession,
    failSession,
  } = useRewindStore.getState();

  const res = await fetch("/api/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const message = (err as { detail?: string }).detail ?? `HTTP ${res.status}`;
    failSession(message);
    throw new Error(message);
  }
  const data = (await res.json()) as StartSessionResponse;
  startLiveSession(
    data.session_id,
    data.trace_id,
    data.branch_id,
    body.runner_ref,
  );

  // Open the SSE stream. Not awaited — it runs until the session terminates.
  void streamSessionDecisions(data.session_id);
  return data.session_id;
}

/**
 * Open the SSE event stream for a session and dispatch events into the store.
 *
 * Returns a close function (also stashed on the store-free module global so
 * a future "cancel" can tear it down). The stream stays open across decision
 * POSTs — that's the whole point of stepping.
 */
let _activeStream: EventSource | null = null;

export function streamSessionDecisions(sessionId: string): void {
  const {
    pauseAtStep,
    completeStep,
    resumeAfterStep,
    finishSession,
    failSession,
  } = useRewindStore.getState();

  // Close any prior stream (defensive — shouldn't happen in normal flow).
  if (_activeStream) {
    _activeStream.close();
    _activeStream = null;
  }

  const es = new EventSource(`/api/v1/sessions/${sessionId}/stream`);
  _activeStream = es;

  es.onmessage = (e: MessageEvent<string>) => {
    let evt: StepEvent;
    try {
      evt = JSON.parse(e.data) as StepEvent;
    } catch {
      // Malformed event — skip; the next well-formed one re-syncs the UI.
      return;
    }
    switch (evt.type) {
      case "paused": {
        const step: PausedStep = {
          cursor: evt.cursor,
          kind: evt.kind,
          payload: evt.step ?? {},
          pausedAt: Date.now(),
          result: null,
        };
        pauseAtStep(step);
        break;
      }
      case "step_completed":
        // The model has returned; attach the result to the current paused
        // step so the UI can render it in the verify panel.
        completeStep(evt.cursor, evt.result);
        break;
      case "resumed":
        resumeAfterStep(evt.decision);
        break;
      case "done":
        finishSession();
        es.close();
        _activeStream = null;
        break;
      case "errored":
        failSession(evt.message);
        es.close();
        _activeStream = null;
        break;
    }
  };

  // EventSource fires onerror on every reconnect attempt. We only treat it
  // as a hard failure if the session is already terminal — otherwise the
  // auto-reconnect is desirable (the server replays state on reconnect).
  es.onerror = () => {
    const { liveSession } = useRewindStore.getState();
    if (liveSession && (liveSession.status === "done" || liveSession.status === "errored")) {
      es.close();
      _activeStream = null;
    }
  };
}

/** Close the active SSE stream, if any. Called on unmount / view switch. */
export function closeSessionStream(): void {
  if (_activeStream) {
    _activeStream.close();
    _activeStream = null;
  }
}

/**
 * POST a Decision to resume the blocked agent. The SSE stream stays open —
 * this is a separate fetch on a separate connection.
 *
 * Returns the server's acknowledgement; the actual step advancement arrives
 * asynchronously via the stream (a "resumed" event followed by the next
 * "paused" or "done").
 */
export async function postDecision(
  sessionId: string,
  body: DecisionBody,
): Promise<void> {
  const { failSession } = useRewindStore.getState();
  const res = await fetch(`/api/v1/sessions/${sessionId}/decide`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    const message = (err as { detail?: string }).detail ?? `HTTP ${res.status}`;
    // A 400 here is usually a validation error (bad decision shape) — surface
    // it via the session's error field so the StepPanel can show it inline
    // without tearing down the still-open stream.
    failSession(message);
    throw new Error(message);
  }
}
