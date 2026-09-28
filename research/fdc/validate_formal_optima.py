"""Recheck durable formal labels, source witnesses, and counterfactual pairs."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from finagent.store import Store

from .formal_optima import _bytes, _hash, _source_markets, verify


def validate(data_dir: Path, output_dir: Path) -> dict:
    store = Store(data_dir)
    state = sqlite3.connect(output_dir / "formal.sqlite3")
    state.row_factory = sqlite3.Row
    counts = {"snapshots": 0, "optima": 0, "pairs": 0, "blocked": 0}
    for indexed in state.execute("SELECT * FROM processed_market_snapshots ORDER BY fetched_at,snapshot_id"):
        batch = output_dir / "batches" / f"{indexed['snapshot_id']}.jsonl"
        raw = batch.read_bytes()
        if hashlib.sha256(raw).hexdigest() != indexed["output_sha256"]:
            raise ValueError("formal batch hash mismatch")
        with store.connect() as db:
            snapshot = db.execute("SELECT * FROM snapshots WHERE id=?",
                                  (indexed["snapshot_id"],)).fetchone()
        if snapshot is None or snapshot["fetched_at"] != indexed["fetched_at"]:
            raise ValueError("market snapshot index mismatch")
        if indexed["blocked_reason"] is None:
            grouped, witness = _source_markets(store, snapshot)
        else:
            if raw:
                raise ValueError("blocked snapshot contains labels")
            counts["blocked"] += 1
            grouped, witness = None, None
        records = [json.loads(line) for line in raw.splitlines() if line]
        if raw != b"".join(_bytes(record) + b"\n" for record in records):
            raise ValueError("batch serialization not canonical")
        cases = {}
        pairs = []
        for record in records:
            if record["record_type"] == "FORMAL_OPTIMUM":
                scenario, target = record["input"], record["target"]
                if (scenario["source_witness"] != witness
                        or scenario["markets"] != sorted(grouped[scenario["asset"]],
                                                         key=lambda row: row["id"])
                        or record["case_id"] != _hash({"input": scenario})
                        or record["rights_status"] != "unknown"
                        or record["training_scope"] != "excluded_rights_unknown"
                        or record["product_actionable"] is not False
                        or record["real_customer_optimum"] is not False):
                    raise ValueError("formal case source or scope mismatch")
                verify(scenario, target)
                if record["case_id"] in cases:
                    raise ValueError("duplicate formal case")
                cases[record["case_id"]] = record
            elif record["record_type"] == "COUNTERFACTUAL_PAIR":
                pairs.append(record)
            else:
                raise ValueError("unknown formal record type")
        for pair in pairs:
            before = cases[pair["base_case_id"]]
            after = cases[pair["changed_case_id"]]
            changed = [key for key in before["input"]
                       if before["input"][key] != after["input"][key]]
            if (changed != [pair["changed_field"]]
                    or pair["allocation_changed"] !=
                    (before["target"]["allocations_atoms"] !=
                     after["target"]["allocations_atoms"])
                    or pair["pair_id"] != _hash({"base": pair["base_case_id"],
                                                 "changed": pair["changed_case_id"],
                                                 "field": pair["changed_field"]})):
                raise ValueError("counterfactual pair mismatch")
        if len(cases) != indexed["scenario_count"] or len(pairs) != indexed["pair_count"]:
            raise ValueError("formal batch count mismatch")
        counts["snapshots"] += 1
        counts["optima"] += len(cases)
        counts["pairs"] += len(pairs)
    state.close()
    return {"status": "VALIDATED_FORMAL_ASSUMPTION_LABELS", **counts,
            "real_customer_optimum_count": 0, "training_enabled": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.data_dir, args.output_dir),
                     ensure_ascii=False, indent=2))
