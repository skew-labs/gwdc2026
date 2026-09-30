import { expect, it } from "vitest";
import { submissionStatus, type PendingSubmission } from "../src/lib/submission";
import type { Workspace } from "../src/api/contracts";

const pointer: PendingSubmission = {
  txid: "a".repeat(64), graph_id: "graph", step_id: "step-002",
  network: "nile", account: "wallet", approval_id: "approval",
};
function workspace(status = "SUBMITTED"): Workspace {
  return {
    network: "nile",
    graph: { id: "graph", network: "nile", account: "wallet",
      expires_at: "2000-01-01T00:00:00Z",
      steps: [{id: "step-002", txid: pointer.txid, status}] },
    execution: { id: pointer.txid, graph_id: "graph", network: "nile", status },
  } as unknown as Workspace;
}
const status = (w: Workspace | undefined) => submissionStatus(pointer, w, "nile", "wallet");

it("keeps a node-accepted transaction pending until position verification arrives", () => {
  expect(status(workspace())).toBe("CONFIRMING");
  expect(status(workspace("CONFIRMED"))).toBe("UNKNOWN");
  // This update comes from the worker through workspace polling, without a user click.
  expect(status(workspace("POSITION_RECONCILED"))).toBe("RECONCILED");
});
it("can clear a completed transaction even after its signing window expires", () => {
  expect(status(workspace("POSITION_RECONCILED"))).toBe("RECONCILED");
});
it("never clears a pointer for another wallet, network, graph or transaction", () => {
  const w = workspace("POSITION_RECONCILED");
  expect(submissionStatus(pointer,w,"mainnet","wallet")).toBe("OTHER_WALLET");
  expect(submissionStatus(pointer,w,"nile","other")).toBe("OTHER_WALLET");
  for (const patch of [{id:"other"},{account:"other"},{network:"mainnet"}]) {
    const mismatch=structuredClone(w);Object.assign(mismatch.graph!,patch);
    expect(status(mismatch)).toBe("UNKNOWN");
  }
  const otherTx=structuredClone(w);otherTx.graph!.steps[0].txid="b".repeat(64);
  expect(status(otherTx)).toBe("UNKNOWN");
  const otherExecution=structuredClone(w);otherExecution.execution!.id="b".repeat(64);
  expect(status(otherExecution)).toBe("UNKNOWN");
});
it("requires the step and execution to agree on a terminal outcome", () => {
  const w=workspace("POSITION_RECONCILED");w.graph!.steps[0].status="SUBMITTED";
  expect(status(w)).toBe("UNKNOWN");
  expect(status(undefined)).toBe("UNKNOWN");
});
it("releases a confirmed failure but retains disputed and uncertain transactions", () => {
  expect(status(workspace("FAILED"))).toBe("FAILED");
  expect(status(workspace("DISPUTED"))).toBe("DISPUTED");
  expect(status(workspace("SUBMISSION_UNKNOWN"))).toBe("UNKNOWN");
});


it("uses the exact confirmed USDD step after a later graph replaces it", () => {
  const pointer = { graph_id: "usdd-flow-0", step_id: "usdd-step-1", txid: "a".repeat(64), network: "mainnet" as const, account: "wallet", approval_id: "approval" };
  const workspace = { network: "mainnet", usdd_workflow: { network: "mainnet", account: "wallet", confirmed_txids: [pointer.txid], steps: [{ id: pointer.step_id, graph_id: pointer.graph_id, txid: pointer.txid, status: "CONFIRMED" }] } } as unknown as Workspace;
  expect(submissionStatus(pointer, workspace, "mainnet", "wallet")).toBe("RECONCILED");
  expect(submissionStatus({ ...pointer, step_id: "other-step" }, workspace, "mainnet", "wallet")).toBe("UNKNOWN");
  expect(submissionStatus(pointer, workspace, "mainnet", "another-wallet")).toBe("OTHER_WALLET");
});
it("releases a browser recovery pointer when the server proves it expired, even with a new graph", () => {
  const w = workspace(); w.graph!.id = "another-graph";
  w.transaction_resolutions = [{ txid:pointer.txid, graph_id:pointer.graph_id, step_id:pointer.step_id, status:"EXPIRED_NOT_OBSERVED", message:"closed" }];
  expect(status(w)).toBe("EXPIRED");
  w.transaction_resolutions[0].txid = "b".repeat(64);
  expect(status(w)).toBe("UNKNOWN");
});
it("uses a native receipt after voting replaces the staking review",()=>{
 const w=workspace();w.graph!.id="next-native-step";
 w.stake_transaction_receipts=[{txid:pointer.txid,graph_id:pointer.graph_id,step_id:pointer.step_id,status:"POSITION_RECONCILED"}];
 expect(status(w)).toBe("RECONCILED");
 w.stake_transaction_receipts[0].step_id="another-step";
 expect(status(w)).toBe("UNKNOWN");
});
