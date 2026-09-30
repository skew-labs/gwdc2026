import { useState } from "react";
import { Check, ExternalLink, LockKeyhole, ShieldCheck } from "lucide-react";
import type { Machine } from "../api/useMachine";
import { Alert, Badge, Button, Empty, Row, Status } from "./ui";
import { decimal, expired, explorer, money, short } from "../lib/format";
import { NativeStakeReview } from "./NativeStakeReview";
import { WithdrawalReview } from "./WithdrawalReview";
export function ExecutionCard({ m }: { m: Machine }) {
  const [acceptedHash, setAcceptedHash] = useState<string | null>(null);
  const w = m.workspace.data;
  const g = w?.graph;
  if (m.pending && (!g || m.pending.graph_id !== g.id)) return (
    <section className="companion-card withdrawal-review" aria-label="Previous wallet request">
      <h3>Checking your previous wallet request</h3>
      <p>{m.pendingMessage}</p>
      <p className="caption">A new withdrawal will be available once this request is confirmed or verified as expired.</p>
      <Button onClick={m.reconcile} disabled={!!m.busy}>Check transaction</Button>
      {m.error && <Alert tone="error">{m.error}</Alert>}
    </section>
  );
  if (!g && w?.stake_workflow?.status === "REVIEW") return <section className="companion-card"><h3>Continue your native stake</h3><p>Refresh the current step from live wallet state.</p><Button disabled={!!m.busy} onClick={()=>m.stakeAction("next")}>Review current step</Button></section>;
  if (!g)
    return (
      <Empty icon={<ShieldCheck />} title="Execution review">
        Choose a plan to review the exact steps, spending limits and fees here.
      </Empty>
    );
  if (g.review_kind === "NATIVE_STAKE") return <NativeStakeReview m={m} />;
  const accepted = acceptedHash === g.hash;
  if (g.review_kind === "ADJUSTMENT" && g.steps[0]?.action === "redeem(uint256)") return <WithdrawalReview m={m} />;
  const a = w.approval;
  const stale = expired(g.expires_at) || w.mandate?.hash !== g.mandate_hash;
  const approved =
    a?.status === "APPROVED" &&
    a.graph_hash === g.hash &&
    !expired(a.expires_at);
  const next = g.steps.find((s) =>
    ["READY", "AWAITING_USER_SIGNATURE"].includes(s.status),
  );
  const blocked = g.steps.some((s) => s.status === "BLOCKED");
  const complete =
    g.steps.length > 0 &&
    g.steps.every((s) => s.status === "POSITION_RECONCILED");
  return (
    <section className="artifact execution-card" aria-label="Execution review">
      <div className="artifact-heading">
        <ShieldCheck />
        <div>
          <strong>
            {complete ? "Your execution receipt" : "Review before signing"}
          </strong>
          <small>
            {g.network === "nile" ? "Nile Testnet" : "TRON Mainnet"} ·{" "}
            {short(g.account)}
          </small>
        </div>
        <Badge tone={complete ? "good" : stale ? "warn" : "neutral"}>
          {complete ? "Reconciled" : stale ? "Expired" : "Review"}
        </Badge>
      </div>
      <div className="artifact-body">
        <Row label="Enforcement">
          {g.enforcement_scope === "DIRECT_PROTOCOL"
            ? "Direct protocol call"
            : "Execution guard"}
        </Row>
        <Row label="Approval scope">Exact amounts · per transaction</Row>
        {g.estimated_fee && (
          <Row label="Estimated fee upper bound">{money(g.estimated_fee)}</Row>
        )}
        {g.expected_fee && <Row label="Expected TRX payment">{money(g.expected_fee)}</Row>}
        {g.minimum_shares && (
          <Row label="Share monitoring floor">{money(g.minimum_shares)}</Row>
        )}
        {g.minimum_received && <Row label="Proceeds monitoring floor">{money(g.minimum_received)}</Row>}
        {g.disclosure && <Alert>{g.disclosure}</Alert>}
        <Row label="Validity">
          {expired(g.expires_at)
            ? "Expired"
            : `${Math.max(0, Math.floor((Date.parse(g.expires_at) - m.clock) / 60000))} min remaining`}
        </Row>
      </div>
      <ol className="steps">
        {g.steps.map((s, i) => {
          const url = s.txid ? explorer(g.network, s.txid) : null;
          return (
            <li key={s.id}>
              <span
                className={`step-number ${s.status === "POSITION_RECONCILED" ? "done" : ""}`}
              >
                {s.status === "POSITION_RECONCILED" ? (
                  <Check size={15} />
                ) : (
                  i + 1
                )}
              </span>
              <div className="step-content">
                <div className="step-top">
                  <strong>{s.title}</strong>
                  <Status value={s.status} />
                </div>
                <Row label="Amount">{money(s.amount)}</Row>
                {s.input_amount && <Row label="Exact input">{decimal(s.input_amount.value, s.input_amount.decimals)} {s.input_amount.symbol}</Row>}
                <Row label="Recipient">
                  <code>{short(s.recipient)}</code>
                </Row>
                <details>
                  <summary>Exact scope & dependencies</summary>
                  <p className="mono">{s.recipient}</p>
                  <Row label="Fee cap">{money(s.fee_cap)}</Row>
                  <Row label="Depends on">
                    {s.depends_on.join(", ") || "None"}
                  </Row>
                  <Row label="Action">{s.action}</Row>
                  {s.allowance_remaining && (
                    <Row label="Remaining allowance">
                      {money(s.allowance_remaining)}
                    </Row>
                  )}
                </details>
                {url && (
                  <a
                    className="text-link"
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {short(s.txid!)} <ExternalLink size={13} />
                  </a>
                )}
                {s.error && <Alert tone="error">{s.error}</Alert>}
              </div>
            </li>
          );
        })}
      </ol>
      {w.execution && (
        <div className="artifact-body">
          <Status value={w.execution.status} />
          <p>{w.execution.message}</p>
        </div>
      )}
      {m.pending && (
        <div className="artifact-body">
          <Alert tone={m.pendingStatus === "DISPUTED" ? "error" : "info"}>
            {m.pendingMessage} Transaction: {short(m.pending.txid)}.
          </Alert>
          <Button secondary disabled={!!m.busy} onClick={m.reconcile}>
            {m.pendingStatus === "CONFIRMING"
              ? "Check confirmation"
              : "Check transaction status"}
          </Button>
        </div>
      )}
      {complete ? null : stale ? (
        <div className="artifact-body">
          <Alert>
            {g.review_kind === "ADJUSTMENT"
              ? "This adjustment expired. In Portfolio, choose Check now and review the withdrawal again."
              : g.review_kind === "USDD_WORKFLOW"
                ? "This step expired. Open the USDD workflow and review its current step again."
                : "This graph is no longer valid. Request a fresh comparison."}
          </Alert>
        </div>
      ) : !m.session.data?.authenticated || !m.walletValid ? (
        <div className="artifact-footer">
          <span>Verify the wallet for this approval.</span>
          <Button onClick={m.connect} disabled={!!m.busy}>
            Connect & verify wallet
          </Button>
        </div>
      ) : blocked ? (
        <div className="artifact-body">
          <Alert>
            Resolve the verification requirements shown above before a
            transaction can be approved.
          </Alert>
        </div>
      ) : !approved ? (
        <div className="artifact-body">
          <label className="check-label">
            <input
              type="checkbox"
              checked={accepted}
              onChange={(e) => setAcceptedHash(e.target.checked ? g.hash : null)}
            />
            I have reviewed the amounts, fee caps, recipients and approval
            scope.
          </label>
          <Button onClick={() => m.consent(g)} disabled={!accepted || !!m.busy}>
            Approve this plan
          </Button>
          <p className="caption">
            Plan approval does not sign or submit a transaction.
          </p>
        </div>
      ) : next ? (
        <>
          <div className="artifact-body">
            <h4>Next: {next.title}</h4>
            {m.preflight?.step_id === next.id && (
              <div className="preflight">
                {m.preflight.checks.map((c) => (
                  <div key={c.name}>
                    <Status value={c.status} />
                    <span>
                      {c.name}
                      <small>{c.detail}</small>
                    </span>
                  </div>
                ))}
              </div>
            )}
            {m.preflight && expired(m.preflight.expires_at) && (
              <Alert>Preflight expired. Run it again before signing.</Alert>
            )}
          </div>
          <div className="artifact-footer">
            <Button
              secondary
              onClick={() => m.check(next.id)}
              disabled={!!m.busy || !!m.pending}
            >
              Run preflight
            </Button>
            <Button
              onClick={() => m.sign(next.id)}
              disabled={
                !!m.busy ||
                !!m.pending ||
                !m.preflight ||
                m.preflight.step_id !== next.id ||
                expired(m.preflight.expires_at) ||
                m.preflight.checks.some((c) => c.status !== "PASS")
              }
            >
              Sign in TronLink
              <LockKeyhole size={15} />
            </Button>
          </div>
        </>
      ) : !complete ? (
        <div className="artifact-body">
          <Alert>
            Waiting for transaction confirmation or reconciliation. Status
            updates arrive from the service.
          </Alert>
          <Button secondary onClick={m.reconcile} disabled={!!m.busy}>
            Check receipt and position
          </Button>
        </div>
      ) : null}
      <div className="inline-risk">
        Smart contract, stablecoin and liquidity risks apply. Each transaction
        requires a separate signature.
      </div>
    </section>
  );
}
