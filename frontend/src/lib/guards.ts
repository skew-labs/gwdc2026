import type { Workspace, Session } from "../api/contracts";
import { expired } from "./format";
export function validateApprovalContext(
  w: Workspace,
  s: Session,
  walletValid: boolean,
) {
  const withdrawal =
    w.graph?.review_kind === "ADJUSTMENT" &&
    w.graph.steps?.[0]?.action === "redeem(uint256)";
  const g = w.graph,
    a = w.approval,
    m = withdrawal
      ? w.withdrawal_policy || w.active_mandate || w.mandate
      : w.mandate;
  if (!g || !a || !m || !s.authenticated || !walletValid)
    throw new Error("Verify your wallet and approve this plan first.");
  if (
    (m.status !== "CONFIRMED" &&
      !(withdrawal && m.status === "DRAFT" && w.withdrawal_policy)) ||
    m.network !== w.network ||
    m.hash !== g.mandate_hash ||
    a.graph_hash !== g.hash ||
    a.plan_hash !== g.plan_hash ||
    a.mandate_hash !== g.mandate_hash ||
    a.network !== w.network ||
    g.network !== w.network ||
    a.account !== g.account ||
    g.account !== s.wallet_address ||
    a.status !== "APPROVED"
  )
    throw new Error(
      "The approval, account, network or mandate changed. Refresh and review again.",
    );
  if (expired(g.expires_at) || expired(a.expires_at))
    throw new Error("This review or approval expired. Review it again.");
  if (g.review_kind === "NATIVE_STAKE") {
    const wf = w.stake_workflow;
    const step = wf?.steps[wf.cursor];
    const actions: Record<string, string> = {
      STAKE: "FreezeBalanceV2Contract",
      VOTE: "VoteWitnessContract",
      UNSTAKE: "UnfreezeBalanceV2Contract",
      WITHDRAW: "WithdrawExpireUnfreezeContract",
      CLAIM: "WithdrawBalanceContract",
    };
    // Each native step receives a fresh account, chain and fee review. Its
    // binding remains valid for voting/exit even after the entry quote expires.
    if (
      !wf ||
      !step ||
      w.network !== "nile" ||
      wf.status !== "REVIEW" ||
      !Number.isInteger(wf.cursor) ||
      wf.cursor < 0 ||
      wf.account !== g.account ||
      wf.network !== g.network ||
      wf.plan_hash !== g.plan_hash ||
      wf.mandate_hash !== g.mandate_hash ||
      g.id !== `${wf.id}-${wf.cursor}` ||
      g.steps.length !== 1 ||
      step.graph_id !== g.id ||
      step.graph_hash !== g.hash ||
      step.step_id !== g.steps[0].id ||
      !step.expires_at ||
      step.expires_at !== g.expires_at ||
      expired(step.expires_at) ||
      !actions[step.action] ||
      actions[step.action] !== g.steps[0].action ||
      step.status !== "READY" ||
      !["READY", "AWAITING_USER_SIGNATURE"].includes(g.steps[0].status) ||
      wf.steps
        .slice(0, wf.cursor)
        .some((s) => s.status !== "POSITION_RECONCILED")
    )
      throw new Error(
        "The native staking step changed or expired. Refresh this step and approve it again.",
      );
    return;
  }
  if (g.review_kind === "USDD_WORKFLOW") {
    const wf = w.usdd_workflow;
    if (
      !wf ||
      wf.hash !== g.plan_hash ||
      wf.account !== g.account ||
      wf.network !== g.network ||
      wf.status !== "AWAITING_SIGNATURE" ||
      wf.steps[wf.cursor]?.graph_id !== g.id ||
      wf.steps[wf.cursor]?.id !== g.steps[0]?.id
    )
      throw new Error(
        "The USDD workflow step changed. Review its current step.",
      );
    return;
  }
  if (g.review_kind === "ADJUSTMENT") {
    const r = w.portfolio_review;
    if (
      !r ||
      r.id !== g.review_id ||
      r.snapshot_hash !== g.snapshot_root ||
      r.policy_hash !== m.hash ||
      r.network !== w.network ||
      expired(r.expires_at) ||
      ["DATA_UNAVAILABLE", "POLICY_INACTIVE", "PENDING_EXECUTION"].includes(
        r.status,
      )
    )
      throw new Error("This adjustment needs a fresh portfolio review.");
    return;
  }
  if (
    expired(g.expires_at) ||
    expired(a.expires_at) ||
    !w.comparison ||
    expired(w.comparison.expires_at) ||
    w.comparison.status !== "READY" ||
    w.comparison.network !== w.network ||
    w.comparison.mandate_hash !== m.hash ||
    !w.comparison.plans.some(
      (p) =>
        p.id === g.plan_id &&
        p.hash === g.plan_hash &&
        p.eligible &&
        p.violations.length === 0,
    )
  )
    throw new Error(
      "This plan or approval expired. Generate and approve a fresh plan.",
    );
  if (
    !w.snapshots.some(
      (x) =>
        x.root === w.comparison!.snapshot_root &&
        x.network === w.network &&
        x.status === "VALID" &&
        !expired(x.expires_at),
    )
  )
    throw new Error(
      "Market evidence is stale or belongs to another network. Refresh the plan.",
    );
}
