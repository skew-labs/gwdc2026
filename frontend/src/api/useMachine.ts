import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { z } from "zod";
import {
  Ack,
  Reconciliation,
  Agent,
  Agents,
  ServiceStatus,
  Usage,
  Challenge,
  Prepared,
  Session,
  Workspace,
  type Network,
  type Role,
  type Mandate,
  type Plan,
  type Graph,
  type Approval,
} from "./contracts";
import { request, setCsrf, ApiError } from "./client";
import {
  connectWallet,
  identity,
  signOwnership,
  signPrepared,
  watchWallet,
} from "../lib/wallet";
import { expired } from "../lib/format";
import { errorMessage } from "../lib/errors";
import { validateApprovalContext } from "../lib/guards";
import {
  submissionStatus,
  submissionMessages,
  type PendingSubmission,
} from "../lib/submission";
export type Pending = PendingSubmission;
const pendingKey = "machine:pending-submission:v1";
const readPending = (): Pending | null => {
  try {
    return z
      .object({
        graph_id: z.string(),
        step_id: z.string(),
        txid: z.string(),
        network: z.enum(["nile", "mainnet"]),
        account: z.string(),
        approval_id: z.string(),
        phase: z.enum(["PREPARED", "SIGNED"]).optional(),
      })
      .parse(JSON.parse(sessionStorage.getItem(pendingKey) || "null"));
  } catch {
    return null;
  }
};
export function useMachine(agentId?: string) {
  const [params, setParams] = useSearchParams();
  const network: Network =
    params.get("network") === "mainnet" ? "mainnet" : "nile";
  const qc = useQueryClient();
  const [busy, setBusy] = useState<string | null>(null);
  const locked = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [walletValid, setWalletValid] = useState(false);
  const [pending, setPending] = useState<Pending | null>(readPending);
  const restoredPending = useRef(pending?.txid);
  const keys = useRef(new Map<string, string>());
  const session = useQuery({
    queryKey: ["session"],
    queryFn: async ({ signal }) => {
      const s = await request("GET", "/v1/session", Session, undefined, {
        signal,
      });
      setCsrf(s.csrf_token);
      return s;
    },
    retry: false,
  });
  const agents = useQuery({
    queryKey: ["agents", session.data?.wallet_address],
    queryFn: ({ signal }) =>
      request("GET", "/v1/agents", Agents, undefined, { signal }),
    enabled: session.isSuccess,
    refetchInterval: 4000,
  });
  const archived = useQuery({
    queryKey: ["agents", "archived", session.data?.wallet_address],
    queryFn: () => request("GET", "/v1/agents?archived=true", Agents),
    enabled: session.isSuccess,
  });
  const service = useQuery({
    queryKey: ["service"],
    queryFn: () => request("GET", "/v1/status", ServiceStatus),
    enabled: session.isSuccess,
  });
  const usage = useQuery({
    queryKey: ["usage", session.data?.wallet_address],
    queryFn: () => request("GET", "/v1/usage", Usage),
    enabled: session.isSuccess,
    refetchInterval: 10000,
  });
  const workspace = useQuery({
    queryKey: ["workspace", network, session.data?.wallet_address, agentId],
    queryFn: ({ signal }) =>
      request(
        "GET",
        `/v1/workspace?network=${network}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ""}`,
        Workspace,
        undefined,
        {
          signal,
        },
      ),
    enabled: session.isSuccess,
    refetchInterval: (q) =>
      q.state.data?.jobs.some((j) => ["QUEUED", "RUNNING"].includes(j.status))
        ? 700
        : 4000,
    retry: false,
    refetchOnWindowFocus: true,
  });
  const [clock, setClock] = useState(Date.now());
  const pendingStatus = submissionStatus(
    pending,
    workspace.data,
    network,
    session.data?.wallet_address,
  );
  const pendingMessage = pending?.phase === "PREPARED" && pendingStatus === "UNKNOWN"
    ? "Your wallet request is unfinished. Continue it here, or wait for automatic chain verification after it expires."
    : submissionMessages[pendingStatus];
  useEffect(() => {
    const saved = workspace.data?.prepared_transactions?.[0];
    if (pending || !saved || saved.network !== network || saved.account !== session.data?.wallet_address) return;
    // Recover from the server even after a new tab, browser restart or lost storage.
    sessionStorage.setItem(pendingKey, JSON.stringify(saved));
    setPending(saved);
  }, [workspace.data?.prepared_transactions, pending, network, session.data?.wallet_address]);
  useEffect(() => {
    // Worker reconciliation arrives through workspace polling. A terminal result
    // must release the local pointer without requiring another recovery click.
    if (!pending || !["RECONCILED", "FAILED", "EXPIRED"].includes(pendingStatus)) return;
    sessionStorage.removeItem(pendingKey);
    setPending(null);
    setNotice(submissionMessages[pendingStatus]);
  }, [pending, pendingStatus]);
  useEffect(() => {
    const id = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    setWalletValid(false);
    const check = () => {
      setWalletValid(false);
      identity()
        .then((i) => {
          setWalletValid(
            i.address === session.data?.wallet_address && i.network === network,
          );
        })
        .catch(() => {});
    };
    check();
    const remove = watchWallet(() => {
      setWalletValid(false);
      setNotice(
        "Wallet changed. Verify the selected account and network before continuing.",
      );
      qc.cancelQueries({ queryKey: ["workspace"] });
    });
    return remove;
  }, [session.data?.wallet_address, network, qc]);
  const refresh = useCallback(async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["workspace"] }),
      qc.invalidateQueries({ queryKey: ["agents"] }),
      qc.invalidateQueries({ queryKey: ["usage"] }),
    ]);
  }, [qc]);
  const run = useCallback(
    async (label: string, fn: () => Promise<void>) => {
      if (locked.current) return false;
      locked.current = true;
      setBusy(label);
      setError(null);
      try {
        await fn();
        return true;
      } catch (e) {
        setError(errorMessage(e));
        if (e instanceof ApiError && e.status === 401) {
          setWalletValid(false);
          await qc.invalidateQueries({ queryKey: ["session"] });
        }
        return false;
      } finally {
        locked.current = false;
        setBusy(null);
      }
    },
    [qc],
  );
  async function mutate(path: string, body: unknown, method = "POST") {
    const fingerprint = method + path + JSON.stringify(body);
    const key = keys.current.get(fingerprint) || crypto.randomUUID();
    keys.current.set(fingerprint, key);
    await request(method, path, Ack, body, { key });
    keys.current.delete(fingerprint);
    await refresh();
  }
  async function connect() {
    return run("Connecting wallet", async () => {
      const i = await connectWallet();
      if (i.network !== network)
        throw new Error(
          `Select ${network === "nile" ? "Nile Testnet" : "TRON Mainnet"} in TronLink first.`,
        );
      const challenge = await request("POST", "/v1/auth/challenge", Challenge, {
        address: i.address,
        network,
        domain: location.host,
      });
      if (
        challenge.domain !== location.host ||
        challenge.address !== i.address ||
        challenge.network !== network ||
        expired(challenge.expires_at)
      )
        throw new Error("The ownership challenge is invalid or expired.");
      const signature = await signOwnership(challenge.message);
      const after = await identity();
      if (after.address !== i.address || after.network !== network)
        throw new Error("Wallet changed. Reconnect to verify the account.");
      const s = await request("POST", "/v1/auth/verify", Session, {
        challenge_id: challenge.id,
        signature,
      });
      setCsrf(s.csrf_token);
      qc.setQueryData(["session"], s);
      setWalletValid(s.authenticated && s.wallet_address === i.address);
      await refresh();
    });
  }
  async function logout() {
    return run("Disconnecting", async () => {
      await request("POST", "/v1/auth/logout", Ack, {});
      setWalletValid(false);
      qc.removeQueries({ queryKey: ["workspace"] });
      await qc.invalidateQueries({ queryKey: ["session"] });
    });
  }
  function changeNetwork(n: Network) {
    if (busy) return;
    setError(null);
    setNotice(null);
    setWalletValid(false);
    setParams({ ...Object.fromEntries(params), network: n });
  }
  async function message(role: Role, text: string) {
    return run("Sending message", () =>
      mutate("/v1/messages", { agent_id: agentId, role, text, network }),
    );
  }
  async function saveMandate(
    constraints: Mandate["constraints"],
    source_text: string,
    terms: Record<string, unknown>,
    assumptions: Record<string, unknown>,
  ) {
    return run("Saving mandate", () =>
      mutate("/v1/mandates", {
        terms,
        assumptions,
        constraints,
        source_text,
        network,
        agent_id: agentId,
        previous_id: workspace.data?.mandate?.id || null,
      }),
    );
  }
  async function confirm(m: Mandate) {
    return run("Confirming mandate", () =>
      mutate(`/v1/mandates/${encodeURIComponent(m.id)}/confirm`, {
        hash: m.hash,
        version: m.version,
        network,
      }),
    );
  }
  async function compare() {
    const m = workspace.data?.mandate;
    if (!m) return;
    return run("Comparing plans", () =>
      mutate("/v1/plan-comparisons", {
        mandate_id: m.id,
        mandate_hash: m.hash,
        network,
        agent_id: agentId,
      }),
    );
  }
  async function review(p: Plan) {
    const w = workspace.data;
    if (!w?.mandate) return;
    return run("Preparing execution review", async () => {
      await mutate("/v1/execution-graphs", {
        plan_id: p.id,
        plan_hash: p.hash,
        mandate_hash: w.mandate!.hash,
        network,
        agent_id: agentId,
      });
      const next = new URLSearchParams(params);
      next.set("panel", "execution");
      setParams(next);
    });
  }
  async function confirmAndCompare(m: Mandate) {
    return run("Confirming conditions and comparing plans", async () => {
      await request(
        "POST",
        `/v1/mandates/${encodeURIComponent(m.id)}/confirm`,
        Ack,
        { hash: m.hash, version: m.version, network },
      );
      const fresh = await request(
        "GET",
        `/v1/workspace?network=${network}`,
        Workspace,
      );
      if (fresh.mandate?.id !== m.id || fresh.mandate.status !== "CONFIRMED")
        throw new Error("The conditions changed. Review them again.");
      await mutate("/v1/plan-comparisons", {
        mandate_id: fresh.mandate.id,
        mandate_hash: fresh.mandate.hash,
        network,
        agent_id: agentId,
      });
    });
  }
  async function consent(g: Graph) {
    return run("Recording your approval", () =>
      mutate("/v1/approvals", {
        graph_id: g.id,
        graph_hash: g.hash,
        mandate_hash: g.mandate_hash,
        plan_hash: g.plan_hash,
        account: g.account,
        network,
      }),
    );
  }
  const [preflight, setPreflight] = useState<Prepared | null>(null);
  useEffect(
    () => setPreflight(null),
    [network, workspace.data?.graph?.hash, workspace.data?.approval?.id],
  );
  function assertContext(_g: Graph, _a: Approval) {
    if (!workspace.data || !session.data)
      throw new Error("Reload the workspace first.");
    validateApprovalContext(workspace.data, session.data, walletValid);
  }
  async function check(step_id: string) {
    const g = workspace.data?.graph;
    const a = workspace.data?.approval;
    if (!g || !a) return;
    return run("Running preflight", async () => {
      assertContext(g, a);
      const result = await request(
        "POST",
        `/v1/execution-graphs/${encodeURIComponent(g.id)}/preflight`,
        Prepared,
        { approval_id: a.id, step_id, network, account: g.account },
      );
      if (
        result.graph_hash !== g.hash ||
        result.approval_id !== a.id ||
        result.step_id !== step_id ||
        result.account !== g.account ||
        result.network !== network
      )
        throw new Error("Preflight does not match this approved step.");
      setPreflight(result);
    });
  }
  async function sign(step_id: string) {
    const g = workspace.data?.graph;
    const a = workspace.data?.approval;
    if (!g || !a) return;
    return run("Waiting for wallet signature", async () => {
      assertContext(g, a);
      const resume = pending?.phase === "PREPARED" && pending.graph_id === g.id && pending.step_id === step_id &&
        workspace.data?.prepared_transactions?.some(p => p.txid === pending.txid && p.approval_id === a.id);
      if (pending && !resume)
        throw new Error(
          "Reconcile the previous submission before requesting another signature.",
        );
      // Always repeat server preflight immediately before the wallet prompt.
      const p = await request(
        "POST",
        `/v1/execution-graphs/${encodeURIComponent(g.id)}/preflight`,
        Prepared,
        { approval_id: a.id, step_id, network, account: g.account },
      );
      setPreflight(p);
      if (
        p.graph_hash !== g.hash ||
        p.approval_id !== a.id ||
        p.step_id !== step_id ||
        p.account !== g.account ||
        p.network !== network ||
        expired(p.expires_at) ||
        p.checks.length === 0 ||
        p.checks.some((c) => c.status !== "PASS")
      )
        throw new Error("Preflight did not pass for the approved step.");
      const step = g.steps.find((s) => s.id === step_id);
      if (
        !step ||
        !["READY", "AWAITING_USER_SIGNATURE"].includes(step.status) ||
        !step.depends_on.every((dep) =>
          g.steps.some(
            (s) =>
              s.id === dep &&
              ["CONFIRMED", "POSITION_RECONCILED"].includes(s.status),
          ),
        )
      )
        throw new Error("Previous steps are still pending.");
      if (!p.transaction) throw new Error("The service did not return an unsigned transaction. Refresh the review.");
      if (resume && p.transaction.txID !== pending?.txid) throw new Error("The wallet request changed. Check its status before signing again.");
      const record: Pending = {
        graph_id: g.id,
        step_id,
        txid: p.transaction.txID,
        network,
        account: g.account,
        approval_id: a.id,
        phase: "PREPARED",
      };
      // Retain a recovery pointer even if the wallet closes or rejects signing.
      if ((g.id.startsWith("native-") || g.id.startsWith("stake-"))) {
        sessionStorage.setItem(pendingKey, JSON.stringify(record));
        setPending(record);
      }
      const signed = await signPrepared(p, step);
      const signedRecord: Pending = { ...record, phase: "SIGNED" };
      // Persist only a reconciliation pointer, never a key or signed payload. Do this before any submission.
      sessionStorage.setItem(pendingKey, JSON.stringify(signedRecord));
      setPending(signedRecord);
      await request(
        "POST",
        "/v1/executions",
        Ack,
        {
          approval_id: a.id,
          graph_id: g.id,
          step_id,
          network,
          signed_transaction: signed,
        },
        { key: `signed-${signed.txID}` },
      );
      // Keep the recovery pointer until the service verifies a terminal chain outcome.
      setPreflight(null);
      await refresh();
    });
  }
  async function reconcile() {
    const graph = workspace.data?.graph;
    const step = graph?.steps.find((s) => s.txid);
    const pointer =
      pending ||
      workspace.data?.prepared_transactions?.[0] ||
      (graph && step?.txid
        ? {
            graph_id: graph.id,
            step_id: step.id,
            txid: step.txid,
            network: graph.network,
            account: graph.account,
            approval_id: workspace.data?.approval?.id || "",
          }
        : null);
    if (!pointer) return;
    return run("Reconciling transaction", async () => {
      if (
        pointer.network !== network ||
        pointer.account !== session.data?.wallet_address
      )
        throw new Error(
          "Select the original network and verify the original wallet to reconcile this submission.",
        );
      const prepared = workspace.data?.prepared_transactions?.find(p => p.txid === pointer.txid);
      if (prepared && Date.parse(prepared.recover_after) > Date.now()) {
        setNotice("This wallet request is still valid. Continue in TronLink, or wait for automatic verification after it expires.");
        return;
      }
      const result = await request(
        "POST",
        "/v1/executions/reconcile",
        Reconciliation,
        {
          graph_id: pointer.graph_id,
          step_id: pointer.step_id,
          txid: pointer.txid,
          network,
        },
        { key: `reconcile-${pointer.txid}-${crypto.randomUUID()}` },
      );
      const w = await request(
        "GET",
        `/v1/workspace?network=${network}${agentId ? `&agent_id=${encodeURIComponent(agentId)}` : ""}`,
        Workspace,
      );
      qc.setQueryData(
        ["workspace", network, session.data?.wallet_address, agentId],
        w,
      );
      const status = submissionStatus(
        pointer,
        w,
        network,
        session.data?.wallet_address,
      );
      const recovered =
        result.resolution?.txid === pointer.txid &&
        result.resolution.graph_id === pointer.graph_id &&
        result.resolution.step_id === pointer.step_id &&
        result.resolution.status === "EXPIRED_NOT_OBSERVED";
      if (recovered || ["RECONCILED", "FAILED", "EXPIRED"].includes(status)) {
        sessionStorage.removeItem(pendingKey);
        setPending(null);
        setNotice(
          recovered ? result.resolution!.message : submissionMessages[status],
        );
      } else setNotice(submissionMessages[status]);
    });
  }
  useEffect(() => {
    if (
      busy ||
      !pending ||
      restoredPending.current !== pending.txid ||
      pending.network !== network ||
      pending.account !== session.data?.wallet_address
    )
      return;
    restoredPending.current = undefined;
    void reconcile();
  }, [busy, pending, network, session.data?.wallet_address]);
  async function saveAgent(
    input: { name: string; role: Role; instructions: string },
    id?: string,
  ) {
    let saved: Agent | undefined;
    await run(id ? "Saving agent" : "Creating agent", async () => {
      const path = id ? `/v1/agents/${encodeURIComponent(id)}` : "/v1/agents";
      const fingerprint = path + JSON.stringify(input);
      const key = keys.current.get(fingerprint) || crypto.randomUUID();
      keys.current.set(fingerprint, key);
      saved = await request(id ? "PATCH" : "POST", path, Agent, input, { key });
      keys.current.delete(fingerprint);
      await refresh();
    });
    return saved;
  }
  const archiveAgent = (id: string) =>
    run("Archiving agent", () =>
      mutate(`/v1/agents/${encodeURIComponent(id)}`, {}, "DELETE"),
    );
  const stop = (id: string) =>
    run("Stopping response", () =>
      mutate(`/v1/jobs/${encodeURIComponent(id)}/cancel`, {}),
    );
  return {
    agentId,
    agents,
    archived,
    restoreAgent: (id: string) =>
      run("Restoring agent", () =>
        mutate(`/v1/agents/${encodeURIComponent(id)}/restore`, {}),
      ),
    service,
    usage,
    saveAgent,
    archiveAgent,
    stop,
    network,
    changeNetwork,
    session,
    workspace,
    busy,
    error,
    setError,
    notice,
    setNotice,
    connect,
    logout,
    walletValid,
    message,
    saveMandate,
    confirm,
    confirmAndCompare,
    compare,
    review,
    consent,
    check,
    sign,
    preflight,
    pending,
    pendingStatus,
    pendingMessage,
    reconcile,
    refresh,
    stakeAction: (action: "next" | "cancel" | "refresh" | "UNSTAKE" | "WITHDRAW" | "CLAIM") => run("Updating native stake", async () => {
      await mutate(`/v1/stake-workflows/${["next","cancel","refresh"].includes(action) ? action : "lifecycle"}`, {network,action,agent_id:agentId});
      if (action !== "refresh" && action !== "cancel") { const next=new URLSearchParams(params);next.set("panel","execution");setParams(next); }
    }),
    reviewPortfolio: () =>
      run("Reviewing current holdings and costs", () =>
        mutate("/v1/portfolio-reviews", { network, agent_id: agentId }),
      ),
    withdrawToWallet: () => run("Preparing your withdrawal", async () => {
      const current = await request("GET", `/v1/workspace?network=${network}`, Workspace);
      const open = current.prepared_transactions?.[0];
      const currentStatus = submissionStatus(pending, current, network, session.data?.wallet_address);
      if (open || (current.execution && !["FAILED", "POSITION_RECONCILED"].includes(current.execution.status)) ||
          (pending && !["EXPIRED", "FAILED", "RECONCILED"].includes(currentStatus))) {
        qc.setQueryData(["workspace", network, session.data?.wallet_address, agentId], current);
        if (open && !pending) { sessionStorage.setItem(pendingKey, JSON.stringify(open)); setPending(open); }
        const next = new URLSearchParams(params); next.set("panel", "execution"); setParams(next);
        setNotice("Continue your existing transaction here. No new withdrawal was created.");
        return;
      }
      if (pending) { sessionStorage.removeItem(pendingKey); setPending(null); }
      await mutate("/v1/portfolio-reviews", { network, agent_id: agentId });
      const fresh = await request("GET", `/v1/workspace?network=${network}`, Workspace);
      const r = fresh.portfolio_review;
      const option = r?.alternatives.find(a => a.action === "REDEEM" && a.position === "0" && a.eligible && a.hash);
      if (!r || !option) throw new Error(r?.reason || "A withdrawal is not available under your active conditions.");
      await mutate("/v1/portfolio-adjustments", { network, review_id: r.id, option_hash: option.hash, agent_id: agentId });
      const next = new URLSearchParams(params);
      next.set("panel", "execution");
      setParams(next);
    }),
    adjustPortfolio: (reviewId: string, optionHash: string) =>
      run("Refreshing and reviewing adjustment", async () => {
        await mutate("/v1/portfolio-adjustments", {
          network, review_id: reviewId, option_hash: optionHash, agent_id: agentId,
        });
        const next = new URLSearchParams(params);
        next.set("panel", "execution");
        setParams(next);
      }),
    refreshSources: () =>
      run("Refreshing observations", () =>
        mutate("/v1/portfolio-reviews", { network }),
      ),
    run,
    mutate,
    clock,
  };
}
export type Machine = ReturnType<typeof useMachine>;
