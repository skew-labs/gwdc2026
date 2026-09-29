import { expect, it } from "vitest";
import { validateApprovalContext } from "../src/lib/guards";
import { assertSignedUnchanged, assertNativeScope, normalizeWalletSignature } from "../src/lib/wallet";
import { utils } from "tronweb";
import type { Workspace, Session, Transaction, Graph } from "../src/api/contracts";

it("accepts the real TronWeb signature encoding without changing signed bytes", () => {
  // Public synthetic fixture only; never connected to a node or funded wallet.
  const sig = utils.crypto.ECKeySign(new Uint8Array(32).fill(1), new Uint8Array(32).fill(49));
  expect(sig.slice(-2)).toMatch(/^1[BC]$/);
  const tx = {txID:"01".repeat(32),raw_data_hex:"ab",raw_data:{},signature:[sig]} as Transaction;
  const normalized = normalizeWalletSignature(tx);
  expect(Buffer.from(normalized.signature![0],"hex")).toEqual(Buffer.from(sig,"hex"));
  expect(normalized.raw_data_hex).toBe(tx.raw_data_hex);
  expect(normalizeWalletSignature({...tx,signature:["0x"+sig]})).toEqual(normalized);
  for (const signatures of [[], ["aa"], [sig,sig], ["z".repeat(130)]])
    expect(() => normalizeWalletSignature({...tx,signature:signatures})).toThrow();
});
function state() {
  const expires_at = new Date(Date.now() + 60000).toISOString();
  return {
    network: "nile",
    mandate: { hash: "mandate", network: "nile", status: "CONFIRMED" },
    graph: {
      hash: "graph",
      plan_id: "p",
      plan_hash: "plan",
      mandate_hash: "mandate",
      network: "nile",
      account: "wallet",
      expires_at,
    },
    approval: {
      graph_hash: "graph",
      plan_hash: "plan",
      mandate_hash: "mandate",
      network: "nile",
      account: "wallet",
      status: "APPROVED",
      expires_at,
    },
    comparison: {
      network: "nile",
      mandate_hash: "mandate",
      snapshot_root: "snapshot",
      expires_at,
      status: "READY",
      plans: [{ id: "p", hash: "plan", eligible: true, violations: [] }],
    },
    snapshots: [
      { root: "snapshot", network: "nile", status: "VALID", expires_at },
    ],
  } as unknown as Workspace;
}
const session = { authenticated: true, wallet_address: "wallet" } as Session;
it("validates a fresh adjustment without requiring an unrelated allocation comparison", () => {
  const w = state();
  w.comparison = null;
  Object.assign(w.graph!, {review_kind:"ADJUSTMENT",review_id:"review",snapshot_root:"snapshot"});
  w.portfolio_review = {id:"review",snapshot_hash:"snapshot",policy_hash:"mandate",network:"nile",status:"HOLD",expires_at:w.graph!.expires_at} as Workspace["portfolio_review"];
  expect(() => validateApprovalContext(w, session, true)).not.toThrow();
  w.portfolio_review!.snapshot_hash = "new";
  expect(() => validateApprovalContext(w, session, true)).toThrow(/fresh portfolio/);
});
it("binds USDD approval to its current workflow step and hash", () => {
  const w = state();w.comparison=null;
  Object.assign(w.graph!,{id:"usdd-step",review_kind:"USDD_WORKFLOW",steps:[{id:"s1"}]});
  w.usdd_workflow={hash:"plan",account:"wallet",network:"nile",status:"AWAITING_SIGNATURE",cursor:0,steps:[{id:"s1",graph_id:"usdd-step"}]} as Workspace["usdd_workflow"];
  expect(() => validateApprovalContext(w,session,true)).not.toThrow();
  w.usdd_workflow!.cursor=1;
  expect(() => validateApprovalContext(w,session,true)).toThrow(/workflow step/);
});
it("withdrawal keeps the active policy while pending edits stay unconfirmed", () => {
  const w = state();
  w.active_mandate = {...w.mandate!};
  w.mandate = {...w.mandate!,status:"DRAFT",hash:"draft"};
  Object.assign(w.graph!,{review_kind:"ADJUSTMENT",review_id:"review",snapshot_root:"snapshot",steps:[{action:"redeem(uint256)"}]});
  w.portfolio_review={id:"review",policy_hash:"mandate",network:"nile",snapshot_hash:"snapshot",status:"HOLD",expires_at:w.graph!.expires_at} as Workspace["portfolio_review"];
  expect(()=>validateApprovalContext(w,session,true)).not.toThrow();
  w.withdrawal_policy = {...w.active_mandate!,status:"DRAFT"};
  w.active_mandate = null;
  expect(()=>validateApprovalContext(w,session,true)).not.toThrow();
  w.withdrawal_policy.hash="new-confirmed";
  expect(()=>validateApprovalContext(w,session,true)).toThrow(/changed/);
});
it("binds redemption calldata, zero call value and the fee cap before wallet signing", () => {
  const data="db006a75"+"1".padStart(64,"0");
  const step={action:"redeem(uint256)",call_data:data,call_value_sun:"0",recipient:"market",amount:{value:"1",symbol:"TRX",decimals:6},fee_cap:{value:"15",symbol:"TRX",decimals:6}} as Graph["steps"][number];
  const tx={raw_data:{fee_limit:13976000,contract:[{type:"TriggerSmartContract",parameter:{type_url:"type.googleapis.com/protocol.TriggerSmartContract",value:{contract_address:"market",call_value:0,data}}}]}} as unknown as Transaction;
  expect(()=>assertNativeScope(tx,step,x=>x)).not.toThrow();
  for (const delta of [{data:"db006a75"+"2".padStart(64,"0")},{call_value:1},{contract_address:"other"}]) {
    const bad=structuredClone(tx);Object.assign((bad.raw_data.contract as any[])[0].parameter.value,delta);
    expect(()=>assertNativeScope(bad,step,x=>x)).toThrow();
  }
});
it("rejects an approval after account, policy, plan or market evidence changes", () => {
  expect(() => validateApprovalContext(state(), session, true)).not.toThrow();
  const wrongAccount = state();
  wrongAccount.graph!.account = "someone-else";
  expect(() => validateApprovalContext(wrongAccount, session, true)).toThrow();
  const wrongPlan = state();
  wrongPlan.comparison!.plans[0].hash = "changed";
  expect(() => validateApprovalContext(wrongPlan, session, true)).toThrow();
  const stale = state();
  stale.snapshots[0].expires_at = "2000-01-01T00:00:00Z";
  expect(() => validateApprovalContext(stale, session, true)).toThrow();
  const policy = state();
  policy.mandate!.hash = "new";
  expect(() => validateApprovalContext(policy, session, true)).toThrow();
});
it("rejects cross-network evidence and expired or revoked approvals", () => {
  const cross = state();
  cross.comparison!.network = "mainnet";
  expect(() => validateApprovalContext(cross, session, true)).toThrow();
  const expired = state();
  expired.approval!.expires_at = "2000-01-01T00:00:00Z";
  expect(() => validateApprovalContext(expired, session, true)).toThrow();
  const revoked = state();
  revoked.approval!.status = "REVOKED";
  expect(() => validateApprovalContext(revoked, session, true)).toThrow();
});
it("does not accept modified raw transaction fields or an empty signature", () => {
  const tx = {
    txID: "a".repeat(64),
    raw_data_hex: "01",
    raw_data: { expiration: 123, fee_limit: 10 },
  } as Transaction;
  expect(() =>
    assertSignedUnchanged(tx, { ...tx, signature: ["signature"] }),
  ).not.toThrow();
  expect(() =>
    assertSignedUnchanged(tx, {
      ...tx,
      raw_data: { ...tx.raw_data, fee_limit: 100 },
      signature: ["signature"],
    }),
  ).toThrow();
  expect(() => assertSignedUnchanged(tx, { ...tx, signature: [] })).toThrow();
});

it("binds native mint amount, recipient and fee to the displayed execution step", () => {
  const step = {action: "mint() · payable TRX", recipient: "market", amount: {value:"1",symbol:"TRX",decimals:6}, fee_cap:{value:"15",symbol:"TRX",decimals:6}} as Graph["steps"][number];
  const tx = {txID:"a".repeat(64),raw_data_hex:"01",raw_data:{fee_limit:13976000,contract:[{type:"TriggerSmartContract",parameter:{type_url:"type.googleapis.com/protocol.TriggerSmartContract",value:{contract_address:"market",call_value:1000000,data:"1249c58b"}}}]}} as Transaction;
  expect(() => assertNativeScope(tx,step,x=>x)).not.toThrow();
  for (const delta of [{call_value:2000000},{contract_address:"other"},{data:"a0712d68"},{call_token_value:1}]) {
    const bad=structuredClone(tx); const c=(bad.raw_data.contract as {parameter:{value:Record<string,unknown>}}[])[0]; Object.assign(c.parameter.value,delta);
    expect(() => assertNativeScope(bad,step,x=>x)).toThrow();
  }
  expect(() => assertNativeScope({...tx,raw_data:{...tx.raw_data,fee_limit:16000000}},step,x=>x)).toThrow();
});
