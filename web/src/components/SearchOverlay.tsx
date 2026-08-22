// Search overlay — full-store text search, accessed from the trace list.
//
// Hits span traces, so they're grouped by trace_id in the result list. Click
// a hit to open the trace timeline with the span selected.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { kindStyle } from "../styles";
import type { SpanSearchHit } from "../types";

interface Props {
  onClose: () => void;
  onSelectResult: (traceId: string, timetravelId: string) => void;
}

export function SearchOverlay({ onClose, onSelectResult }: Props): JSX.Element {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [results, setResults] = useState<SpanSearchHit[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (query.trim() === "") {
      setResults([]);
      setError(null);
      return;
    }
    let cancelled = false;
    const handle = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      api
        .search(query.trim(), {
          kind: kind || undefined,
          status: status || undefined,
        })
        .then((res) => {
          if (!cancelled) setResults(res.items);
        })
        .catch((err: ApiError | Error) => {
          if (!cancelled) setError(err.message);
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });
    }, 250); // debounce — keep the server out of the way while typing.
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [query, kind, status]);

  return (
    <div className="overlay" role="dialog" aria-label="Search spans">
      <div className="overlay__panel">
        <header className="overlay__header">
          <h3>search</h3>
          <button type="button" className="link-button" onClick={onClose}>
            close ✕
          </button>
        </header>

        <input
          type="search"
          autoFocus
          placeholder='where did the agent say "…"?'
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="search-input"
        />

        <div className="search-filters">
          <label>
            kind
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">(any)</option>
              <option value="gen_ai.llm">LLM</option>
              <option value="gen_ai.tool">Tool</option>
              <option value="gen_ai.mcp">MCP</option>
              <option value="gen_ai.agent">Agent</option>
              <option value="timetravel.unknown">Unknown</option>
            </select>
          </label>
          <label>
            status
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">(any)</option>
              <option value="OK">OK</option>
              <option value="ERROR">ERROR</option>
              <option value="UNSET">UNSET</option>
            </select>
          </label>
        </div>

        {error !== null && <div className="banner banner--error">{error}</div>}
        {loading && <div className="muted">searching…</div>}
        {!loading && query.trim() !== "" && results.length === 0 && (
          <div className="muted">no spans matched.</div>
        )}

        <ul className="search-results">
          {results.map((hit) => (
            <li key={hit.timetravel_id}>
              <button
                type="button"
                className="search-hit"
                onClick={() => onSelectResult(hit.trace_id, hit.timetravel_id)}
              >
                <span
                  className="kind-pill"
                  style={{ background: kindStyle(hit.kind).swatch }}
                >
                  {kindStyle(hit.kind).label}
                </span>
                <span className="search-hit__name">{hit.name}</span>
                <code className="muted">{hit.trace_id.slice(0, 12)}…</code>
                <pre className="snippet">{hit.snippet}</pre>
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
