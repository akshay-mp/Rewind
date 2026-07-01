// Timeline view — the core Phase 2 deliverable.
//
// Renders spans as horizontal bars laid out against a shared time axis,
// colour-coded by kind. Spans with a parent are visually nested; root spans
// (parent_span_id is null) start new swim-lanes. Clicking a bar opens the
// inspector for that span. A filter rail on the left lets the user narrow by
// kind / model / status / free text search.
//
// Phase 5 added a "branch&nbsp;⎇" toggle that swaps the canvas for a
// BranchTree + DiffView panel. The tree lets users pick two branches to
// compare; the diff view shows the side-by-side span comparison with
// token-level message diffs.

import { useEffect, useMemo, useState } from "react";
import { ApiError, api } from "../api";
import { formatDuration, formatTimestamp, kindStyle, statusStyle } from "../styles";
import type {
  BranchNodeView,
  CreateBranchRequest,
  SpanKind,
  SpanStatus,
  SpanView,
  TraceDetail,
} from "../types";
import { BranchTree } from "./BranchTree";
import { DiffView } from "./DiffView";

interface Props {
  traceId: string;
  onBack: () => void;
  onSelectSpan: (rewindId: string) => void;
  selectedRewindId: string | null;
}

interface Filters {
  kind: SpanKind | "";
  model: string;
  status: SpanStatus | "";
  text: string;
  parentOnly: boolean;
}

const DEFAULT_FILTERS: Filters = {
  kind: "",
  model: "",
  status: "",
  text: "",
  parentOnly: false,
};

const KIND_OPTIONS: SpanKind[] = [
  "gen_ai.agent",
  "gen_ai.llm",
  "gen_ai.tool",
  "gen_ai.mcp",
  "rewind.unknown",
];
const STATUS_OPTIONS: SpanStatus[] = ["OK", "ERROR", "UNSET"];

type Mode = "timeline" | "branches";

interface ForkFormState {
  parent: BranchNodeView;
  label: string;
  branchAtIndex: number;
}

export function Timeline({ traceId, onBack, onSelectSpan, selectedRewindId }: Props): JSX.Element {
  const [trace, setTrace] = useState<TraceDetail | null>(null);
  const [filters, setFilters] = useState<Filters>(DEFAULT_FILTERS);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Phase 5 — branch & diff state.
  const [mode, setMode] = useState<Mode>("timeline");
  const [leftBranchId, setLeftBranchId] = useState<string | null>(null);
  const [rightBranchId, setRightBranchId] = useState<string | null>(null);
  const [forkForm, setForkForm] = useState<ForkFormState | null>(null);
  const [forkStatus, setForkStatus] = useState<
    | { kind: "idle" }
    | { kind: "saving" }
    | { kind: "error"; message: string }
  >({ kind: "idle" });
  // Bumped after a successful createBranch so the BranchTree refetches.
  const [branchRevision, setBranchRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getTrace(traceId)
      .then((detail) => {
        if (!cancelled) setTrace(detail);
      })
      .catch((err: ApiError | Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [traceId]);

  const filteredSpans = useMemo<SpanView[]>(() => {
    if (trace === null) return [];
    const q = filters.text.trim().toLowerCase();
    return trace.spans.filter((span) => {
      if (filters.kind !== "" && span.kind !== filters.kind) return false;
      if (filters.status !== "" && span.status !== filters.status) return false;
      if (filters.parentOnly && span.parent_span_id !== null) return false;
      if (filters.model.trim() !== "") {
        const needle = filters.model.trim().toLowerCase();
        if (span.model_name === null || !span.model_name.toLowerCase().includes(needle)) {
          return false;
        }
      }
      if (q !== "") {
        const haystack = (
          span.name +
          " " +
          (span.status_message ?? "") +
          " " +
          JSON.stringify(span.raw_attributes)
        ).toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });
  }, [trace, filters]);

  return (
    <section className="timeline">
      <header className="timeline__header">
        <button type="button" className="link-button" onClick={onBack}>
          ← traces
        </button>
        <h2>
          trace <code>{traceId.slice(0, 16)}…</code>
        </h2>
        {trace !== null && (
          <span className="muted">
            {filteredSpans.length} / {trace.spans.length} spans · created{" "}
            {formatTimestamp(trace.created_at)}
          </span>
        )}
        <nav className="timeline__mode-toggle">
          <button
            type="button"
            className={mode === "timeline" ? "tab tab--active" : "tab"}
            onClick={() => setMode("timeline")}
          >
            timeline
          </button>
          <button
            type="button"
            className={mode === "branches" ? "tab tab--active" : "tab"}
            onClick={() => setMode("branches")}
            title="Compare branches"
          >
            branches ⎇
          </button>
        </nav>
      </header>

      {error !== null && <div className="banner banner--error">{error}</div>}
      {loading && <div className="muted">loading…</div>}
      {!loading && error === null && trace !== null && mode === "timeline" && (
        <>
          <FilterRail filters={filters} onChange={setFilters} />
          <TimelineCanvas
            spans={filteredSpans}
            onSelectSpan={onSelectSpan}
            selectedRewindId={selectedRewindId}
          />
        </>
      )}
      {!loading && error === null && trace !== null && mode === "branches" && (
        <div className="branch-panel">
          <BranchTree
            // ``branchRevision`` is in the key so a successful fork remounts
            // the tree (cheap GET, guarantees a freshly sorted view).
            key={`${traceId}-${branchRevision}`}
            traceId={traceId}
            leftBranchId={leftBranchId}
            rightBranchId={rightBranchId}
            onPickLeft={setLeftBranchId}
            onPickRight={setRightBranchId}
            onBranchFrom={(node) => {
              setForkForm({
                parent: node,
                label: defaultForkLabel(node),
                branchAtIndex: node.branch_at_index ?? 0,
              });
              setForkStatus({ kind: "idle" });
            }}
          />
          {leftBranchId !== null && rightBranchId !== null ? (
            <DiffView
              traceId={traceId}
              leftBranchId={leftBranchId}
              rightBranchId={rightBranchId}
            />
          ) : (
            <div className="branch-panel__hint muted">
              Pick a left branch (← button) and a right branch (→ button)
              to see the side-by-side diff.
            </div>
          )}
          {forkForm !== null && (
            <ForkBranchModal
              traceId={traceId}
              form={forkForm}
              status={forkStatus}
              onChangeLabel={(label) =>
                setForkForm((prev) =>
                  prev === null ? prev : { ...prev, label },
                )
              }
              onChangeIndex={(branchAtIndex) =>
                setForkForm((prev) =>
                  prev === null ? prev : { ...prev, branchAtIndex },
                )
              }
              onCancel={() => {
                setForkForm(null);
                setForkStatus({ kind: "idle" });
              }}
              onSubmit={async () => {
                setForkStatus({ kind: "saving" });
                const body: CreateBranchRequest = {
                  trace_id: traceId,
                  parent_branch_id: forkForm.parent.branch_id,
                  branch_at_index: forkForm.branchAtIndex,
                  mode: "manual",
                  label: forkForm.label,
                };
                try {
                  await api.createBranch(traceId, body);
                  setForkStatus({ kind: "idle" });
                  setForkForm(null);
                  setBranchRevision((n) => n + 1);
                } catch (err) {
                  setForkStatus({
                    kind: "error",
                    message: err instanceof ApiError ? err.message : String(err),
                  });
                }
              }}
            />
          )}
        </div>
      )}
    </section>
  );
}

function defaultForkLabel(node: BranchNodeView): string {
  // Suggest "fork of <label> @ <index>" so the user has something to edit.
  return `fork of ${node.label} @ ${node.branch_at_index ?? 0}`;
}

interface ForkBranchModalProps {
  traceId: string;
  form: ForkFormState;
  status: { kind: "idle" } | { kind: "saving" } | { kind: "error"; message: string };
  onChangeLabel: (label: string) => void;
  onChangeIndex: (index: number) => void;
  onCancel: () => void;
  onSubmit: () => void;
}

function ForkBranchModal({
  traceId: _traceId,
  form,
  status,
  onChangeLabel,
  onChangeIndex,
  onCancel,
  onSubmit,
}: ForkBranchModalProps): JSX.Element {
  // ``_traceId`` unused locally — kept on the props so the modal's POST is
  // trace-scoped at the call-site (where the closure builds the body).
  void _traceId;
  return (
    <div className="modal-backdrop" role="dialog" aria-label="Fork branch">
      <div className="modal">
        <header className="modal__header">
          <h3>fork branch</h3>
          <button type="button" className="link-button" onClick={onCloseIfIdle(status, onCancel)}>
            close ✕
          </button>
        </header>
        <p className="muted">
          off <code>{form.parent.label}</code> @ index {form.parent.branch_at_index ?? 0}
        </p>
        <label className="modal__field">
          label
          <input
            type="text"
            value={form.label}
            onChange={(e) => onChangeLabel(e.target.value)}
            disabled={status.kind === "saving"}
          />
        </label>
        <label className="modal__field">
          branch at index (inclusive of parent)
          <input
            type="number"
            min={0}
            value={form.branchAtIndex}
            onChange={(e) => onChangeIndex(Number.parseInt(e.target.value, 10) || 0)}
            disabled={status.kind === "saving"}
          />
        </label>
        {status.kind === "error" && (
          <div className="banner banner--error">{status.message}</div>
        )}
        <footer className="modal__footer">
          <button
            type="button"
            className="link-button"
            onClick={onCancel}
            disabled={status.kind === "saving"}
          >
            cancel
          </button>
          <button
            type="button"
            className="primary-button"
            onClick={onSubmit}
            disabled={status.kind === "saving" || form.label.trim() === ""}
          >
            {status.kind === "saving" ? "forking…" : "fork"}
          </button>
        </footer>
      </div>
    </div>
  );
}

// When a save is in flight we don't want the ✕ to dismiss the modal —
// otherwise the user has no way to see the response. ``onCloseIfIdle`` returns
// a handler that no-ops while saving.
function onCloseIfIdle(
  status: ForkBranchModalProps["status"],
  onCancel: () => void,
): () => void {
  return () => {
    if (status.kind !== "saving") onCancel();
  };
}

function FilterRail({
  filters,
  onChange,
}: {
  filters: Filters;
  onChange: (next: Filters) => void;
}): JSX.Element {
  const update = <K extends keyof Filters>(key: K, value: Filters[K]): void => {
    onChange({ ...filters, [key]: value });
  };

  return (
    <div className="filter-rail">
      <label>
        kind
        <select
          value={filters.kind}
          onChange={(e) => update("kind", e.target.value as Filters["kind"])}
        >
          <option value="">(all)</option>
          {KIND_OPTIONS.map((k) => (
            <option key={k} value={k}>
              {kindStyle(k).label}
            </option>
          ))}
        </select>
      </label>

      <label>
        status
        <select
          value={filters.status}
          onChange={(e) => update("status", e.target.value as Filters["status"])}
        >
          <option value="">(all)</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>

      <label>
        model (substring)
        <input
          type="text"
          placeholder="qwen3 / gpt-4o / …"
          value={filters.model}
          onChange={(e) => update("model", e.target.value)}
        />
      </label>

      <label>
        search
        <input
          type="text"
          placeholder="name, status_message, raw attributes…"
          value={filters.text}
          onChange={(e) => update("text", e.target.value)}
        />
      </label>

      <label className="checkbox">
        <input
          type="checkbox"
          checked={filters.parentOnly}
          onChange={(e) => update("parentOnly", e.target.checked)}
        />
        root spans only
      </label>
    </div>
  );
}

interface TimelineCanvasProps {
  spans: SpanView[];
  onSelectSpan: (rewindId: string) => void;
  selectedRewindId: string | null;
}

function TimelineCanvas({
  spans,
  onSelectSpan,
  selectedRewindId,
}: TimelineCanvasProps): JSX.Element {
  if (spans.length === 0) {
    return <div className="muted timeline__empty">no spans match filters.</div>;
  }

  const { originMs, endMs } = computeWindow(spans);
  const windowMs = Math.max(1, endMs - originMs);

  // Group spans into swim-lanes by walking parent_span_id. Root spans start a
  // lane; children inherit the parent's lane. This gives the nested view that
  // matches OTel's tree semantics without a layout library.
  const lanes = computeLanes(spans);

  return (
    <div className="timeline__canvas" role="grid">
      <div className="timeline__axis">
        <span>{formatTimestamp(new Date(originMs).toISOString())}</span>
        <span className="muted">{formatDuration(new Date(originMs).toISOString(), new Date(endMs).toISOString())}</span>
        <span>{formatTimestamp(new Date(endMs).toISOString())}</span>
      </div>
      <div className="timeline__lanes">
        {lanes.map((lane, laneIdx) => (
          <div key={laneIdx} className="timeline__lane">
            {lane.map((span) => {
              const startMs = Date.parse(span.start_time);
              const stopMs = Date.parse(span.end_time);
              const leftPct = ((startMs - originMs) / windowMs) * 100;
              const widthPct = Math.max(0.5, ((stopMs - startMs) / windowMs) * 100);
              const ks = kindStyle(span.kind);
              const isSelected = selectedRewindId === span.rewind_id;
              return (
                <button
                  key={span.rewind_id}
                  type="button"
                  className="span-bar"
                  style={{
                    left: `${leftPct}%`,
                    width: `${widthPct}%`,
                    background: ks.swatch,
                    borderColor: isSelected
                      ? "var(--rewind-selected)"
                      : statusStyle(span.status) === "var(--rewind-status-error)"
                        ? "var(--rewind-selected)"
                        : ks.border,
                    boxShadow: isSelected ? "0 0 0 2px var(--rewind-selected)" : "none",
                  }}
                  title={`${ks.label}: ${span.name} (${formatDuration(span.start_time, span.end_time)})`}
                  onClick={() => onSelectSpan(span.rewind_id)}
                >
                  <span className="span-bar__label">{ks.label}</span>
                  <span className="span-bar__name">{span.name}</span>
                </button>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

function computeWindow(spans: SpanView[]): { originMs: number; endMs: number } {
  let origin = Number.POSITIVE_INFINITY;
  let end = Number.NEGATIVE_INFINITY;
  for (const span of spans) {
    const startMs = Date.parse(span.start_time);
    const stopMs = Date.parse(span.end_time);
    if (startMs < origin) origin = startMs;
    if (stopMs > end) end = stopMs;
  }
  if (!Number.isFinite(origin) || !Number.isFinite(end)) {
    return { originMs: 0, endMs: 1 };
  }
  return { originMs: origin, endMs: end };
}

function computeLanes(spans: SpanView[]): SpanView[][] {
  // Map span_id -> lane index; roots start at lane 0, children inherit the
  // parent's lane. Returns one child-array per lane.
  const byId = new Map<string, SpanView>();
  for (const span of spans) byId.set(span.span_id, span);
  const lanes: SpanView[][] = [];
  const laneOf = new Map<string, number>();

  const laneFor = (span: SpanView): number => {
    const cached = laneOf.get(span.span_id);
    if (cached !== undefined) return cached;
    let lane = 0;
    if (span.parent_span_id !== null && byId.has(span.parent_span_id)) {
      lane = laneFor(byId.get(span.parent_span_id) as SpanView);
    } else {
      lane = lanes.length;
    }
    laneOf.set(span.span_id, lane);
    while (lanes.length <= lane) lanes.push([]);
    return lane;
  };

  // Render order: parents before children so lane assignment is stable.
  for (const span of spans) laneFor(span);
  for (const span of spans) {
    const lane = laneOf.get(span.span_id) ?? 0;
    lanes[lane].push(span);
  }
  return lanes;
}
