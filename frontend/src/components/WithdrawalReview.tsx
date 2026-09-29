import { useSearchParams } from "react-router-dom";
import { ArrowDownLeft, Check, ExternalLink, LockKeyhole } from "lucide-react";
import type { Machine } from "../api/useMachine";
import { Alert, Badge, Button, Row } from "./ui";
import { decimal, expired, explorer, money, short } from "../lib/format";

/** One review card; the server still checks active permission and exact bytes. */
export function WithdrawalReview({ m }: { m: Machine }) {
  const [params, setParams] = useSearchParams();
  const w = m.workspace.data;
  const g = w?.graph;
  if (!w || !g) return null;
  const step = g.steps[0];
  const active = w.withdrawal_policy || w.active_mandate || w.mandate;
  const stale = expired(g.expires_at) || active?.hash !== g.mandate_hash;
  const done = step.status === "POSITION_RECONCILED";
  const approved = w.approval?.status === "APPROVED" && w.approval.graph_hash === g.hash && !expired(w.approval.expires_at);
  const submitted = !!m.pending || !!w.execution && !["FAILED", "POSITION_RECONCILED"].includes(w.execution.status);
  const failed = w.execution?.status === "FAILED";
  const prepared = w.prepared_transactions?.find(p => p.graph_id === g.id && p.txid === m.pending?.txid);
  const canResume = approved && !stale && prepared && m.pending?.phase === "PREPARED" && !w.execution;
  const tx = step.txid && explorer(g.network, step.txid);
  const expensive = g.expected_fee?.symbol === step.amount.symbol && Number(g.expected_fee.value) > Number(step.amount.value);
  const portfolio = () => { const next = new URLSearchParams(params); next.set("panel", "portfolio"); setParams(next); };
  return (
    <section className="companion-card withdrawal-review" aria-label="Review withdrawal">
      <div className="review-intro">
        <div className="watch-mark">{done ? <Check size={20} /> : <ArrowDownLeft size={20} />}</div>
        <div><span className="eyebrow">JustLend → your wallet</span><h3>{done ? "Your withdrawal is complete" : "Your withdrawal"}</h3></div>
      </div>
      <div className="withdrawal-amount"><span>{done ? "Reconciled amount" : "Expected to receive"}</span><strong>{money(step.amount)}</strong></div>
      <Row label="Wallet">{short(g.account)}</Row>
      <Row label="Network"><Badge>{g.network === "nile" ? "Nile testnet" : "TRON mainnet"}</Badge></Row>
      {!done && <>
        <Row label="Estimated network fee">{money(g.expected_fee)}</Row>
        <Row label="Maximum fee you approve">{money(step.fee_cap)}</Row>
        {expensive && <p className="withdrawal-cost-note">The fee is higher than the amount you receive. This withdrawal reduces your wallet balance after fees.</p>}
      </>}
      {done ? <Button onClick={portfolio}>View updated portfolio</Button>
        : submitted ? <div className="withdrawal-action"><p role="status">{m.pending ? m.pendingMessage : w.execution?.message}</p>
            {canResume ? <Button onClick={() => m.sign(step.id)} disabled={!!m.busy || !m.walletValid}>Continue in TronLink <LockKeyhole size={15} /></Button>
              : <Button secondary onClick={m.reconcile} disabled={!!m.busy}>Check transaction</Button>}
            {prepared && !canResume && <small>We’ll check automatically after {new Date(prepared.recover_after).toLocaleTimeString("en", {hour:"numeric", minute:"2-digit"})}. A fresh review unlocks only after the chain confirms it was not sent.</small>}
          </div>
        : stale || failed ? <div className="withdrawal-action"><p>{failed ? "The previous transaction failed. Review the latest amount and fee before trying again." : "This quote expired. Refresh it before approving."}</p><Button onClick={m.withdrawToWallet} disabled={!!m.busy}>Refresh withdrawal</Button></div>
        : !m.session.data?.authenticated || !m.walletValid ? <Button onClick={m.connect} disabled={!!m.busy}>Connect your wallet</Button>
        : approved ? <div className="withdrawal-action"><p>Approved. We’ll check the latest state before opening your wallet.</p><Button onClick={() => m.sign(step.id)} disabled={!!m.busy}>Sign in TronLink <LockKeyhole size={15} /></Button></div>
        : <div className="withdrawal-action"><p>Approve this amount and fee limit, then sign in your wallet.</p><Button onClick={() => m.consent(g)} disabled={!!m.busy}>Approve withdrawal</Button><small>Approval alone does not move funds.</small></div>}
      {m.busy && <p className="caption" role="status">{m.busy}…</p>}
      {m.error && <Alert tone="error">{m.error}</Alert>}
      {tx && <a className="text-link" href={tx} target="_blank" rel="noreferrer">View transaction <ExternalLink size={14} /></a>}
      <details className="companion-disclosure">
        <summary>What you’re approving<span>Amounts & protection</span></summary>
        {step.input_amount && <Row label="Shares to redeem">{decimal(step.input_amount.value, step.input_amount.decimals)} {step.input_amount.symbol}</Row>}
        <Row label="Contract"><code>{step.recipient}</code></Row>
        <Row label="Proceeds monitoring floor">{money(g.minimum_received)}</Row>
        <p className="caption">Approval applies only to this exact withdrawal, within your previous limits. Your investment draft remains unconfirmed. Liquidity and smart contract risks apply. The output floor is checked after settlement and cannot reverse a completed transaction.</p>
      </details>
    </section>
  );
}
