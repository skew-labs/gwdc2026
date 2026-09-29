"""Fail a supervisor check on unhealthy or critically alerted service state."""

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request


def check(url, *, allow_http_localhost=False):
    parsed = urllib.parse.urlparse(url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (allow_http_localhost and local):
        raise RuntimeError("health URL must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("health URL must not contain credentials or query data")
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read(32_769)
            status = response.status
    except urllib.error.URLError as exc:
        raise RuntimeError("finance service health endpoint is unavailable") from exc
    if status != 200 or len(body) > 32_768:
        raise RuntimeError("finance service health response is invalid")
    document = json.loads(body)
    storage = document.get("storage", {})
    alerts = storage.get("alerts", [])
    if (document.get("status") != "ok"
            or document.get("execution_authority") != "NONE"
            or storage.get("backend") != "POSTGRESQL"
            or storage.get("journal_integrity") is not True):
        raise RuntimeError("finance service reported an unsafe state")
    critical = [item.get("code") for item in alerts
                if isinstance(item, dict) and item.get("severity") == "CRITICAL"]
    if critical:
        raise RuntimeError("finance service has critical alerts: " + ",".join(critical))
    return {"status": "PASS", "backend": "POSTGRESQL",
            "journal_integrity": True,
            "warning_codes": [item.get("code") for item in alerts
                              if isinstance(item, dict)
                              and item.get("severity") == "WARNING"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--allow-http-localhost", action="store_true")
    args = parser.parse_args()
    print(json.dumps(check(args.url,
                           allow_http_localhost=args.allow_http_localhost),
                     sort_keys=True))


if __name__ == "__main__":
    main()
