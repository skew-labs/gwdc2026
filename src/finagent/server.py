"""Loopback-only review server. No transaction or signing endpoint."""

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .contracts import ContractError, Need
from .planner import PlanUnavailable, compare
from .qwen import ModelUnavailable, configured, interpret
from .store import Store


def serve(store: Store, web_root: Path, port: int = 8765) -> None:
    web_root = Path(web_root).resolve()

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/api/status":
                source_ids = (("justlend_contracts", 86400),
                              ("justlend_markets_v1", 900),
                              ("justlend_usdd_rewards_v1", 900),
                              ("usdd_earn_apy", 900))
                sources = {}
                now = datetime.now(timezone.utc)
                for source_id, max_age in source_ids:
                    row = store.latest(source_id)
                    sources[source_id] = ({"fetched_at": row["fetched_at"],
                                           "snapshot_id": row["id"],
                                           "fresh": 0 <= (now - datetime.fromisoformat(
                                               row["fetched_at"])).total_seconds() <= max_age}
                                          if row else None)
                self.send_json(200, {"model_configured": configured(),
                                     "sources": sources, "execution_enabled": False})
                return
            names = {"/": "index.html", "/index.html": "index.html",
                     "/app.css": "app.css", "/app.js": "app.js"}
            filename = names.get(self.path)
            if filename is None:
                self.send_error(404)
                return
            raw = (web_root / filename).read_bytes()
            content_type = {"html": "text/html; charset=utf-8", "css": "text/css; charset=utf-8",
                            "js": "text/javascript; charset=utf-8"}[filename.rsplit(".", 1)[-1]]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path not in {"/api/interpret", "/api/plan", "/api/approve"}:
                self.send_error(404)
                return
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{port}",
                                         f"http://localhost:{port}"}:
                self.send_json(403, {"error": "cross-origin request denied"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                self.send_json(415, {"error": "application/json required"})
                return
            length = self.headers.get("Content-Length", "")
            if not length.isdecimal() or not 0 < int(length) <= 16_384:
                self.send_json(413, {"error": "request size must be 1 to 16384 bytes"})
                return
            try:
                body = json.loads(self.rfile.read(int(length)))
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
            except (ValueError, UnicodeError):
                self.send_json(400, {"error": "invalid JSON request"})
                return
            if self.path == "/api/approve":
                self.send_json(403, {"state": "BLOCKED",
                    "reason": "Execution route, fee quote and wallet authorization are not connected"})
                return
            if self.path == "/api/interpret":
                try:
                    draft, event = interpret(body.get("message", ""))
                    store.record_inference(event)
                except ModelUnavailable as exc:
                    if exc.event is not None:
                        store.record_inference(exc.event)
                    self.send_json(503, {"error": str(exc), "state": "MODEL_UNAVAILABLE"})
                    return
                except ContractError as exc:
                    self.send_json(400, {"error": str(exc), "state": "INVALID_MESSAGE"})
                    return
                self.send_json(200, {"draft": draft,
                                     "usage": {"input_tokens": event["input_tokens"],
                                               "output_tokens": event["output_tokens"]},
                                     "state": "NEEDS_USER_REVIEW"})
                return
            try:
                need = Need.from_json(body)
                answer = compare(store, need)
            except ContractError as exc:
                self.send_json(400, {"error": str(exc), "state": "INVALID_NEED"})
                return
            except PlanUnavailable as exc:
                self.send_json(409, {"error": str(exc), "state": "DATA_UNAVAILABLE"})
                return
            self.send_json(200, answer)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Review UI: http://127.0.0.1:{port}", flush=True)
    httpd.serve_forever()
