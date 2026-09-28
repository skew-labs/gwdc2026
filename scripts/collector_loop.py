"""Bounded, single-instance public-source collector for the dedicated service."""

import argparse
import fcntl
import json
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from finagent.collect import collect_all, source_registry
from finagent.release import write_manifest
from finagent.store import Store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not 300 <= args.interval_seconds <= 3600:
        parser.error("interval must be 300..3600 seconds")
    store = Store(args.data_dir)
    lock = (store.root / "collector.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("another collector is already running")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    ids = [source["id"] for source in source_registry(args.registry)]
    while not stop.is_set():
        started = datetime.now(timezone.utc).isoformat()
        result = collect_all(store, args.registry)
        release = None
        if all(not value.startswith("ERROR:") for value in result.values()):
            release = str(write_manifest(store, ids))
        print(json.dumps({"started_at": started, "sources": result,
                          "release": release}, ensure_ascii=False), flush=True)
        if args.once:
            break
        stop.wait(args.interval_seconds)


if __name__ == "__main__":
    main()
