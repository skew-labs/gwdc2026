import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Activity,
  Download,
  ExternalLink,
  Layers,
  Repeat2,
  X,
} from "lucide-react";
import type { Machine } from "../api/useMachine";
import { Alert, Badge, Button, Empty, Row, Status } from "./ui";
import {
  date,
  downloadJson,
  expired,
  explorer,
  money,
  short,
} from "../lib/format";
import { NileFunding } from "./NileFunding";
import { MandateEditor } from "./MandateEditor";
import { MandateCard, PlanCards } from "./PlanCards";
import { UsddWorkflow } from "./UsddWorkflow";
import { StakePositionCard, ProductCatalogCard } from "./StakePositionCard";
import { ExecutionCard } from "./ExecutionCard";
import { PortfolioChart } from "./PortfolioChart";
import { PortfolioReviewCard, Notifications } from "./PortfolioReview";
export const panelTitles: Record<string, string> = {
  portfolio: "Your portfolio",
  notifications: "Account notifications",
  mandate: "Your mandate",
  routines: "Routines",
  activity: "Activity & sources",
  evidence: "Run evidence",
  settings: "Workspace settings",
  wallet: "Your wallet",
  execution: "Review transaction",
};
export type Panel = keyof typeof panelTitles;
function NewRoutine({ m }: { m: Machine }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [time, setTime] = useState("09:00");
  return open ? (
    <form
      className="routine-form"
      onSubmit={async (e) => {
        e.preventDefault();
        if (
          await m.run("Creating routine", () =>
            m.mutate("/v1/routines", {
              name,
              time,
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
              enabled: true,
              notify_on: ["Portfolio review"],
              network: m.network,
            }),
          )
        ) {
          setOpen(false);
          setName("");
        }
      }}
    >
      <label>
        Routine name
        <input
          required
          maxLength={80}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label>
        Daily at
        <input
          type="time"
          required
          value={time}
          onChange={(e) => setTime(e.target.value)}
        />
      </label>
      <p className="caption">
        Timezone: {Intl.DateTimeFormat().resolvedOptions().timeZone}. Alerts
        appear in Account notifications.
      </p>
      <Button type="submit" disabled={!!m.busy}>
        Create routine
      </Button>
      <Button secondary onClick={() => setOpen(false)}>
        Cancel
      </Button>
    </form>
  ) : (
    <Button secondary onClick={() => setOpen(true)}>
      Add routine
    </Button>
  );
}
export function RoutineEditor({ m }: { m: Machine }) {
  return (
    <div className="routine-list">
      {!m.workspace.data?.routines.length && (
        <Empty title="No routines yet">
          After investing, choose when to check your holdings, confirmed
          conditions and the costs of holding versus adjusting.
        </Empty>
      )}
      <div className="routine-intro">
        <div className="watch-mark">W</div>
        <h3>Watch keeps an eye on it.</h3>
        <p>
          Choose when to check. You’ll hear from us when something needs your
          attention.
        </p>
      </div>
      <NewRoutine m={m} />
      <Button
        secondary
        disabled={!!m.busy || !m.session.data?.authenticated}
        onClick={m.reviewPortfolio}
      >
        Review current portfolio
      </Button>
      {m.workspace.data?.routines.map((r) => (
        <RoutineItem key={r.id} routine={r} m={m} />
      ))}
      <details className="companion-disclosure">
        <summary>What Watch checks</summary>
        <p className="caption">
          Fresh balances, JustLend jTRX positions and rates on Nile. Confirmed
          conditions are checked before comparing hold and adjust, including
          entry and exit costs. Alerts are saved to your account; repeated
          incidents are grouped with a six-hour economic-alert cooldown. Watch
          cannot sign or execute trades.
        </p>
      </details>
    </div>
  );
}
function RoutineItem({
  m,
  routine: r,
}: {
  m: Machine;
  routine: NonNullable<Machine["workspace"]["data"]>["routines"][number];
}) {
  const [time, setTime] = useState(r.time);
  const [zone, setZone] = useState(r.timezone);
  const [enabled, setEnabled] = useState(r.enabled);
  const [notify, setNotify] = useState(r.notify_on);
  const [saved, setSaved] = useState(false);
  return (
    <form
      className="routine-form"
      onSubmit={async (e) => {
        e.preventDefault();
        setSaved(false);
        if (
          await m.run("Saving routine", () =>
            m.mutate(
              `/v1/routines/${encodeURIComponent(r.id)}`,
              {
                enabled,
                time,
                timezone: zone,
                notify_on: notify,
                network: m.network,
              },
              "PATCH",
            ),
          )
        )
          setSaved(true);
      }}
    >
      <div className="section-heading">
        <Repeat2 />
        <h3>{r.name}</h3>
      </div>
      <div className="routine-at">
        <strong>{r.time}</strong>
        <span>Every day · {r.timezone}</span>
        <Badge>{r.enabled ? "On" : "Paused"}</Badge>
      </div>
      {r.next_due_at && (
        <p className="quiet-note">Next check {date(r.next_due_at)}</p>
      )}
      <p className="quiet-note">
        {r.worker_status === "CONNECTED"
          ? "Watch is connected"
          : "Watch is offline"}
        {r.last_result
          ? ` · Last result: ${r.last_result.replaceAll("_", " ").toLowerCase()}`
          : " · First check pending"}
      </p>
      <details className="companion-disclosure">
        <summary>
          Edit routine<span>Schedule & alerts</span>
        </summary>
        <label className="check-label">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Enable daily portfolio review
        </label>
        <div className="form-pair">
          <label>
            Daily at
            <input
              type="time"
              value={time}
              required
              onChange={(e) => setTime(e.target.value)}
            />
          </label>
          <label>
            Timezone
            <select value={zone} onChange={(e) => setZone(e.target.value)}>
              {Array.from(
                new Set([
                  r.timezone,
                  "Asia/Seoul",
                  "UTC",
                  "America/New_York",
                  "Europe/London",
                ]),
              ).map((z) => (
                <option key={z}>{z}</option>
              ))}
            </select>
          </label>
        </div>
        <fieldset>
          <legend>Notify me about</legend>
          {["Portfolio review"].map((n) => (
            <label className="check-label" key={n}>
              <input
                type="checkbox"
                checked={notify.length > 0}
                onChange={(e) => setNotify(e.target.checked ? [n] : [])}
              />
              {n}
            </label>
          ))}
        </fieldset>
        {r.next_due_at && <Row label="Next run">{date(r.next_due_at)}</Row>}
        {r.last_checked_at && (
          <Row label="Last checked">{date(r.last_checked_at)}</Row>
        )}
        {r.last_result && (
          <Row label="Last result">{r.last_result.replaceAll("_", " ")}</Row>
        )}
        {r.last_error && <p className="field-error">{r.last_error}</p>}
        <Row label="Last successful observation">
          {r.last_success_at ? date(r.last_success_at) : "None"}
        </Row>
        <Button type="submit" disabled={!!m.busy}>
          Save routine
        </Button>
        {saved && (
          <p role="status" className="success-text">
            Routine saved.
          </p>
        )}
      </details>
    </form>
  );
}
export function Portfolio({ m }: { m: Machine }) {
  const w = m.workspace.data;
  if (!w) return null;
  const p = w.performance;
  return (
    <div className="portfolio-space">
      <PortfolioChart workspace={w} />
      {w.network === "nile" && w.positions.some(position => position.product === "justlend.v1.jTRX" && Number(position.current_value?.value) > 0) && (
        <section className="companion-card withdrawal-entry" aria-label="Withdraw your investment">
          <div className="eyebrow">JustLend</div>
          <h3>Back to your wallet</h3>
          <p>Review the amount and current fee before you withdraw.</p>
          <Button onClick={m.withdrawToWallet} disabled={!!m.busy || !m.session.data?.authenticated}>
            Withdraw to wallet
          </Button>
        </section>
      )}
      <StakePositionCard m={m} />
      <PortfolioReviewCard m={m} />
      <ProductCatalogCard m={m} />
      <UsddWorkflow m={m} />
      <details className="companion-card companion-disclosure">
        <summary>
          Holdings
          <span>
            {w.positions.length}{" "}
            {w.positions.length === 1 ? "position" : "positions"}
          </span>
        </summary>
        <div className="balance-list">
          {w.balances.map((a) => (
            <Row key={a.symbol} label="Wallet cash">
              {money(a)}
            </Row>
          ))}
        </div>
        {w.positions.length ? (
          w.positions.map((position) => (
            <article className="holding-item" key={position.id}>
              <div className="section-heading">
                <Layers />
                <strong>
                  {position.product.replace("justlend.v1.", "JustLend · ")}
                </strong>
                <Badge>{position.network}</Badge>
              </div>
              <div className="holding-value">
                {money(position.current_value)}
              </div>
              <Row label="Supplied">{money(position.principal)}</Row>
              <Row label="Debt">{money(position.debt)}</Row>
              <p className="caption">{position.exit_status}</p>
              {position.observed_at && (
                <small className="muted">
                  Observed {date(position.observed_at)}
                </small>
              )}
              {position.receipt_txid &&
                explorer(position.network, position.receipt_txid) && (
                  <a
                    className="text-link"
                    href={explorer(position.network, position.receipt_txid)!}
                    target="_blank"
                    rel="noreferrer"
                  >
                    View receipt <ExternalLink size={14} />
                  </a>
                )}
            </article>
          ))
        ) : (
          <Empty title="No positions yet">
            Executed and reconciled investments appear here.
          </Empty>
        )}
      </details>
      <details className="companion-card companion-disclosure">
        <summary>
          Performance & costs
          <span>{p?.fees ? `${money(p.fees)} paid` : "View details"}</span>
        </summary>
        {p ? (
          <>
            {p.status === "INCOMPLETE" && <p className="quiet-note">Income attribution needs reconciliation.</p>}
            <Row label="Original net forecast · full horizon">{money(p.expected_return)}</Row>
            <Row label="Actual minus expected">{money(p.variance ?? null)}</Row>
            <Row label="Accrued position income · before fees">{money(p.accrued)}</Row>
            <Row label="Realized position income · before fees">{money(p.realized)}</Row>
            <Row label="Fees already paid">{money(p.fees)}</Row>
            <p className="caption">
              Observed {date(p.as_of)}. Variance includes fee savings; it is
              not additional yield.
            </p>
            <details>
              <summary>Accounting & original forecast</summary>
              <Row label="Rewards">{money(p.rewards)}</Row>
              <Row label="Price P&L">{money(p.price_pnl)}</Row>
              <Row label="Debt costs">{money(p.debt_cost)}</Row>
              <Row label="Net deposits">{money(p.net_deposits)}</Row>
              {p.open_cost_basis && <Row label="Remaining principal basis">{money(p.open_cost_basis)}</Row>}
              {p.withdrawn && <Row label="Withdrawn proceeds">{money(p.withdrawn)}</Row>}
              {p.period_start && <p className="caption">Since {date(p.period_start)}</p>}
              {p.basis?.map((note) => <p className="caption" key={note}>{note}</p>)}
              <p className="caption">Deposits are capital flows. Unmeasured returns are shown as unavailable.</p>
            </details>
          </>
        ) : (
          <p className="caption">
            Measured performance will appear after execution.
          </p>
        )}
      </details>
    </div>
  );
}

export function Evidence({ m }: { m: Machine }) {
  const w = m.workspace.data;
  if (!w) return null;
  return (
    <>
      <p className="muted">
        Inspect how each confirmed condition connects to a plan, approval and
        receipt.
      </p>
      <Button
        secondary
        onClick={() =>
          downloadJson(
            {
              exported_at: new Date().toISOString(),
              network: m.network,
              runs: w.evidence,
            },
            `faat-${m.network}-evidence.json`,
          )
        }
        disabled={!w.evidence.length}
      >
        <Download size={15} />
        Export run records
      </Button>
      {!w.evidence.length ? (
        <Empty title="No run evidence yet">
          Completed runs will retain their original mandate, assumptions and
          transaction links.
        </Empty>
      ) : (
        w.evidence.map((run) => (
          <section className="evidence-run" key={run.id}>
            <div className="section-heading">
              <strong>{short(run.id)}</strong>
              <Badge>{run.provenance.toLowerCase()}</Badge>
            </div>
            <p>{run.outcome}</p>
            <Row label="Confirmed cash floor">
              {run.inputs.mandate.constraints.min_cash_bps / 100}%
            </Row>
            <Row label="TRX exposure cap">
              {run.inputs.mandate.constraints.max_trx_exposure_bps / 100}%
            </Row>
            <Row label="Borrowing">
              {run.inputs.mandate.constraints.allow_debt
                ? "Permitted"
                : "Not permitted"}
            </Row>
            <details>
              <summary>Original conditions & assumptions</summary>
              <p>{run.inputs.mandate.source_text}</p>
              <p>{run.inputs.comparison?.search_scope}</p>
              <p>{run.inputs.comparison?.usdd_vault.reason}</p>
            </details>
            <ol className="trace">
              {[
                ["Mandate", run.mandate_hash],
                ["Snapshot", run.snapshot_root],
                ["Plan", run.plan_hash],
                ["Graph", run.graph_hash],
                ["Approval", run.approval_id],
              ].map(([label, value]) => (
                <li key={label}>
                  <span>{label}</span>
                  <code>{value || "Not produced"}</code>
                </li>
              ))}
            </ol>
            <h4>On-chain receipts</h4>
            {run.txids.length ? (
              run.txids.map((tx) => (
                <a
                  key={tx}
                  className="text-link"
                  href={explorer(run.network, tx) || undefined}
                  target="_blank"
                  rel="noreferrer"
                >
                  {short(tx)} <ExternalLink size={13} />
                </a>
              ))
            ) : (
              <p className="caption">
                No on-chain transaction. This is not live execution evidence.
              </p>
            )}
            <Row label="Actual model">
              {run.model_id || "No model call recorded"}
            </Row>
            <h4>Inference by flow</h4>
            {run.flows.map((f) => (
              <div className="flow" key={f.name}>
                <strong>{f.name}</strong>
                <Row label="Calls">{f.llm_calls ?? "Not measured"}</Row>
                <Row label="Input / output tokens">
                  {f.input_tokens ?? "—"} / {f.output_tokens ?? "—"}
                </Row>
                <Row label="Latency">
                  {f.latency_ms === null
                    ? "Not measured"
                    : `${f.latency_ms} ms`}
                </Row>
              </div>
            ))}
            <Row label="Energy">
              {run.energy.status === "UNAVAILABLE"
                ? "Unavailable"
                : `${run.energy.wh ?? "—"} Wh · ${run.energy.status.toLowerCase()}`}
            </Row>
            {run.energy.basis && <p className="caption">{run.energy.basis}</p>}
          </section>
        ))
      )}
    </>
  );
}
export function SidePanel({
  panel,
  m,
  onClose,
  theme,
  setTheme,
}: {
  panel: string;
  m: Machine;
  onClose: () => void;
  theme: string;
  setTheme: (v: string) => void;
}) {
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    close.current?.focus();
    return () => {
      if (previous?.isConnected) previous.focus();
    };
  }, [panel]);
  const w = m.workspace.data;
  const [params, setParams] = useSearchParams();
  const editingMandate = params.get("edit") === "conditions";
  const setEditingMandate = (editing: boolean) => {
    const next = new URLSearchParams(params);
    if (editing) next.set("edit", "conditions");
    else next.delete("edit");
    setParams(next);
  };
  return (
    <aside
      className={`side-panel companion-panel panel-${panel}`}
      aria-label={panelTitles[panel] || "Details"}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
    >
      <header>
        <h2>{panelTitles[panel] || "Details"}</h2>
        <button
          className="icon-button"
          ref={close}
          onClick={onClose}
          aria-label="Close details"
        >
          <X />
        </button>
      </header>
      <div className="panel-content">
        {m.error && <Alert tone="error">{m.error}</Alert>}
        {m.busy && <p role="status">{m.busy}</p>}
        {panel === "portfolio" && <Portfolio m={m} />}
        {panel === "execution" && <ExecutionCard m={m} />}
        {panel === "notifications" && <Notifications m={m} />}
        {panel === "mandate" && (
          <>
            {editingMandate || !w?.mandate ? (
              <>
                <div className="editor-heading">
                  <strong>Edit conditions</strong>
                  <Button
                    secondary
                    onClick={() =>
                      w?.mandate ? setEditingMandate(false) : onClose()
                    }
                  >
                    Cancel
                  </Button>
                </div>
                <MandateEditor
                  key={w?.mandate?.hash || "new"}
                  m={m}
                  onSaved={() => setEditingMandate(false)}
                />
              </>
            ) : (
              <>
                <MandateCard
                  m={m}
                  edit={() => setEditingMandate(true)}
                  onCompared={m.agentId ? onClose : undefined}
                />
                {!m.agentId && (
                  <>
                    <PlanCards
                      m={m}
                      onReview={() => {}}
                      onEdit={() => setEditingMandate(true)}
                    />
                    <ExecutionCard m={m} />
                  </>
                )}
                {w?.intent && (
                  <Button secondary onClick={() => setEditingMandate(true)}>
                    Review new conditions from chat
                  </Button>
                )}
              </>
            )}
          </>
        )}
        {panel === "routines" && <RoutineEditor m={m} />}
        {panel === "evidence" && <Evidence m={m} />}
        {panel === "activity" && (
          <>
            <h3>Market sources</h3>
            {w?.snapshots.map((s) => (
              <section className="source" key={s.id}>
                <strong>{s.source}</strong>
                <Status value={expired(s.expires_at) ? "STALE" : s.status} />
                <Row label="Network">{s.network}</Row>
                <Row label="Observed">{date(s.observed_at)}</Row>
                <Row label="Expires">{date(s.expires_at)}</Row>
                <Row label="Block">{s.block || "Not observed"}</Row>
                {s.source_url && /^https:\/\//.test(s.source_url) && (
                  <a
                    className="text-link"
                    href={s.source_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Source <ExternalLink size={13} />
                  </a>
                )}
              </section>
            ))}
            <h3 className="section-label">Activity</h3>
            {w?.activity.map((a) => (
              <div className="activity-item" key={a.id}>
                <Activity size={16} />
                <div>
                  <strong>{a.title}</strong>
                  <p>{a.detail}</p>
                  <small>
                    {date(a.at)} · {a.role}
                  </small>
                </div>
              </div>
            ))}
            {!w?.activity.length && <Empty title="No activity yet" />}
          </>
        )}
        {panel === "wallet" && (
          <>
            <div className="wallet-symbol">T</div>
            <h3>TronLink</h3>
            <p className="muted">
              Connect your account and prove ownership. Every transaction still
              needs your signature.
            </p>
            <Row label="Connection">
              {m.session.data?.authenticated
                ? "Verified session"
                : "Not connected"}
            </Row>
            <Row label="Account">
              {m.session.data?.wallet_address
                ? short(m.session.data.wallet_address)
                : "—"}
            </Row>
            <Row label="Network">
              {m.network === "nile" ? "Nile Testnet" : "TRON Mainnet"}
            </Row>
            <Row label="Signing">Per transaction</Row>
            {m.network === "nile" && (
              <details className="companion-disclosure">
                <summary>Testnet funding tools</summary>
                <NileFunding m={m} />
              </details>
            )}
            <Button onClick={m.connect} disabled={!!m.busy}>
              {m.session.data?.authenticated
                ? "Verify wallet again"
                : "Connect & verify TronLink"}
            </Button>
            {m.session.data?.authenticated && (
              <Button secondary onClick={m.logout} disabled={!!m.busy}>
                End session
              </Button>
            )}
          </>
        )}
        {panel === "settings" && (
          <>
            <h3>Appearance</h3>
            <label className="field-label">
              Theme
              <select value={theme} onChange={(e) => setTheme(e.target.value)}>
                <option value="system">System</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
              </select>
            </label>
            <details className="companion-card companion-disclosure">
              <summary>
                Connected services<span>Kiln · Finance</span>
              </summary>
              <Row label="Data mode">Persistent workspace</Row>
              <Row label="Inference provider">Kiln</Row>
              <Row label="Intent model">Qwen3 32B</Row>
              <p className="caption">
                Actual model and usage are recorded per run by the service.
              </p>
              <Row label="Kiln">
                {m.service.data?.kiln_configured
                  ? "Configured"
                  : "Not configured"}
              </Row>
              <Row label="Financial service">
                {m.service.data?.finance_connected
                  ? "Configured"
                  : "Not connected"}
              </Row>
            </details>
            <h3 className="section-label">Archived agents</h3>
            {m.archived.data?.length ? (
              m.archived.data.map((a) => (
                <div className="restore-row" key={a.id}>
                  <span>
                    {a.name}
                    <small>{a.role}</small>
                  </span>
                  <Button
                    secondary
                    disabled={!!m.busy}
                    onClick={() => m.restoreAgent(a.id)}
                  >
                    Restore
                  </Button>
                </div>
              ))
            ) : (
              <p className="muted">No archived agents.</p>
            )}
            <details className="companion-card companion-disclosure">
              <summary>
                Model usage<span>History & export</span>
              </summary>
              <Button
                secondary
                disabled={!m.usage.data?.length}
                onClick={() =>
                  downloadJson(m.usage.data, "faat-model-usage.json")
                }
              >
                Export usage
              </Button>
              {m.usage.data?.length ? (
                m.usage.data.slice(0, 10).map((u) => (
                  <section className="source" key={u.id}>
                    <Status value={u.status} />
                    <Row label="Model">{u.model || "Not reported"}</Row>
                    <Row label="Input / output tokens">
                      {u.input_tokens ?? "—"} / {u.output_tokens ?? "—"}
                    </Row>
                    <Row label="Elapsed">
                      {u.latency_ms === null
                        ? "—"
                        : `${(u.latency_ms / 1000).toFixed(1)}s`}
                    </Row>
                    <small>{date(u.created)}</small>
                  </section>
                ))
              ) : (
                <Empty title="No model calls yet" />
              )}
            </details>
          </>
        )}
      </div>
    </aside>
  );
}
