"use client";

import { useCallback, useEffect, useState } from "react";
import { z } from "zod";
import { api, apiJson, can } from "@/lib/api-client";
import { formatMoney } from "@/lib/money";
import { commentSchema, exceptionSchema, type Me, type ReconException } from "@/lib/schemas";

const nextStates = ["TRIAGED", "ASSIGNED", "INVESTIGATING", "PROPOSED_RESOLUTION", "AWAITING_APPROVAL", "RESOLVED", "CLOSED", "BLOCKED", "ESCALATED", "REOPENED"];

export function Exceptions({ me, runId }: { me: Me; runId: string }) {
  const [items, setItems] = useState<ReconException[]>([]);
  const [status, setStatus] = useState("");
  const [severity, setSeverity] = useState("");
  const [message, setMessage] = useState("");
  const refresh = useCallback(() => {
    const query = new URLSearchParams();
    if (runId) query.set("run_id", runId);
    if (status) query.set("status", status);
    if (severity) query.set("severity", severity);
    return api(`/exceptions?${query}`, z.array(exceptionSchema)).then(setItems).catch((error: unknown) => setMessage(error instanceof Error ? error.message : "Unable to load exceptions"));
  }, [runId, severity, status]);
  useEffect(() => { void refresh(); }, [refresh]);

  async function transition(item: ReconException, target: string) {
    try {
      await apiJson(`/exceptions/${item.id}/transition`, exceptionSchema, "POST", { status: target, expected_version: item.version, reason: `Moved to ${target} from exception queue` });
      setMessage(`${item.title} moved to ${target}.`);
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Transition failed"); }
  }

  async function addComment(item: ReconException) {
    const body = window.prompt("Comment");
    if (!body) return;
    try {
      await apiJson(`/exceptions/${item.id}/comment`, commentSchema, "POST", { body });
      setMessage("Comment added.");
    } catch (error) { setMessage(error instanceof Error ? error.message : "Comment failed"); }
  }

  async function uploadEvidence(item: ReconException, file: File) {
    const body = new FormData(); body.set("file", file);
    try {
      await api(`/exceptions/${item.id}/evidence`, z.object({ id: z.string() }).passthrough(), { method: "POST", body });
      setMessage(`${file.name} attached with an immutable checksum.`);
    } catch (error) { setMessage(error instanceof Error ? error.message : "Evidence upload failed"); }
  }

  return <section>
    <div className="page-heading"><div><p className="eyebrow">Resolution workflow</p><h1>Exceptions</h1><p>Filter, assign, investigate, evidence, propose, approve, and close.</p></div></div>
    <div className="filters"><label>Status<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All</option>{["OPEN", ...nextStates].map((value) => <option key={value}>{value}</option>)}</select></label><label>Severity<select value={severity} onChange={(event) => setSeverity(event.target.value)}><option value="">All</option>{["LOW", "MEDIUM", "HIGH", "CRITICAL"].map((value) => <option key={value}>{value}</option>)}</select></label></div>
    {message && <p className="notice" role="status">{message}</p>}
    <div className="exception-list">{items.map((item) => <article className="card exception-card" key={item.id}>
      <div><span className={`severity ${item.severity.toLowerCase()}`}>{item.severity}</span><span className="status">{item.status}</span></div>
      <h2>{item.title}</h2><p>{item.detail}</p>
      <dl><div><dt>Exposure</dt><dd>{item.amount_exposure && item.currency ? formatMoney(item.amount_exposure, item.currency) : "Not quantified"}</dd></div><div><dt>Owner</dt><dd>{item.owner_user_id ?? "Unassigned"}</dd></div><div><dt>Reason codes</dt><dd>{item.reason_codes.join(", ") || "—"}</dd></div></dl>
      <div className="actions">
        {can(me.permissions, "exception:comment") && <button onClick={() => void addComment(item)}>Add comment</button>}
        {can(me.permissions, "exception:evidence") && <label className="button-label">Add evidence<input className="sr-only" type="file" onChange={(event) => event.target.files?.[0] && void uploadEvidence(item, event.target.files[0])} /></label>}
        {can(me.permissions, "exception:comment") && <select aria-label={`Transition ${item.title}`} defaultValue="" onChange={(event) => event.target.value && void transition(item, event.target.value)}><option value="">Transition…</option>{nextStates.map((value) => <option key={value}>{value}</option>)}</select>}
      </div>
    </article>)}</div>
  </section>;
}
