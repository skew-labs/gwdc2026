"""One-shot public read/replay evidence. Run only on the approved Cherry host."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from finagent.collect import collect_all
from finagent.store import Store
from economic_machine.snapshot_assembly import SnapshotAssembler
from economic_machine.tron_products import discover_products
from economic_machine.tron_rpc_snapshot import capture_rpc
from economic_machine.tron_sources import capture_from_store, fetch_capture
from economic_machine.values import canonical


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--replay-captures", type=Path, help="Replay a prior Cherry capture file without network access")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if root != Path("/srv/skew/gwdc-financial-agent-20260924"):
        raise SystemExit("Live verification runs only in the approved Cherry project")
    output = args.output.resolve()
    if not output.is_relative_to(root / "data/verification"):
        raise SystemExit("Evidence must stay in the project verification directory")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "captures.json").exists():
        raise SystemExit("Evidence run already exists; choose a new output directory")
    config = json.loads((root / "config/tron_product_registry.json").read_text())
    if args.replay_captures:
        replay_path = args.replay_captures.resolve()
        if not replay_path.is_relative_to(root / "data/verification") or replay_path.stat().st_size > 16000000:
            raise SystemExit("Bounded project-local verification capture required")
        saved = json.loads(replay_path.read_text())
        captures, rpc = saved["captures"], saved["rpc"]
    else:
        store = Store(output / "live-store")
        collection = collect_all(store, root / "config/sources.json")
        print(json.dumps({"collection": collection}), flush=True)
        captures = []
        for source, result in collection.items():
            if result.startswith("ERROR:"):
                # Never fall back to yesterday's successful snapshot after failure.
                from economic_machine.tron_sources import make_capture
                captures.append(make_capture(source, None, received_at=datetime.now(timezone.utc).isoformat(), error="TRANSPORT_ERROR"))
            else:
                captures.append(capture_from_store(store, source))
        for source in ("justlend_strx_v1", "usdd_vault_config", "tron_chain_parameters"):
            captures.append(fetch_capture(source, store))
        products = discover_products(config, next(c for c in captures if c["source_id"] == "justlend_contracts"))
        rpc = capture_rpc(config, products, store=store)
    # Preserve responses even if semantic validation fails; never overwrite a run.
    (output / "captures.json").write_bytes(canonical({"captures": captures, "rpc": rpc}))
    assembler = SnapshotAssembler(config)
    snapshot = assembler.assemble(captures, as_of=datetime.now(timezone.utc).isoformat(), rpc=rpc)
    assembler.verify(snapshot)
    (output / "snapshot.json").write_bytes(canonical(snapshot))
    manifest = {"schema_version": "economic-tron-live-manifest-1", "at": snapshot["as_of"],
        "replay_of": str(args.replay_captures) if args.replay_captures else None,
        "snapshot_hash": snapshot["snapshot_hash"], "mode": snapshot["mode"], "wallet_provided": False,
        "execution_enabled": False, "products": len(snapshot["products"]), "facts": len(snapshot["facts"]),
        "quality_counts": dict(Counter(f["quality"] for f in snapshot["facts"].values())),
        "state_eligible_count": sum(f["state_eligible"] for f in snapshot["facts"].values()),
        "unavailable": snapshot["unavailable"], "source_status": snapshot["source_status"],
        "sources": [{key: c[key] for key in ("source_id", "url", "received_at", "observed_at", "block", "raw_sha256", "error", "capture_hash")}
                    for c in snapshot["captures"]],
        "rpc": {"error": rpc["error"], "capture_hash": rpc["capture_hash"], "records": len(rpc["records"]),
                "requests": [{"path": r["path"], "payload": r["payload"], "raw_sha256": r["raw_sha256"]} for r in rpc["records"]]},
        "comparisons": snapshot["comparisons"],
        "field_mappings": {path: {key: fact[key] for key in ("value", "unit", "quality", "source_id", "json_pointer", "block", "observed_at", "withheld_reasons")}
                           for path, fact in snapshot["facts"].items()},
        "evidence_files": {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in ("captures.json", "snapshot.json")}}
    (output / "live-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({key: manifest[key] for key in ("products", "facts", "quality_counts", "state_eligible_count", "unavailable")}), flush=True)


if __name__ == "__main__":
    main()
