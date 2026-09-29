import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { z } from "zod";
beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("VITE_API_BASE_URL", "");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});
it("uses cookie auth, CSRF and a stable supplied idempotency key", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(
      new Response(JSON.stringify({ accepted: true }), { status: 200 }),
    );
  vi.stubGlobal("fetch", fetch);
  const { request, setCsrf } = await import("../src/api/client");
  setCsrf("csrf-test");
  await request(
    "POST",
    "/v1/test",
    z.object({ accepted: z.boolean() }),
    { value: "1" },
    { key: "same-transaction" },
  );
  const [url, init] = fetch.mock.calls[0];
  expect(url).toBe("/v1/test");
  expect(init.credentials).toBe("include");
  expect(init.headers["X-CSRF-Token"]).toBe("csrf-test");
  expect(init.headers["Idempotency-Key"]).toBe("same-transaction");
  expect(init.headers.Authorization).toBeUndefined();
});
it("fails closed for malformed backend data", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ accepted: "yes" }), { status: 200 }),
      ),
  );
  const { request } = await import("../src/api/client");
  await expect(
    request("GET", "/v1/test", z.object({ accepted: z.boolean() })),
  ).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
});
it("does not retry an uncertain mutating request", async () => {
  const fetch = vi.fn().mockRejectedValue(new TypeError("Network error"));
  vi.stubGlobal("fetch", fetch);
  const { request } = await import("../src/api/client");
  await expect(
    request("POST", "/v1/executions", z.object({}), {}),
  ).rejects.toMatchObject({ code: "CONNECTION_UNKNOWN" });
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("retains server error codes and request IDs", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "POLICY_CHANGED",
            message: "Review the new mandate.",
            request_id: "req-123",
          },
        }),
        { status: 409 },
      ),
    ),
  );
  const { request } = await import("../src/api/client");
  await expect(
    request("POST", "/v1/approvals", z.object({}), {}),
  ).rejects.toMatchObject({
    code: "POLICY_CHANGED",
    requestId: "req-123",
    status: 409,
  });
});
