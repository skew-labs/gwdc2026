import type { DatabaseSync } from "node:sqlite";
import type { Workspace } from "../src/api/contracts";
import { readableServiceError } from "../src/lib/errors";

/** Present current work without rewriting the durable job audit. */
export function workspaceJobs(
  db: DatabaseSync,
  workspace: string,
  wallet: string | null,
  network: string,
  agent: string | null,
  completedPolicyHash: string | null,
): Workspace["jobs"] {
  const completed =
    completedPolicyHash && wallet
      ? (db
          .prepare(
            "SELECT j.rowid AS sequence FROM jobs j JOIN plan_jobs p ON p.job_id=j.id WHERE j.workspace=? AND j.network=? AND p.wallet=? AND j.status='SUCCEEDED' AND p.confirmed_hash=? ORDER BY j.rowid DESC LIMIT 1",
          )
          .get(workspace, network, wallet, completedPolicyHash) as
          { sequence: number } | undefined)
      : undefined;
  const rows = db
    .prepare(
      "SELECT j.rowid AS sequence,j.id,j.status,j.error,p.job_id AS planning_id FROM jobs j LEFT JOIN plan_jobs p ON p.job_id=j.id WHERE j.workspace=? AND j.network=? AND " +
        (agent ? "j.agent=?" : "p.wallet=?") +
        " ORDER BY j.rowid DESC LIMIT 5",
    )
    .all(workspace, network, agent || wallet || "") as {
    sequence: number;
    id: string;
    status: Workspace["jobs"][number]["status"];
    error: string | null;
    planning_id: string | null;
  }[];
  return rows
    .filter(
      (job) =>
        !(
          job.status === "FAILED" &&
          job.planning_id &&
          completed &&
          job.sequence < completed.sequence
        ),
    )
    .map((job) => ({
      id: job.id,
      status: job.status,
      label: job.planning_id
        ? "Checking conditions and calculating two plans"
        : job.status === "RUNNING"
          ? "Writing a response"
          : "Message processing",
      error: job.error ? readableServiceError(job.error) : null,
    }));
}
