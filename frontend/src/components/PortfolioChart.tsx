import { useState } from "react";
import type { Workspace } from "../api/contracts";
import { decimal, date } from "../lib/format";
import { portfolioAllocation } from "../lib/portfolio";
import { BalancePerformance } from "./BalancePerformance";
const precise = (a: { value: string; symbol: string; decimals: number }) =>
  `${decimal(a.value, Math.min(a.decimals, 6))} ${a.symbol}`;
const colors = [
  "#718678",
  "#c39858",
  "#7799ac",
  "#a28fb5",
  "#ce8972",
  "#93a56a",
];
export function PortfolioChart({ workspace: w }: { workspace: Workspace }) {
  const assets = [
    ...new Set([
      ...w.balances.map((a) => a.symbol),
      ...w.positions
        .filter((p) => p.network === w.network)
        .flatMap((p) => (p.current_value ? [p.current_value.symbol] : [])),
    ]),
  ];
  const [chosen, setChosen] = useState("");
  const [active, setActive] = useState<string | null>(null);
  const symbol = assets.includes(chosen)
    ? chosen
    : assets.includes("TRX")
      ? "TRX"
      : assets[0];
  if (!symbol)
    return (
      <p className="muted">
        The allocation chart appears when balances are available.
      </p>
    );
  const data = portfolioAllocation(w, symbol);
  const selected = data.slices.find((s) => s.id === active);
  let offset = 0;
  return (
    <section
      className="portfolio-allocation companion-card"
      aria-label="Portfolio allocation"
    >
      <div className="section-heading">
        <div>
          <h3>Your balance</h3>
          <p className="caption">
            {""}
            {w.network === "nile" ? "Nile testnet" : "Mainnet"}
          </p>
        </div>
        {assets.length > 1 && (
          <select
            aria-label="Chart asset"
            value={symbol}
            onChange={(e) => {
              setChosen(e.target.value);
              setActive(null);
            }}
          >
            {assets.map((a) => (
              <option key={a}>{a}</option>
            ))}
          </select>
        )}
      </div>
      <div className="donut-wrap">
        <svg
          viewBox="0 0 200 200"
          role="img"
          aria-label={
            data.slices.length
              ? data.slices
                  .map(
                    (s) =>
                      `${s.label}: ${precise(s.amount)}, ${s.percent.toFixed(2)}%`,
                  )
                  .join(". ")
              : `No positive ${symbol} balance`
          }
        >
          <circle
            cx="100"
            cy="100"
            r="78"
            fill="none"
            stroke="var(--line)"
            strokeWidth="20"
          />
          {data.slices.map((slice, i) => {
            const start = offset;
            offset += slice.percent;
            return (
              <circle
                key={slice.id}
                cx="100"
                cy="100"
                r="78"
                pathLength="100"
                fill="none"
                stroke={colors[i % colors.length]}
                strokeWidth={selected?.id === slice.id ? 26 : 20}
                strokeDasharray={`${slice.percent} ${100 - slice.percent}`}
                strokeDashoffset={-start}
                transform="rotate(-90 100 100)"
                opacity={selected && selected.id !== slice.id ? 0.35 : 1}
              >
                <title>
                  {slice.label}: {precise(slice.amount)}
                </title>
              </circle>
            );
          })}
        </svg>
        <div className="donut-center">
          <span>{selected?.label || "Total assets"}</span>
          <strong>
            {decimal((selected?.amount || data.total).value, 2)} {symbol}
          </strong>
          <small>
            {selected
              ? `${selected.percent < 0.1 ? "<0.1" : selected.percent.toFixed(1)}%`
              : "Gross assets"}
          </small>
        </div>
      </div>
      <ul className="allocation-legend">
        {data.slices.map((slice, i) => (
          <li key={slice.id}>
            <button
              aria-pressed={active === slice.id}
              onClick={() => setActive(active === slice.id ? null : slice.id)}
            >
              <span
                className="legend-dot"
                style={{ background: colors[i % colors.length] }}
              />
              <span>
                {slice.label}
                <small>{precise(slice.amount)}</small>
              </span>
              <strong>
                {slice.percent < 0.1 ? "<0.1" : slice.percent.toFixed(1)}%
              </strong>
            </button>
          </li>
        ))}
      </ul>
      {data.unpriced > 0 && (
        <p className="caption">
          {data.unpriced} position(s) have no current valuation and are
          excluded.
        </p>
      )}
      {assets.length > 1 && (
        <p className="caption">
          Each asset is shown separately; no exchange rate is assumed.
        </p>
      )}
      <BalancePerformance workspace={w} symbol={symbol} />
      <details className="chart-basis">
        <summary>Valuation details</summary>
        <p className="caption">
          {w.performance
            ? `Position observed ${date(w.performance.as_of)}. `
            : ""}
          Wallet and position values may have different observation times. Past
          fees are excluded. This is gross asset value before debt.
        </p>
      </details>
    </section>
  );
}
