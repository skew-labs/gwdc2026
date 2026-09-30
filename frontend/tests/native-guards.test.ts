import { expect, it } from "vitest";
import {
  StakeWorkflow,
  type Session,
  type Workspace,
} from "../src/api/contracts";
import { validateApprovalContext } from "../src/lib/guards";

const session = { authenticated: true, wallet_address: "wallet" } as Session;
const actions = {
  STAKE: "FreezeBalanceV2Contract",
  VOTE: "VoteWitnessContract",
  UNSTAKE: "UnfreezeBalanceV2Contract",
  WITHDRAW: "WithdrawExpireUnfreezeContract",
  CLAIM: "WithdrawBalanceContract",
};
function native(action: keyof typeof actions = "STAKE") {
  const expires_at = new Date(Date.now() + 60000).toISOString();
  const cursor = action === "VOTE" ? 1 : 0;
  const bindings = {
    plan_hash: "plan",
    mandate_hash: "policy",
    account: "wallet",
    network: "nile",
  };
  const step = {
    action,
    status: "READY",
    graph_id: `workflow-${cursor}`,
    graph_hash: "graph",
    step_id: `step-${cursor}`,
    expires_at,
  };
  return {
    network: "nile",
    mandate: { hash: "policy", network: "nile", status: "CONFIRMED" },
    graph: {
      ...bindings,
      id: step.graph_id,
      hash: "graph",
      expires_at,
      review_kind: "NATIVE_STAKE",
      steps: [{ id: step.step_id, action: actions[action], status: "READY" }],
    },
    approval: {
      ...bindings,
      graph_hash: "graph",
      expires_at,
      status: "APPROVED",
    },
    stake_workflow: StakeWorkflow.parse({
      ...bindings,
      id: "workflow",
      status: "REVIEW",
      cursor,
      amount: { value: "237", symbol: "TRX", decimals: 6 },
      representative: "representative",
      steps: cursor
        ? [{ action: "STAKE", status: "POSITION_RECONCILED" }, step]
        : [step],
      forecast: { gross: "3", net: "1", fees: "2", horizon_seconds: 31536000 },
    }),
    // Entry comparison evidence is distinct from the independently reviewed step.
    comparison: {
      snapshot_root: "combined-root",
      expires_at: "2000-01-01T00:00:00Z",
    },
    snapshots: [{ root: "wallet-component" }, { root: "voting-component" }],
  } as unknown as Workspace;
}
it.each(Object.keys(actions) as (keyof typeof actions)[])(
  "accepts a separately reviewed %s step, including after entry comparison expiry",
  (action) => {
    const w = native(action);
    expect(() => validateApprovalContext(w, session, true)).not.toThrow();
    w.comparison = null;
    expect(() => validateApprovalContext(w, session, true)).not.toThrow();
  },
);
it.each([
  [
    "account",
    (w: Workspace) => {
      w.stake_workflow!.account = "other";
    },
  ],
  [
    "network",
    (w: Workspace) => {
      w.stake_workflow!.network = "mainnet";
    },
  ],
  [
    "policy",
    (w: Workspace) => {
      w.stake_workflow!.mandate_hash = "other";
    },
  ],
  [
    "plan",
    (w: Workspace) => {
      w.stake_workflow!.plan_hash = "other";
    },
  ],
  [
    "graph ID",
    (w: Workspace) => {
      w.stake_workflow!.steps[0].graph_id = "other";
    },
  ],
  [
    "graph hash",
    (w: Workspace) => {
      w.stake_workflow!.steps[0].graph_hash = "other";
    },
  ],
  [
    "step ID",
    (w: Workspace) => {
      w.stake_workflow!.steps[0].step_id = "other";
    },
  ],
  [
    "cursor",
    (w: Workspace) => {
      w.stake_workflow!.cursor = 1;
    },
  ],
  [
    "action",
    (w: Workspace) => {
      w.stake_workflow!.steps[0].action = "VOTE";
    },
  ],
  [
    "submitted",
    (w: Workspace) => {
      w.stake_workflow!.steps[0].status = "SUBMITTED";
    },
  ],
  [
    "graph status",
    (w: Workspace) => {
      w.graph!.steps[0].status = "FAILED";
    },
  ],
  [
    "expired step",
    (w: Workspace) => {
      w.stake_workflow!.steps[0].expires_at = "2000-01-01T00:00:00Z";
    },
  ],
  [
    "missing binding",
    (w: Workspace) => {
      delete w.stake_workflow!.plan_hash;
    },
  ],
  [
    "workflow status",
    (w: Workspace) => {
      w.stake_workflow!.status = "COMPLETE";
    },
  ],
] as const)(
  "rejects changed native %s before wallet signing",
  (_name, mutate) => {
    const w = native();
    mutate(w);
    expect(() => validateApprovalContext(w, session, true)).toThrow(
      /native staking step/,
    );
  },
);
it("retains approval expiry, user ownership and previous receipt gates", () => {
  const w = native("VOTE");
  w.stake_workflow!.steps[0].status = "SUBMITTED";
  expect(() => validateApprovalContext(w, session, true)).toThrow();
  const expired = native();
  expired.approval!.expires_at = "2000-01-01T00:00:00Z";
  expect(() => validateApprovalContext(expired, session, true)).toThrow(
    /expired/,
  );
  const revoked = native();
  revoked.approval!.status = "REVOKED";
  expect(() => validateApprovalContext(revoked, session, true)).toThrow();
  expect(() =>
    validateApprovalContext(
      native(),
      { ...session, wallet_address: "other" },
      true,
    ),
  ).toThrow();
  expect(() => validateApprovalContext(native(), session, false)).toThrow();
});
