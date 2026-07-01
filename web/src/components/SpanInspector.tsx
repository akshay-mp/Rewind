// Span inspector — slide-over panel that renders the selected span.
//
// Two tabs: "Structured" (the projected SpanView fields) and "Raw JSON" (the
// verbatim raw_attributes blob). Messages from llm.input_messages /
// gen_ai.prompt / gen_ai.input.messages are rendered readably; everything
// else is left as-is so we never silently drop data.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { formatDuration, formatTimestamp, kindStyle, statusStyle } from "../styles";
import type { SpanView } from "../types";

interface Props {
  rewindId: string;
  onClose: () => void;
  /**
   * Optional Phase 5 hook — switches the parent view to the branch panel.
   * Wired when the inspector is rendered inside a trace timeline view
   * (in other contexts, e.g. search result, it's omitted).
   */
  onViewBranches?: () => void;
}

type Tab = "structured" | "raw";

export function SpanInspector({ rewindId, onClose, onViewBranches }: Props): JSX.Element {
  const [span, setSpan] = useState<SpanView | null>(null);
  const [tab, setTab] = useState<Tab>("structured");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getSpan(rewindId)
      .then((s) => {
        if (!cancelled) setSpan(s);
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
  }, [rewindId]);

  return (
    <aside className="inspector" role="dialog" aria-label="Span inspector">
      <header className="inspector__header">
        <h3>span</h3>
        <button type="button" className="link-button" onClick={onClose}>
          close ✕
        </button>
      </header>

      {error !== null && <div className="banner banner--error">{error}</div>}
      {loading && <div className="muted">loading…</div>}
      {!loading && span !== null && (
        <>
          <div className="inspector__summary">
            <span className="kind-pill" style={{ background: kindStyle(span.kind).swatch }}>
              {kindStyle(span.kind).label}
            </span>
            <code className="muted">{span.name}</code>
            <span
              className="status-pill"
              style={{
                color: statusStyle(span.status),
                borderColor: statusStyle(span.status),
              }}
            >
              {span.status}
            </span>
          </div>

          <nav className="inspector__tabs">
            <button
              type="button"
              className={tab === "structured" ? "tab tab--active" : "tab"}
              onClick={() => setTab("structured")}
            >
              structured
            </button>
            <button
              type="button"
              className={tab === "raw" ? "tab tab--active" : "tab"}
              onClick={() => setTab("raw")}
            >
              raw JSON
            </button>
          </nav>

          {tab === "structured" ? (
            <StructuredView span={span} onViewBranches={onViewBranches} />
          ) : (
            <RawView raw={span.raw_attributes} />
          )}
        </>
      )}
    </aside>
  );
}

function StructuredView({ span, onViewBranches }: { span: SpanView; onViewBranches: (() => void) | undefined }): JSX.Element {
  const messages = extractMessages(span.raw_attributes);
  const tools = extractTools(span.raw_attributes);
  return (
    <div className="inspector__body">
      <Field label="span_id" value={<code>{span.span_id}</code>} />
      {span.parent_span_id !== null && (
        <Field label="parent" value={<code>{span.parent_span_id}</code>} />
      )}
      <Field label="start" value={formatTimestamp(span.start_time)} />
      <Field label="end" value={formatTimestamp(span.end_time)} />
      <Field label="duration" value={formatDuration(span.start_time, span.end_time)} />
      {span.status_message !== null && (
        <Field label="status_message" value={<code>{span.status_message}</code>} />
      )}
      {span.model_name !== null && <Field label="model" value={span.model_name} />}
      {span.prompt_tokens !== null && (
        <Field label="prompt tokens" value={String(span.prompt_tokens)} />
      )}
      {span.completion_tokens !== null && (
        <Field label="completion tokens" value={String(span.completion_tokens)} />
      )}
      {span.total_tokens !== null && (
        <Field label="total tokens" value={String(span.total_tokens)} />
      )}
      {span.messages_hash !== null && (
        <Field
          label="messages_hash"
          value={<code className="hash">{span.messages_hash.slice(0, 12)}…</code>}
        />
      )}
      {span.tools_hash !== null && (
        <Field
          label="tools_hash"
          value={<code className="hash">{span.tools_hash.slice(0, 12)}…</code>}
        />
      )}
      {span.branch_id !== null && onViewBranches !== undefined && (
        <div className="inspector__actions">
          <button
            type="button"
            className="link-button"
            onClick={onViewBranches}
            title="Switch to the branch panel for this trace"
          >
            view branches / diff ⎇
          </button>
        </div>
      )}
      {messages.length > 0 && <MessagesPanel messages={messages} />}
      {tools.length > 0 && <ToolsPanel tools={tools} />}
    </div>
  );
}

function RawView({ raw }: { raw: Record<string, unknown> }): JSX.Element {
  return (
    <pre className="raw-json" aria-label="Raw attributes JSON">
      {JSON.stringify(raw, null, 2)}
    </pre>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }): JSX.Element {
  return (
    <div className="field">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function MessagesPanel({ messages }: { messages: unknown[] }): JSX.Element {
  return (
    <div className="messages">
      <h4>messages</h4>
      <ol>
        {messages.map((m, i) => (
          <li key={i}>
            <MessageItem message={m} />
          </li>
        ))}
      </ol>
    </div>
  );
}

function MessageItem({ message }: { message: unknown }): JSX.Element {
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

function ToolsPanel({ tools }: { tools: unknown[] }): JSX.Element {
  return (
    <div className="messages">
      <h4>tools</h4>
      <ol>
        {tools.map((t, i) => (
          <li key={i}>
            <pre className="message-text">{JSON.stringify(t, null, 2)}</pre>
          </li>
        ))}
      </ol>
    </div>
  );
}

// --- message / tool extraction -----------------------------------------------

// These keys are the union of OpenInference and GenAI semconv conventions we
// know the Phase 1 ingest layer sees. Unknown shapes fall back gracefully.
const MESSAGE_KEYS = [
  "llm.input_messages",
  "llm.output_messages",
  "gen_ai.prompt",
  "gen_ai.completion",
  "gen_ai.input.messages",
  "gen_ai.output.messages",
] as const;

const TOOL_KEYS = ["llm.tools", "gen_ai.tools"] as const;

function extractMessages(raw: Record<string, unknown>): unknown[] {
  const out: unknown[] = [];
  for (const key of MESSAGE_KEYS) {
    const v = raw[key];
    if (typeof v === "string") {
      out.push(v);
    } else if (Array.isArray(v)) {
      out.push(...v);
    } else if (v !== null && typeof v === "object") {
      // Some exporters send {messages: [...]} or just an object message.
      const maybe = v as { messages?: unknown };
      if (Array.isArray(maybe.messages)) out.push(...maybe.messages);
      else out.push(v);
    }
  }
  return dedupeMessages(out);
}

function extractTools(raw: Record<string, unknown>): unknown[] {
  for (const key of TOOL_KEYS) {
    const v = raw[key];
    if (Array.isArray(v)) return v;
  }
  return [];
}

// Identical messages across llm.input_messages and gen_ai.prompt are common
// when both instrumentation paths fire. Drop string-equal duplicates so the
// inspector doesn't show the user the same system prompt twice.
function dedupeMessages(messages: unknown[]): unknown[] {
  const seen = new Set<string>();
  const out: unknown[] = [];
  for (const m of messages) {
    const key = typeof m === "string" ? m : JSON.stringify(m);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(m);
  }
  return out;
}
