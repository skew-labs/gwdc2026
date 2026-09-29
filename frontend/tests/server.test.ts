import { beforeAll, afterAll, expect, it } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { TronWeb } from "tronweb";
let child: ChildProcess, base: string, dir: string;
type Client = { cookie: string; csrf: string };
const origin = "http://127.0.0.1:5173";
async function start() {
  child = spawn(process.execPath, ["--import", "tsx", "server/index.ts"], {
    env: {
      ...process.env,
      PORT: "0",
      APP_ORIGIN: origin,
      DATABASE_PATH: join(dir, "db.sqlite"),
      KILN_API_KEY: "",
      FINANCE_API_URL: "",
      NODE_ENV: "test",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  base = await new Promise<string>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("server start timeout")),
      10000,
    );
    child.stdout!.on("data", (data) => {
      const match = String(data).match(/http:\/\/127.0.0.1:\d+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`server exited ${code}`));
    });
  });
}
async function stop() {
  await new Promise<void>((resolve) => {
    child.once("exit", () => resolve());
    child.kill("SIGTERM");
  });
}
async function session(): Promise<Client> {
  const r = await fetch(base + "/v1/session");
  return {
    cookie: r.headers.get("set-cookie")!.split(";")[0],
    csrf: (await r.json()).csrf_token,
  };
}
async function call(
  c: Client,
  path: string,
  method = "GET",
  data?: unknown,
  headers: Record<string, string> = {},
) {
  const r = await fetch(base + path, {
    method,
    headers: {
      Cookie: c.cookie,
      Origin: origin,
      "X-CSRF-Token": c.csrf,
      "Idempotency-Key": crypto.randomUUID(),
      "Content-Type": "application/json",
      ...headers,
    },
    body: data === undefined ? undefined : JSON.stringify(data),
  });
  return { status: r.status, body: await r.json() };
}
beforeAll(async () => {
  dir = await mkdtemp(join(tmpdir(), "machine-test-"));
  await start();
}, 15000);
afterAll(async () => {
  if (child) await stop();
  if (dir) await rm(dir, { recursive: true, force: true });
});
const input = {
  name: "Allocation research",
  role: "alpha",
  instructions: "Ask one question at a time.",
};
it("starts with zero agents, messages and financial records", async () => {
  const c = await session();
  expect((await call(c, "/v1/agents")).body).toEqual([]);
  const { body: w } = await call(c, "/v1/workspace?network=nile");
  expect(w.messages).toEqual([]);
  expect(w.balances).toEqual([]);
  expect(w.graph).toBeNull();
  expect(w.mandate).toBeNull();
});
it("rejects cross-origin mutations and missing CSRF", async () => {
  const c = await session();
  expect(
    (
      await call(c, "/v1/agents", "POST", input, {
        Origin: "https://untrusted.example",
      })
    ).status,
  ).toBe(403);
  expect(
    (await call(c, "/v1/agents", "POST", input, { "X-CSRF-Token": "" })).status,
  ).toBe(403);
  expect((await call(c, "/v1/agents")).body).toEqual([]);
});
it("deduplicates creates and rejects changed payload under the same key", async () => {
  const c = await session();
  const key = { "Idempotency-Key": "repeat" };
  const a = await call(c, "/v1/agents", "POST", input, key);
  const b = await call(c, "/v1/agents", "POST", input, key);
  expect(a.status).toBe(200);
  expect(a.body.id).toBe(b.body.id);
  expect((await call(c, "/v1/agents")).body).toHaveLength(1);
  expect(
    (await call(c, "/v1/agents", "POST", { ...input, name: "Different" }, key))
      .status,
  ).toBe(409);
});
it("persists edited agents across a process restart and isolates sessions", async () => {
  const c = await session();
  const other = await session();
  const a = (await call(c, "/v1/agents", "POST", input)).body;
  expect((await call(other, `/v1/agents/${a.id}`, "PATCH", input)).status).toBe(
    404,
  );
  await call(c, `/v1/agents/${a.id}`, "PATCH", {
    ...input,
    name: "Treasury",
    role: "vault",
  });
  await stop();
  await start();
  expect((await call(c, "/v1/agents")).body[0]).toMatchObject({
    name: "Treasury",
    role: "vault",
  });
  expect((await call(other, "/v1/agents")).body).toEqual([]);
}, 15000);
it("archives and restores without deleting conversation ownership", async () => {
  const c = await session();
  const a = (await call(c, "/v1/agents", "POST", input)).body;
  expect((await call(c, `/v1/agents/${a.id}`, "DELETE", {})).status).toBe(200);
  expect((await call(c, "/v1/agents")).body).toEqual([]);
  expect((await call(c, "/v1/agents?archived=true")).body[0].id).toBe(a.id);
  expect((await call(c, `/v1/agents/${a.id}/restore`, "POST", {})).status).toBe(
    200,
  );
  expect((await call(c, "/v1/agents")).body[0].id).toBe(a.id);
});
it("does not invent assistant messages when Kiln is unavailable", async () => {
  const c = await session();
  const a = (await call(c, "/v1/agents", "POST", input)).body;
  const r = await call(c, "/v1/messages", "POST", {
    agent_id: a.id,
    role: "alpha",
    text: "Hello",
    network: "nile",
  });
  expect(r.status).toBe(503);
  expect(r.body.error.code).toBe("KILN_NOT_CONFIGURED");
  expect(
    (await call(c, `/v1/workspace?network=nile&agent_id=${a.id}`)).body
      .messages,
  ).toEqual([]);
  expect((await call(c, "/v1/mandates", "POST", {})).body.error.code).toBe(
    "FINANCE_NOT_CONNECTED",
  );
});
it("verifies a real cryptographic ownership proof and rejects nonce replay", async () => {
  const c = await session();
  const account = await TronWeb.createAccount();
  const tron = new TronWeb({
    fullHost: "https://api.trongrid.io",
    privateKey: account.privateKey,
  });
  const { body: challenge } = await call(c, "/v1/auth/challenge", "POST", {
    address: account.address.base58,
    network: "nile",
    domain: "127.0.0.1:5173",
  });
  const signature = await tron.trx.signMessageV2(challenge.message);
  const verified = await call(c, "/v1/auth/verify", "POST", {
    challenge_id: challenge.id,
    signature,
  });
  expect(verified.status).toBe(200);
  expect(verified.body.wallet_address).toBe(account.address.base58);
  c.csrf = verified.body.csrf_token;
  expect(
    (
      await call(c, "/v1/auth/verify", "POST", {
        challenge_id: challenge.id,
        signature,
      })
    ).status,
  ).toBe(401);
  expect((await call(c, "/v1/session")).body.authenticated).toBe(true);
});
