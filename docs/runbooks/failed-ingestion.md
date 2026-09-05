# Runbook: failed ingestion

**Symptom:** a file was uploaded but never became transactions, or a user
reports missing rows.

## First: do not "fix" the data

The platform deliberately refuses to repair accounting data. A blocked
ingestion is usually the system working. Establish which of the cases below
applies before changing anything.

## 1. Find the file

```
GET /api/v1/files
GET /api/v1/files/{id}/profile
```

The file's `status` says where it stopped.

| Status | Meaning | Action |
| --- | --- | --- |
| `UPLOADED` | Stored, not yet profiled | Re-request the profile. |
| `PROFILED` | Profiled, awaiting a confirmed mapping | The user must confirm the mapping. Nothing is wrong. |
| `MAPPED` | Mapping confirmed, not yet ingested | `POST /files/{id}/ingest`. |
| `QUALITY_FAILED` | A blocking data-quality error | Go to step 2. |
| `INGESTED` | Complete | Go to step 3. |

## 2. Blocking data-quality errors

Read `quality_report.findings`.

| Code | Cause | Correct response |
| --- | --- | --- |
| `FILE_EMPTY` | No data rows | Ask for the file again. |
| `MALFORMED_ROWS` | Row field count differs from the header | Usually an unquoted delimiter inside a narrative. Ask for a properly quoted export. |
| `AMBIGUOUS_DATE_FORMAT` | Every day and month value is 12 or less | The user must state the format. Guessing silently misdates a month of transactions. |
| `INVALID_CURRENCY` | Missing or malformed currency | Fix the export, or map a static currency value. |
| `DUPLICATE_SOURCE_RECORD_ID` | The same ID on two rows | The source system's ID is not unique. Map a different column, or a composite. |
| `DUPLICATE_TRANSACTION_ID` | The same external transaction ID twice | Either a duplicated export or a duplicate payment. Investigate before re-exporting. |
| `TOTAL_IMBALANCE` | The sum does not match the declared control total | The export is incomplete. Get a new one. |
| `ROW_MAPPING_FAILED` | Specific rows could not be mapped | The per-row reasons are in the response. |

Never bypass a quality gate by editing the source file. If the data is genuinely
correct and the gate fired anyway, the gate is the bug.

## 3. Ingested, but rows appear to be missing

Compare `transactions_created`, `duplicates_skipped` and `rows_failed`.

- **`duplicates_skipped` high, `transactions_created` zero.** The file was
  already ingested. This is idempotency working. Confirm with
  `GET /api/v1/transactions?search=<a known reference>`.
- **`rows_failed` non-zero.** Those rows could not be mapped; the reasons are in
  the response. Usually a blank amount or an unparseable date on specific rows.
- **Counts look right but a reconciliation sees nothing.** The run's period
  filter uses the effective date - transaction, else posting, else value. Check
  that the mapping populated a date at all, and that the run period covers it.

## 4. Escalate

Escalate to platform engineering when the checksum in `source_files` does not
match the stored object. That means object storage lost or altered an upload,
which is a data-integrity incident: follow `security-incident.md`.
