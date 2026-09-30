import type { Machine } from "../api/useMachine";
import { Badge, Button, Row } from "./ui";
import { date, money, short } from "../lib/format";
export function StakePositionCard({ m }: { m: Machine }) {
  const p = m.workspace.data?.stake_position;
  const wf = m.workspace.data?.stake_workflow;
  if (!p) return null;
  const busy = !!m.busy || !m.session.data?.authenticated || !!m.pending;
  return (
    <section className="companion-card" aria-label="Native staking position">
      <div className="eyebrow">TRON Native · Stake 2.0</div>
      <h3>
        {money(p.amount)} <Badge>{p.status.replaceAll("_", " ")}</Badge>
      </h3>
      <Row label="Representative">{short(p.representative)}</Row>
      <Row label="Original net forecast">{money(p.forecast_net)}</Row>
      <Row label="Rewards accrued">{money(p.accrued)}</Row>
      <Row label="Rewards claimed">{money(p.realized)}</Row>
      <Row label="Actual fees paid">{money(p.fees)}</Row>
      <Row label="Current net income">{money(p.net_income)}</Row>
      <p className="caption">
        Updated {date(p.as_of)}. {p.basis}
      </p>
      {p.status === "UNFREEZING" && p.unfreeze_at && (
        <p>Available after {date(new Date(p.unfreeze_at).toISOString())}</p>
      )}
      <div className="condition-actions">
        <Button
          secondary
          disabled={busy}
          onClick={() => m.stakeAction("refresh")}
        >
          Refresh rewards
        </Button>
        {p.status === "STAKED_NOT_VOTED" && wf?.status === "NEXT_REVIEW" && (
          <Button disabled={busy} onClick={() => m.stakeAction("next")}>
            Activate voting rewards
          </Button>
        )}
        {["EARNING", "REWARDS_PENDING"].includes(p.status) && (
          <Button
            secondary
            disabled={busy || !p.accrued || Number(p.accrued.value) <= 0}
            onClick={() => m.stakeAction("CLAIM")}
          >
            Review reward claim
          </Button>
        )}
        {["EARNING", "STAKED_NOT_VOTED"].includes(p.status) && (
          <Button
            secondary
            disabled={busy}
            onClick={() => m.stakeAction("UNSTAKE")}
          >
            Review unstaking
          </Button>
        )}
        {p.status === "UNFREEZING" && (
          <Button
            disabled={busy || !p.unfreeze_at || m.clock < p.unfreeze_at}
            onClick={() => m.stakeAction("WITHDRAW")}
          >
            Withdraw unlocked TRX
          </Button>
        )}
      </div>
    </section>
  );
}
export function ProductCatalogCard({ m }: { m: Machine }) {
  const rows = m.workspace.data?.product_catalog;
  if (!rows?.length) return null;
  return (
    <details className="companion-card companion-disclosure">
      <summary>
        Available routes<span>{rows.length} checked capabilities</span>
      </summary>
      {rows.map((r) => (
        <article className="holding-item" key={r.id}>
          <strong>{r.name}</strong>
          <Badge>{r.status}</Badge>
          <p className="caption">{r.reason}</p>
        </article>
      ))}
      <p className="caption">
        Capabilities describe this release. Each comparison still requires fresh
        network data and your confirmed limits.
      </p>
    </details>
  );
}
