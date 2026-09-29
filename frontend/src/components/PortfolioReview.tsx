import { useSearchParams } from "react-router-dom";
import type { Machine } from "../api/useMachine";
import { Badge, Button, Empty, Row } from "./ui";
import { date, expired, short } from "../lib/format";

const reasons: Record<string, string> = {
  PAID_FEE_LIMIT_BREACH: "Recorded fees exceed your confirmed fee budget",
  CASH_FLOOR_BREACH: "Available cash is below your confirmed minimum",
  PROTOCOL_CAP_BREACH: "JustLend exposure exceeds your confirmed cap",
  DAILY_LOSS_LIMIT_BREACH: "Assumed daily loss exceeds your limit",
  STRESS_LOSS_LIMIT_BREACH: "Assumed stress loss exceeds your limit",
  DEBT_LIMIT_BREACH: "Debt exceeds your confirmed permission or limit",
  TRX_EXPOSURE_BREACH:
    "TRX exposure exceeds your confirmed cap, including wallet cash",
  EXIT_LIQUIDITY_SHORTFALL: "The pool has insufficient cash for a full exit",
  CASH_AND_EXIT_RESERVE_LIMIT:
    "Cash after fees and the exit reserve would be too low",
  SINGLE_AMOUNT_LIMIT: "Change exceeds the transaction amount limit",
  CUMULATIVE_AMOUNT_LIMIT: "Change exceeds remaining cumulative permission",
  EXIT_NOT_VERIFIED: "This exit amount could not be verified",
  ACTION_NOT_ALLOWED: "Action is outside your confirmed permissions",
  TOTAL_COST_LIMIT:
    "Paid fees, change cost and the exit reserve exceed your total cost limit",
  FEE_LIMIT: "Live cost bound exceeds your confirmed fee limit",
};
const label = (code: string) =>
  reasons[code] || code.toLowerCase().replaceAll("_", " ");
const titles: Record<string, string> = {
  HOLD: "Keep your current allocation",
  ADJUST: "An adjustment is worth reviewing",
  POLICY_BREACH: "Conditions need attention",
  POLICY_INACTIVE: "Renew your investment horizon",
  DATA_UNAVAILABLE: "Review incomplete",
  PENDING_EXECUTION: "Transaction reconciliation needed",
};

export function PortfolioReviewCard({
  m,
  targetId,
}: {
  m: Machine;
  targetId?: string;
}) {
  const [params, setParams] = useSearchParams();
  const r = m.workspace.data?.portfolio_review;
  const trx = (n: string | null | undefined) => n == null ? "Unavailable" : `${n} ${r?.denomination || "TRX"}`;
  const open = (panel: string, edit?: string) => {
    const p = new URLSearchParams(params);
    p.set("panel", panel);
    p.delete("edit");
    if (edit) p.set("edit", edit);
    setParams(p);
  };
  if (!r)
    return (
      <Empty title="No portfolio review yet">
        <p>
          Refresh your holdings to compare keeping them with a permitted
          adjustment.
        </p>
        <Button
          disabled={!!m.busy || !m.session.data?.authenticated}
          onClick={m.reviewPortfolio}
        >
          Review current portfolio
        </Button>
      </Empty>
    );
  if (targetId && targetId !== r.id)
    return (
      <div className="conversation-reference">
        <span>A newer portfolio review is available.</span>
        <Button secondary onClick={() => open("portfolio")}>
          View latest review
        </Button>
      </div>
    );
  const stale = expired(r.expires_at);
  const draft = m.workspace.data?.mandate?.status === "DRAFT";
  return (
    <section
      className="portfolio-review companion-card"
      aria-label="Hold versus adjust"
    >
      <div className="review-intro">
        <div className={`watch-mark ${r.status === "HOLD" ? "" : "attention"}`}>
          W
        </div>
        <div>
          <span className="eyebrow">Watch review</span>
          <h3>{titles[r.status]}</h3>
        </div>
      </div>
      <p className="review-reason">{r.reason}</p>
      <div className="review-freshness">
        <span className={stale ? "" : "live-dot"} />
        {stale ? "Update needed" : "Checked"} · {date(r.observed_at)}
      </div>
      {(m.workspace.data?.mandate?.status === "DRAFT" ||
        m.workspace.data?.intent) && (
        <p className="quiet-note">
          Using your last confirmed conditions. Draft edits are not active.
          {draft && " You can separately review and approve a withdrawal without confirming these edits."}
        </p>
      )}
      {r.checks.length > 0 && (
        <ul className="review-checks">
          {r.checks.map((c) => (
            <li key={c}>{label(c)}</li>
          ))}
        </ul>
      )}
      <div className="condition-actions">
        <Button
          disabled={!!m.busy || !m.session.data?.authenticated}
          onClick={m.reviewPortfolio}
        >
          Check now
        </Button>
        <Button
          secondary
          onClick={() =>
            open(
              draft || r.status === "POLICY_BREACH" || r.status === "POLICY_INACTIVE"
                ? "mandate"
                : "routines",
              draft || r.status === "POLICY_BREACH" || r.status === "POLICY_INACTIVE"
                ? "conditions"
                : undefined,
            )
          }
        >
          {draft || r.status === "POLICY_BREACH" || r.status === "POLICY_INACTIVE"
            ? "Review conditions"
            : "Review routine"}
        </Button>
      </div>
      {r.hold && (
        <details className="companion-disclosure">
          <summary>
            Keep or adjust
            <span>{r.alternatives.length + 1} options checked</span>
          </summary>
          <div className="review-options">
            <article>
              <div className="option-heading">
                <strong>Keep current holdings</strong>
                <Badge>No trade</Badge>
              </div>
              <p>{trx(r.hold.position)} in JustLend</p>
              <Row label="Cost to change">{trx("0")}</Row>
              <details>
                <summary>Income & exit costs</summary>
                <Row label={r.denomination === "USDD" ? "Projected carry after interest" : "Projected income"}>{trx(r.hold.gross_income)}</Row>
                <Row label="Future exit estimate">
                  {trx(r.hold.future_exit_estimate)}
                </Row>
                <Row label="Exit cash reserve">{trx(r.hold.exit_reserve)}</Row>
                <p className="caption">
                  Past entry fees are already paid and are excluded from this
                  decision.
                </p>
              </details>
            </article>
            {r.alternatives.map((a, i) => (
              <article key={i} className={!a.eligible ? "review-blocked" : ""}>
                <div className="option-heading">
                  <strong>
                    {a.action === "SUPPLY" ? "Supply more" : "Withdraw to cash"}
                  </strong>
                  <Badge>{a.eligible ? "Within limits" : "Blocked"}</Badge>
                </div>
                <p>{trx(a.position)} remaining in JustLend</p>
                <Row label="Cost to change">{trx(a.change_cost)}</Row>
                <Row label="Improvement after costs">
                  {trx(a.net_improvement)}
                </Row>
                {a.hash && r.network === "nile" && (
                  <Button secondary
                    disabled={!!m.busy || (draft && a.action !== "REDEEM") || stale || !a.eligible || !m.session.data?.authenticated ||
                      ["DATA_UNAVAILABLE", "POLICY_INACTIVE", "PENDING_EXECUTION"].includes(r.status)}
                    onClick={() => m.adjustPortfolio(r.id, a.hash!)}>
                    {a.action === "REDEEM" ? "Review withdrawal" : "Review additional supply"}
                  </Button>
                )}
                {a.reasons.length > 0 && (
                  <ul>
                    {a.reasons.map((x) => (
                      <li key={x}>{label(x)}</li>
                    ))}
                  </ul>
                )}
                <details>
                  <summary>Income & exit costs</summary>
                  <Row label="Projected income">{trx(a.gross_income)}</Row>
                  <Row label="Future exit estimate">
                    {trx(a.future_exit_estimate)}
                  </Row>
                  <Row label="Exit cash reserve">{trx(a.exit_reserve)}</Row>
                  <Row label="Extra income vs. holding">
                    {trx(a.benefit_over_hold)}
                  </Row>
                  <Row label="Additional cost">{trx(a.additional_cost)}</Row>
                </details>
              </article>
            ))}
          </div>
        </details>
      )}
      <details className="companion-disclosure">
        <summary>
          Conditions & calculation<span>View basis</span>
        </summary>
        {r.capital && <Row label="Policy budget">{trx(r.capital)}</Row>}
        {r.paid_fees && <Row label="Already paid">{trx(r.paid_fees)}</Row>}
        {r.remaining_budget && (
          <Row label="Budget after fees">{trx(r.remaining_budget)}</Row>
        )}
        {r.cash_floor && <Row label="Minimum cash">{trx(r.cash_floor)}</Row>}
        {r.remaining_seconds !== undefined && (
          <Row label="Remaining horizon">
            {(r.remaining_seconds / 86400).toFixed(2)} days
          </Row>
        )}
        <p className="caption">
          {r.block ? `Block ${r.block} · ` : ""}
          {r.policy_hash
            ? `Policy ${short(r.policy_hash)}`
            : "No confirmed policy"}
        </p>
        <ul className="review-checks">
          {r.limitations.map((x) => (
            <li key={x}>{x}</li>
          ))}
        </ul>
        <p className="caption">
          Only your policy budget is compared. A reserve is not a paid fee.
          Future exit estimates assume current resource costs continue.
        </p>
        {r.rate && (
          <p className="caption">
            Observed APY: {(Number(r.rate.annual_fraction) * 100).toFixed(4)}% ·
            ACT/365F
          </p>
        )}
        <Button secondary onClick={() => open("mandate", "conditions")}>
          Edit conditions
        </Button>
      </details>
      <p className="quiet-note">
        Any trade needs a separate review and your wallet signature.
      </p>
    </section>
  );
}

export function Notifications({ m }: { m: Machine }) {
  const [params, setParams] = useSearchParams();
  const rows = m.workspace.data?.notifications || [];
  return (
    <div className="notification-list">
      <p className="muted">
        Account alerts from your portfolio checks. Routine alerts are stored
        here even while you are away.
      </p>
      {!rows.length && (
        <Empty title="No account alerts">
          You will see a notice when a check finds an adjustment, a conditions
          breach, or missing data.
        </Empty>
      )}
      {[...rows].reverse().map((n) => (
        <article
          className={`notification-item ${n.read_at ? "" : "unread"}`}
          key={n.id}
        >
          <div className="section-heading">
            <strong>{n.title}</strong>
            <Badge>
              {n.resolved_at ? "Resolved" : n.read_at ? "Read" : "New"}
            </Badge>
          </div>
          <p>{n.detail}</p>
          <small>{date(n.created_at)}</small>
          <div className="condition-actions">
            <Button
              secondary
              disabled={!!m.busy}
              onClick={async () => {
                const ok = await m.run("Opening portfolio alert", async () => {
                  await m.mutate(
                    `/v1/notifications/${encodeURIComponent(n.id)}/read`,
                    { network: m.network },
                  );
                  await m.mutate("/v1/portfolio-reviews", {
                    network: m.network,
                  });
                });
                if (ok) {
                  const p = new URLSearchParams(params);
                  p.set("panel", "portfolio");
                  setParams(p);
                }
              }}
            >
              Open & recheck
            </Button>
            {!n.read_at && (
              <Button
                secondary
                disabled={!!m.busy}
                onClick={() =>
                  m.run("Marking alert read", () =>
                    m.mutate(
                      `/v1/notifications/${encodeURIComponent(n.id)}/read`,
                      { network: m.network },
                    ),
                  )
                }
              >
                Mark read
              </Button>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}
