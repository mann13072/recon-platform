import { z } from "zod";

const uuid = z.string().uuid();
const isoDate = z.string().nullable();
const money = z.string().regex(/^-?\d+(?:\.\d+)?$/, "Money must be a decimal string");

export const meSchema = z.object({
  user_id: uuid.nullable(),
  email: z.string().nullable(),
  tenant_id: uuid,
  actor_type: z.string(),
  roles: z.array(z.string()),
  permissions: z.array(z.string()),
  approval_limit: z.number().nullable(),
  ai: z.object({ enabled: z.boolean(), policy: z.string(), provider: z.string() }),
});

export const reconciliationSchema = z.object({
  id: uuid,
  slug: z.string(),
  name: z.string(),
  template: z.string().nullable(),
  config: z.record(z.string(), z.unknown()),
  config_version: z.string(),
  config_hash: z.string(),
  entity: z.string(),
  version: z.number().int(),
  created_at: z.string(),
});

export const runSchema = z.object({
  id: uuid,
  reconciliation_id: uuid,
  status: z.string(),
  period_start: isoDate,
  period_end: isoDate,
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  result_hash: z.string().nullable(),
  version: z.number().int(),
  replayed: z.boolean().default(false),
});

export const runSummarySchema = z.object({
  run_id: uuid,
  reconciliation_id: uuid,
  status: z.string(),
  period_start: isoDate,
  period_end: isoDate,
  currency: z.string(),
  side_a_balance: money,
  side_b_balance: money,
  difference: money,
  matched_amount: money,
  matched_transaction_count: z.number().int(),
  auto_matched_count: z.number().int(),
  human_approved_count: z.number().int(),
  suggested_count: z.number().int(),
  exception_count: z.number().int(),
  high_risk_exception_count: z.number().int(),
  unmatched_a_count: z.number().int(),
  unmatched_b_count: z.number().int(),
  oldest_exception_age_days: z.number().int().nullable(),
  completion_pct: z.number(),
  auto_match_rate: z.number(),
  false_match_rate: z.number().nullable(),
  manual_review_rate: z.number(),
});

export const transactionSchema = z.object({
  id: uuid,
  source_system: z.string(),
  source_record_id: z.string(),
  transaction_date: isoDate,
  posting_date: isoDate,
  value_date: isoDate,
  amount: money,
  currency: z.string(),
  debit_credit: z.string(),
  description: z.string().nullable(),
  reference: z.string().nullable(),
  counterparty_name: z.string().nullable(),
  invoice_number: z.string().nullable(),
  settlement_id: z.string().nullable(),
  external_transaction_id: z.string().nullable(),
  gross_amount: money.nullable(),
  fee_amount: money.nullable(),
  net_amount: money.nullable(),
  status: z.string().nullable(),
  imported_at: z.string(),
});

export const matchSchema = z.object({
  id: uuid,
  run_id: uuid,
  cardinality: z.string(),
  status: z.string(),
  decision: z.string(),
  confidence: z.number(),
  score: z.number(),
  currency: z.string(),
  total_amount: money,
  rule_id: z.string().nullable(),
  rule_version: z.string().nullable(),
  rule_set_version: z.string().nullable(),
  model_version: z.string().nullable(),
  engine_stage: z.string(),
  competing_candidate_count: z.number().int(),
  members: z.array(z.object({ transaction_id: uuid, side: z.string(), allocated_amount: money })),
  reasons: z.array(z.object({ code: z.string(), contribution: z.number(), description: z.string() })),
  warnings: z.array(z.record(z.string(), z.string())),
  approved_by: uuid.nullable(),
  approved_at: z.string().nullable(),
  override_reason: z.string().nullable(),
  version: z.number().int(),
  explanation: z.string(),
});

export const exceptionSchema = z.object({
  id: uuid,
  reconciliation_run_id: uuid,
  transaction_ids: z.array(uuid),
  category: z.string(),
  severity: z.string(),
  status: z.string(),
  amount_exposure: money.nullable(),
  currency: z.string().nullable(),
  title: z.string(),
  detail: z.string(),
  reason_codes: z.array(z.string()),
  owner_user_id: uuid.nullable(),
  first_detected_at: z.string(),
  due_at: z.string().nullable(),
  proposed_resolution: z.string().nullable(),
  resolution_code: z.string().nullable(),
  proposed_by_actor_type: z.string().nullable(),
  requires_approval: z.boolean(),
  closed_by: uuid.nullable(),
  closed_at: z.string().nullable(),
  version: z.number().int(),
});

export const fileSchema = z.object({
  id: uuid,
  filename: z.string(),
  byte_size: z.number().int(),
  checksum: z.string(),
  status: z.string(),
  row_count: z.number().int().nullable(),
  encoding: z.string().nullable(),
  delimiter: z.string().nullable(),
  sheet_name: z.string().nullable(),
  created_at: z.string(),
});

export const lineageSchema = z.object({
  transaction_id: uuid,
  source_record_id: z.string(),
  source_checksum: z.string(),
  records: z.array(z.record(z.string(), z.string())),
});

export const commentSchema = z.object({
  id: uuid,
  exception_id: uuid,
  author_id: uuid,
  body: z.string(),
  created_at: z.string(),
});

export const evidenceSchema = z.object({
  id: uuid,
  exception_id: uuid.nullable(),
  filename: z.string(),
  mime_type: z.string(),
  byte_size: z.number().int(),
  sha256: z.string(),
  kind: z.string(),
  uploaded_by: uuid,
  uploaded_at: z.string(),
  download_url: z.string().nullable(),
});

export type Me = z.infer<typeof meSchema>;
export type Reconciliation = z.infer<typeof reconciliationSchema>;
export type Run = z.infer<typeof runSchema>;
export type RunSummary = z.infer<typeof runSummarySchema>;
export type Transaction = z.infer<typeof transactionSchema>;
export type Match = z.infer<typeof matchSchema>;
export type ReconException = z.infer<typeof exceptionSchema>;
export type SourceFile = z.infer<typeof fileSchema>;
export type Lineage = z.infer<typeof lineageSchema>;
export type Comment = z.infer<typeof commentSchema>;
export type Evidence = z.infer<typeof evidenceSchema>;
