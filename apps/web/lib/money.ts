import Decimal from "decimal.js";

export type MoneyString = string & { readonly __money: unique symbol };

export function asMoney(value: string): MoneyString {
  return value as MoneyString;
}

export function formatMoney(value: MoneyString | string, currency: string): string {
  const precise = new Decimal(value);
  return `${currency} ${precise.toDecimalPlaces(2).toFixed(2)}`;
}

export function isZero(value: MoneyString | string): boolean {
  return new Decimal(value).isZero();
}
