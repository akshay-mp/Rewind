// Trace list view — the landing page of the timeline UI.
//
// Fetches /api/v1/traces and renders a paginated table. Clicking a row
// navigates to the timeline view for that trace.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { kindStyle, statusStyle } from "../styles";
import type { TraceSummary } from "../types";

const PAGE_SIZE = 25;

interface Props {
  onOpenTrace: (traceId: string) => void;
}

export function TraceList({ onOpenTrace }: Props): JSX.Element {
  const [items, setItems] = useState<TraceSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listTraces({ limit: PAGE_SIZE, offset })
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
  }, [offset]);

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  return (
    <section className="trace-list">
      <header className="trace-list__header">
        <h2>Traces</h2>
        <span className="muted" aria-live="polite">
          {total === 0 ? "no traces yet" : `${total} trace${total === 1 ? "" : "s"}`}
        </span>
      </header>

      {error !== null && <div className="banner banner--error">{error}</div>}
      {loading && <div className="muted">loading…</div>}
      {!loading && error === null && items.length === 0 && (
        <div className="banner banner--hint">
          <p>No traces ingested yet.</p>
          <p className="muted">
            Ship OTLP/HTTP to <code>POST /v1/traces</code> from an
            OpenInference-instrumented agent. See <code>docs/wiring/</code>.
          </p>
        </div>
      )}

      {items.length > 0 && (
        <table className="trace-table">
          <thead>
            <tr>
              <th>Trace ID</th>
              <th>Created</th>
              <th>Spans</th>
              <th>Kinds</th>
              <th>Models</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr
                key={t.trace_id}
                onClick={() => onOpenTrace(t.trace_id)}
                className="trace-table__row"
              >
                <td>
                  <code className="trace-id">{t.trace_id.slice(0, 16)}…</code>
                </td>
                <td className="muted">{t.created_at.replace("T", " ").replace(/\.\d.*$/, "")}</td>
                <td>{t.span_count}</td>
                <td>
                  <KindBadges counts={t.span_count_by_kind} />
                </td>
                <td>
                  {t.model_names.length === 0 ? (
                    <span className="muted">—</span>
                  ) : (
                    t.model_names.join(", ")
                  )}
                </td>
                <td>
                  <StatusDot hasError={t.has_error} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {total > PAGE_SIZE && (
        <nav className="pagination">
          <button
            type="button"
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
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            next →
          </button>
        </nav>
      )}
    </section>
  );
}

function KindBadges({ counts }: { counts: Record<string, number> }): JSX.Element {
  const entries = Object.entries(counts);
  if (entries.length === 0) return <span className="muted">—</span>;
  return (
    <span className="kind-badges">
      {entries.map(([kind, count]) => (
        <span key={kind} className="kind-badge" style={{ background: kindStyle(kind as never).swatch }}>
          {kindStyle(kind as never).label} {count}
        </span>
      ))}
    </span>
  );
}

function StatusDot({ hasError }: { hasError: boolean }): JSX.Element {
  return (
    <span
      className="status-dot"
      style={{ background: statusStyle(hasError ? "ERROR" : "OK") }}
      aria-label={hasError ? "has error" : "ok"}
    />
  );
}
