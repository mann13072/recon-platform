import { describe, expect, it } from "vitest";
import { runSummarySchema } from "@/lib/schemas";

const summary = {
  run_id: "7bdcd1d6-d5b7-4f25-a967-7301d1cbdd87",
  reconciliation_id: "e9567bf3-5958-4236-b132-cb5869701577",
  status: "COMPLETED", period_start: "2026-08-01", period_end: "2026-08-31", currency: "EUR",
  side_a_balance: "9012.00", side_b_balance: "9012.00", difference: "0.00", matched_amount: "8476.25",
  matched_transaction_count: 12, auto_matched_count: 5, human_approved_count: 1, suggested_count: 2,
  exception_count: 3, high_risk_exception_count: 1, unmatched_a_count: 2, unmatched_b_count: 1,
  oldest_exception_age_days: 4, completion_pct: 80, auto_match_rate: .5, false_match_rate: null, manual_review_rate: .2,
};

describe("wire schemas", () => {
  it("accepts API money strings", () => expect(runSummarySchema.parse(summary).difference).toBe("0.00"));
  it("rejects JSON numbers for money", () => expect(() => runSummarySchema.parse({ ...summary, difference: 0 })).toThrow());
});
