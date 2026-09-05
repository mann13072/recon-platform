import { describe, expect, it } from "vitest";
import { formatMoney, isZero } from "@/lib/money";

describe("money", () => {
  it("formats decimal strings without binary floating-point conversion", () => {
    expect(formatMoney("982.45", "EUR")).toBe("EUR 982.45");
    expect(formatMoney("0.105", "EUR")).toBe("EUR 0.11");
  });
  it("compares exact decimal zero", () => expect(isZero("0.00000000")).toBe(true));
});
