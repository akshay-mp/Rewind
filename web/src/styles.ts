// Stable colour + label conventions for span kinds.
//
// The kind → colour map is centralised so the timeline bars, the inspector
// header, and search hits all agree. Dark theme first; light theme variants
// are picked by the browser via `color-scheme`.

import type { EvalVerdict, SpanKind, SpanStatus } from "./types";

interface KindStyle {
  label: string;
  swatch: string;
  border: string;
}

const KIND_STYLES: Record<SpanKind, KindStyle> = {
  "gen_ai.llm": {
    label: "LLM",
    swatch: "var(--rewind-kind-llm)",
    border: "color-mix(in srgb, var(--rewind-kind-llm) 60%, transparent)",
  },
  "gen_ai.tool": {
    label: "Tool",
    swatch: "var(--rewind-kind-tool)",
    border: "color-mix(in srgb, var(--rewind-kind-tool) 60%, transparent)",
  },
  "gen_ai.mcp": {
    label: "MCP",
    swatch: "var(--rewind-kind-mcp)",
    border: "color-mix(in srgb, var(--rewind-kind-mcp) 60%, transparent)",
  },
  "gen_ai.agent": {
    label: "Agent",
    swatch: "var(--rewind-kind-agent)",
    border: "color-mix(in srgb, var(--rewind-kind-agent) 60%, transparent)",
  },
  "rewind.unknown": {
    label: "?",
    swatch: "var(--rewind-kind-unknown)",
    border: "color-mix(in srgb, var(--rewind-kind-unknown) 60%, transparent)",
  },
};

export function kindStyle(kind: SpanKind): KindStyle {
  return KIND_STYLES[kind];
}

export function statusStyle(status: SpanStatus): string {
  switch (status) {
    case "ERROR":
      return "var(--rewind-status-error)";
    case "OK":
      return "var(--rewind-status-ok)";
    case "UNSET":
    default:
      return "var(--rewind-status-unset)";
  }
}

/**
 * Phase 5.5 — CSS class for an eval verdict pill. The pill is a small
 * inline element next to each scenario row. The classes (``pill--pass``,
 * etc.) are defined in ``styles.css``.
 */
export function evalVerdictClass(verdict: EvalVerdict): string {
  switch (verdict) {
    case "PASS":
      return "pill pill--pass";
    case "FAIL":
      return "pill pill--fail";
    case "SKIP":
      return "pill pill--skip";
    case "ERROR":
    default:
      return "pill pill--error";
  }
}

export function formatDuration(startIso: string, endIso: string): string {
  const startMs = Date.parse(startIso);
  const endMs = Date.parse(endIso);
  if (Number.isNaN(startMs) || Number.isNaN(endMs) || endMs < startMs) return "—";
  const ms = endMs - startMs;
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  return `${(ms / 60_000).toFixed(2)} min`;
}

export function formatTimestamp(iso: string): string {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return iso;
  // Locale-neutral, fixed-width — easier to scan in a table.
  return new Date(ms).toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}
