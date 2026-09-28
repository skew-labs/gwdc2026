"""Mine audited *synthetic constraint* examples, preserving family splits.

The output deliberately strips the episode's target. It is not financial gold
and cannot be used to train a real allocation recommender without review.
"""

import argparse
import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from .allocation_oracle import judge, validate_verdict
from .validate_v3 import validate


def _fmt(value: Decimal) -> str:
    return format(value, "f")


def _variants(row: dict):
    if row["target"]["mode"] != "propose":
        return
    base = {"mode": "propose", "plans": deepcopy(row["target"]["plans"])}
    yield "original_proposal", row, base, "CONSTRAINTS_PASS", None
    budget = deepcopy(base)
    budget["plans"][0]["hold"] = _fmt(Decimal(budget["plans"][0]["hold"]) + 1)
    yield "budget_plus_one", row, budget, "REJECTED", "BUDGET_NOT_CONSERVED"
    first = base["plans"][0]
    used = [cid for cid, value in first["allocations"].items() if Decimal(value) > 0]
    if used:
        candidate = next(item for item in row["candidates"] if item["id"] == used[0])
        dropped = deepcopy(base)
        dropped["plans"][0]["evidence_ids"] = [eid for eid in first["evidence_ids"]
                                               if eid not in candidate["evidence_ids"]]
        yield "drop_used_evidence", row, dropped, "REJECTED", "CANDIDATE_EVIDENCE_MISSING"
        stale = deepcopy(row)
        target = next(item for item in stale["candidates"] if item["id"] == used[0])
        from datetime import datetime
        target["valid_until"] = (datetime.fromisoformat(row["as_of"])
                                 - timedelta(seconds=1)).isoformat()
        yield "expire_used_market", stale, base, "REJECTED", "SOURCE_OUT_OF_TIME"
    first_two = row["candidates"][:2]
    amount = Decimal(row["need"]["budget"]["value"])
    share = amount * Decimal("0.3")
    if (len(first_two) == 2 and first_two[0]["risk_groups"] == first_two[1]["risk_groups"]
            and all(item["status"] == "synthetic"
                    and Decimal(item["available_cash"]["value"]) >= share
                    and item["lock_days"] <= row["need"]["withdrawal"]["latest_day"]
                    and item["notice_days"] <= row["need"]["withdrawal"]["max_notice_days"]
                    for item in first_two)):
        concentrated = deepcopy(base)
        plan = concentrated["plans"][0]
        plan["allocations"] = {item["id"]: _fmt(share) for item in first_two}
        plan["hold"] = _fmt(amount - 2 * share)
        plan["evidence_ids"] = sorted({eid for item in first_two
                                       for eid in item["evidence_ids"]})
        yield "shared_group_over_cap", row, concentrated, "REJECTED", "SHARED_RISK_CAP_EXCEEDED"


def mine(dataset: Path, output: Path, schema: Path) -> dict:
    audit = validate(dataset, schema)
    if audit["status"] != "SYNTHETIC_RESEARCH_OR_EXCLUDED_DRAFT_ONLY":
        raise ValueError("dataset gate did not pass")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    counts = Counter()
    splits = Counter()
    families: dict[str, str] = {}
    case_families: set[str] = set()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        for raw in dataset.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            row = json.loads(raw)
            if row["source_mode"] != "synthetic_fixture":
                continue
            family = row["scenario_family"]
            if family in families and families[family] != row["split"]:
                raise ValueError("family split leakage")
            families[family] = row["split"]
            for mutation, changed, proposal, expected, code in _variants(row):
                input_only = {key: value for key, value in changed.items() if key != "target"}
                result = judge(input_only, proposal)
                validate_verdict(result)
                if result["verdict"] != expected or (code is not None and code not in
                                                      {item["code"] for item in result["hard_failures"]}):
                    raise ValueError(f"oracle mutation failed: {row['episode_id']}:{mutation}")
                case_id = hashlib.sha256((row["episode_id"] + ":" + mutation).encode()).hexdigest()
                record = {"schema_version": "1.0.0", "case_id": case_id,
                          "scenario_family": family, "split": row["split"],
                          "mutation": mutation, "input": input_only,
                          "proposal": proposal, "oracle": result,
                          "label_scope": "synthetic_constraint_only",
                          "human_gold": False}
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                counts[mutation] += 1
                splits[row["split"]] += 1
                case_families.add(family)
    manifest = {"schema_version": "1.0.0", "input_sha256": digest,
                "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "input_audit": audit, "case_count": sum(counts.values()),
                "mutations": dict(counts), "splits": dict(splits),
                "source_family_count": len(families),
                "case_family_count": len(case_families),
                "label_scope": "synthetic_constraint_only", "human_gold": False,
                "usable_for_real_allocation_training": False}
    manifest_path = output.with_suffix(".manifest.json")
    with manifest_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2,
                                sort_keys=True) + "\n")
    return {"output": str(output), "manifest": str(manifest_path), **manifest}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", type=Path,
                        default=Path("contracts/episode_v3.schema.json"))
    args = parser.parse_args()
    print(json.dumps(mine(args.dataset, args.output, args.schema),
                     ensure_ascii=False, indent=2))
