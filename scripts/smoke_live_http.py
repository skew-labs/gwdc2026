"""Bounded loopback smoke against an already collected read-only dataset."""

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "finagent.cli", "--data-dir", str(args.data_dir),
         "serve", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(40):
            try:
                with urllib.request.urlopen(base + "/api/status", timeout=1) as response:
                    status = json.load(response)
                break
            except urllib.error.URLError:
                if process.poll() is not None:
                    raise RuntimeError("review server exited during startup")
                time.sleep(0.1)
        else:
            raise RuntimeError("review server did not start")
        if sum(bool(source and source["fresh"]) for source in status["sources"].values()) != 4:
            raise RuntimeError("required sources are not all fresh")
        with urllib.request.urlopen(base + "/", timeout=2) as response:
            if response.status != 200 or b"workspace" not in response.read():
                raise RuntimeError("workspace HTML not served")
        need = {"asset": "USDT", "amount": "2000", "liquid_reserve": "500",
                "horizon_days": 30, "risk": "balanced"}
        request = urllib.request.Request(
            base + "/api/plan", data=json.dumps(need).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            answer = json.load(response)
        if len(answer["plans"]) < 2 or any(plan["executable"] for plan in answer["plans"]):
            raise RuntimeError("expected two non-executable research plans")
        request = urllib.request.Request(
            base + "/api/approve", data=json.dumps({"plan_id": answer["plan_id"]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as exc:
            if exc.code != 403:
                raise RuntimeError("approval gate returned unexpected status") from exc
        else:
            raise RuntimeError("approval gate unexpectedly allowed execution")
        print(json.dumps({"sources_fresh": 4, "workspace_http": 200,
                          "plan_count": len(answer["plans"]),
                          "plan_id": answer["plan_id"], "approval_http": 403,
                          "state": answer["state"]}, ensure_ascii=False))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
