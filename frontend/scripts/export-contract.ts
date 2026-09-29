import { writeFile } from "node:fs/promises";
import { z } from "zod";
import {
  endpoints,
  ErrorBody,
  Workspace,
  Mandate,
  Comparison,
  Graph,
  Approval,
  Prepared,
  Performance,
  Position,
  EvidenceRun,
} from "../src/api/contracts";
const schema = (s: z.ZodType) =>
  z.toJSONSchema(s, { target: "draft-2020-12", unrepresentable: "any" });
const paths: Record<string, unknown> = {};
for (const endpoint of endpoints) {
  const parameters: Array<unknown> = [];
  for (const match of endpoint.path.matchAll(/\{(\w+)\}/g))
    parameters.push({
      name: match[1],
      in: "path",
      required: true,
      schema: { type: "string" },
    });
  if (["/v1/workspace", "/v1/evidence", "/v1/funding/state"].includes(endpoint.path))
    parameters.push({
      name: "network",
      in: "query",
      required: true,
      schema: { type: "string", enum: ["nile", "mainnet"] },
    });
  if (endpoint.path === "/v1/workspace")
    parameters.push({
      name: "agent_id",
      in: "query",
      required: false,
      schema: { type: "string" },
    });
  if (endpoint.path === "/v1/agents" && endpoint.method === "get")
    parameters.push({
      name: "archived",
      in: "query",
      required: false,
      schema: { type: "boolean" },
    });
  if (endpoint.method !== "get")
    parameters.push(
      {
        name: "Idempotency-Key",
        in: "header",
        required: true,
        schema: { type: "string" },
      },
      {
        name: "X-CSRF-Token",
        in: "header",
        required: true,
        schema: { type: "string" },
      },
    );
  const operation = {
    operationId:
      endpoint.method + "_" + endpoint.path.replace(/[^a-z0-9]+/gi, "_"),
    parameters,
    security: [{ sessionCookie: [] }],
    ...("request" in endpoint
      ? {
          requestBody: {
            required: true,
            content: {
              "application/json": { schema: schema(endpoint.request) },
            },
          },
        }
      : {}),
    responses: {
      "200": {
        description: "Validated response",
        content: { "application/json": { schema: schema(endpoint.response) } },
      },
      "401": {
        description: "Session absent or expired",
        content: { "application/json": { schema: schema(ErrorBody) } },
      },
      "409": {
        description:
          "Stale, expired, changed policy, conflict, or unresolved execution",
        content: { "application/json": { schema: schema(ErrorBody) } },
      },
      "422": {
        description: "Constraint violation or invalid request",
        content: { "application/json": { schema: schema(ErrorBody) } },
      },
    },
  };
  paths[endpoint.path] = {
    ...((paths[endpoint.path] as object) || {}),
    [endpoint.method]: operation,
  };
}
await writeFile(
  "docs/openapi.json",
  JSON.stringify(
    {
      openapi: "3.1.0",
      info: {
        title: "Machine frontend integration contract",
        version: "1.0.0",
        description:
          "Runtime schemas for the Machine gateway. OVH implements chat, wallet ownership, mandates, live observations, comparisons and routines. Nile jTRX supply and redemption have verified signed transaction receipts. Portfolio reviews compare holding and adjustments with current state and costs, and persist account notifications. A new performance period requires closed Nile positions and reconciled transactions; historical receipts and policy spending remain intact. USDD execution adapters exist but have not completed a mainnet live lifecycle. Workspace identity is derived from the session.",
      },
      servers: [{ url: "http://localhost:8000" }],
      paths,
      components: {
        securitySchemes: {
          sessionCookie: {
            type: "apiKey",
            in: "cookie",
            name: "machine_session",
          },
        },
        schemas: Object.fromEntries(
          Object.entries({
            Workspace,
            Mandate,
            PlanComparison: Comparison,
            ExecutionGraph: Graph,
            Approval,
            Prepared,
            Position,
            PerformanceSnapshot: Performance,
            EvidenceRun,
          }).map(([name, s]) => [name, schema(s)]),
        ),
      },
    },
    null,
    2,
  ) + "\n",
);
console.log("docs/openapi.json generated from runtime schemas.");
