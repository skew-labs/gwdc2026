"""Bounded concurrent load/soak probe for the PostgreSQL finance repository."""

import argparse
import concurrent.futures
import itertools
import json
import math
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from finance_service.postgres_repository import PostgresOperationalRepository
from finance_service.postgres_runtime import PostgresRuntimePolicy


def _percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return round(ordered[index], 3)


def _scope(index):
    return {"tenant_id": "load-tenant-" + str(index),
            "owner_id": "load-owner-" + str(index),
            "wallet": "41" + format(index + 1, "040x"),
            "network": "tron-mainnet"}


def run(args):
    policy = PostgresRuntimePolicy(
        min_pool_size=min(2, args.workers), max_pool_size=args.workers,
        max_waiting=args.workers * 2, pool_timeout_seconds=5,
        statement_timeout_ms=15_000, lock_timeout_ms=5_000)
    repository = PostgresOperationalRepository(
        api_dsn_file=args.api_dsn_file, worker_dsn_file=args.worker_dsn_file,
        runtime_policy=policy,
        allow_insecure_localhost=args.allow_insecure_localhost)
    deadline = time.monotonic() + args.duration_seconds
    sequence = itertools.count()
    lock = threading.Lock()
    latencies = []
    errors = Counter()
    completed = 0

    def worker():
        nonlocal completed
        local_latencies = []
        local_errors = Counter()
        local_completed = 0
        while time.monotonic() < deadline:
            number = next(sequence)
            customer = _scope(number % args.scope_count)
            record_id = "probe-" + str(number)
            started = time.perf_counter()
            try:
                created = repository.put_record(customer, "LOAD_PROBE", record_id,
                    {"sequence": number}, expected_version=0,
                    at=datetime.now(UTC).isoformat())
                fetched = repository.get_record(customer, "LOAD_PROBE", record_id)
                if created["body_hash"] != fetched["body_hash"]:
                    raise RuntimeError("load probe read-after-write mismatch")
            except Exception as exc:  # noqa: BLE001 - every probe failure is evidence
                message = " ".join(str(exc).split())[:120]
                local_errors[type(exc).__name__ + ":" + message] += 1
            else:
                local_completed += 1
                local_latencies.append((time.perf_counter() - started) * 1000)
        with lock:
            completed += local_completed
            latencies.extend(local_latencies)
            errors.update(local_errors)

    started_at = datetime.now(UTC).isoformat()
    start = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(worker) for _ in range(args.workers)]
            for future in futures:
                future.result()
        elapsed = time.monotonic() - start
        integrity = repository.verify_journal()
        with repository.connect(worker=True, repeatable_read=True) as db:
            event_count = db.execute("SELECT count(*) AS n FROM finance_service_journal "
                                     "WHERE journal_version=2").fetchone()["n"]
            stream_count = db.execute("SELECT count(*) AS n FROM "
                                      "finance_service_journal_heads").fetchone()["n"]
        status = ("PASS" if not errors and integrity
                  and completed >= args.minimum_operations else "FAIL")
        result = {"schema_version": "gwdc-postgres-load-probe-1", "status": status,
            "started_at": started_at, "completed_at": datetime.now(UTC).isoformat(),
            "duration_seconds": round(elapsed, 3), "workers": args.workers,
            "pool_max_size": policy.max_pool_size, "scope_count": args.scope_count,
            "completed_operations": completed, "minimum_operations": args.minimum_operations,
            "operations_per_second": round(completed / elapsed, 3),
            "latency_ms": {"p50": _percentile(latencies, 0.50),
                           "p95": _percentile(latencies, 0.95),
                           "p99": _percentile(latencies, 0.99),
                           "max": round(max(latencies), 3) if latencies else None},
            "errors": dict(sorted(errors.items())), "journal_integrity": integrity,
            "journal_v2_events": event_count, "journal_streams": stream_count,
            "execution_authority": "NONE", "transactions": 0,
            "broadcasts": 0, "asset_movements": 0}
        evidence = Path(args.evidence)
        if evidence.exists():
            raise RuntimeError("load probe evidence already exists")
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        if status != "PASS":
            raise RuntimeError("PostgreSQL load probe failed acceptance")
        return result
    finally:
        repository.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-dsn-file", required=True)
    parser.add_argument("--worker-dsn-file", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--scope-count", type=int, default=256)
    parser.add_argument("--minimum-operations", type=int, default=1_000)
    parser.add_argument("--allow-insecure-localhost", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 600:
        raise SystemExit("duration outside probe policy")
    if not 1 <= args.workers <= 64 or not 1 <= args.scope_count <= 10_000:
        raise SystemExit("load dimensions outside probe policy")
    print(json.dumps(run(args), sort_keys=True))


if __name__ == "__main__":
    main()
