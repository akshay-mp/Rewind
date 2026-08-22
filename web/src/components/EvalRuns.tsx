// Phase 5.5 — eval run list.
//
// Shows every persisted eval run newest-first, with a per-row "open"
// button. Clicking the button navigates to the per-run detail view;
// we don't do meta-click or row-click, per repo convention, so the
// discoverable affordance is always a visible button.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { evalVerdictClass } from "../styles";
import type { EvalRunSummaryView } from "../types";

const PAGE_SIZE = 25;

interface Props {
  onOpenRun: (runId: string) => void;
}

export function EvalRuns({ onOpenRun }: Props): JSX.Element {
  const [items, setItems] = useState<EvalRunSummaryView[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listEvalRuns({ limit: PAGE_SIZE, offset })
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
    <section className="eval-list">
      <header className="eval-detail__header">
        <h2>eval runs</h2>
        <span className="muted">{total} total</span>
      </header>

      {error !== null && (
        <div className="banner banner--error" role="alert">
          {error}
        </div>
      )}
      {loading && <p className="muted">loading…</p>}
      {!loading && items.length === 0 && error === null && (
        <p className="muted">
          no eval runs yet. Submit a suite via{" "}
          <code>POST /api/v1/evals</code> or the <code>agent-timetravel eval</code> CLI.
        </p>
      )}

      {items.length > 0 && (
        <table className="eval-list__table">
          <thead>
            <tr>
              <th>verdict</th>
              <th>suite</th>
              <th>started</th>
              <th>finished</th>
              <th>run id</th>
              <th aria-label="open"></th>
            </tr>
          </thead>
          <tbody>
            {items.map((run) => (
              <tr key={run.run_id}>
                <td>
                  <span className={evalVerdictClass(run.overall_verdict)}>
                    {run.overall_verdict}
                  </span>
                </td>
                <td>{run.suite_name}</td>
                <td>{run.started_at}</td>
                <td>{run.finished_at}</td>
                <td>
                  <code className="muted">{run.run_id.slice(0, 8)}</code>
                </td>
                <td>
                  <button
                    type="button"
                    className="eval-row__open"
                    onClick={() => onOpenRun(run.run_id)}
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
