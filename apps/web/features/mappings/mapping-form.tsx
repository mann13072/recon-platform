"use client";

import { useEffect, useState } from "react";
import { z } from "zod";
import { api, apiJson, can } from "@/lib/api-client";
import { type Me, fileSchema } from "@/lib/schemas";

const columnSchema = z.object({
  source_column: z.string(),
  canonical_field: z.string(),
  date_format: z.string().nullable().optional(),
  number_format: z.string().nullable().optional(),
  negate: z.boolean().default(false),
});
const suggestedSchema = z.object({
  source_system: z.string().default("uploaded-file"),
  columns: z.array(columnSchema),
  static_values: z.record(z.string(), z.string()).default({}),
  debit_column: z.string().nullable().optional(),
  credit_column: z.string().nullable().optional(),
});
type Mapping = z.infer<typeof suggestedSchema>;
const profileSchema = z.object({
  profile: z.object({
    row_count: z.number().int(),
    column_count: z.number().int(),
    columns: z.array(
      z.object({
        name: z.string(),
        inferred_type: z.string(),
        sample_values: z.array(z.string()),
        warnings: z.array(z.string()),
      }).passthrough(),
    ),
  }).passthrough(),
  suggested_mapping: suggestedSchema.nullable(),
});

const canonicalFields = ["transaction_date", "posting_date", "value_date", "amount", "currency", "description", "reference", "counterparty_name", "invoice_number", "settlement_id", "external_transaction_id", "ignore"];

export function MappingForm({ me, fileId }: { me: Me; fileId: string }) {
  const [mapping, setMapping] = useState<Mapping | null>(null);
  const [profile, setProfile] = useState<z.infer<typeof profileSchema>["profile"] | null>(null);
  const [message, setMessage] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => {
    if (!fileId) return;
    setConfirmed(false);
    api(`/files/${fileId}/profile`, profileSchema).then((result) => {
      setProfile(result.profile);
      setMapping(result.suggested_mapping);
    }).catch((error: unknown) => setMessage(error instanceof Error ? error.message : "Unable to profile file"));
  }, [fileId]);

  function changeField(index: number, canonical_field: string) {
    if (!mapping) return;
    setConfirmed(false);
    setMapping({ ...mapping, columns: mapping.columns.map((column, current) => current === index ? { ...column, canonical_field } : column) });
  }

  async function confirm() {
    if (!mapping) return;
    const payload = { ...mapping, columns: mapping.columns.filter((column) => column.canonical_field !== "ignore") };
    try {
      await apiJson(`/files/${fileId}/mapping`, fileSchema, "POST", payload);
      setConfirmed(true);
      setMessage("Mapping confirmed. Ingestion is now available as a separate action.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Mapping failed");
    }
  }

  async function ingest() {
    try {
      const result = await api(`/files/${fileId}/ingest`, z.object({ transactions_created: z.number(), duplicates_skipped: z.number(), rows_failed: z.number(), quality_level: z.string() }).passthrough(), { method: "POST" });
      setMessage(`Ingested ${result.transactions_created} transactions; ${result.duplicates_skipped} duplicates skipped; quality ${result.quality_level}.`);
      setConfirmed(false);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Ingestion failed");
    }
  }

  if (!fileId) return <section className="empty-state"><p className="eyebrow">Controlled ingestion</p><h1>Column mapping</h1><p>Choose “Profile & map” from Source files.</p></section>;
  if (!profile || !mapping) return <section className="empty-state"><h1>Column mapping</h1><p>{message || "Loading profile…"}</p></section>;

  return (
    <section>
      <div className="page-heading"><div><p className="eyebrow">Controlled ingestion</p><h1>Confirm column mapping</h1><p>{profile.row_count} rows · {profile.column_count} columns · nothing is ingested yet</p></div></div>
      <div className="mapping-list">
        {profile.columns.map((column) => {
          const mapped = mapping.columns.findIndex((item) => item.source_column === column.name);
          const value = mapped >= 0 ? mapping.columns[mapped].canonical_field : "ignore";
          return <div className="mapping-row" key={column.name}>
            <div><strong>{column.name}</strong><small>{column.inferred_type} · {column.sample_values.slice(0, 2).join(", ") || "empty"}</small></div>
            <span aria-hidden="true">→</span>
            <select aria-label={`Map ${column.name}`} value={value} onChange={(event) => mapped >= 0 && changeField(mapped, event.target.value)} disabled={mapped < 0}>
              {canonicalFields.map((field) => <option key={field} value={field}>{field}</option>)}
            </select>
          </div>;
        })}
      </div>
      {message && <p className="notice" role="status">{message}</p>}
      {can(me.permissions, "data:ingest") && <div className="actions"><button className="primary" onClick={() => void confirm()}>Confirm mapping</button><button disabled={!confirmed} onClick={() => void ingest()}>Ingest confirmed file</button></div>}
    </section>
  );
}
