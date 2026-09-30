import { useSearchParams } from "react-router-dom";
import type { Machine } from "../api/useMachine";
import { Alert, Badge, Button, Row } from "./ui";
import { expired, explorer, money, short } from "../lib/format";
export function NativeStakeReview({ m }: { m: Machine }) {
  const [params, setParams] = useSearchParams();
  const w = m.workspace.data,
    g = w?.graph;
  if (!w || !g) return null;
  const s = g.steps[0],
    wf = w.stake_workflow;
  const done = s.status === "POSITION_RECONCILED",
    failed = w.execution?.status === "FAILED";
  const stale = expired(g.expires_at) || w.mandate?.hash !== g.mandate_hash;
  const approved =
    w.approval?.status === "APPROVED" &&
    w.approval.graph_hash === g.hash &&
    !expired(w.approval.expires_at);
  const pending =
    !!m.pending ||
    (!!w.execution &&
      !["FAILED", "POSITION_RECONCILED"].includes(w.execution.status));
  const prepared = w.prepared_transactions?.find(
    (p) => p.graph_id === g.id && p.txid === m.pending?.txid,
  );
  const resume =
    approved &&
    !stale &&
    prepared &&
    m.pending?.phase === "PREPARED" &&
    !w.execution;
  const portfolio = () => {
    const p = new URLSearchParams(params);
    p.set("panel", "portfolio");
    setParams(p);
  };
  const verb: Record<string, string> = {
    FreezeBalanceV2Contract: "staking",
    VoteWitnessContract: "voting",
    UnfreezeBalanceV2Contract: "unstaking",
    WithdrawExpireUnfreezeContract: "withdrawal",
    WithdrawBalanceContract: "reward claim",
  };
  return (
    <section
      className="companion-card withdrawal-review"
      aria-label="Native staking review"
    >
      <div className="eyebrow">
        TRON Native · Step {(wf?.cursor ?? 0) + 1} of {wf?.steps.length}
      </div>
      <h3>{done ? `${s.title} · confirmed` : s.title}</h3>
      <div className="withdrawal-amount">
        <span>
          {s.action === "VoteWitnessContract"
            ? "Voting power from your stake"
            : s.action === "WithdrawBalanceContract"
              ? "Unclaimed rewards at review"
              : "Reviewed amount"}
        </span>
        <strong>{money(s.amount)}</strong>
      </div>
      <Row label="Wallet">{short(g.account)}</Row>
      <Row label="Network">
        <Badge>Nile testnet</Badge>
      </Row>
      {s.native_votes?.map((v) => (
        <Row key={v.vote_address} label={`${v.vote_count} votes`}>
          {short(s.recipient)}
        </Row>
      ))}
      {!done && <Row label="Bandwidth fee reserve">{money(s.fee_cap)}</Row>}
      {s.action === "FreezeBalanceV2Contract" && !done && (
        <p>
          TRX will be staked for Energy. A second, separately reviewed vote
          activates voting rewards. Unstaking has the chain waiting period.
        </p>
      )}
      {s.action === "VoteWitnessContract" && !done && (
        <p>
          This changes your representative votes. It does not transfer more TRX.
          Income is variable and starts after a maintenance update.
        </p>
      )}
      {done ? (
        <div className="condition-actions">
          {wf?.status === "NEXT_REVIEW" ? (
            <Button disabled={!!m.busy} onClick={() => m.stakeAction("next")}>
              Continue to voting
            </Button>
          ) : (
            <Button onClick={portfolio}>View updated portfolio</Button>
          )}
        </div>
      ) : pending ? (
        <div>
          <p role="status">
            {m.pending ? m.pendingMessage : w.execution?.message}
          </p>
          {resume ? (
            <Button
              disabled={!!m.busy || !m.walletValid}
              onClick={() => m.sign(s.id)}
            >
              Continue in TronLink
            </Button>
          ) : (
            <Button secondary disabled={!!m.busy} onClick={m.reconcile}>
              Check transaction
            </Button>
          )}
        </div>
      ) : stale || failed ? (
        <div>
          <p>Review the latest account and fee before continuing.</p>
          <Button disabled={!!m.busy} onClick={() => m.stakeAction("next")}>
            Refresh this step
          </Button>
        </div>
      ) : !m.session.data?.authenticated || !m.walletValid ? (
        <Button disabled={!!m.busy} onClick={m.connect}>
          Connect your wallet
        </Button>
      ) : approved ? (
        <Button disabled={!!m.busy} onClick={() => m.sign(s.id)}>
          Sign in TronLink
        </Button>
      ) : (
        <div>
          <p>
            Approve the exact action and fee reserve, then sign in your wallet.
          </p>
          <Button disabled={!!m.busy} onClick={() => m.consent(g)}>
            Approve {verb[s.action] || "this step"}
          </Button>
          <p className="caption">Approval alone does not move funds.</p>
        </div>
      )}
      {!done && !pending && !w.execution && wf?.cursor === 0 && (
        <Button
          secondary
          disabled={!!m.busy}
          onClick={() => m.stakeAction("cancel")}
        >
          Cancel review
        </Button>
      )}
      {m.busy && <p role="status">{m.busy}…</p>}
      {m.error && <Alert tone="error">{m.error}</Alert>}
      {s.txid && (
        <a
          className="text-link"
          href={explorer("nile", s.txid)!}
          target="_blank"
          rel="noreferrer"
        >
          View transaction
        </a>
      )}
      <details className="companion-disclosure">
        <summary>Exact scope & fees</summary>
        <p>{g.disclosure}</p>
        <Row label="Action">{s.action}</Row>
        <Row label="Account / representative">{s.recipient}</Row>
        <p className="caption">
          The native protocol has no on-chain fee_limit. We bound the signed
          transaction size and recheck the resource price before broadcast; a
          later network price change cannot be reversed.
        </p>
      </details>
    </section>
  );
}
