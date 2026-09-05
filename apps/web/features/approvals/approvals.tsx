"use client";

import { useCallback, useEffect, useState } from "react";
import { z } from "zod";
import { api, apiJson, can } from "@/lib/api-client";
import { formatMoney } from "@/lib/money";
import { matchSchema, type Match, type Me } from "@/lib/schemas";

export function Approvals({ me, runId }: { me: Me; runId: string }) {
  const [matches, setMatches] = useState<Match[]>([]);
  const [message, setMessage] = useState("");
  const refresh = useCallback(() => runId ? api(`/runs/${runId}/matches?status=SUGGESTED`, z.array(matchSchema)).then(setMatches).catch((error: unknown) => setMessage(error instanceof Error ? error.message : "Unable to load approvals")) : Promise.resolve(), [runId]);
  useEffect(() => { void refresh(); }, [refresh]);
  async function approve(match: Match) {
    try {
      await apiJson(`/matches/${match.id}/approve`, matchSchema, "POST", { expected_version: match.version, reason: "Approved from pending approvals queue" });
      setMessage("Approval recorded with actor and timestamp."); await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Approval failed"); }
  }
  return <section>
    <div className="page-heading"><div><p className="eyebrow">Maker–checker</p><h1>Pending approvals</h1><p>Only actions permitted for {me.email ?? "this principal"} are shown.</p></div></div>
    {!runId && <p className="notice">Open a run first.</p>}{message && <p className="notice" role="status">{message}</p>}
    <div className="list-grid">{matches.map((match) => <article className="card" key={match.id}><p className="eyebrow">{match.cardinality} · {(match.confidence * 100).toFixed(1)}%</p><h2>{formatMoney(match.total_amount, match.currency)}</h2><p>{match.reasons.map((reason) => reason.code).join(" · ")}</p>{can(me.permissions, "match:approve") && <button className="primary" onClick={() => void approve(match)}>Approve</button>}</article>)}</div>
    {runId && matches.length === 0 && <div className="empty-state small"><h2>No pending match approvals</h2><p>The queue is clear for this run.</p></div>}
  </section>;
}
