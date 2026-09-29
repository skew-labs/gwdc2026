import type { Workspace } from "../api/contracts";
import { date, decimal, money } from "../lib/format";

export function BalancePerformance({ workspace: w, symbol }: { workspace: Workspace; symbol: string }) {
  const p = w.performance;
  const matching = p?.network === w.network && (p.supplied_capital || p.net_income || p.accrued)?.symbol === symbol;
  if (!p || !matching) return <div className="balance-performance"><h4>Expected vs. actual</h4><p className="caption">Performance will appear here after a verified investment in {symbol}.</p></div>;
  const known = p.status !== "INCOMPLETE";
  const fresh = known && p.period_empty === true;
  const percent = (value: string | null | undefined) => value != null && known ? `${decimal(value, 2)}%` : "Not measured";
  const actualReturn = fresh ? "0%" : percent(p.net_return_pct);
  return <section className="balance-performance" aria-label="Expected versus actual performance">
    <div className="section-heading"><h4>Expected vs. actual</h4><span className="caption">{p.period_id ? "Current period" : "To date"}</span></div>
    {fresh && <p className="quiet-note">New performance period. No investments yet. Returns start at 0%; your next investment starts measurement.</p>}
    {known ? <>
      <div className="balance-return"><span>{fresh ? "Starting return" : "Net return on supplied capital"}</span><strong>{actualReturn}</strong></div>
      <table className="performance-comparison">
        <thead><tr><th scope="col">After fees</th><th scope="col">Expected</th><th scope="col">Actual</th></tr></thead>
        <tbody>
          <tr><th scope="row">Net income</th><td>{money(p.expected_to_date)}</td><td>{money(p.net_income)}</td></tr>
          <tr><th scope="row">Return</th><td>{percent(p.expected_return_pct)}</td><td>{actualReturn}</td></tr>
        </tbody>
      </table>
      <div className="income-split">
        <div><span>Accrued income</span><strong>{money(p.accrued)}</strong></div>
        <div><span>Realized income</span><strong>{money(p.realized)}</strong></div>
      </div>
      <p className="caption">Income before fees · {money(p.fees)} fees paid</p>
    </> : <p className="quiet-note">We’re checking the position history. Returns are unavailable until it reconciles.</p>}
    <details className="chart-basis"><summary>How this is measured</summary>
      <p className="caption">Net income includes paid fees. Return = net income ÷ total supplied capital ({money(p.supplied_capital)}). This measures investments in the current performance period, not annual APY or a return on your whole wallet. Reinvested capital counts again in this simple ratio.</p>
      {p.period_id && <p className="caption">Earlier transactions remain in your receipts. Their income and fees belong to the previous period.</p>}
      <p className="caption">Accrued income remains in the position. Realized income is the gain or loss on withdrawn principal, before fees. Lower fees can improve actual vs. expected without creating investment yield.</p>
      <p className="caption">Observed {date(p.as_of)}{p.period_start ? ` · Since ${date(p.period_start)}` : ""}</p>
    </details>
  </section>;
}
