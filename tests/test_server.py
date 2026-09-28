"""Loopback HTTP smoke; no external calls and no signing."""

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


class ServerTests(unittest.TestCase):
    def test_status_static_plan_gate_and_approval_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen(
                [sys.executable, "-m", "finagent.cli", "--data-dir", temporary,
                 "serve", "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            base = f"http://127.0.0.1:{port}"
            try:
                for _ in range(30):
                    try:
                        with urllib.request.urlopen(base + "/api/status", timeout=1) as response:
                            status = json.load(response)
                        break
                    except urllib.error.URLError:
                        if process.poll() is not None:
                            self.fail("review server exited before startup")
                        time.sleep(0.1)
                else:
                    self.fail("review server did not start")
                self.assertFalse(status["execution_enabled"])
                self.assertTrue(all(source is None for source in status["sources"].values()))
                with urllib.request.urlopen(base + "/", timeout=2) as response:
                    self.assertIn(b"workspace", response.read())
                need = {"asset": "USDT", "amount": "1000", "liquid_reserve": "200",
                        "horizon_days": 30, "risk": "balanced"}
                for endpoint, expected in (("/api/plan", 409), ("/api/approve", 403)):
                    request = urllib.request.Request(
                        base + endpoint, data=json.dumps(need).encode(),
                        headers={"Content-Type": "application/json"}, method="POST")
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(request, timeout=2)
                    self.assertEqual(raised.exception.code, expected)
                request = urllib.request.Request(
                    base + "/api/interpret", data=b'{}',
                    headers={"Content-Type": "text/plain", "Origin": "https://elsewhere.example"},
                    method="POST")
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(request, timeout=2)
                self.assertEqual(raised.exception.code, 403)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
