"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api-client";
import { formatMoney, isZero } from "@/lib/money";
import { type RunSummary, runSummarySchema } from "@/lib/schemas";

type Props = { runId: string; onRunId: (value: string) => void };

function percent(value: number | null): string {
  return value === null ? "Not yet measured" : `${(value * 100).toFixed(1)}%`;
}

export function Dashboard({ runId, onRunId }: Props) {
  const [draft, setDraft] = useState(runId);
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [error, setError] = useState("");

  useEffect(() => setDraft(runId), [runId]);
  useEffect(() => {
    if (!runId) return;
    setError("");
    api(`/runs/${runId}/summary`, runSummarySchema)
      .then(setSummary)
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Unable to load run"));
  }, [runId]);

  if (!runId || !summary) {
    return (
      <section className="empty-state" aria-labelledby="dashboard-heading">
        <p className="eyebrow">Close readiness</p>
        <h1 id="dashboard-heading">Are the accounts reconciled?</h1>
        <p>Enter the run ID printed by the demo seed, or start a run from Reconciliations.</p>
        <form
          className="run-picker"
          onSubmit={(event) => {
            event.preventDefault();
            onRunId(draft.trim());
          }}
        >
          <label htmlFor="run-id">Run ID</label>
          <input id="run-id" value={draft} onChange={(event) => setDraft(event.target.value)} />
          <button className="primary" type="submit">Open run</button>
        </form>
        {error && <p className="error" role="alert">{error}</p>}
      </section>
    );
  }

  const reconciled = isZero(summary.difference) && summary.exception_count === 0;
  const metrics = [
    ["Side A balance", formatMoney(summary.side_a_balance, summary.currency)],
    ["Side B balance", formatMoney(summary.side_b_balance, summary.currency)],
    ["Difference", formatMoney(summary.difference, summary.currency)],
    ["Matched amount", formatMoney(summary.matched_amount, summary.currency)],
    ["Matched transactions", summary.matched_transaction_count.toString()],
    ["Auto-matched", summary.auto_matched_count.toString()],
    ["Human-approved", summary.human_approved_count.toString()],
    ["Suggested", summary.suggested_count.toString()],
    ["Exceptions", summary.exception_count.toString()],
    ["High-risk exceptions", summary.high_risk_exception_count.toString()],
    ["Unmatched side A", summary.unmatched_a_count.toString()],
    ["Unmatched side B", summary.unmatched_b_count.toString()],
    ["Oldest exception", summary.oldest_exception_age_days === null ? "None" : `${summary.oldest_exception_age_days} days`],
    ["Completion", `${summary.completion_pct.toFixed(1)}%`],
    ["Manual review rate", percent(summary.manual_review_rate)],
  ];

  return (
    <section aria-labelledby="dashboard-heading">
      <div className="page-heading">
        <div>
          <p className="eyebrow">Close readiness</p>
          <h1 id="dashboard-heading">{reconciled ? "Accounts are reconciled" : "Attention is required"}</h1>
          <p>{summary.period_start ?? "Open"} — {summary.period_end ?? "current"} · {summary.status}</p>
        </div>
        <span className={`status ${reconciled ? "good" : "warn"}`}>{reconciled ? "Ready" : "Not ready"}</span>
      </div>
      <div className="metric-grid">
        {metrics.map(([label, value]) => (
          <article className="metric" key={label}>
            <span>{label}</span><strong>{value}</strong>
          </article>
        ))}
      </div>
      <div className="paired-metric" aria-label="Automation quality">
        <div><span>Automation rate</span><strong>{percent(summary.auto_match_rate)}</strong></div>
        <div><span>False-match rate</span><strong>{percent(summary.false_match_rate)}</strong></div>
        <p>Automation is always shown with its observed error rate.</p>
      </div>
    </section>
  );
}
