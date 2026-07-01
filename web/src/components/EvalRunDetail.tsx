// Phase 5.5 — single eval run detail with per-scenario verdict pills.
//
// Renders the run header (suite name, overall verdict, totals) and a row per
// scenario with its rollup and evaluator outcomes. A back button returns to
// the list; a "compare" toggle prompts for a baseline run UUID and renders
// the per-scenario verdict diff inline.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { evalVerdictClass } from "../styles";
import type {
  EvalBaselineDiffView,
  EvalRunDetailView,
} from "../types";

interface Props {
  runId: string;
  onBack: () => void;
}

export function EvalRunDetail({ runId, onBack }: Props): JSX.Element {
  const [run, setRun] = useState<EvalRunDetailView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Baseline diff state — populated on demand via the "compare" button.
  const [diff, setDiff] = useState<EvalBaselineDiffView | null>(null);
  const [diffError, setDiffError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getEvalRun(runId)
      .then((res) => {
        if (cancelled) return;
        setRun(res);
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
  }, [runId]);

  const handleCompare = (): void => {
    const baseline = window.prompt(
      "baseline run UUID (golden/good run to diff against):",
    );
    if (baseline === null || baseline.trim() === "") return;
    setDiffError(null);
    api
      .compareEvalBaseline(runId, baseline.trim())
      .then(setDiff)
      .catch((err: ApiError | Error) => setDiffError(err.message));
  };

  if (loading) return <p className="muted">loading…</p>;
  if (error !== null)
    return (
      <div className="banner banner--error" role="alert">
        {error}
      </div>
    );
  if (run === null) return <p className="muted">no data.</p>;

  const totalPrompt = run.scenarios.reduce(
    (n, s) => n + s.rollup.prompt_tokens,
    0,
  );
  const totalCompletion = run.scenarios.reduce(
    (n, s) => n + s.rollup.completion_tokens,
    0,
  );
  const totalLlmCalls = run.scenarios.reduce(
    (n, s) => n + s.rollup.llm_call_count,
    0,
  );

  return (
    <section className="eval-detail">
      <div className="eval-detail__header">
        <button type="button" className="link-button" onClick={onBack}>
          ← back
        </button>
        <h2>{run.suite_name}</h2>
        <span className={evalVerdictClass(run.overall_verdict)}>
          {run.overall_verdict}
        </span>
        <code className="muted">{run.run_id.slice(0, 8)}</code>
        <button type="button" className="link-button" onClick={handleCompare}>
          ⎇ compare to baseline
        </button>
      </div>

      <div className="eval-detail__rollup">
        <span>started {run.started_at}</span>
        <span>finished {run.finished_at}</span>
        <span>scenarios {run.scenarios.length}</span>
        <span>
          tokens {" "}
          {totalPrompt.toLocaleString()}p / {" "}
          {totalCompletion.toLocaleString()}c
        </span>
        <span>llm calls {totalLlmCalls}</span>
      </div>

      {diffError !== null && (
        <div className="banner banner--error" role="alert">
          diff failed: {diffError}
        </div>
      )}

      {diff !== null && (
        <div className="banner banner--info">
          baseline <code>{diff.baseline_run_id.slice(0, 8)}</code> → candidate{" "}
          <code>{diff.candidate_run_id.slice(0, 8)}</code>:{" "}
          {diff.overall_changed ? "verdicts changed" : "no changes"}
        </div>
      )}

      <table className="eval-detail__scenarios">
        <thead>
          <tr>
            <th>verdict</th>
            <th>scenario</th>
            <th>seed</th>
            <th>evaluators</th>
            <th>tokens</th>
            <th>latency</th>
          </tr>
        </thead>
        <tbody>
          {run.scenarios.map((scen) => {
            const baselineRow = diff?.scenarios.find(
              (d) => d.scenario_name === scen.name,
            );
            return (
              <tr key={scen.name}>
                <td>
                  <span className={evalVerdictClass(scen.verdict)}>
                    {scen.verdict}
                  </span>
                  {baselineRow?.changed && (
                    <span
                      className="muted"
                      title={`${baselineRow.baseline_verdict} → ${baselineRow.candidate_verdict}`}
                    >
                      {" "}Δ
                    </span>
                  )}
                </td>
                <td>{scen.name}</td>
                <td>
                  <code className="muted">{scen.seed_trace_id.slice(0, 8)}</code>
                  {scen.branch_id !== null && (
                    <>
                      {" "}@{" "}
                      <code className="muted">
                        {scen.branch_id.slice(0, 8)}
                      </code>
                    </>
                  )}
                </td>
                <td>
                  <ul className="eval-detail__outcomes">
                    {scen.outcomes.map((o, i) => (
                      <li key={`${scen.name}-${i}`}>
                        <span className={evalVerdictClass(o.verdict)}>
                          {o.verdict.slice(0, 1)}
                        </span>{" "}
                        <code>{o.kind}</code>{" "}
                        <span className="muted">{o.detail}</span>
                      </li>
                    ))}
                  </ul>
                  {scen.error_message !== null && (
                    <div className="muted">{scen.error_message}</div>
                  )}
                </td>
                <td>
                  {scen.rollup.total_tokens}t / {" "}
                  {scen.rollup.llm_call_count} calls
                </td>
                <td>
                  {scen.latency.total_s.toFixed(2)}s{" "}
                  <span className="muted">
                    (replay {scen.latency.replay_s.toFixed(2)}s + eval{" "}
                    {scen.latency.evaluate_s.toFixed(2)}s)
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}
