import type { Amount, Network } from "../api/contracts";
export function decimal(value: string, places = 2) {
  const [whole, fraction = ""] = value.split(".");
  return (
    whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",") +
    (fraction
      ? "." + fraction.slice(0, places).replace(/0+$/, "")
      : ""
    ).replace(/\.$/, "")
  );
}
export const money = (a: Amount | null | undefined) =>
  a
    ? `${decimal(a.value, Math.min(a.decimals, 6))} ${a.symbol}`
    : "Not measured";
export const short = (s: string) =>
  s.length > 18 ? `${s.slice(0, 7)}…${s.slice(-6)}` : s;
export const human = (s: string) =>
  s
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/^./, (c) => c.toUpperCase());
export const time = (s: string) =>
  new Intl.DateTimeFormat("en", { hour: "numeric", minute: "2-digit" }).format(
    new Date(s),
  );
export const date = (s: string) =>
  new Intl.DateTimeFormat("en", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(s));
export const expired = (s: string) =>
  !Number.isFinite(Date.parse(s)) || Date.parse(s) <= Date.now();
export const explorer = (n: Network, txid: string) =>
  /^[a-f\d]{64}$/i.test(txid)
    ? `https://${n === "nile" ? "nile." : ""}tronscan.org/#/transaction/${txid}`
    : null;
export function toUnits(value: string, decimals: number): bigint {
  if (!/^\d+(\.\d+)?$/.test(value))
    throw new Error("Enter a positive decimal amount.");
  const [a, b = ""] = value.split(".");
  if (b.length > decimals)
    throw new Error(`Use at most ${decimals} decimal places.`);
  return BigInt(a + b.padEnd(decimals, "0"));
}
export function downloadJson(value: unknown, name: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
