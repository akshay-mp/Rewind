// Phase 9 — interactive stepping session detail.
//
// The visual step-through debugger. Consumes the SSE event stream for a
// session, renders each paused step (messages / tools / params / raw), and
// exposes the four decisions: approve, edit & continue, stop, step once.
// A history sidebar accumulates consumed steps so the developer sees the
// run unfold.
//
// The SSE stream is the single source of truth for the paused step + the
// session's terminal state. We don't poll the REST endpoint on a timer —
// EventSource auto-reconnects, and each event is self-contained.

import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { sessionStatusClass } from "../styles";
import type { DecisionRequest, SessionDetailView, StepEvent, StepPayload } from "../types";

interface Props {
  sessionId: string;
  onBack: () => void;
}

interface HistoryEntry {
  cursor: number;
  kind: string;
  decision: string;
}

export function SessionDetail({ sessionId, onBack }: Props): JSX.Element {
  const [session, setSession] = useState<SessionDetailView | null>(null);
  const [paused, setPaused] = useState<{ cursor: number; kind: string; step: StepPayload } | null>(
    null,
  );
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  // Edit-buffer state — populated when the developer toggles "edit".
  const [editedMessages, setEditedMessages] = useState<string>("");
  const [editedModel, setEditedModel] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const unsubscribeRef = useRef<(() => void) | null>(null);

  // Fetch the session detail once (for the header status/runner/trace).
  useEffect(() => {
    let cancelled = false;
    api
      .getSession(sessionId)
      .then((s) => {
        if (!cancelled) setSession(s);
      })
      .catch((err: ApiError | Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Subscribe to the SSE stream for the session's lifetime.
  useEffect(() => {
    const unsubscribe = api.streamSession(
      sessionId,
      (event: StepEvent) => {
        switch (event.type) {
          case "paused": {
            // Pre-fill the edit buffer with the current values so the dev
            // sees what they're editing, not a blank form.
            const stepPayload = (event.step ?? {}) as StepPayload;
            setPaused({
              cursor: event.cursor,
              kind: event.kind,
              step: stepPayload,
            });
            setEditedMessages(
              stepPayload.messages ? JSON.stringify(stepPayload.messages, null, 2) : "",
            );
            setEditedModel(stepPayload.model ?? "");
            setEditing(false);
            break;
          }
          case "resumed":
            // The decision has been applied; clear the paused step and
            // record the outcome in history.
            setPaused((prev) => {
              if (prev !== null) {
                setHistory((h) => [
                  ...h,
                  { cursor: prev.cursor, kind: prev.kind, decision: event.decision },
                ]);
              }
              return null;
            });
            break;
          case "done":
            setPaused(null);
            setSession((s) => (s ? { ...s, status: "done" } : s));
            break;
          case "errored":
            setPaused(null);
            setError(event.message);
            setSession((s) => (s ? { ...s, status: "errored" } : s));
            break;
        }
      },
      () => {
        // EventSource fires onerror on every reconnect attempt; we only
        // surface a hard failure if the session detail also says it's gone.
      },
    );
    unsubscribeRef.current = unsubscribe;
    return () => {
      unsubscribe();
    };
  }, [sessionId]);

  const postDecision = (body: DecisionRequest) => {
    setBusy(true);
    api
      .decide(sessionId, body)
      .catch((err: ApiError | Error) => setError(err.message))
      .finally(() => setBusy(false));
  };

  const handleApprove = () => postDecision({ kind: "approve" });
  const handleStepOnce = () => postDecision({ kind: "step_once" });
  const handleStop = () => {
    if (!window.confirm("Stop the agent run? This terminates the session.")) return;
    postDecision({ kind: "stop" });
  };

  const handleEditApply = () => {
    let parsedMessages: unknown[] | null = null;
    if (editedMessages.trim() !== "") {
      try {
        parsedMessages = JSON.parse(editedMessages) as unknown[];
      } catch (err) {
        setError(`invalid messages JSON: ${(err as Error).message}`);
        return;
      }
    }
    const body: DecisionRequest = { kind: "edit" };
    if (parsedMessages !== null) body.messages = parsedMessages;
    if (editedModel.trim() !== "") body.model = editedModel.trim();
    postDecision(body);
    setEditing(false);
  };

  const terminal =
    session?.status === "done" || session?.status === "errored";

  return (
    <section className="session-detail">
      <header className="eval-detail__header">
        <button type="button" className="link-button" onClick={onBack}>
          ← sessions
        </button>
        <h2>session {sessionId.slice(0, 8)}</h2>
        {session && (
          <span className={sessionStatusClass(session.status)}>{session.status}</span>
        )}
      </header>

      {session && (
        <dl className="fields">
          <div className="field">
            <dt>runner</dt>
            <dd>
              <code>{session.runner_ref}</code>
            </dd>
          </div>
          <div className="field">
            <dt>trace</dt>
            <dd>
              <code className="muted">{session.trace_id.slice(0, 16)}…</code>
            </dd>
          </div>
          <div className="field">
            <dt>branch</dt>
            <dd>
              <code className="muted">{session.branch_id.slice(0, 16)}…</code>
            </dd>
          </div>
        </dl>
      )}

      {error !== null && (
        <div className="banner banner--error" role="alert">
          {error}
        </div>
      )}

      <div className="session-detail__body">
        <div className="session-detail__step">
          {terminal && paused === null && (
            <p className="muted">
              session {session?.status}.{" "}
              {session?.status === "errored" ? "see the error banner above." : ""}
            </p>
          )}
          {!terminal && paused === null && (
            <p className="muted">waiting for the agent to reach a step…</p>
          )}
          {paused !== null && (
            <StepPanel
              cursor={paused.cursor}
              kind={paused.kind}
              step={paused.step}
              editing={editing}
              editedMessages={editedMessages}
              editedModel={editedModel}
              busy={busy}
              onEditedMessages={setEditedMessages}
              OnEditedModel={setEditedModel}
              onApprove={handleApprove}
              onEditToggle={() => setEditing((e) => !e)}
              onEditApply={handleEditApply}
              onStepOnce={handleStepOnce}
              onStop={handleStop}
            />
          )}
        </div>

        <aside className="session-detail__history">
          <h4>history ({history.length})</h4>
          {history.length === 0 && <p className="muted">no steps yet.</p>}
          <ol className="history-list">
            {history.map((h, i) => (
              <li key={i} className="history-item">
                <span className="muted">#{h.cursor}</span>{" "}
                <code>{h.kind}</code>{" "}
                <span className="pill pill--info">{h.decision}</span>
              </li>
            ))}
          </ol>
        </aside>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Step panel — renders the pending call + decision controls.
// ---------------------------------------------------------------------------

interface StepPanelProps {
  cursor: number;
  kind: string;
  step: StepPayload;
  editing: boolean;
  editedMessages: string;
  editedModel: string;
  busy: boolean;
  onEditedMessages: (v: string) => void;
  OnEditedModel: (v: string) => void;
  onApprove: () => void;
  onEditToggle: () => void;
  onEditApply: () => void;
  onStepOnce: () => void;
  onStop: () => void;
}

function StepPanel(props: StepPanelProps): JSX.Element {
  const {
    cursor,
    kind,
    step,
    editing,
    editedMessages,
    editedModel,
    busy,
    onEditedMessages,
    OnEditedModel,
    onApprove,
    onEditToggle,
    onEditApply,
    onStepOnce,
    onStop,
  } = props;

  return (
    <div className="step-panel">
      <header className="step-panel__header">
        <h3>
          step #{cursor} <span className="muted">({kind})</span>
        </h3>
      </header>

      {!editing && (
        <div className="step-panel__view">
          {step.model && (
            <div className="field">
              <dt>model</dt>
              <dd>
                <code>{step.model}</code>
              </dd>
            </div>
          )}
          {step.messages && step.messages.length > 0 && (
            <div className="messages">
              <h4>messages</h4>
              <ol>
                {step.messages.map((m, i) => (
                  <li key={i}>
                    <MessageView message={m} />
                  </li>
                ))}
              </ol>
            </div>
          )}
          {step.tools && step.tools.length > 0 && (
            <div className="messages">
              <h4>tools</h4>
              <ol>
                {step.tools.map((t, i) => (
                  <li key={i}>
                    <pre className="message-text">{JSON.stringify(t, null, 2)}</pre>
                  </li>
                ))}
              </ol>
            </div>
          )}
          {step.params && Object.keys(step.params).length > 0 && (
            <div className="field">
              <dt>params</dt>
              <dd>
                <pre className="raw-json">
                  {JSON.stringify(step.params, null, 2)}
                </pre>
              </dd>
            </div>
          )}
        </div>
      )}

      {editing && (
        <div className="step-panel__edit">
          <div className="field">
            <dt>model</dt>
            <dd>
              <input
                type="text"
                className="session-list__input"
                value={editedModel}
                onChange={(e) => OnEditedModel(e.target.value)}
              />
            </dd>
          </div>
          <div className="field">
            <dt>
              messages <span className="muted">(JSON)</span>
            </dt>
            <dd>
              <textarea
                className="step-panel__edit-textarea"
                rows={Math.min(20, Math.max(6, editedMessages.split("\n").length + 1))}
                value={editedMessages}
                onChange={(e) => onEditedMessages(e.target.value)}
                spellCheck={false}
              />
            </dd>
          </div>
        </div>
      )}

      <footer className="step-panel__actions">
        {!editing && (
          <>
            <button
              type="button"
              className="link-button"
              onClick={onApprove}
              disabled={busy}
            >
              ✓ approve
            </button>
            <button
              type="button"
              className="link-button"
              onClick={onEditToggle}
              disabled={busy}
            >
              ✎ edit
            </button>
            <button
              type="button"
              className="link-button"
              onClick={onStepOnce}
              disabled={busy}
            >
              ⤓ step once
            </button>
            <button
              type="button"
              className="link-button"
              onClick={onStop}
              disabled={busy}
            >
              ⏹ stop
            </button>
          </>
        )}
        {editing && (
          <>
            <button
              type="button"
              className="link-button"
              onClick={onEditApply}
              disabled={busy}
            >
              ✓ apply edit &amp; continue
            </button>
            <button
              type="button"
              className="link-button"
              onClick={onEditToggle}
              disabled={busy}
            >
              cancel
            </button>
          </>
        )}
      </footer>
    </div>
  );
}

// Renders a single message — handles strings, {role, content} objects, and
// arbitrary JSON. Matches SpanInspector's MessageItem shape (kept inline so
// SessionDetail is self-contained; the SpanInspector version is file-local).
function MessageView({ message }: { message: unknown }): JSX.Element {
  if (typeof message === "string") {
    return <pre className="message-text">{message}</pre>;
  }
  if (message !== null && typeof message === "object") {
    const m = message as { role?: unknown; content?: unknown };
    return (
      <div className="message-obj">
        {typeof m.role === "string" && <span className="message-role">{m.role}</span>}
        <pre className="message-text">
          {typeof m.content === "string"
            ? m.content
            : JSON.stringify(m.content ?? message, null, 2)}
        </pre>
      </div>
    );
  }
  return <pre className="message-text">{String(message)}</pre>;
}
