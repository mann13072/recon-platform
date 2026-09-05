import { can } from "@/lib/api-client";
import type { Me } from "@/lib/schemas";

export function Settings({ me }: { me: Me }) {
  return <section>
    <div className="page-heading"><div><p className="eyebrow">Tenant controls</p><h1>Settings</h1><p>Effective policy for tenant {me.tenant_id}.</p></div></div>
    <div className="settings-grid">
      <article className="card"><h2>AI policy</h2><dl><div><dt>Enabled</dt><dd>{me.ai.enabled ? "Yes" : "No"}</dd></div><div><dt>Policy</dt><dd>{me.ai.policy}</dd></div><div><dt>Provider</dt><dd>{me.ai.provider}</dd></div></dl>{can(me.permissions, "ai:configure") ? <p className="notice">This role may configure AI through the tenant administration API when enabled.</p> : <p className="micro">Read only for your role.</p>}</article>
      <article className="card"><h2>Thresholds & rules</h2><p>Versioned reconciliation definitions hold matching rules and thresholds. Changes are reviewed and audited.</p><div className="permission-line"><span>Edit thresholds</span><strong>{can(me.permissions, "thresholds:edit") ? "Allowed" : "Read only"}</strong></div><div className="permission-line"><span>Edit rules</span><strong>{can(me.permissions, "rules:edit") ? "Allowed" : "Read only"}</strong></div><div className="permission-line"><span>Simulate rules</span><strong>{can(me.permissions, "rules:simulate") ? "Allowed" : "Read only"}</strong></div></article>
      <article className="card"><h2>Identity & access</h2><dl><div><dt>User</dt><dd>{me.email ?? me.user_id ?? "Service principal"}</dd></div><div><dt>Roles</dt><dd>{me.roles.join(", ")}</dd></div><div><dt>Permissions</dt><dd>{me.permissions.length}</dd></div></dl></article>
    </div>
  </section>;
}
