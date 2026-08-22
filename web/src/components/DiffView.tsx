// DiffView — side-by-side span comparison with token-level message diff.
//
// Loads the span diff for a (leftBranchId, rightBranchId) pair and renders:
//   1. A summary stripe — left/right counts, divergence index, identical badge.
//   2. A pair-of-spans list: each row is either equal (grey), added (green),
//      removed (red), or changed (amber). The first divergent row is marked
//      so the user can spot where the fork actually diverged.
//   3. Per-row "compare messages" toggle: if both spans carry LLM message
//      payloads, the user can expand the row to load the token-level message
//      diff via the ``messageDiff`` endpoint.
//
// Phase 5 exit criteria:
//   * "Diffing two branches marks exactly which span first diverged" → the
//     ``is_first_divergence`` flag on the row drives the "branch point" marker.
//   * "Token-level message diff renders add/remove/change correctly" → the
//     ``message-diff`` panel renders ``added`` in green, ``removed`` in red,
//     and ``changed`` as a strike-through → insert pair.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { kindStyle } from "../styles";
import type {
  MessageDiffView,
  SpanDiffView,
  SpanView,
} from "../types";

interface Props {
  traceId: string;
  leftBranchId: string;
  rightBranchId: string;
}

export function DiffView({
  traceId,
  leftBranchId,
  rightBranchId,
}: Props): JSX.Element {
  const [diff, setDiff] = useState<SpanDiffView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .diffBranches(traceId, leftBranchId, rightBranchId)
      .then((d) => {
        if (!cancelled) setDiff(d);
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
  }, [traceId, leftBranchId, rightBranchId]);

  if (loading) {
    return (
      <section className="diff-view" aria-label="Branch diff">
        <div className="muted">loading diff…</div>
      </section>
    );
  }
  if (error !== null) {
    return (
      <section className="diff-view" aria-label="Branch diff">
        <div className="banner banner--error">{error}</div>
      </section>
    );
  }
  if (diff === null) {
    return (
      <section className="diff-view" aria-label="Branch diff">
        <div className="muted">no diff available.</div>
      </section>
    );
  }

  return (
    <section className="diff-view" aria-label="Branch diff">
      <DiffSummary diff={diff} leftBranchId={leftBranchId} rightBranchId={rightBranchId} />
      {diff.identical ? (
        <div className="banner banner--info">
          branches are identical — no divergence detected.
        </div>
      ) : (
        <ol className="diff-view__pairs">
          {diff.pairs.map((pair) => (
            <DiffRow
              key={pair.index}
              pair={pair}
              leftBranchId={leftBranchId}
              rightBranchId={rightBranchId}
            />
          ))}
        </ol>
      )}
    </section>
  );
}

function DiffSummary({
  diff,
  leftBranchId,
  rightBranchId,
}: {
  diff: SpanDiffView;
  leftBranchId: string;
  rightBranchId: string;
}): JSX.Element {
  return (
    <header className="diff-view__summary">
      <span className="muted">comparing</span>
      <code className="diff-view__branch">{shortUuid(leftBranchId)}</code>
      <span className="muted"> ⇄ </span>
      <code className="diff-view__branch">{shortUuid(rightBranchId)}</code>
      <span className="muted">
        {" · "}
        {diff.left_count} vs {diff.right_count} spans
      </span>
      {diff.first_divergence_index !== null && (
        <span className="muted">
          {" · "}
          divergence at <strong>index {diff.first_divergence_index}</strong>
        </span>
      )}
    </header>
  );
}

function DiffRow({
  pair,
  leftBranchId,
  rightBranchId,
}: {
  pair: SpanDiffView["pairs"][number];
  leftBranchId: string;
  rightBranchId: string;
}): JSX.Element {
  const [showMessageDiff, setShowMessageDiff] = useState(false);
  const className = `diff-view__row diff-view__row--${pair.status}`;
  const isDiffable = pair.left !== null && pair.right !== null;

  return (
    <li className={className}>
      {pair.is_first_divergence && (
        <div className="diff-view__divergence" title="first divergence">
          ⟶ branch point
        </div>
      )}
      <div className="diff-view__row-main">
        <span className="diff-view__index muted">#{pair.index}</span>
        <span className="diff-view__status">
          {STATUS_GLYPH[pair.status]} {pair.status}
        </span>
        <DiffSpanColumn
          label="L"
          span={pair.left}
          branchId={leftBranchId}
        />
        <DiffSpanColumn
          label="R"
          span={pair.right}
          branchId={rightBranchId}
        />
        {isDiffable && (
          <button
            type="button"
            className="link-button diff-view__msg-toggle"
            onClick={() => setShowMessageDiff((v) => !v)}
          >
            {showMessageDiff ? "hide msg diff" : "msg diff"}
          </button>
        )}
      </div>
      {showMessageDiff && isDiffable && pair.left !== null && pair.right !== null && (
        <MessageDiffBlock left={pair.left} right={pair.right} />
      )}
    </li>
  );
}

const STATUS_GLYPH: Record<SpanDiffView["pairs"][number]["status"], string> = {
  equal: "=",
  added: "+",
  removed: "−",
  changed: "≠",
};

function DiffSpanColumn({
  label,
  span,
  branchId,
}: {
  label: string;
  span: SpanView | null;
  branchId: string;
}): JSX.Element {
  if (span === null) {
    return (
      <span className="diff-view__span diff-view__span--missing">
        <span className="muted">{label}:</span>{" "}
        <span className="muted">—</span>
      </span>
    );
  }
  const style = kindStyle(span.kind);
  return (
    <span className="diff-view__span">
      <span className="muted">{label}:</span>{" "}
      <span
        className="kind-pill"
        style={{ background: style.swatch }}
        title={`branch ${shortUuid(branchId)}`}
      >
        {style.label}
      </span>{" "}
      <code className="diff-view__span-name">{span.name}</code>
    </span>
  );
}

function MessageDiffBlock({
  left,
  right,
}: {
  left: SpanView;
  right: SpanView;
}): JSX.Element {
  const [diff, setDiff] = useState<MessageDiffView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .messageDiff(left.timetravel_id, right.timetravel_id)
      .then((d) => {
        if (!cancelled) setDiff(d);
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
  }, [left.timetravel_id, right.timetravel_id]);

  if (loading) {
    return <div className="diff-view__msg-body muted">computing message diff…</div>;
  }
  if (error !== null) {
    return <div className="banner banner--error">{error}</div>;
  }
  if (diff === null) {
    return <div className="muted">no message diff.</div>;
  }
  if (diff.identical) {
    return <div className="muted">messages identical.</div>;
  }
  return (
    <div className="diff-view__msg-body">
      <p className="muted">
        +{diff.added_tokens} / −{diff.removed_tokens} word tokens
      </p>
      <p className="message-diff-text">
        {diff.fragments.map((f, i) => (
          <FragmentSpan key={i} fragment={f} />
        ))}
      </p>
    </div>
  );
}

function FragmentSpan({
  fragment,
}: {
  fragment: MessageDiffView["fragments"][number];
}): JSX.Element {
  const { text, kind } = fragment;
  if (kind === "equal") {
    return <span>{text}</span>;
  }
  if (kind === "added") {
    return <ins className="message-diff__ins">{text}</ins>;
  }
  if (kind === "removed") {
    return <del className="message-diff__del">{text}</del>;
  }
  // changed: render as a remove→add pair so both sides of the substitution
  // are visible inline.
  return (
    <span className="message-diff__changed">
      <del>{fragment.removed}</del>
      <ins>{fragment.added}</ins>
    </span>
  );
}

function shortUuid(id: string): string {
  // First 8 chars are enough for human eyeballing; UUIDs are
  // statistically-unique in that prefix.
  return id.length <= 8 ? id : `${id.slice(0, 8)}…`;
}
