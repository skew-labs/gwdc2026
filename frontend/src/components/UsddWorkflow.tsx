import { useState } from "react";
import { ArrowRight, ShieldCheck } from "lucide-react";
import type { Machine } from "../api/useMachine";
import { Alert, Badge, Button, Row } from "./ui";
import { ExecutionCard } from "./ExecutionCard";

export function UsddWorkflow({ m }: { m: Machine }) {
  const [mode, setMode] = useState("OWNED");
  const [amount, setAmount] = useState("");
  const [collateral, setCollateral] = useState("");
  const [loops, setLoops] = useState("0");
  const [fraction, setFraction] = useState("");
  const [perFee, setPerFee] = useState("");
  const [totalFee, setTotalFee] = useState("");
  const [claim, setClaim] = useState("");
  const w = m.workspace.data;
  if (!w) return null;
  const wf = w.usdd_workflow;
  const action = (suffix: string, data: Record<string, unknown> = {}) =>
    m.run("Reviewing USDD workflow", () =>
      m.mutate(`/v1/usdd-workflows${suffix}`, { ...data, network: m.network }),
    );
  const active = wf && !["CLOSED", "CANCELLED"].includes(wf.status);
  return (
    <details className="companion-card companion-disclosure">
      <summary>USDD strategies <span>{active ? "Your workflow" : "Explore"}</span></summary>
      {m.network === "nile" ? (
        <Alert>Nile Vault USDD and Nile JustLend USDD use different token contracts. This route needs compatible deployments before it can run on Nile.</Alert>
      ) : null}
      {active ? (
        <div className="artifact-body">
          <div className="section-heading"><ShieldCheck /><strong>{wf.mode === "VAULT" ? "TRX collateral → USDD" : "USDD lending"}</strong><Badge>{wf.phase === "RECOVERY" ? "Recovery" : "Entry"}</Badge></div>
          <Row label="Confirmed transactions">{wf.confirmed_txids.length}</Row>
          <Row label="Status">{wf.status.replaceAll("_", " ")}</Row>
          <Row label="Fees paid">{(Number(wf.spent_fees) / 1e6).toLocaleString()} TRX</Row>
          <p className="caption">Each step has its own review and wallet signature. Earlier transactions remain on-chain if a later step stops.</p>
          {w.graph?.id.startsWith(wf.id) && <ExecutionCard m={m} />}
          {wf.status === "AWAITING_SIGNATURE" && !wf.prepared && w.graph && Date.parse(w.graph.expires_at) < m.clock && <Button disabled={!!m.busy} onClick={() => action("/next")}>Refresh this review</Button>}
          {wf.prepared && Date.parse(wf.prepared.expires_at) + 60000 < m.clock && <Button secondary disabled={!!m.busy} onClick={() => m.run("Refreshing expired review", () => m.mutate("/v1/executions/reconcile", { ...wf.prepared, network: m.network }))}>Refresh expired review</Button>}
          {wf.status === "AWAITING_NEXT_REVIEW" && <Button disabled={!!m.busy} onClick={() => action("/next")}>Review next step <ArrowRight size={14} /></Button>}
          {["COMPLETE", "NEEDS_RECOVERY", "AWAITING_NEXT_REVIEW", "AWAITING_SIGNATURE"].includes(wf.status) && wf.confirmed_txids.length > 0 && !m.pending && !wf.prepared ? (
            <Button secondary disabled={!!m.busy} onClick={() => action("/recover")}>{wf.phase === "RECOVERY" ? "Continue recovery" : "Review repayment & exit"}</Button>
          ) : null}
          {wf.status === "AWAITING_SIGNATURE" && wf.confirmed_txids.length === 0 && !m.pending && !wf.prepared && <Button secondary disabled={!!m.busy} onClick={() => action("/cancel")}>Cancel unsubmitted review</Button>}
          <details><summary>Claim an available USDD reward</summary>
            <p className="caption">Load available rewards, then choose a period to review. Every claim is simulated before signing.</p>
            <Button secondary disabled={!!m.busy} onClick={() => action("/rewards")}>Load claimable rewards</Button>
            <label>Available reward<select value={claim} onChange={(e) => setClaim(e.target.value)}><option value="">Choose a period</option>{wf.rewards?.map(r => <option key={r.key} value={r.key}>Round {r.round} · {r.amount} USDD</option>)}</select></label>
            <Button secondary disabled={!!m.busy || !claim || !["COMPLETE", "NEEDS_RECOVERY"].includes(wf.status)} onClick={() => action("/recover", { claim_key: claim })}>Review reward claim</Button>
          </details>
        </div>
      ) : (
        <form className="mandate-editor" onSubmit={(e) => {
          e.preventDefault();
          void action("", { mode, amount_usdd: amount, collateral_trx: collateral,
            ilk: "TRX-C", loops: Number(loops), borrow_bps: loops === "0" ? 0 : Math.round(Number(fraction) * 100),
            per_step_fee_cap_trx: perFee, total_fee_cap_trx: totalFee });
        }}>
          <p className="caption">Compare the complete cost of entering and exiting. Borrowing is offered only when your confirmed conditions allow it and the current spread is positive.</p>
          <label>Funding<select value={mode} onChange={(e) => setMode(e.target.value)}><option value="OWNED">USDD I already own</option><option value="VAULT">Issue USDD against TRX</option></select></label>
          <label>{mode === "OWNED" ? "USDD to supply" : "USDD to issue"}<input required inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} /></label>
          {mode === "VAULT" && <label>TRX collateral · TRX-C<input required inputMode="decimal" value={collateral} onChange={(e) => setCollateral(e.target.value)} /></label>}
          <details><summary>Borrow and resupply</summary>
            <label>Additional cycles<select value={loops} onChange={(e) => setLoops(e.target.value)}>{[0, 1, 2, 3, 4].map(n => <option key={n} value={n}>{n}</option>)}</select></label>
            {loops !== "0" && <label>Borrow fraction per cycle · %<input required type="number" min="1" max="85" step="0.01" value={fraction} onChange={(e) => setFraction(e.target.value)} /></label>}
          </details>
          <label>Maximum fee per transaction · TRX<input required inputMode="decimal" value={perFee} onChange={(e) => setPerFee(e.target.value)} /></label>
          <label>Total fee reserve, including exit · TRX<input required inputMode="decimal" value={totalFee} onChange={(e) => setTotalFee(e.target.value)} /></label>
          {w.mandate?.status !== "CONFIRMED" && <Alert>Confirm your conditions before reviewing an executable strategy.</Alert>}
          <Button type="submit" disabled={!!m.busy || !m.walletValid || w.mandate?.status !== "CONFIRMED" || m.network !== "mainnet"}>Check live strategy <ArrowRight size={14} /></Button>
        </form>
      )}
    </details>
  );
}
