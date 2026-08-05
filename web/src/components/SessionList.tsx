// Phase 9 — interactive stepping session list + new-session entry point.
//
// Mirrors EvalRuns.tsx in structure: fetch-on-mount, paginated table,
// per-row "open" button. The top of the view has a small form to start a
// new session — trace_id + runner_ref are the minimum the server needs;
// mode defaults to "interactive" (see stepping_api.StartSessionRequest).

import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { sessionStatusClass } from "../styles";
import type { SessionDetailView } from "../types";

const PAGE_SIZE = 25;

interface Props {
  onOpenSession: (sessionId: string) => void;
}

export function SessionList({ onOpenSession }: Props): JSX.Element {
  const [items, setItems] = useState<SessionDetailView[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // New-session form state.
  const [traceId, setTraceId] = useState("");
  const [runnerRef, setRunnerRef] = useState("");
  const [starting, setStarting] = useState(false);

  const refresh = (off: number) => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listSessions({ limit: PAGE_SIZE, offset: off })
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setTotal(res.total);
      })
      .catch((err: ApiError | Error) => {
        if (cancelled) return;
        setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  };

  useEffect(() => refresh(offset), [offset]);

  const startSession = (e: React.FormEvent) => {
    e.preventDefault();
    if (traceId.trim() === "" || runnerRef.trim() === "") return;
    setStarting(true);
    setError(null);
    api
      .startSession({ trace_id: traceId.trim(), runner_ref: runnerRef.trim() })
      .then((res) => {
        setTraceId("");
        setRunnerRef("");
        onOpenSession(res.session_id);
      })
      .catch((err: ApiError | Error) => setError(err.message))
      .finally(() => setStarting(false));
  };

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <section className="session-list">
      <header className="eval-detail__header">
        <h2>interactive sessions</h2>
        <span className="muted">{total} total</span>
      </header>

      <form className="session-list__form" onSubmit={startSession}>
        <input
          type="text"
          placeholder="trace id (32-hex)"
          value={traceId}
          onChange={(e) => setTraceId(e.target.value)}
          className="session-list__input"
          aria-label="trace id"
        />
        <input
          type="text"
          placeholder="runner ref"
          value={runnerRef}
          onChange={(e) => setRunnerRef(e.target.value)}
          className="session-list__input"
          aria-label="runner ref"
        />
        <button
          type="submit"
          className="link-button"
          disabled={starting || traceId.trim() === "" || runnerRef.trim() === ""}
        >
          {starting ? "starting…" : "start session →"}
        </button>
      </form>

      {error !== null && (
        <div className="banner banner--error" role="alert">
          {error}
        </div>
      )}
      {loading && <p className="muted">loading…</p>}
      {!loading && items.length === 0 && error === null && (
        <p className="muted">
          no sessions yet. Register a runner with{" "}
          <code>rewind.stepping_api.register_runner</code> and start one above.
        </p>
      )}

      {items.length > 0 && (
        <table className="eval-list__table">
          <thead>
            <tr>
              <th>status</th>
              <th>runner</th>
              <th>trace</th>
              <th>created</th>
              <th>session</th>
              <th aria-label="open"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.session_id}>
                <td>
                  <span className={sessionStatusClass(s.status)}>{s.status}</span>
                </td>
                <td>{s.runner_ref}</td>
                <td>
                  <code className="muted">{s.trace_id.slice(0, 8)}</code>
                </td>
                <td>{s.created_at}</td>
                <td>
                  <code className="muted">{s.session_id.slice(0, 8)}</code>
                </td>
                <td>
                  <button
                    type="button"
                    className="eval-row__open"
                    onClick={() => onOpenSession(s.session_id)}
                  >
                    open →
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {pageCount > 1 && (
        <footer className="pager">
          <button
            type="button"
            className="link-button"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            ← prev
          </button>
          <span className="muted">
            page {currentPage} / {pageCount}
          </span>
          <button
            type="button"
            className="link-button"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            next →
          </button>
        </footer>
      )}
    </section>
  );
}
