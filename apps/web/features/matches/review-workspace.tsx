"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { z } from "zod";
import { api, apiJson, can } from "@/lib/api-client";
import { formatMoney } from "@/lib/money";
import {
  commentSchema,
  evidenceSchema,
  exceptionSchema,
  lineageSchema,
  matchSchema,
  transactionSchema,
  type Comment,
  type Evidence,
  type Lineage,
  type Match,
  type Me,
  type ReconException,
  type Transaction,
} from "@/lib/schemas";

export function ReviewWorkspace({ me, runId }: { me: Me; runId: string }) {
  const [matches, setMatches] = useState<Match[]>([]);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [exceptions, setExceptions] = useState<ReconException[]>([]);
  const [selected, setSelected] = useState(0);
  const [lineage, setLineage] = useState<Lineage | null>(null);
  const [comments, setComments] = useState<Comment[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [message, setMessage] = useState("");
  const [help, setHelp] = useState(false);

  const refresh = useCallback(async () => {
    if (!runId) return;
    try {
      const [matchRows, transactionRows, exceptionRows] = await Promise.all([
        api(`/runs/${runId}/matches`, z.array(matchSchema)),
        api("/transactions?limit=1000", z.array(transactionSchema)),
        api(`/runs/${runId}/exceptions`, z.array(exceptionSchema)),
      ]);
      setMatches([
        ...matchRows.filter((item) => item.status === "SUGGESTED"),
        ...matchRows.filter((item) => item.status !== "SUGGESTED"),
      ]);
      setTransactions(transactionRows);
      setExceptions(exceptionRows);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to load review queue");
    }
  }, [runId]);
  useEffect(() => { void refresh(); }, [refresh]);

  const current = matches[selected] ?? null;
  const canDecide = current?.status === "SUGGESTED";
  const transactionById = useMemo(() => new Map(transactions.map((item) => [item.id, item])), [transactions]);
  const selectedTransactions = current?.members.map((member) => transactionById.get(member.transaction_id)).filter((item): item is Transaction => Boolean(item)) ?? [];
  const selectedException = exceptions.find((item) => item.transaction_ids.some((id) => current?.members.some((member) => member.transaction_id === id))) ?? null;
  const matchedIds = new Set(matches.flatMap((match) => match.members.map((member) => member.transaction_id)));
  const unmatched = transactions.filter((item) => !matchedIds.has(item.id));
  const suggestedCount = matches.filter((match) => match.status === "SUGGESTED").length;

  useEffect(() => {
    const transaction = selectedTransactions[0];
    if (!transaction) { setLineage(null); return; }
    api(`/transactions/${transaction.id}/lineage`, lineageSchema).then(setLineage).catch(() => setLineage(null));
  }, [current?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!selectedException) { setComments([]); setEvidence([]); return; }
    Promise.all([
      api(`/exceptions/${selectedException.id}/comments`, z.array(commentSchema)),
      api(`/exceptions/${selectedException.id}/evidence`, z.array(evidenceSchema)),
    ]).then(([notes, files]) => { setComments(notes); setEvidence(files); }).catch(() => { setComments([]); setEvidence([]); });
  }, [selectedException?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const decide = useCallback(async (decision: "approve" | "reject") => {
    if (!current || current.status !== "SUGGESTED") return;
    try {
      const body = decision === "approve" ? { expected_version: current.version } : { expected_version: current.version, reason: "Rejected during transaction review" };
      await apiJson(`/matches/${current.id}/${decision}`, matchSchema, "POST", body);
      setMessage(`Match ${decision === "approve" ? "approved" : "rejected"}.`);
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Decision failed"); }
  }, [current, refresh]);

  useEffect(() => {
    function keys(event: KeyboardEvent) {
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement) return;
      if (event.key === "j") setSelected((value) => Math.min(value + 1, matches.length - 1));
      if (event.key === "k") setSelected((value) => Math.max(value - 1, 0));
      if (event.key === "a" && can(me.permissions, "match:approve")) void decide("approve");
      if (event.key === "r" && can(me.permissions, "match:reject")) void decide("reject");
      if (event.key === "?") setHelp((value) => !value);
    }
    window.addEventListener("keydown", keys);
    return () => window.removeEventListener("keydown", keys);
  }, [decide, matches.length, me.permissions]);

  async function exceptionAction(action: "assign" | "note" | "request" | "adjust" | "escalate") {
    if (!selectedException) { setMessage("This match has no linked exception."); return; }
    try {
      if (action === "assign" && me.user_id) await apiJson(`/exceptions/${selectedException.id}/assign`, exceptionSchema, "POST", { owner_user_id: me.user_id, expected_version: selectedException.version });
      if (action === "note" || action === "request") await apiJson(`/exceptions/${selectedException.id}/comment`, commentSchema, "POST", { body: action === "request" ? "Evidence requested during match review." : "Reviewed from the match workspace." });
      if (action === "adjust") await apiJson(`/exceptions/${selectedException.id}/propose-resolution`, exceptionSchema, "POST", { proposed_resolution: "Proposed accounting adjustment; supporting evidence required.", resolution_code: "ADJUSTMENT", expected_version: selectedException.version });
      if (action === "escalate") await apiJson(`/exceptions/${selectedException.id}/transition`, exceptionSchema, "POST", { status: "ESCALATED", expected_version: selectedException.version, reason: "Escalated from match review" });
      setMessage(`${action} action recorded.`);
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Action failed"); }
  }

  async function manualMatch() {
    const sideA = unmatched.find((item) => item.source_system === "bank") ?? unmatched[0];
    const sideB = unmatched.find((item) => item.id !== sideA?.id);
    if (!sideA || !sideB) { setMessage("Two unmatched transactions are needed to create a group."); return; }
    try {
      await apiJson("/matches/manual", matchSchema, "POST", { run_id: runId, side_a_ids: [sideA.id], side_b_ids: [sideB.id], reason: "Manual group created from review workspace" });
      setMessage("Manual match group created for second-person approval.");
      await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Manual match failed"); }
  }

  if (!runId) return <section className="empty-state"><p className="eyebrow">Keyboard-first review</p><h1>Transaction review</h1><p>Open a run from Dashboard or Reconciliations first.</p></section>;
  return (
    <section>
      <div className="page-heading compact"><div><p className="eyebrow">Keyboard-first review · run {runId.slice(0, 8)}</p><h1>Transaction review</h1></div><button onClick={() => setHelp((value) => !value)}>Keyboard help <kbd>?</kbd></button></div>
      {help && <div className="notice" role="note"><kbd>j</kbd> next · <kbd>k</kbd> previous · <kbd>a</kbd> approve · <kbd>r</kbd> reject · <kbd>?</kbd> help</div>}
      {message && <p className="notice" role="status">{message}</p>}
      <div className="review-grid">
        <aside className="pane queue" aria-label="Unmatched and suggested transactions">
          <div className="pane-title"><span>Queue</span><small>{suggestedCount} suggested · {unmatched.length} unmatched</small></div>
          {matches.map((match, index) => <button className={index === selected ? "queue-item selected" : "queue-item"} key={match.id} onClick={() => setSelected(index)}>
            <span><strong>{match.cardinality}</strong><small>{match.decision} · {match.status}</small></span><b>{formatMoney(match.total_amount, match.currency)}</b>
          </button>)}
          {unmatched.slice(0, 12).map((item) => <div className="queue-item muted" key={item.id}><span><strong>{item.description ?? item.reference ?? "Unmatched"}</strong><small>{item.source_system}</small></span><b>{formatMoney(item.amount, item.currency)}</b></div>)}
        </aside>
        <main className="pane candidates" aria-label="Candidate match reasoning">
          <div className="pane-title"><span>Candidate</span>{current && <strong>{(current.confidence * 100).toFixed(1)}% confidence</strong>}</div>
          {!current ? <p className="pane-empty">No candidate matches in this run.</p> : <>
            <div className="candidate-total"><span>{current.cardinality} · {current.engine_stage}</span><strong>{formatMoney(current.total_amount, current.currency)}</strong></div>
            <h3>Reason codes</h3>
            <ul className="reason-list">{current.reasons.map((reason) => <li key={reason.code}><div><code>{reason.code}</code><strong>{reason.description}</strong></div><span>{(reason.contribution * 100).toFixed(0)}%</span></li>)}</ul>
            {current.explanation && <p>{current.explanation}</p>}
            {current.warnings.map((warning) => <p className="warning" key={warning.code}>{warning.code}: {warning.description}</p>)}
          </>}
        </main>
        <aside className="pane detail" aria-label="Source details, evidence, comments and history">
          <div className="pane-title"><span>Evidence & history</span></div>
          {selectedTransactions.map((item) => <article className="source-detail" key={item.id}><span>{item.source_system} · {item.transaction_date ?? item.posting_date}</span><strong>{item.description ?? "No description"}</strong><small>{item.reference ?? item.source_record_id} · {formatMoney(item.amount, item.currency)}</small></article>)}
          <h3>Lineage</h3><p className="micro">{lineage ? `${lineage.records.length} mapped fields · checksum ${lineage.source_checksum.slice(0, 12)}…` : "No lineage selected"}</p>
          <h3>Evidence</h3>{evidence.length ? evidence.map((item) => <p className="micro" key={item.id}>{item.filename} · {item.kind}</p>) : <p className="micro">No evidence attached</p>}
          <h3>Comments</h3>{comments.length ? comments.map((item) => <p className="micro" key={item.id}>{item.body}</p>) : <p className="micro">No comments</p>}
          <h3>History</h3><p className="micro">{current ? `${current.status} · version ${current.version}${current.approved_at ? ` · approved ${current.approved_at}` : ""}` : "No candidate selected"}</p>
        </aside>
      </div>
      <div className="action-bar" aria-label="Review actions">
        {can(me.permissions, "match:approve") && <button className="primary" disabled={!canDecide} onClick={() => void decide("approve")}>Approve <kbd>a</kbd></button>}
        {can(me.permissions, "match:reject") && <button disabled={!canDecide} onClick={() => void decide("reject")}>Reject <kbd>r</kbd></button>}
        {can(me.permissions, "match:create") && <button onClick={() => void manualMatch()}>Create manual match</button>}
        {can(me.permissions, "match:create") && <button onClick={() => void manualMatch()}>Split / group</button>}
        {can(me.permissions, "exception:assign") && <button onClick={() => void exceptionAction("assign")}>Assign exception</button>}
        {can(me.permissions, "exception:comment") && <button onClick={() => void exceptionAction("note")}>Add note</button>}
        {can(me.permissions, "exception:comment") && <button onClick={() => void exceptionAction("request")}>Request evidence</button>}
        {can(me.permissions, "exception:propose") && <button onClick={() => void exceptionAction("adjust")}>Propose adjustment</button>}
        {can(me.permissions, "exception:comment") && <button onClick={() => void exceptionAction("escalate")}>Escalate</button>}
      </div>
    </section>
  );
}
