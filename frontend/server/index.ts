import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import {
  randomBytes,
  randomUUID,
  createHash,
  timingSafeEqual,
} from "node:crypto";
import { resolve, extname, sep } from "node:path";
import { existsSync, readFileSync, statSync } from "node:fs";
import { z } from "zod";
import { TronWeb } from "tronweb";
import {
  AgentInput,
  MessageInput,
  Network,
  Workspace,
  type Role,
} from "../src/api/contracts";
import { openStore } from "./store";
import { kilnStream, type ChatMessage } from "./kiln";
import {
  financialReply,
  leverageRequest,
  comparisonRequest,
  portfolioOnlyRequest,
  portfolioRequest,
} from "./conversation";
const port = Number(process.env.PORT || 8000);
const production = process.env.NODE_ENV === "production";
const origin = process.env.APP_ORIGIN || "http://127.0.0.1:5173";
const db = openStore(
  resolve(process.env.DATABASE_PATH || ".data/machine.sqlite"),
);
const financial = process.env.FINANCE_API_URL?.replace(/\/$/, "");
const financialToken = process.env.FINANCE_SERVICE_TOKEN;
if (production && (!origin.startsWith("https://") || !process.env.APP_ORIGIN))
  throw new Error("Production requires APP_ORIGIN with HTTPS.");
if (
  financial &&
  (!financialToken ||
    (!financial.startsWith("https://") &&
      !financial.startsWith("http://127.0.0.1:")))
)
  throw new Error(
    "Finance integration requires HTTPS (or loopback) and FINANCE_SERVICE_TOKEN.",
  );
type SessionRow = {
  id: string;
  workspace: string;
  csrf: string;
  wallet: string | null;
  expires: number;
};
type AgentRow = {
  id: string;
  workspace: string;
  name: string;
  role: Role;
  instructions: string;
  created: string;
  updated: string;
  archived: number;
};
type JobRow = {
  id: string;
  workspace: string;
  agent: string;
  network: string;
  message_id: string;
  status: string;
  error: string | null;
  created: string;
};
const running = new Map<string, AbortController>();
const rate = new Map<string, { at: number; count: number }>();
const stamp = () => new Date().toISOString();
const token = () => randomBytes(32).toString("hex");
const hash = (s: string) => createHash("sha256").update(s).digest("hex");
class HttpError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}
function json(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
  });
  res.end(JSON.stringify(body));
}
function sessionFor(req: IncomingMessage, res: ServerResponse) {
  const raw = (req.headers.cookie || "")
    .split(";")
    .map((x) => x.trim())
    .find((x) => x.startsWith("machine_session="))
    ?.slice(16);
  let row = raw
    ? (db
        .prepare("SELECT * FROM sessions WHERE id=? AND expires>?")
        .get(hash(raw), Date.now()) as SessionRow | undefined)
    : undefined;
  if (!row) {
    const value = token();
    row = {
      id: hash(value),
      workspace: randomUUID(),
      csrf: token(),
      wallet: null,
      expires: Date.now() + 7 * 86400000,
    };
    db.prepare(
      "INSERT INTO sessions(id,workspace,csrf,wallet,expires) VALUES(?,?,?,?,?)",
    ).run(row.id, row.workspace, row.csrf, row.wallet, row.expires);
    res.setHeader(
      "Set-Cookie",
      `machine_session=${value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800${production ? "; Secure" : ""}`,
    );
  }
  return row;
}
async function body(req: IncomingMessage) {
  let input = "";
  for await (const chunk of req) {
    input += chunk.toString();
    if (Buffer.byteLength(input) > 131072)
      throw new HttpError(413, "TOO_LARGE", "Request body is too large.");
  }
  try {
    return input ? JSON.parse(input) : {};
  } catch {
    throw new HttpError(400, "INVALID_JSON", "Invalid JSON.");
  }
}
function ownedAgent(id: string, s: SessionRow) {
  const agent = db
    .prepare("SELECT * FROM agents WHERE id=? AND workspace=? AND archived=0")
    .get(id, s.workspace) as AgentRow | undefined;
  if (!agent)
    throw new HttpError(
      404,
      "AGENT_NOT_FOUND",
      "This agent does not exist in your workspace.",
    );
  return agent;
}
function agentView(row: AgentRow) {
  const job = db
    .prepare(
      "SELECT status FROM jobs WHERE agent=? AND status IN ('QUEUED','RUNNING') LIMIT 1",
    )
    .get(row.id);
  const last = db
    .prepare(
      "SELECT text FROM messages WHERE agent=? AND text<>'' ORDER BY rowid DESC LIMIT 1",
    )
    .get(row.id) as { text: string } | undefined;
  return {
    id: row.id,
    name: row.name,
    role: row.role,
    instructions: row.instructions,
    created_at: row.created,
    updated_at: row.updated,
    status: job ? "WORKING" : "IDLE",
    last_message: last?.text || null,
  };
}
const ack = (job_id: string | null = null) => ({
  accepted: true as const,
  job_id,
});
async function finance(
  path: string,
  s: SessionRow,
  method = "GET",
  payload?: unknown,
  key?: string,
) {
  if (!financial)
    throw new HttpError(
      503,
      "FINANCE_NOT_CONNECTED",
      "The financial service is not connected. No plan or transaction was created.",
    );
  const r = await fetch(financial + path.replace(/^\/v1\//, "/v1/machine/"), {
    method,
    signal: AbortSignal.timeout(
      path.includes("agent-intent")
        ? 65000
        : /portfolio-reviews|usdd-reviews|usdd-workflows|stake-workflows|approvals|executions/.test(
              path,
            )
          ? 90000
          : /observations|plan-comparisons|execution-graphs/.test(path)
            ? 50000
            : 23000,
    ),
    headers: {
      Authorization: `Bearer ${financialToken}`,
      "Content-Type": "application/json",
      "X-Machine-Workspace": s.workspace,
      "X-Verified-Wallet": s.wallet || "",
      ...(key ? { "Idempotency-Key": key } : {}),
    },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const result = await r.json();
  if (!r.ok)
    throw new HttpError(
      r.status,
      result.error?.code || "FINANCE_ERROR",
      result.error?.message ||
        "The financial service could not complete the request.",
    );
  return result;
}
type PlanningRow = {
  job_id: string;
  wallet: string;
  confirmation_path: string;
  payload: string;
  confirmed_hash: string | null;
};
function recordCard(
  job: JobRow,
  text: string,
  cards: Workspace["messages"][number]["cards"],
) {
  if (!job.agent) return;
  const id = "planning-result-" + job.id;
  db.exec("BEGIN IMMEDIATE");
  try {
    db.prepare("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?)").run(
      id,
      job.workspace,
      job.agent,
      job.network,
      "agent",
      text,
      stamp(),
    );
    db.prepare("INSERT OR REPLACE INTO message_cards VALUES(?,?)").run(
      id,
      JSON.stringify(cards),
    );
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
}
async function processPlanning(
  job: JobRow,
  task: PlanningRow,
  signal: AbortSignal,
) {
  const payload = JSON.parse(task.payload);
  const s: SessionRow = {
    id: "planning-worker",
    workspace: job.workspace,
    csrf: "",
    wallet: task.wallet,
    expires: 0,
  };
  if (
    !db
      .prepare("SELECT address FROM wallets WHERE workspace=? AND address=?")
      .get(job.workspace, task.wallet)
  )
    throw new Error(
      "The verified wallet changed. Review your conditions again.",
    );
  if (job.agent) ownedAgent(job.agent, s);
  const checkActive = () => {
    if (
      signal.aborted ||
      (
        db.prepare("SELECT status FROM jobs WHERE id=?").get(job.id) as {
          status: string;
        }
      )?.status !== "RUNNING"
    )
      throw new Error(
        "Plan calculation was stopped. Your wallet has not signed a transaction.",
      );
  };
  checkActive();
  // This is the user's explicit Confirm & compare request, persisted before execution.
  // A restarted worker reuses the same confirmation key; it never confirms new terms.
  if (!task.confirmed_hash) {
    await finance(
      task.confirmation_path,
      s,
      "POST",
      payload,
      job.id + "-confirm",
    );
  }
  let w = Workspace.parse(
    await finance(`/v1/workspace?network=${job.network}`, s),
  );
  const mandateId = decodeURIComponent(task.confirmation_path.split("/")[3]);
  if (
    w.network !== job.network ||
    w.mandate?.id !== mandateId ||
    w.mandate.status !== "CONFIRMED" ||
    (task.confirmed_hash && w.mandate.hash !== task.confirmed_hash) ||
    (!task.confirmed_hash && w.mandate.version !== payload.version + 1)
  )
    throw new Error(
      "The conditions changed while calculating. Review the latest conditions again.",
    );
  const policyHash = w.mandate.hash;
  db.prepare("UPDATE plan_jobs SET confirmed_hash=? WHERE job_id=?").run(
    policyHash,
    job.id,
  );
  checkActive();
  await finance(
    "/v1/plan-comparisons",
    s,
    "POST",
    {
      network: job.network,
      mandate_id: mandateId,
      mandate_hash: policyHash,
    },
    job.id + "-compare",
  );
  checkActive();
  w = Workspace.parse(await finance(`/v1/workspace?network=${job.network}`, s));
  if (
    w.network !== job.network ||
    w.mandate?.hash !== policyHash ||
    w.comparison?.mandate_hash !== policyHash
  )
    throw new Error(
      "The comparison no longer matches your conditions. Review the latest conditions again.",
    );
  const comparison = w.comparison;
  const ready = comparison.status === "READY" && comparison.plans.length >= 2;
  recordCard(
    job,
    ready
      ? "Your conditions are confirmed. Here are two calculated options using current network data, your limits and projected costs. Choose a plan to review; wallet approval comes later."
      : `${comparison.reason || "Fewer than two investments meet your conditions after costs."} Review the blockers below and edit your conditions if you want to try again. No transaction has been approved.`,
    [{ kind: "plans", target_id: comparison.id }],
  );
  db.prepare(
    "UPDATE jobs SET status='SUCCEEDED',model='verified allocation engine',updated=? WHERE id=? AND status='RUNNING'",
  ).run(stamp(), job.id);
}
const server = createServer(async (req, res) => {
  const requestId = randomUUID();
  try {
    if (
      req.headers.host &&
      !["127.0.0.1", "localhost", new URL(origin).hostname].includes(
        req.headers.host.split(":")[0],
      )
    )
      throw new HttpError(403, "HOST_DENIED", "Unrecognized host.");
    const url = new URL(req.url || "/", origin);
    const path = url.pathname;
    const method = req.method || "GET";
    if (path === "/health" && ["GET", "HEAD"].includes(method)) {
      json(res, 200, { status: "ok" });
      return;
    }
    if (
      ["GET", "HEAD"].includes(method) &&
      !path.startsWith("/v1/") &&
      existsSync("dist/index.html")
    ) {
      const root = resolve("dist");
      const asset = resolve(root, "." + decodeURIComponent(path));
      const file =
        asset.startsWith(root + sep) &&
        existsSync(asset) &&
        statSync(asset).isFile()
          ? asset
          : resolve(root, "index.html");
      const mime: Record<string, string> = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
      };
      res.writeHead(200, {
        "Content-Type": mime[extname(file)] || "application/octet-stream",
        "Cache-Control": file.includes(`${sep}assets${sep}`)
          ? "public,max-age=31536000,immutable"
          : "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy":
          "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
        "Referrer-Policy": "same-origin",
      });
      res.end(readFileSync(file));
      return;
    }
    const s = sessionFor(req, res);
    const mutating = method !== "GET";
    const payload = mutating ? await body(req) : undefined;
    if (mutating) {
      if (req.headers.origin !== origin)
        throw new HttpError(
          403,
          "ORIGIN_DENIED",
          "Request origin is not allowed.",
        );
      const csrf = String(req.headers["x-csrf-token"] || "");
      if (
        !/^\w{64}$/.test(csrf) ||
        !timingSafeEqual(Buffer.from(csrf), Buffer.from(s.csrf))
      )
        throw new HttpError(
          403,
          "CSRF_INVALID",
          "Reload this page before continuing.",
        );
      const window = rate.get(s.id);
      if (window && window.at > Date.now() - 60000) {
        if (++window.count > 60)
          throw new HttpError(
            429,
            "RATE_LIMIT",
            "Too many requests. Please wait a minute.",
          );
      } else rate.set(s.id, { at: Date.now(), count: 1 });
    }
    const key = String(req.headers["idempotency-key"] || "");
    if (mutating && (!key || key.length > 200))
      throw new HttpError(
        400,
        "IDEMPOTENCY_REQUIRED",
        "An idempotency key is required.",
      );
    const scope = s.id + s.workspace + method + path + key;
    const fingerprint = hash(JSON.stringify(payload ?? null));
    const previous = mutating
      ? (db
          .prepare("SELECT fingerprint,response FROM idempotency WHERE scope=?")
          .get(scope) as { fingerprint: string; response: string } | undefined)
      : undefined;
    if (previous) {
      if (previous.fingerprint !== fingerprint)
        throw new HttpError(
          409,
          "IDEMPOTENCY_CONFLICT",
          "The request key was reused with different content.",
        );
      json(res, 200, JSON.parse(previous.response));
      return;
    }
    let result: unknown;
    if (path === "/v1/session" && method === "GET")
      result = {
        authenticated: !!s.wallet,
        user_name: null,
        wallet_address: s.wallet,
        csrf_token: s.csrf,
      };
    else if (path === "/v1/status" && method === "GET")
      result = {
        kiln_configured: !!process.env.KILN_API_KEY,
        finance_connected: !!financial,
        model: "qwen3-32b",
      };
    else if (path === "/v1/agents" && method === "GET")
      result = (
        db
          .prepare(
            "SELECT * FROM agents WHERE workspace=? AND archived=? ORDER BY created",
          )
          .all(
            s.workspace,
            url.searchParams.get("archived") === "true" ? 1 : 0,
          ) as AgentRow[]
      ).map(agentView);
    else if (/^\/v1\/agents\/[^/]+\/restore$/.test(path) && method === "POST") {
      const id = path.split("/")[3];
      const a = db
        .prepare(
          "SELECT * FROM agents WHERE id=? AND workspace=? AND archived=1",
        )
        .get(id, s.workspace) as AgentRow | undefined;
      if (!a)
        throw new HttpError(
          404,
          "AGENT_NOT_FOUND",
          "Archived agent not found.",
        );
      if (
        Number(
          (
            db
              .prepare(
                "SELECT count(*) AS n FROM agents WHERE workspace=? AND archived=0",
              )
              .get(s.workspace) as { n: number }
          ).n,
        ) >= 30
      )
        throw new HttpError(422, "AGENT_LIMIT", "Archive another agent first.");
      db.prepare("UPDATE agents SET archived=0,updated=? WHERE id=?").run(
        stamp(),
        id,
      );
      result = ack();
    } else if (path === "/v1/agents" && method === "POST") {
      const input = AgentInput.parse(payload);
      const id = randomUUID(),
        created = stamp();
      if (
        Number(
          (
            db
              .prepare(
                "SELECT count(*) AS n FROM agents WHERE workspace=? AND archived=0",
              )
              .get(s.workspace) as { n: number }
          ).n,
        ) >= 30
      )
        throw new HttpError(
          422,
          "AGENT_LIMIT",
          "This workspace supports up to 30 agents.",
        );
      db.prepare(
        "INSERT INTO agents(id,workspace,name,role,instructions,created,updated) VALUES(?,?,?,?,?,?,?)",
      ).run(
        id,
        s.workspace,
        input.name,
        input.role,
        input.instructions,
        created,
        created,
      );
      result = agentView(ownedAgent(id, s));
    } else if (/^\/v1\/agents\/[^/]+$/.test(path)) {
      const a = ownedAgent(path.split("/").pop()!, s);
      if (method === "PATCH") {
        if (
          db
            .prepare(
              "SELECT id FROM jobs WHERE agent=? AND status IN ('QUEUED','RUNNING')",
            )
            .get(a.id)
        )
          throw new HttpError(
            409,
            "AGENT_BUSY",
            "Stop the current response before editing this agent.",
          );
        const input = AgentInput.parse(payload);
        db.prepare(
          "UPDATE agents SET name=?,role=?,instructions=?,updated=? WHERE id=?",
        ).run(input.name, input.role, input.instructions, stamp(), a.id);
        result = agentView(ownedAgent(a.id, s));
      } else if (method === "DELETE") {
        db.prepare("UPDATE agents SET archived=1,updated=? WHERE id=?").run(
          stamp(),
          a.id,
        );
        for (const j of db
          .prepare(
            "SELECT id FROM jobs WHERE agent=? AND status IN ('QUEUED','RUNNING')",
          )
          .all(a.id) as { id: string }[]) {
          running.get(j.id)?.abort();
          db.prepare(
            "UPDATE jobs SET status='FAILED',error='Agent archived',updated=? WHERE id=?",
          ).run(stamp(), j.id);
        }
        result = ack();
      } else
        throw new HttpError(405, "METHOD_NOT_ALLOWED", "Method not allowed.");
    } else if (path === "/v1/messages" && method === "POST") {
      if (production && !s.wallet)
        throw new HttpError(
          401,
          "WALLET_REQUIRED",
          "Verify your wallet before sending messages.",
        );
      if (
        Number(
          (
            db
              .prepare(
                "SELECT count(*) AS n FROM jobs WHERE workspace=? AND created>?",
              )
              .get(
                s.workspace,
                new Date(Date.now() - 86400000).toISOString(),
              ) as { n: number }
          ).n,
        ) >= Number(process.env.DAILY_MESSAGE_LIMIT || 100)
      )
        throw new HttpError(
          429,
          "DAILY_LIMIT",
          "This workspace reached its daily message limit.",
        );
      const input = MessageInput.parse(payload);
      const agent = ownedAgent(input.agent_id, s);
      if (agent.role !== input.role)
        throw new HttpError(
          409,
          "ROLE_CHANGED",
          "This agent role changed. Reload the conversation.",
        );
      if (!process.env.KILN_API_KEY)
        throw new HttpError(
          503,
          "KILN_NOT_CONFIGURED",
          "Kiln is not configured on the server.",
        );
      if (
        db
          .prepare(
            "SELECT id FROM jobs WHERE agent=? AND status IN ('QUEUED','RUNNING')",
          )
          .get(agent.id)
      )
        throw new HttpError(
          409,
          "AGENT_BUSY",
          "Wait for this agent to finish or stop its current response.",
        );
      const message_id = randomUUID(),
        job_id = randomUUID(),
        created = stamp();
      db.exec("BEGIN IMMEDIATE");
      try {
        db.prepare("INSERT INTO messages VALUES(?,?,?,?,?,?,?)").run(
          message_id,
          s.workspace,
          agent.id,
          input.network,
          "user",
          input.text,
          created,
        );
        db.prepare(
          "INSERT INTO jobs(id,workspace,agent,network,message_id,status,created,updated) VALUES(?,?,?,?,?,?,?,?)",
        ).run(
          job_id,
          s.workspace,
          agent.id,
          input.network,
          message_id,
          "QUEUED",
          created,
          created,
        );
        db.exec("COMMIT");
      } catch (e) {
        db.exec("ROLLBACK");
        throw e;
      }
      result = ack(job_id);
    } else if (/^\/v1\/jobs\/[^/]+\/cancel$/.test(path) && method === "POST") {
      const id = path.split("/")[3];
      const j = db
        .prepare("SELECT * FROM jobs WHERE id=? AND workspace=?")
        .get(id, s.workspace) as JobRow | undefined;
      if (!j) throw new HttpError(404, "JOB_NOT_FOUND", "Task not found.");
      running.get(id)?.abort();
      db.prepare(
        "UPDATE jobs SET status='FAILED',error='Response stopped by you.',updated=? WHERE id=? AND status IN ('RUNNING','QUEUED')",
      ).run(stamp(), id);
      result = ack();
    } else if (/^\/v1\/jobs\/[^/]+\/retry$/.test(path) && method === "POST") {
      const id = path.split("/")[3];
      const task = db
        .prepare(
          "SELECT j.*,p.wallet FROM jobs j JOIN plan_jobs p ON p.job_id=j.id WHERE j.id=? AND j.workspace=?",
        )
        .get(id, s.workspace) as (JobRow & { wallet: string }) | undefined;
      if (
        !task ||
        !s.wallet ||
        task.wallet !== s.wallet ||
        task.network !== payload.network
      )
        throw new HttpError(
          404,
          "JOB_NOT_FOUND",
          "This calculation was not found for your wallet and network.",
        );
      if (task.agent) ownedAgent(task.agent, s);
      db.prepare(
        "UPDATE jobs SET status='QUEUED',error=NULL,updated=? WHERE id=? AND status='FAILED'",
      ).run(stamp(), id);
      result = ack(id);
    } else if (path === "/v1/workspace" && method === "GET") {
      const network = Network.parse(url.searchParams.get("network"));
      const agentId = url.searchParams.get("agent_id");
      const agent = agentId ? ownedAgent(agentId, s) : null;
      const base: Workspace = financial
        ? Workspace.parse(await finance(`/v1/workspace?network=${network}`, s))
        : {
            revision: 0,
            network,
            mode: "LIVE",
            messages: [],
            mandate: null,
            comparison: null,
            graph: null,
            approval: null,
            execution: null,
            positions: [],
            performance: null,
            balances: [],
            snapshots: [],
            routines: [],
            evidence: [],
            activity: [],
            jobs: [],
          };
      if (base.network !== network)
        throw new HttpError(
          502,
          "NETWORK_MISMATCH",
          "Financial service returned a different network.",
        );
      if (agent) {
        base.messages = (
          db
            .prepare(
              "SELECT * FROM (SELECT rowid,* FROM messages WHERE workspace=? AND agent=? AND network=? ORDER BY rowid DESC LIMIT 200) ORDER BY rowid",
            )
            .all(s.workspace, agent.id, network) as {
            id: string;
            author: "user" | "agent";
            text: string;
            created: string;
          }[]
        )
          .filter((m) => m.text)
          .map((m) => ({
            id: m.id,
            role: agent.role,
            author: m.author,
            text: m.text,
            created_at: m.created,
            cards: JSON.parse(
              (
                db
                  .prepare("SELECT body FROM message_cards WHERE message_id=?")
                  .get(m.id) as { body: string } | undefined
              )?.body || "[]",
            ),
          }));
        base.jobs = (
          db
            .prepare(
              "SELECT * FROM jobs WHERE workspace=? AND agent=? AND network=? ORDER BY rowid DESC LIMIT 5",
            )
            .all(s.workspace, agent.id, network) as JobRow[]
        ).map((j) => ({
          id: j.id,
          status: j.status,
          label: db
            .prepare("SELECT job_id FROM plan_jobs WHERE job_id=?")
            .get(j.id)
            ? "Checking conditions and calculating two plans"
            : j.status === "RUNNING"
              ? "Writing a response"
              : "Message processing",
          error: j.error,
        })) as Workspace["jobs"];
      }
      if (!agent) {
        const planning = db
          .prepare(
            "SELECT j.* FROM jobs j JOIN plan_jobs p ON p.job_id=j.id WHERE j.workspace=? AND j.network=? AND p.wallet=? ORDER BY j.rowid DESC LIMIT 5",
          )
          .all(s.workspace, network, s.wallet || "") as JobRow[];
        base.jobs.push(
          ...planning.map((j) => ({
            id: j.id,
            status: j.status as Workspace["jobs"][number]["status"],
            label: "Checking conditions and calculating two plans",
            error: j.error,
          })),
        );
      }
      base.revision = Date.now();
      result = Workspace.parse(base);
    } else if (path === "/v1/usage" && method === "GET") {
      result = db
        .prepare(
          "SELECT id,agent,network,status,model,input_tokens,output_tokens,latency_ms,created,error FROM jobs WHERE workspace=? ORDER BY rowid DESC LIMIT 100",
        )
        .all(s.workspace);
    } else if (path === "/v1/auth/challenge" && method === "POST") {
      const b = z
        .object({ address: z.string(), network: Network, domain: z.string() })
        .parse(payload);
      if (b.domain !== new URL(origin).host || !TronWeb.isAddress(b.address))
        throw new HttpError(
          400,
          "INVALID_CHALLENGE",
          "The address or domain is invalid.",
        );
      const id = randomUUID(),
        expires = Date.now() + 300000;
      const message = `${b.domain} requests proof of ownership.\nAddress: ${b.address}\nNetwork: ${b.network}\nNonce: ${token()}\nExpires: ${new Date(expires).toISOString()}\nThis signature does not approve any transaction.`;
      db.prepare("INSERT INTO challenges VALUES(?,?,?,?,?,?,0)").run(
        id,
        s.id,
        b.address,
        b.network,
        message,
        expires,
      );
      result = {
        id,
        message,
        domain: b.domain,
        address: b.address,
        network: b.network,
        expires_at: new Date(expires).toISOString(),
      };
    } else if (path === "/v1/auth/verify" && method === "POST") {
      const b = z
        .object({ challenge_id: z.string(), signature: z.string() })
        .parse(payload);
      const c = db
        .prepare(
          "SELECT * FROM challenges WHERE id=? AND session=? AND used=0 AND expires>?",
        )
        .get(b.challenge_id, s.id, Date.now()) as
        { id: string; message: string; address: string } | undefined;
      if (!c)
        throw new HttpError(
          401,
          "CHALLENGE_EXPIRED",
          "The ownership challenge expired.",
        );
      let recovered: string;
      try {
        recovered = await new TronWeb({
          fullHost: "https://api.trongrid.io",
        }).trx.verifyMessageV2(c.message, b.signature);
      } catch {
        throw new HttpError(
          401,
          "SIGNATURE_INVALID",
          "This ownership signature is invalid.",
        );
      }
      if (recovered !== c.address)
        throw new HttpError(
          401,
          "SIGNATURE_INVALID",
          "The signature does not match this wallet.",
        );
      if (
        !db
          .prepare("UPDATE challenges SET used=1 WHERE id=? AND used=0")
          .run(c.id).changes
      )
        throw new HttpError(
          401,
          "CHALLENGE_USED",
          "This challenge was already used.",
        );
      const existing = db
        .prepare("SELECT workspace FROM wallets WHERE address=?")
        .get(c.address) as { workspace: string } | undefined;
      const workspace =
        existing?.workspace ||
        (s.wallet && s.wallet !== c.address ? randomUUID() : s.workspace);
      if (!existing)
        db.prepare("INSERT INTO wallets VALUES(?,?)").run(c.address, workspace);
      const csrf = token();
      db.prepare(
        "UPDATE sessions SET wallet=?,workspace=?,csrf=? WHERE id=?",
      ).run(c.address, workspace, csrf, s.id);
      result = {
        authenticated: true,
        user_name: null,
        wallet_address: c.address,
        csrf_token: csrf,
      };
    } else if (path === "/v1/auth/logout" && method === "POST") {
      db.prepare("DELETE FROM sessions WHERE id=?").run(s.id);
      res.setHeader(
        "Set-Cookie",
        `machine_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0${production ? "; Secure" : ""}`,
      );
      result = ack();
    } else if (
      /^\/v1\/mandates\/[^/]+\/confirm$/.test(path) &&
      method === "POST"
    ) {
      if (!financial)
        throw new HttpError(
          503,
          "FINANCE_NOT_CONNECTED",
          "Connect the financial service before confirming conditions.",
        );
      if (!s.wallet)
        throw new HttpError(
          401,
          "WALLET_REQUIRED",
          "Connect and verify your wallet first.",
        );
      const input = z
        .object({
          hash: z.string().min(1),
          version: z.number().int().positive(),
          network: Network,
          agent_id: z.string().optional(),
        })
        .parse(payload);
      const agent = input.agent_id
        ? ownedAgent(input.agent_id, s)
        : (db
            .prepare(
              "SELECT * FROM agents WHERE workspace=? AND archived=0 ORDER BY created LIMIT 1",
            )
            .get(s.workspace) as AgentRow | undefined);
      const jobId =
        "plan-" +
        hash(
          JSON.stringify([
            s.workspace,
            s.wallet,
            input.network,
            path,
            input.hash,
            input.version,
            agent?.id || "",
          ]),
        );
      const existing = db
        .prepare("SELECT status FROM jobs WHERE id=?")
        .get(jobId) as { status: string } | undefined;
      db.exec("BEGIN IMMEDIATE");
      try {
        if (!existing) {
          const created = stamp();
          db.prepare(
            "INSERT INTO jobs(id,workspace,agent,network,message_id,status,created,updated,model) VALUES(?,?,?,?,?,?,?,?,?)",
          ).run(
            jobId,
            s.workspace,
            agent?.id || "",
            input.network,
            "",
            "QUEUED",
            created,
            created,
            "verified allocation engine",
          );
          db.prepare(
            "INSERT INTO plan_jobs(job_id,wallet,confirmation_path,payload) VALUES(?,?,?,?)",
          ).run(
            jobId,
            s.wallet,
            path,
            JSON.stringify({
              hash: input.hash,
              version: input.version,
              network: input.network,
            }),
          );
        } else if (existing.status === "FAILED") {
          db.prepare(
            "UPDATE jobs SET status='QUEUED',error=NULL,updated=? WHERE id=?",
          ).run(stamp(), jobId);
        }
        db.exec("COMMIT");
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
      result = ack(jobId);
    } else if (path.startsWith("/v1/")) {
      const allowed =
        /^\/v1\/(funding|observations|usdd-reviews|usdd-workflows|stake-workflows|portfolio-reviews|portfolio-adjustments|notifications|mandates|plan-comparisons|execution-graphs|approvals|executions|positions|performance|routines|evidence)(\/[^?]*)?$/.test(
          path,
        );
      if (!allowed)
        throw new HttpError(404, "NOT_FOUND", "Endpoint not found.");
      if (!financial)
        throw new HttpError(
          503,
          "FINANCE_NOT_CONNECTED",
          "Connect the financial backend to create mandates, plans and transactions.",
        );
      if (!s.wallet)
        throw new HttpError(
          401,
          "WALLET_REQUIRED",
          "Connect and verify your wallet first.",
        );
      const chatAgent =
        typeof payload?.agent_id === "string"
          ? ownedAgent(payload.agent_id, s)
          : null;
      result = await finance(path + url.search, s, method, payload, key);
      const kind =
        method === "POST"
          ? (
              {
                "/v1/mandates": "mandate",
                "/v1/plan-comparisons": "plans",
                "/v1/portfolio-reviews": "review",
                "/v1/portfolio-adjustments": "execution",
                "/v1/stake-workflows/next": "execution",
                "/v1/stake-workflows/lifecycle": "execution",
                "/v1/execution-graphs": "execution",
              } as const
            )[path as "/v1/mandates"]
          : undefined;
      if (chatAgent && kind) {
        const current = Workspace.parse(
          await finance(`/v1/workspace?network=${payload.network}`, s),
        );
        const target =
          kind === "mandate"
            ? current.mandate?.hash
            : kind === "plans"
              ? current.comparison?.id
              : kind === "review"
                ? current.portfolio_review?.id
                : current.graph?.id;
        if (target) {
          const id = "card-" + createHash("sha256").update(scope).digest("hex");
          const text =
            kind === "mandate"
              ? "Your updated conditions are ready to review. Confirm & compare to see the two calculated options here."
              : kind === "plans"
                ? "Compare the calculated options below and choose the plan you want to review."
                : kind === "review"
                  ? "Your holdings and market inputs have been refreshed. Here is the current hold-versus-adjust review."
                  : "Review the exact transaction below. Approval and your wallet signature are separate steps.";
          db.exec("BEGIN IMMEDIATE");
          try {
            db.prepare(
              "INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?)",
            ).run(
              id,
              s.workspace,
              chatAgent.id,
              payload.network,
              "agent",
              text,
              stamp(),
            );
            db.prepare("INSERT OR IGNORE INTO message_cards VALUES(?,?)").run(
              id,
              JSON.stringify([{ kind, target_id: target }]),
            );
            db.exec("COMMIT");
          } catch (e) {
            db.exec("ROLLBACK");
            throw e;
          }
        }
      }
    } else throw new HttpError(404, "NOT_FOUND", "Endpoint not found.");
    if (mutating)
      db.prepare("INSERT OR REPLACE INTO idempotency VALUES(?,?,?,?)").run(
        scope,
        fingerprint,
        JSON.stringify(result),
        Date.now(),
      );
    json(res, 200, result);
  } catch (e) {
    if (res.writableEnded) return;
    const error =
      e instanceof HttpError
        ? e
        : e instanceof z.ZodError
          ? new HttpError(
              422,
              "INVALID_INPUT",
              e.issues
                .map((x) => `${x.path.join(".")}: ${x.message}`)
                .join("; "),
            )
          : new HttpError(
              500,
              "INTERNAL_ERROR",
              "The service could not complete this request.",
            );
    json(res, error.status, {
      error: {
        code: error.code,
        message: error.message,
        request_id: requestId,
      },
    });
  }
});
let processing = false;
const timer = setInterval(async () => {
  if (processing) return;
  const job = db
    .prepare("SELECT * FROM jobs WHERE status='QUEUED' ORDER BY rowid LIMIT 1")
    .get() as JobRow | undefined;
  if (!job) return;
  processing = true;
  const controller = new AbortController();
  running.set(job.id, controller);
  const timeout = setTimeout(() => controller.abort(), 120000);
  const started = Date.now();
  let messageId = "";
  try {
    db.prepare(
      "UPDATE jobs SET status='RUNNING',updated=? WHERE id=? AND status='QUEUED'",
    ).run(stamp(), job.id);
    const planning = db
      .prepare("SELECT * FROM plan_jobs WHERE job_id=?")
      .get(job.id) as PlanningRow | undefined;
    if (planning) {
      try {
        await processPlanning(job, planning, controller.signal);
      } catch (error) {
        const saved = db
          .prepare("SELECT confirmed_hash FROM plan_jobs WHERE job_id=?")
          .get(job.id) as { confirmed_hash: string | null };
        recordCard(
          job,
          `I could not finish the plan comparison. ${error instanceof HttpError ? error.message : "Open your conditions and retry the calculation."} No wallet transaction was approved or sent.`,
          saved.confirmed_hash
            ? [{ kind: "mandate", target_id: saved.confirmed_hash }]
            : [{ kind: "conditions", target_id: "new" }],
        );
        throw error;
      }
      return;
    }
    const a = db
      .prepare("SELECT * FROM agents WHERE id=? AND archived=0")
      .get(job.agent) as AgentRow | undefined;
    if (!a) throw new Error("Agent no longer exists.");
    const rolePrompt: Record<Role, string> = {
      alpha:
        "Help clarify financial conditions, compare allocation plans only from verified tool data, and explain trade-offs.",
      vault:
        "Explain cash, debt, approval scope and transaction status. You do not sign or spend.",
      watch:
        "Explain market, liquidity, exit and transaction changes only when backed by supplied observations. Do not claim monitoring is active without worker evidence.",
    };
    const history = db
      .prepare(
        "SELECT author,text FROM (SELECT rowid,author,text FROM messages WHERE workspace=? AND agent=? AND network=? ORDER BY rowid DESC LIMIT 30) ORDER BY rowid",
      )
      .all(job.workspace, job.agent, job.network) as {
      author: string;
      text: string;
    }[];
    let intentContext = "";
    let intentResult: Parameters<typeof financialReply>[1];
    let financialWorkspace: Workspace | undefined;
    let financialContext =
      "Financial service is not connected. No balances, market observations, mandate or transactions are available.";
    let financialIssue: string | null = null;
    const latest =
      history.filter((m) => m.author === "user").at(-1)?.text || "";
    if (financial) {
      const wallet = db
        .prepare("SELECT address FROM wallets WHERE workspace=? LIMIT 1")
        .get(job.workspace) as { address: string } | undefined;
      const workerSession: SessionRow = {
        id: "worker",
        workspace: job.workspace,
        csrf: "",
        wallet: wallet?.address || null,
        expires: 0,
      };
      const report = (operation: string, error: unknown) => {
        console.error(
          JSON.stringify({
            event: "financial_operation_failed",
            operation,
            job_id: job.id,
            code:
              error instanceof HttpError
                ? error.code
                : error instanceof z.ZodError
                  ? "RESPONSE_SCHEMA_INVALID"
                  : "REQUEST_FAILED",
          }),
        );
      };
      if (workerSession.wallet && latest && !portfolioOnlyRequest(latest)) {
        try {
          const intent = await finance(
            "/v1/agent-intent",
            workerSession,
            "POST",
            {
              message: latest,
              network: job.network,
              agent_id: job.agent,
              message_id: job.message_id,
            },
            job.id,
          );
          intentResult = z
            .object({
              intent: z.string().nullable().optional(),
              status: z.string(),
              patch: z.record(z.string(), z.unknown()),
              pending_proposal: z.boolean().optional(),
            })
            .parse(intent);
          intentContext = JSON.stringify(intent);
        } catch (error) {
          report("extract_conditions", error);
          intentResult = {
            intent: null,
            status: "MODEL_UNAVAILABLE",
            patch: {},
          };
          intentContext =
            "Condition extraction was unavailable. Existing financial data may still be available.";
        }
      }
      if (workerSession.wallet && portfolioRequest(latest)) {
        try {
          await finance(
            "/v1/portfolio-reviews",
            workerSession,
            "POST",
            { network: job.network },
            job.id + "-portfolio-review",
          );
        } catch (error) {
          report("review_portfolio", error);
          financialIssue =
            "I could not refresh the portfolio review. The last recorded values may be out of date. Open Portfolio and retry Check now.";
        }
      }
      if (workerSession.wallet && leverageRequest(latest)) {
        try {
          await finance(
            "/v1/usdd-reviews",
            workerSession,
            "POST",
            { network: job.network },
            job.id + "-usdd-review",
          );
        } catch (error) {
          report("review_usdd", error);
          financialIssue =
            "I could not refresh the USDD route assessment. Retry the review in Portfolio.";
        }
      }
      try {
        let w = Workspace.parse(
          await finance(`/v1/workspace?network=${job.network}`, workerSession),
        );
        if (w.network !== job.network) throw new Error("Network mismatch");
        financialWorkspace = w;
        if (
          workerSession.wallet &&
          comparisonRequest(latest) &&
          !portfolioRequest(latest) &&
          !w.intent &&
          w.mandate?.status === "CONFIRMED"
        ) {
          try {
            await finance(
              "/v1/plan-comparisons",
              workerSession,
              "POST",
              {
                network: job.network,
                mandate_id: w.mandate.id,
                mandate_hash: w.mandate.hash,
              },
              job.id + "-compare",
            );
            w = Workspace.parse(
              await finance(
                `/v1/workspace?network=${job.network}`,
                workerSession,
              ),
            );
            if (w.network !== job.network) throw new Error("Network mismatch");
            financialWorkspace = w;
          } catch (error) {
            report("compare_plans", error);
            financialIssue =
              "I could not finish the plan comparison. Your conditions are saved. Open your conditions to retry the calculation.";
          }
        }
        financialContext = JSON.stringify({
          network: w.network,
          balances: w.balances,
          mandate: w.mandate,
          pending_draft: w.intent,
          comparison: w.comparison,
          graph: w.graph,
          execution: w.execution,
          positions: w.positions,
          performance: w.performance,
          snapshots: w.snapshots,
          routines: w.routines,
          portfolio_review: w.portfolio_review,
          stake_position: w.stake_position,
          stake_workflow: w.stake_workflow,
          product_catalog: w.product_catalog,
          ...(leverageRequest(latest)
            ? { usdd_review: w.usdd_review, usdd_workflow: w.usdd_workflow }
            : {}),
        }).slice(0, 40000);
      } catch (error) {
        report("read_workspace", error);
        financialIssue =
          "I could not read your financial workspace right now. Retry shortly. No new plan or transaction has been authorized.";
      }
    }
    const prompt = `You are ${a.name}, a ${a.role} role in faat (Finance AI Agent Tron), a TRON asset management service. ${rolePrompt[a.role]} Reply in English, concisely and conversationally. Answer the current question directly; do not repeat a mandate or a questionnaire on unrelated messages. Never invent balances, returns, allocations, fees, transaction hashes, completed actions or monitoring. Never call proposed edits confirmed. High risk or a generic yes never permits borrowing; only explicit borrowing consent with bounded debt can propose it. Do not suggest an 80/20 allocation unless it exists in the calculated comparison. Distinguish recorded actual fees from planning assumptions. Ask at most two missing questions and use already known fields. The validated extraction is ${intentContext || "not available"}. Interactive conditions, calculated Plan A/Plan B and transaction cards are shown in this conversation when relevant. Use only supplied service facts, checking network and timestamps. Native Stake 2.0 plus representative voting is available on Nile only with explicit STAKE/VOTE permission and a positive native protocol cap; separate stake and vote signatures are needed. Rewards use current chain reward parameters, representative commission, vote weights and network maintenance interval, not fixed APY. Rental income is excluded. Native stake rewards, fees and forecast are a separate measured ledger, never substitute old jTRX performance zeros for native positions. Product catalog describes capability, not current quote eligibility. Mixed native/lending automatic reallocation is not enabled. Live transaction support also includes Nile native TRX supply and exact-share redemption from a fresh Watch review, and a separate mainnet USDD workflow in Portfolio. A recorded original forecast, expected net to date, actual accrued and realized income, paid fees and variance are supplied by receipt-backed performance accounting; do not replace unavailable values with estimates. ${leverageRequest(latest) ? "For the requested USDD route, use only the supplied fresh assessment. Nile Vault USDD and configured JustLend USDD have different token identities; do not offer incompatible execution. Mainnet USDD execution is not proven with this wallet. Leverage requires positive incremental return after interest, fees and losses, and explicit debt limits." : "Do not introduce USDD, Vault or borrowing workflows when the user is setting unleveraged TRX conditions."} Do not imply these routes are available because policy allows borrowing. New investment plans must have positive projected net income after costs; keeping cash is a valid outcome. This conversation cannot grant trade authority. No automatic authorization or spending. No reasoning traces. Agent preferences: ${JSON.stringify(a.instructions)}. Financial context: ${financialContext}`;
    const messages: ChatMessage[] = [
      { role: "system", content: prompt },
      ...history
        .filter((m) => m.text.trim())
        .map((m) => ({
          role:
            m.author === "user" ? ("user" as const) : ("assistant" as const),
          content: m.text,
        })),
    ];
    messageId = randomUUID();
    db.prepare("INSERT INTO messages VALUES(?,?,?,?,?,?,?)").run(
      messageId,
      job.workspace,
      job.agent,
      job.network,
      "agent",
      "",
      stamp(),
    );
    let text = "",
      last = 0;
    const grounded = financialIssue
      ? { text: financialIssue, cards: [] }
      : financialWorkspace
        ? financialReply(
            financialWorkspace,
            intentResult,
            history.filter((x) => x.author === "user").at(-1)?.text || "",
          )
        : null;
    if (grounded) {
      text = grounded.text;
      db.prepare("INSERT INTO message_cards VALUES(?,?)").run(
        messageId,
        JSON.stringify(grounded.cards),
      );
      // The extraction still uses real Qwen. Do not fabricate usage for the
      // deterministic rendering of its validated result and server facts.
      db.prepare("UPDATE jobs SET model=? WHERE id=?").run(
        intentResult
          ? "qwen3-32b · validated conditions"
          : "verified service data",
        job.id,
      );
    } else
      for await (const part of kilnStream(messages, controller.signal)) {
        if (part.text) {
          text += part.text;
          if (Date.now() - last > 150) {
            db.prepare("UPDATE messages SET text=? WHERE id=?").run(
              text,
              messageId,
            );
            last = Date.now();
          }
        }
        if (part.usage)
          db.prepare(
            "UPDATE jobs SET model=?,input_tokens=?,output_tokens=? WHERE id=?",
          ).run(
            part.model || "qwen3-32b",
            part.usage.prompt_tokens ?? null,
            part.usage.completion_tokens ?? null,
            job.id,
          );
      }
    if (controller.signal.aborted) throw new Error("Response stopped.");
    if (!text.trim()) throw new Error("Kiln returned no message content.");
    db.prepare("UPDATE messages SET text=? WHERE id=?").run(text, messageId);
    db.prepare(
      "UPDATE jobs SET status='SUCCEEDED',latency_ms=?,updated=? WHERE id=? AND status='RUNNING'",
    ).run(Date.now() - started, stamp(), job.id);
    db.prepare("UPDATE agents SET updated=? WHERE id=?").run(
      stamp(),
      job.agent,
    );
  } catch (e) {
    const message = controller.signal.aborted
      ? "Response stopped or timed out."
      : e instanceof Error
        ? e.message
        : "Generation failed.";
    db.prepare(
      "UPDATE jobs SET status='FAILED',error=?,latency_ms=?,updated=? WHERE id=? AND status='RUNNING'",
    ).run(message, Date.now() - started, stamp(), job.id);
  } finally {
    clearTimeout(timeout);
    running.delete(job.id);
    processing = false;
  }
}, 250);
server.listen(port, "127.0.0.1", () =>
  console.log(
    `faat API listening on http://127.0.0.1:${(server.address() as { port: number }).port}`,
  ),
);
function close() {
  clearInterval(timer);
  for (const c of running.values()) c.abort();
  server.close(() => process.exit(0));
}
process.on("SIGINT", close);
process.on("SIGTERM", close);
