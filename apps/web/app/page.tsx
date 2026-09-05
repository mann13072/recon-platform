"use client";

import * as Tabs from "@radix-ui/react-tabs";
import { useEffect, useState } from "react";
import { api, setToken } from "@/lib/api-client";
import { meSchema, type Me } from "@/lib/schemas";
import { Dashboard } from "@/features/dashboard/dashboard";
import { Sources } from "@/features/sources/sources";
import { MappingForm } from "@/features/mappings/mapping-form";
import { Reconciliations } from "@/features/reconciliations/reconciliations";
import { ReviewWorkspace } from "@/features/matches/review-workspace";
import { Exceptions } from "@/features/exceptions/exceptions";
import { Approvals } from "@/features/approvals/approvals";
import { Settings } from "@/features/settings/settings";

const navigation = [
  ["dashboard", "Overview", "01"], ["review", "Review", "02"], ["exceptions", "Exceptions", "03"],
  ["approvals", "Approvals", "04"], ["sources", "Sources", "05"], ["mappings", "Mappings", "06"],
  ["reconciliations", "Reconciliations", "07"], ["settings", "Settings", "08"],
] as const;

export default function Home() {
  const [me, setMe] = useState<Me | null>(null);
  const [authError, setAuthError] = useState("");
  const [tokenDraft, setTokenDraft] = useState("");
  const [tab, setTab] = useState("dashboard");
  const [runId, setRunIdState] = useState("");
  const [fileId, setFileId] = useState("");

  function authenticate() {
    setAuthError("");
    api("/me", meSchema).then(setMe).catch((error: unknown) => setAuthError(error instanceof Error ? error.message : "Authentication failed"));
  }
  useEffect(() => {
    const storedRun = window.localStorage.getItem("recon.runId") ?? process.env.NEXT_PUBLIC_DEFAULT_RUN_ID ?? "";
    setRunIdState(storedRun);
    authenticate();
  }, []);

  function chooseRun(value: string) {
    setRunIdState(value);
    if (value) window.localStorage.setItem("recon.runId", value);
    setTab("dashboard");
  }

  if (!me) return <main className="auth-shell"><section className="auth-card">
    <div className="brand-mark">CL</div><p className="eyebrow">Clearledger control room</p><h1>Connect to the reconciliation API</h1><p>Use a development token printed by <code>python scripts/seed_demo.py</code>. The token remains in this browser only.</p>
    <form onSubmit={(event) => { event.preventDefault(); setToken(tokenDraft); authenticate(); }}>
      <label htmlFor="token">Development bearer token</label><textarea id="token" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} rows={4} required />
      <button className="primary" type="submit">Open control room</button>
    </form>{authError && <p className="error" role="alert">{authError}</p>}
  </section></main>;

  return <Tabs.Root className="app-shell" value={tab} onValueChange={setTab} orientation="vertical">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">CL</span><div><strong>Clearledger</strong><small>Reconciliation OS</small></div></div>
      <Tabs.List className="nav" aria-label="Application sections">{navigation.map(([value, label, number]) => <Tabs.Trigger key={value} value={value}><span>{number}</span>{label}</Tabs.Trigger>)}</Tabs.List>
      <div className="identity"><span className="avatar">{(me.email ?? "U").slice(0, 1).toUpperCase()}</span><div><strong>{me.email ?? "Service principal"}</strong><small>{me.roles.join(" · ")}</small></div></div>
    </aside>
    <main className="content">
      <header className="topbar"><div><span className="live-dot" /> Live API</div><div className="run-context"><span>Active run</span><code>{runId ? runId.slice(0, 12) : "not selected"}</code></div></header>
      <Tabs.Content value="dashboard"><Dashboard runId={runId} onRunId={chooseRun} /></Tabs.Content>
      <Tabs.Content value="review"><ReviewWorkspace me={me} runId={runId} /></Tabs.Content>
      <Tabs.Content value="exceptions"><Exceptions me={me} runId={runId} /></Tabs.Content>
      <Tabs.Content value="approvals"><Approvals me={me} runId={runId} /></Tabs.Content>
      <Tabs.Content value="sources"><Sources me={me} onMap={(id) => { setFileId(id); setTab("mappings"); }} /></Tabs.Content>
      <Tabs.Content value="mappings"><MappingForm me={me} fileId={fileId} /></Tabs.Content>
      <Tabs.Content value="reconciliations"><Reconciliations me={me} onRun={chooseRun} /></Tabs.Content>
      <Tabs.Content value="settings"><Settings me={me} /></Tabs.Content>
    </main>
  </Tabs.Root>;
}
