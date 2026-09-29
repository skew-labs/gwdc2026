import { z } from "zod";
import { ErrorBody } from "./contracts";
const base = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
if (
  base &&
  !/^https:\/\//.test(base) &&
  !/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(base)
)
  throw new Error("API base must use HTTPS, or localhost for development.");
export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status = 0,
    public requestId?: string,
  ) {
    super(message);
  }
}
let csrf = "";
export const setCsrf = (token: string) => {
  csrf = token;
};
export async function request<T>(
  method: string,
  path: string,
  schema: z.ZodType<T>,
  body?: unknown,
  options: { signal?: AbortSignal; key?: string } = {},
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timeout = setTimeout(
    () => controller.abort(),
    path.includes("portfolio-reviews")
      ? 95000
      : /observations|plan-comparisons|execution-graphs/.test(path)
        ? 55000
        : 25000,
  );
  try {
    const r = await fetch(base + path, {
      method,
      credentials: "include",
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        ...(method !== "GET"
          ? {
              "X-CSRF-Token": csrf,
              "Idempotency-Key": options.key || crypto.randomUUID(),
            }
          : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const json = await r.json().catch(() => null);
    if (!r.ok) {
      const p = ErrorBody.safeParse(json);
      throw new ApiError(
        p.success ? p.data.error.code : "HTTP_ERROR",
        p.success ? p.data.error.message : `Service returned HTTP ${r.status}.`,
        r.status,
        p.success ? p.data.error.request_id : undefined,
      );
    }
    const parsed = schema.safeParse(json);
    if (!parsed.success)
      throw new ApiError(
        "INVALID_RESPONSE",
        "The service returned an incompatible response. Check the API contract.",
        r.status,
      );
    return parsed.data;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    if (options.signal?.aborted) throw e;
    throw new ApiError(
      "CONNECTION_UNKNOWN",
      method === "GET"
        ? "The service is unreachable. Check the connection and try again."
        : "The result of this request is unknown. Refresh the workspace before retrying.",
    );
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener("abort", abort);
  }
}
