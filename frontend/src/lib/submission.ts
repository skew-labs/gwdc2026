import type { Network, Workspace } from "../api/contracts";

export type PendingSubmission = {
  graph_id: string;
  step_id: string;
  txid: string;
  network: Network;
  account: string;
  approval_id: string;
  phase?: "PREPARED" | "SIGNED";
};

export function submissionStatus(
  pending: PendingSubmission | null,
  workspace: Workspace | undefined,
  network: Network,
  account: string | null | undefined,
) {
  if (!pending) return "NONE";
  if (pending.network !== network || pending.account !== account)
    return "OTHER_WALLET";
  if (workspace?.network === network && workspace.transaction_resolutions?.some(r =>
    r.txid === pending.txid && r.graph_id === pending.graph_id && r.step_id === pending.step_id && r.status === "EXPIRED_NOT_OBSERVED"))
    return "EXPIRED";
  if (workspace?.network === network) {
    const receipt=workspace.stake_transaction_receipts?.find(r=>r.txid===pending.txid && r.graph_id===pending.graph_id && r.step_id===pending.step_id);
    if (receipt?.status === "POSITION_RECONCILED") return "RECONCILED";
    if (receipt?.status === "FAILED") return "FAILED";
    if (receipt?.status === "DISPUTED") return "DISPUTED";
  }
  const workflow = workspace?.usdd_workflow;
  if (workspace?.network === pending.network && workflow?.network === pending.network && workflow.account === pending.account) {
    const recorded = workflow.steps.find(s => s.id === pending.step_id && s.graph_id === pending.graph_id && s.txid === pending.txid);
    if (recorded?.status === "CONFIRMED" && workflow.confirmed_txids.includes(pending.txid)) return "RECONCILED";
    if (recorded?.status === "FAILED") return "FAILED";
    if (recorded?.status === "DISPUTED") return "DISPUTED";
  }
  const graph = workspace?.graph;
  const execution = workspace?.execution;
  const step = graph?.steps.find((s) => s.id === pending.step_id);
  if (
    workspace?.network !== pending.network ||
    graph?.network !== pending.network ||
    graph?.account !== pending.account ||
    graph?.id !== pending.graph_id ||
    step?.txid !== pending.txid ||
    execution?.id !== pending.txid ||
    execution.graph_id !== pending.graph_id ||
    execution.network !== pending.network
  )
    return "UNKNOWN";
  if (
    execution.status === "POSITION_RECONCILED" &&
    step.status === "POSITION_RECONCILED"
  )
    return "RECONCILED";
  if (execution.status === "FAILED" && step.status === "FAILED")
    return "FAILED";
  if (execution.status === "DISPUTED") return "DISPUTED";
  if (execution.status === "SUBMITTED") return "CONFIRMING";
  return "UNKNOWN";
}

export const submissionMessages = {
  NONE: "",
  EXPIRED: "The previous wallet request expired without a transaction. You can review a fresh withdrawal.",
  OTHER_WALLET:
    "Select the original wallet and network to check this transaction.",
  UNKNOWN:
    "Checking whether the signed transaction reached the network. Check its status before signing again.",
  CONFIRMING:
    "Transaction submitted. Waiting for chain confirmation and position verification. No further signature is needed.",
  DISPUTED:
    "The transaction needs review because its receipt and position checks differ. Further execution is blocked.",
  RECONCILED: "Transaction confirmed and position verified.",
  FAILED:
    "The transaction failed on-chain. Check its receipt for any fees charged.",
};
