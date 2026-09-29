import { spawn } from "node:child_process";
const children = [
  spawn(
    process.execPath,
    [
      "--import",
      "tsx",
      "--env-file-if-exists=.env.server.local",
      "server/index.ts",
    ],
    { stdio: "inherit" },
  ),
  spawn(
    process.execPath,
    ["node_modules/vite/bin/vite.js", "--host", "127.0.0.1"],
    { stdio: "inherit" },
  ),
];
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  children.forEach((p) => p.kill("SIGTERM"));
  setTimeout(() => process.exit(code), 300).unref();
}
children.forEach((p) => p.on("exit", (code) => stop(code || 0)));
process.on("SIGINT", () => stop());
process.on("SIGTERM", () => stop());
