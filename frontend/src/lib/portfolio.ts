import type { Amount, Workspace } from "../api/contracts";
import { toUnits } from "./format";

export function portfolioAllocation(w: Workspace, symbol: string) {
  const entries: {
    id: string;
    label: string;
    amount: Amount;
    kind: "cash" | "position";
  }[] = [
    ...w.balances
      .filter((a) => a.symbol === symbol)
      .map((a, i) => ({
        id: `cash-${i}`,
        label: "Wallet cash",
        amount: a,
        kind: "cash" as const,
      })),
    ...w.positions
      .filter(
        (p) => p.network === w.network && p.current_value?.symbol === symbol,
      )
      .map((p) => ({
        id: p.id,
        label: p.product.replace(/^justlend\.v1\./, "JustLend "),
        amount: p.current_value!,
        kind: "position" as const,
      })),
  ];
  const decimals = Math.max(0, ...entries.map((e) => e.amount.decimals));
  const values = entries
    .filter((e) => !e.amount.value.startsWith("-"))
    .map((e) => ({ ...e, units: toUnits(e.amount.value, decimals) }))
    .filter((e) => e.units > 0n);
  const total = values.reduce((sum, e) => sum + e.units, 0n);
  const digits = total.toString().padStart(decimals + 1, "0");
  const value = decimals
    ? `${digits.slice(0, -decimals)}.${digits.slice(-decimals)}`.replace(
        /\.?0+$/,
        "",
      )
    : digits;
  return {
    total: { value: value || "0", symbol, decimals },
    slices: values.map((e) => ({
      ...e,
      percent: total ? Number((e.units * 100000000n) / total) / 1000000 : 0,
    })),
    unpriced: w.positions.filter(
      (p) => p.network === w.network && !p.current_value,
    ).length,
  };
}
