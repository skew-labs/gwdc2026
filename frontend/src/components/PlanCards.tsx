import { useSearchParams } from "react-router-dom";
import {
  ArrowUpRight,
  ChartNoAxesCombined,
  Check,
  Info,
  ShieldCheck,
} from "lucide-react";
import type { Machine } from "../api/useMachine";
import { Badge, Button, Row } from "./ui";
import { expired, money } from "../lib/format";
export function MandateCard({
  m,
  edit,
  inConversation = false,
  onCompared,
}: {
  m: Machine;
  edit: () => void;
  inConversation?: boolean;
  onCompared?: () => void;
}) {
  const mandate = m.workspace.data?.mandate;
  if (!mandate) return null;
  const c = mandate.constraints;
  const limits = mandate.terms?.limits as
    Record<string, { asset: string; amount: string }> | undefined;
  return (
    <section
      className="artifact mandate-card companion-card"
      aria-label="Mandate confirmation"
    >
      <div className="artifact-heading">
        <ShieldCheck />
        <div>
          <strong>
            {inConversation
              ? "Review your conditions"
              : "Your investment conditions"}
          </strong>
          <small>Version {mandate.version}</small>
        </div>
        <Badge>{mandate.status === "DRAFT" ? "Draft" : "Confirmed"}</Badge>
      </div>
      <div className="artifact-body">
        <div className="mandate-metrics">
          <div>
            <span>Investment budget</span>
            <strong>{money(c.capital)}</strong>
          </div>
          <div>
            <span>Time horizon</span>
            <strong>
              {c.horizon_days}
              <small> days</small>
            </strong>
          </div>
        </div>
        <div className="cash-condition">
          <div>
            <span>Keep available as cash</span>
            <strong>At least {c.min_cash_bps / 100}%</strong>
          </div>
          <div className="cash-track">
            <span style={{ width: `${c.min_cash_bps / 100}%` }} />
          </div>
        </div>
        <Row label="Borrowing">
          {c.allow_debt ? "Permitted within debt limits" : "Not permitted"}
        </Row>
        <Row label="Fee cap per transaction">{money(c.max_fee)}</Row>
        {c.allow_debt && (
          <p className="quiet-note">
            Review USDD strategies in Portfolio. Each step needs current
            liquidity, a positive return after costs and a separate wallet signature.
          </p>
        )}
        <details
          className="companion-disclosure"
          open={mandate.status === "DRAFT"}
        >
          <summary>
            Spending & risk limits<span>Review before confirming</span>
          </summary>
          <Row label="TRX exposure cap">{c.max_trx_exposure_bps / 100}%</Row>
          {Object.entries({
            single_amount: "Maximum single allocation",
            cumulative_amount: "Maximum total invested",
            fee_amount: "Total planning cost budget",
            daily_loss: "Daily loss limit",
            stress_loss: "Stress loss limit",
          }).map(
            ([key, label]) =>
              limits?.[key] && (
                <Row key={key} label={label}>
                  {limits[key].amount} {limits[key].asset}
                </Row>
              ),
          )}
          <details className="policy-record">
            <summary>Full policy & assumptions</summary>
            <p>{mandate.source_text}</p>
            <pre className="mono">
              {JSON.stringify(
                {
                  policy: mandate.terms,
                  planning_assumptions: m.workspace.data?.planning_assumptions,
                },
                null,
                2,
              )}
            </pre>
          </details>
        </details>
        {mandate.missing_fields.length > 0 && (
          <p className="warning-text">
            Still needed: {mandate.missing_fields.join(", ")}
          </p>
        )}
      </div>
      <div className="artifact-footer">
        <Button secondary onClick={edit} disabled={!!m.busy}>
          Edit conditions
        </Button>
        {mandate.status === "DRAFT" ? (
          <Button
            onClick={async () => {
              if (await m.confirmAndCompare(mandate)) onCompared?.();
            }}
            disabled={!!m.busy || mandate.missing_fields.length > 0}
          >
            Confirm & compare <Check size={16} />
          </Button>
        ) : (
          <Button
            onClick={async () => {
              if (await m.compare()) onCompared?.();
            }}
            disabled={!!m.busy}
          >
            Compare plans <ArrowUpRight size={16} />
          </Button>
        )}
      </div>
    </section>
  );
}

export function PlanCards({
  m,
  onReview,
  onEdit,
}: {
  m: Machine;
  onReview: () => void;
  onEdit?: () => void;
}) {
  const comparison = m.workspace.data?.comparison;
  const [params, setParams] = useSearchParams();
  const selected = params.get("plan");
  const setSelected = (id: string) =>
    setParams({ ...Object.fromEntries(params), plan: id });
  if (!comparison) return null;
  const plan =
    comparison.plans.find((p) => p.id === selected) || comparison.plans[0];
  const stale =
    expired(comparison.expires_at) ||
    comparison.status === "STALE" ||
    comparison.mandate_hash !== m.workspace.data?.mandate?.hash;
  if (comparison.status === "INFEASIBLE" || comparison.plans.length < 2)
    return (
      <section className="artifact">
        <div className="artifact-heading">
          <Info />
          <strong>No two eligible plans</strong>
        </div>
        <div className="artifact-body">
          <p>
            {comparison.reason ||
              "Two distinct plans could not be found within your conditions."}
          </p>
          <p className="muted">
            Your limits have been preserved. Review your conditions to request
            another comparison.
          </p>
          {onEdit && (
            <Button secondary onClick={onEdit}>
              Edit conditions
            </Button>
          )}
        </div>
      </section>
    );
  return (
    <>
      <section className="artifact" aria-label="Allocation comparison">
        <div className="artifact-heading">
          <ChartNoAxesCombined />
          <div>
            <strong>
              {m.workspace.data?.mandate?.constraints.horizon_days}-day capital
              allocation
            </strong>
            <small>Same mandate. Two eligible allocations.</small>
          </div>
          <Badge tone={stale ? "warn" : "neutral"}>
            {stale ? "Expired" : "Compare"}
          </Badge>
        </div>
        <div className="plan-options">
          {comparison.plans.map((p, i) => (
            <section
              className={`plan-option ${p.id === plan?.id ? "selected" : ""}`}
              key={p.id}
            >
              <button
                className="plan-select"
                aria-pressed={p.id === plan?.id}
                onClick={() => setSelected(p.id)}
              >
                <span>Plan {String.fromCharCode(65 + i)}</span>
                <span className="selection-circle">
                  {p.id === plan?.id && <Check size={11} />}
                </span>
              </button>
              <h3>{p.title}</h3>
              <div
                className="allocation-bar"
                role="img"
                aria-label={p.allocations
                  .map((x) => `${x.product}: ${x.share_bps / 100}%`)
                  .join(", ")}
              >
                {p.allocations.map((x) => (
                  <span
                    key={x.product}
                    className={x.kind === "CASH" ? "cash" : ""}
                    style={{ width: `${x.share_bps / 100}%` }}
                  />
                ))}
              </div>
              {p.allocations.map((x) => (
                <Row
                  key={x.product}
                  label={x.kind === "CASH" ? "Wallet cash" : x.product}
                >
                  {money(x.amount)}
                </Row>
              ))}
              <Row label="Expected net return">
                {money(p.expected_net_return)}
              </Row>
              <Row label="Estimated costs">{money(p.estimated_fees)}</Row>
              <details>
                <summary>Yield, exits & risks</summary>
                <p>{p.summary}</p>
                {p.allocations.map((x) => (
                  <div className="plan-detail" key={x.product}>
                    <strong>{x.product}</strong>
                    <Row label="Base yield">{money(x.base_yield)}</Row>
                    <Row label="Incentives">{money(x.incentive_rewards)}</Row>
                    {x.costs.map((c) => (
                      <Row key={c.label} label={c.label}>
                        {money(c.amount)}
                      </Row>
                    ))}
                    <p>{x.participation_terms}</p>
                    <p>Exit: {x.exit_description}</p>
                  </div>
                ))}
                <ul>
                  {p.risks.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
                {p.recoverable_cash.map((x) => (
                  <p key={x.days}>
                    Within {x.days} days: {money(x.amount)} — {x.evidence}
                  </p>
                ))}
              </details>
            </section>
          ))}
        </div>
        <div className="artifact-footer">
          <span className="muted">
            {stale
              ? "Market inputs expired. Refresh before continuing."
              : "Each plan follows your confirmed conditions."}
          </span>
          {onEdit && (
            <Button secondary onClick={onEdit} disabled={!!m.busy}>
              Edit conditions
            </Button>
          )}
          {stale ? (
            <Button onClick={m.compare} disabled={!!m.busy}>
              Refresh plans
            </Button>
          ) : (
            <Button
              onClick={async () => {
                if (!m.session.data?.authenticated) {
                  await m.connect();
                  return;
                }
                if (plan && (await m.review(plan))) onReview();
              }}
              disabled={!!m.busy || !plan?.eligible}
            >
              {m.session.data?.authenticated
                ? "Review selected plan"
                : "Connect to review"}
              <ArrowUpRight size={16} />
            </Button>
          )}
        </div>
      </section>
      <div className="inline-note">
        <Info size={14} />
        <span>USDD Vault · {comparison.usdd_vault.reason}</span>
      </div>
      <details className="calculation-note">
        <summary>Calculation & sources</summary>
        <p>{comparison.search_scope}</p>
        <p>
          Math: {comparison.math_version} · Adapter:{" "}
          {comparison.adapter_version}
        </p>
        <p>Snapshot: {comparison.snapshot_root}</p>
      </details>
    </>
  );
}
