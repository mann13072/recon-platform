"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { api, apiJson, can } from "@/lib/api-client";
import {
  type Me,
  type Reconciliation,
  reconciliationSchema,
  runSchema,
} from "@/lib/schemas";

const formSchema = z.object({
  slug: z.string().min(2).max(64).regex(/^[a-z0-9][a-z0-9-]*$/),
  name: z.string().min(1).max(255),
  template: z.string().min(1),
  entity: z.string().min(1),
});
type FormValues = z.infer<typeof formSchema>;

export function Reconciliations({ me, onRun }: { me: Me; onRun: (runId: string) => void }) {
  const [items, setItems] = useState<Reconciliation[]>([]);
  const [message, setMessage] = useState("");
  const { register, handleSubmit, reset, formState: { errors } } = useForm<FormValues>({
    resolver: zodResolver(formSchema),
    defaultValues: { slug: "", name: "", template: "bank_gl", entity: "default" },
  });

  const refresh = () => api("/reconciliations", z.array(reconciliationSchema)).then(setItems).catch((error: unknown) => setMessage(error instanceof Error ? error.message : "Unable to load"));
  useEffect(() => { void refresh(); }, []);

  const create = handleSubmit(async (values) => {
    try {
      await apiJson("/reconciliations", reconciliationSchema, "POST", values);
      reset();
      setMessage("Reconciliation created.");
      await refresh();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Create failed");
    }
  });

  async function start(item: Reconciliation) {
    try {
      const run = await apiJson(`/reconciliations/${item.id}/runs`, runSchema, "POST", {});
      window.localStorage.setItem("recon.runId", run.id);
      onRun(run.id);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Run failed");
    }
  }

  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">Definitions</p><h1>Reconciliations</h1><p>Create from a controlled template, then run it against ingested data.</p></div></div>
      {can(me.permissions, "recon:create") && (
        <form className="card form-grid" onSubmit={create}>
          <h2>New reconciliation</h2>
          <label>Name<input {...register("name")} />{errors.name && <small>{errors.name.message}</small>}</label>
          <label>Slug<input {...register("slug")} />{errors.slug && <small>{errors.slug.message}</small>}</label>
          <label>Template<select {...register("template")}><option value="bank_gl">Bank ↔ GL</option><option value="processor_bank">Processor ↔ bank</option><option value="intercompany">Intercompany</option></select></label>
          <label>Entity<input {...register("entity")} /></label>
          <button className="primary" type="submit">Create definition</button>
        </form>
      )}
      {message && <p className="notice" role="status">{message}</p>}
      <div className="list-grid">
        {items.map((item) => (
          <article className="card" key={item.id}>
            <p className="eyebrow">{item.template ?? "Custom"} · v{item.version}</p>
            <h2>{item.name}</h2><p>{item.entity} · {item.slug}</p>
            {can(me.permissions, "recon:run") && <button className="primary" onClick={() => void start(item)}>Start run</button>}
          </article>
        ))}
      </div>
    </section>
  );
}
