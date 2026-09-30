import { afterAll, beforeAll, expect, it } from "vitest";
import { createServer, type Server } from "node:http";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { TronWeb } from "tronweb";
import { Workspace } from "../src/api/contracts";
let finance: Server, child: ChildProcess, dir: string, base: string;
const origin = "http://127.0.0.1:5173";
const empty = () =>
  Workspace.parse({
    revision: 0,
    network: "nile",
    mode: "SIMULATION",
    messages: [],
    mandate: null,
    comparison: null,
    graph: null,
    approval: null,
    execution: null,
    positions: [],
    performance: null,
    balances: [{ symbol: "TRX", value: "100", decimals: 6 }],
    snapshots: [],
    routines: [],
    evidence: [],
    activity: [],
    jobs: [],
    intent: null,
  });
const states = new Map<string, Workspace>();
let intentMode = "normal";
let comparisonMode = "ready";
let releaseComparison: (() => void) | null = null;
const completedRequests = new Set<string>();
let usddReads = 0;
let reviewReads = 0,
  allocationComparisons = 0;
async function start() {
  child = spawn(process.execPath, ["--import", "tsx", "server/index.ts"], {
    env: {
      ...process.env,
      PORT: "0",
      APP_ORIGIN: origin,
      DATABASE_PATH: join(dir, "test.sqlite"),
      KILN_API_KEY: "test-unused",
      FINANCE_API_URL: `http://127.0.0.1:${(finance.address() as any).port}`,
      FINANCE_SERVICE_TOKEN: "test-only",
      NODE_ENV: "test",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  base = await new Promise<string>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("startup timeout")), 10000);
    child.stdout!.on("data", (d) => {
      const m = String(d).match(/http:\/\/127.0.0.1:\d+/);
      if (m) {
        clearTimeout(timer);
        resolve(m[0]);
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`exit ${code}`));
    });
  });
}
async function stop() {
  await new Promise<void>((resolve) => {
    child.once("exit", () => resolve());
    child.kill("SIGTERM");
  });
}
type Client = { cookie: string; csrf: string };
async function session() {
  const r = await fetch(base + "/v1/session");
  return {
    cookie: r.headers.get("set-cookie")!.split(";")[0],
    csrf: (await r.json()).csrf_token,
  } as Client;
}
async function call(
  c: Client,
  path: string,
  method = "GET",
  body?: unknown,
  key: string = crypto.randomUUID(),
) {
  const r = await fetch(base + path, {
    method,
    headers: {
      Cookie: c.cookie,
      Origin: origin,
      "X-CSRF-Token": c.csrf,
      "Idempotency-Key": key,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: r.status, body: await r.json() };
}
beforeAll(async () => {
  dir = await mkdtemp(join(tmpdir(), "conversation-gateway-"));
  finance = createServer(async (req, res) => {
    const scope = String(req.headers["x-machine-workspace"]);
    const w = states.get(scope) || empty();
    states.set(scope, w);
    let body = "";
    for await (const chunk of req) body += chunk;
    const payload = body ? JSON.parse(body) : {};
    if (req.url === "/v1/machine/agent-intent" && intentMode !== "normal") {
      const mode = intentMode;
      intentMode = "normal";
      res.setHeader("Content-Type", "application/json");
      if (mode === "transport") {
        res.statusCode = 503;
        res.end(
          JSON.stringify({
            error: {
              code: "MODEL_DOWN",
              message: "Model extraction unavailable",
            },
          }),
        );
        return;
      }
      res.end(
        JSON.stringify({
          status: "MODEL_OUTPUT_REJECTED",
          intent: null,
          patch: {},
          reason_codes: ["INVALID_MODEL_OUTPUT"],
        }),
      );
      return;
    }
    if (req.method === "POST" && req.url === "/v1/machine/mandates")
      w.mandate = {
        id: "test-draft",
        version: 1,
        hash: "draft-hash",
        network: "nile",
        status: "DRAFT",
        source_text: "Synthetic API test form",
        constraints: {
          capital: { symbol: "TRX", value: "100", decimals: 6 },
          horizon_days: 7,
          min_cash_bps: 2000,
          max_trx_exposure_bps: 10000,
          allow_debt: false,
          allowed_protocols: ["JustLend"],
          max_fee: { symbol: "TRX", value: "15", decimals: 6 },
        },
        missing_fields: [],
        confirmed_at: null,
      };
    if (req.method === "POST" && req.url === "/v1/machine/portfolio-reviews") {
      reviewReads++;
      w.portfolio_review = {
        id: `fresh-review-${reviewReads}`,
        network: "nile",
        policy_hash: "confirmed-policy",
        observed_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 300000).toISOString(),
        source: "ON_DEMAND",
        status: "HOLD",
        reason: "Fresh RPC inputs show no economic improvement after costs.",
        checks: [],
        hold: null,
        alternatives: [],
        suggested: null,
        execution_authority: "NONE",
        limitations: [],
        snapshot_hash: "test-snapshot",
        block: "123",
      };
    }
    if (req.method === "POST" && req.url === "/v1/machine/usdd-reviews") {
      usddReads++;
      w.usdd_review = {
        id: `usdd-${usddReads}`,
        hash: "test-hash",
        network: "nile",
        observed_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 60000).toISOString(),
        status: "BLOCKED",
        policy_hash: null,
        blockers: ["VAULT_DESTINATION_TOKEN_MISMATCH"],
        reason: "Synthetic route identity mismatch for gateway test.",
        execution_authority: "NONE",
        evidence_hash: "test-evidence",
        basis: "Synthetic gateway test",
        source_urls: [],
        block_range: null,
        facts: {
          energy_sun: "100",
          bandwidth_sun: "1000",
          vault_token: "vault",
          destination_token: "destination",
          token_match: false,
          supply_apy: "0.01",
          borrow_apy: "0.02",
          loop_spread: "-0.01",
          market_cash_usdd: "10",
          collateral_factor: "0.8",
          collaterals: [],
        },
      };
    }
    const requestKey = scope + req.url + req.headers["idempotency-key"];
    if (
      req.method === "POST" &&
      req.url === "/v1/machine/mandates/test-draft/confirm" &&
      !completedRequests.has(requestKey)
    ) {
      if (
        !w.mandate ||
        w.mandate.status !== "DRAFT" ||
        payload.hash !== w.mandate.hash ||
        payload.version !== w.mandate.version
      ) {
        res.statusCode = 409;
        res.setHeader("Content-Type", "application/json");
        res.end(
          JSON.stringify({
            error: {
              code: "STALE_DRAFT",
              message: "The draft changed. Review it again.",
            },
          }),
        );
        return;
      }
      w.mandate.status = "CONFIRMED";
      w.mandate.version++;
      w.mandate.hash = "confirmed-" + scope;
      completedRequests.add(requestKey);
    }
    if (
      req.method === "POST" &&
      req.url === "/v1/machine/plan-comparisons" &&
      !completedRequests.has(requestKey)
    ) {
      if (comparisonMode === "failure") {
        res.statusCode = 503;
        res.setHeader("Content-Type", "application/json");
        res.end(
          JSON.stringify({
            error: {
              code: "QUOTE_DOWN",
              message:
                "Current market quotes could not be read. Retry calculation.",
            },
          }),
        );
        return;
      }
      allocationComparisons++;
      const money = (value: string) => ({ symbol: "TRX", value, decimals: 6 });
      w.comparison = {
        id: "comparison-" + scope,
        network: "nile",
        mandate_hash: w.mandate!.hash,
        snapshot_root: "synthetic-test-snapshot",
        expires_at: new Date(Date.now() + 300000).toISOString(),
        status: comparisonMode === "infeasible" ? "INFEASIBLE" : "READY",
        reason:
          comparisonMode === "infeasible"
            ? "Retained 2 TRX cap is below full entry and exit costs."
            : null,
        plans:
          comparisonMode === "infeasible"
            ? []
            : ["conservative", "income"].map((id, i) => ({
                id,
                hash: id + "-hash",
                title: id,
                summary: "Synthetic gateway fixture, not a market quote",
                allocations: [],
                expected_net_return: money(String(i + 1)),
                estimated_fees: money("2"),
                immediate_cash: money("20"),
                recoverable_cash: [],
                risks: [],
                eligible: true,
                violations: [],
              })),
        usdd_vault: { status: "UNAVAILABLE", reason: "Synthetic fixture" },
        math_version: "test",
        adapter_version: "test",
        search_scope: "Synthetic API contract fixture",
      };
      completedRequests.add(requestKey);
      if (comparisonMode === "pause")
        await new Promise<void>((resolve) => {
          releaseComparison = resolve;
        });
    }
    res.setHeader("Content-Type", "application/json");
    res.end(
      JSON.stringify(
        req.method === "GET"
          ? w
          : req.url === "/v1/machine/agent-intent"
            ? { status: "NO_CONDITIONS", patch: {} }
            : { accepted: true, job_id: null },
      ),
    );
  });
  await new Promise<void>((resolve) => finance.listen(0, "127.0.0.1", resolve));
  await start();
}, 15000);
afterAll(async () => {
  if (child) await stop();
  if (finance)
    await new Promise<void>((resolve) => finance.close(() => resolve()));
  if (dir) await rm(dir, { recursive: true, force: true });
});
it("persists cards, deduplicates retries, isolates conversations, and grounds portfolio chat without a model call", async () => {
  const c = await session();
  const account = await TronWeb.createAccount();
  const tron = new TronWeb({
    fullHost: origin,
    privateKey: account.privateKey,
  });
  const challenge = (
    await call(c, "/v1/auth/challenge", "POST", {
      address: account.address.base58,
      network: "nile",
      domain: "127.0.0.1:5173",
    })
  ).body;
  c.csrf = (
    await call(c, "/v1/auth/verify", "POST", {
      challenge_id: challenge.id,
      signature: await tron.trx.signMessageV2(challenge.message),
    })
  ).body.csrf_token;
  const a = (
    await call(c, "/v1/agents", "POST", {
      name: "Test agent",
      role: "alpha",
      instructions: "",
    })
  ).body;
  const b = (
    await call(c, "/v1/agents", "POST", {
      name: "Other conversation",
      role: "vault",
      instructions: "",
    })
  ).body;
  const payload = { agent_id: a.id, network: "nile" };
  expect(
    (await call(c, "/v1/mandates", "POST", payload, "same-key")).status,
  ).toBe(200);
  expect(
    (await call(c, "/v1/mandates", "POST", payload, "same-key")).status,
  ).toBe(200);
  let w = (await call(c, `/v1/workspace?network=nile&agent_id=${a.id}`)).body;
  expect(w.messages).toHaveLength(1);
  expect(w.messages[0].cards).toEqual([
    { kind: "mandate", target_id: "draft-hash" },
  ]);
  expect(
    (await call(c, `/v1/workspace?network=nile&agent_id=${b.id}`)).body
      .messages,
  ).toEqual([]);
  const other = await session();
  expect(
    (await call(other, `/v1/workspace?network=nile&agent_id=${a.id}`)).status,
  ).toBe(404);
  await stop();
  await start();
  expect(
    (await call(c, `/v1/workspace?network=nile&agent_id=${a.id}`)).body
      .messages[0].cards,
  ).toEqual(w.messages[0].cards);
  expect(
    (
      await call(c, "/v1/messages", "POST", {
        agent_id: a.id,
        role: "alpha",
        network: "nile",
        text: "Show my portfolio",
      })
    ).status,
  ).toBe(200);
  for (let i = 0; i < 30; i++) {
    w = (await call(c, `/v1/workspace?network=nile&agent_id=${a.id}`)).body;
    if (w.jobs[0]?.status === "SUCCEEDED") break;
    await new Promise((r) => setTimeout(r, 100));
  }
  expect(w.jobs[0].status).toBe("SUCCEEDED");
  expect(reviewReads).toBe(1);
  expect(w.messages.at(-1).cards[0]).toEqual({
    kind: "review",
    target_id: "fresh-review-1",
  });
  expect(w.messages.at(-1).text).toContain("Fresh RPC inputs");
  expect((await call(c, "/v1/portfolio-reviews", "POST", payload)).status).toBe(
    200,
  );
  w = (await call(c, `/v1/workspace?network=nile&agent_id=${a.id}`)).body;
  expect(reviewReads).toBe(2);
  expect(w.messages.at(-1).cards[0]).toEqual({
    kind: "review",
    target_id: "fresh-review-2",
  });
  expect(w.mandate.status).toBe("DRAFT");
  expect(w.approval).toBeNull();
  const stored = [...states.values()].find(
    (x) => x.mandate?.id === "test-draft",
  )!;
  stored.mandate!.status = "CONFIRMED";
  await call(c, "/v1/messages", "POST", {
    agent_id: a.id,
    role: "alpha",
    network: "nile",
    text: "Compare my current portfolio",
  });
  for (let i = 0; i < 30; i++) {
    w = (await call(c, `/v1/workspace?network=nile&agent_id=${a.id}`)).body;
    if (w.jobs[0]?.status === "SUCCEEDED") break;
    await new Promise((r) => setTimeout(r, 100));
  }
  expect(w.jobs[0].status).toBe("SUCCEEDED");
  expect(reviewReads).toBe(3);
  expect(allocationComparisons).toBe(0);
  expect(w.messages.at(-1).cards[0].kind).toBe("review");
  for (let n = 1; n <= 2; n++) {
    await call(c, "/v1/messages", "POST", {
      agent_id: a.id,
      role: "alpha",
      network: "nile",
      text: "USDD leverage rates?",
    });
    for (let i = 0; i < 30; i++) {
      w = (await call(c, `/v1/workspace?network=nile&agent_id=${a.id}`)).body;
      if (w.jobs[0]?.status === "SUCCEEDED") break;
      await new Promise((r) => setTimeout(r, 100));
    }
    expect(w.jobs[0].status).toBe("SUCCEEDED");
    expect(usddReads).toBe(n);
    expect(w.usdd_review.id).toBe(`usdd-${n}`);
    expect(w.messages.at(-1).text).toContain("1% supply / 2% borrow");
    expect(w.messages.at(-1).text).toContain("No transaction was sent");
    expect(w.approval).toBeNull();
    expect(w.mandate.status).toBe("CONFIRMED");
  }
}, 15000);

async function authenticatedAgent() {
  const c = await session();
  const account = await TronWeb.createAccount();
  const tron = new TronWeb({
    fullHost: origin,
    privateKey: account.privateKey,
  });
  const challenge = (
    await call(c, "/v1/auth/challenge", "POST", {
      address: account.address.base58,
      network: "nile",
      domain: "127.0.0.1:5173",
    })
  ).body;
  c.csrf = (
    await call(c, "/v1/auth/verify", "POST", {
      challenge_id: challenge.id,
      signature: await tron.trx.signMessageV2(challenge.message),
    })
  ).body.csrf_token;
  const a = (
    await call(c, "/v1/agents", "POST", {
      name: "Auto planner",
      role: "alpha",
      instructions: "",
    })
  ).body;
  await call(c, "/v1/mandates", "POST", { agent_id: a.id, network: "nile" });
  return { c, a };
}
async function waitJob(c: Client, agent: string, status = "SUCCEEDED") {
  let w: any;
  for (let i = 0; i < 60; i++) {
    w = (await call(c, `/v1/workspace?network=nile&agent_id=${agent}`)).body;
    if (w.jobs[0]?.status === status) return w;
    if (status !== "FAILED" && w.jobs[0]?.status === "FAILED")
      throw new Error(w.jobs[0].error);
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(
    "Job did not reach " + status + ": " + JSON.stringify(w?.jobs),
  );
}
it("nullable or failed condition extraction keeps the financial workspace and returns an editable card without USDD diversion", async () => {
  const { c, a } = await authenticatedAgent();
  for (const mode of ["rejected", "transport"]) {
    intentMode = mode;
    await call(c, "/v1/messages", "POST", {
      agent_id: a.id,
      role: "alpha",
      network: "nile",
      text: "300 TRX·365일·현금 20%·차입 없음",
    });
    const w = await waitJob(c, a.id);
    expect(w.messages.at(-1).text).toContain("could not reliably extract");
    expect(w.messages.at(-1).text).not.toMatch(
      /financial service.*unavailable|USDD|Mainnet|Vault/,
    );
    expect(w.messages.at(-1).cards[0].kind).toBe("conditions");
    expect(w.mandate.status).toBe("DRAFT");
    expect(w.approval).toBeNull();
  }
}, 15000);
it("one confirmation automatically publishes two plans, deduplicates clicks and requires no chat prompt or spending approval", async () => {
  const { c, a } = await authenticatedAgent();
  const input = {
    hash: "draft-hash",
    version: 1,
    network: "nile",
    agent_id: a.id,
  };
  const first = await call(
    c,
    "/v1/mandates/test-draft/confirm",
    "POST",
    input,
    "confirm-once",
  );
  expect(first.status).toBe(200);
  expect(first.body.job_id).toMatch(/^plan-/);
  const duplicate = await call(
    c,
    "/v1/mandates/test-draft/confirm",
    "POST",
    input,
  );
  expect(duplicate.body.job_id).toBe(first.body.job_id);
  const w = await waitJob(c, a.id);
  expect(w.comparison.plans).toHaveLength(2);
  expect(w.messages.filter((m: any) => m.author === "user")).toHaveLength(0);
  expect(
    w.messages.filter((m: any) => m.cards?.[0]?.kind === "plans"),
  ).toHaveLength(1);
  expect(w.messages.at(-1).cards[0].target_id).toBe(w.comparison.id);
  expect(w.approval).toBeNull();
  expect(w.graph).toBeNull();
  expect(w.execution).toBeNull();
  expect(w.jobs[0].label).toContain("calculating two plans");
  const outside = (await call(c, "/v1/workspace?network=nile")).body;
  expect(outside.jobs.some((j: any) => j.id === first.body.job_id)).toBe(true);
  const unauth = await session();
  expect(
    (await call(unauth, "/v1/mandates/test-draft/confirm", "POST", input))
      .status,
  ).toBe(401);
}, 15000);
it("confirmed plan generation resumes after restart and does not confirm twice", async () => {
  const { c, a } = await authenticatedAgent();
  comparisonMode = "pause";
  const before = allocationComparisons;
  await call(c, "/v1/mandates/test-draft/confirm", "POST", {
    hash: "draft-hash",
    version: 1,
    network: "nile",
    agent_id: a.id,
  });
  for (let i = 0; i < 60 && !releaseComparison; i++)
    await new Promise((r) => setTimeout(r, 50));
  expect(releaseComparison).not.toBeNull();
  await stop();
  releaseComparison!();
  releaseComparison = null;
  comparisonMode = "ready";
  await start();
  const w = await waitJob(c, a.id);
  expect(w.mandate.version).toBe(2);
  expect(allocationComparisons - before).toBe(1);
  expect(w.comparison.plans).toHaveLength(2);
  expect(
    w.messages.filter((m: any) => m.cards?.[0]?.kind === "plans"),
  ).toHaveLength(1);
}, 15000);
it("infeasible conditions explain blockers, calculation errors stay retryable and stale drafts cannot be confirmed", async () => {
  for (const mode of ["infeasible", "failure", "stale"]) {
    const { c, a } = await authenticatedAgent();
    comparisonMode = mode;
    const input = {
      hash: mode === "stale" ? "changed" : "draft-hash",
      version: 1,
      network: "nile",
      agent_id: a.id,
    };
    await call(c, "/v1/mandates/test-draft/confirm", "POST", input);
    let w = await waitJob(
      c,
      a.id,
      mode === "infeasible" ? "SUCCEEDED" : "FAILED",
    );
    if (mode === "infeasible") {
      expect(w.comparison.plans).toEqual([]);
      expect(w.messages.at(-1).text).toContain("2 TRX cap");
    }
    if (mode === "stale") {
      expect(w.mandate.status).toBe("DRAFT");
      expect(w.comparison).toBeNull();
    }
    if (mode === "failure") {
      expect(w.mandate.status).toBe("CONFIRMED");
      expect(w.messages.at(-1).text).toContain("Current market quotes");
      comparisonMode = "ready";
      expect(w.messages.at(-1).cards[0]).toEqual({
        kind: "mandate",
        target_id: w.mandate.hash,
      });
      const jobId = w.jobs[0].id;
      const foreign = await session();
      expect(
        (
          await call(foreign, `/v1/jobs/${jobId}/retry`, "POST", {
            network: "nile",
          })
        ).status,
      ).toBe(404);
      expect(
        (
          await call(c, `/v1/jobs/${jobId}/retry`, "POST", {
            network: "mainnet",
          })
        ).status,
      ).toBe(404);
      await call(c, `/v1/jobs/${jobId}/retry`, "POST", { network: "nile" });
      w = await waitJob(c, a.id);
      expect(w.comparison.plans).toHaveLength(2);
      expect(w.mandate.version).toBe(2);
    }
    expect(w.approval).toBeNull();
    expect(w.execution).toBeNull();
  }
  comparisonMode = "ready";
}, 15000);
