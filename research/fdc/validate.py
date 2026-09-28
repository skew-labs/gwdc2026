"""Episode gate: provenance, split isolation, evidence, and exact conservation.

This is a dataset integrity check, not a claim that labels are financially true.
"""

import argparse
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path


class DatasetError(ValueError):
    pass


def decimal(value, where: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", value):
        raise DatasetError(f"{where}: decimal must be a plain nonnegative string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise DatasetError(f"{where}: invalid decimal") from exc
    if not number.is_finite() or number < 0:
        raise DatasetError(f"{where}: must be finite and nonnegative")
    return number


def check_episode(row: dict, line: int) -> None:
    where = f"line {line}"
    if row.get("schema_version") != "2.0.0":
        raise DatasetError(f"{where}: wrong schema version")
    need = row["needs"]
    amount = decimal(need["amount"], where + " amount")
    reserve = decimal(need["liquid_reserve"], where + " reserve")
    if amount <= 0 or reserve > amount:
        raise DatasetError(f"{where}: invalid need amount/reserve")
    if type(need["horizon_days"]) is not int or not 1 <= need["horizon_days"] <= 365:
        raise DatasetError(f"{where}: horizon out of range")
    if row["split"] not in {"train", "development", "calibration", "sealed",
                            "synthetic_holdout", "excluded"}:
        raise DatasetError(f"{where}: unknown split")
    if row["source_mode"] not in {"synthetic_fixture", "official_document",
                                  "api_observation", "historical_replay"}:
        raise DatasetError(f"{where}: unknown source mode")
    if row["rights_status"] not in {"synthetic_generated", "unknown",
                                    "restricted", "verified_reusable"}:
        raise DatasetError(f"{where}: unknown rights status")
    candidates = row["candidates"]
    candidate_ids = [c["id"] for c in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise DatasetError(f"{where}: duplicate candidate ID")
    for candidate in candidates:
        decimal(candidate["supply_apy"], where + " APY")
        decimal(candidate["available_cash"], where + " liquidity")
        if candidate["fee_quote"] is not None:
            decimal(candidate["fee_quote"], where + " fee")
        ages = [point["age_days"] for point in candidate["history"]]
        if any(not isinstance(age, int) or age < 0 for age in ages):
            raise DatasetError(f"{where}: invalid history age")
        if ages != sorted(set(ages), reverse=True):
            raise DatasetError(f"{where}: history must be old to new without duplicates")
        for point in candidate["history"]:
            decimal(point["supply_apy"], where + " history APY")
            decimal(point["available_cash"], where + " history cash")
    evidence = {item["id"] for item in row["evidence"]}
    if len(evidence) != len(row["evidence"]):
        raise DatasetError(f"{where}: duplicate evidence ID")
    if row["source_mode"] == "synthetic_fixture":
        if row["review_status"] != "synthetic_unreviewed":
            raise DatasetError(f"{where}: synthetic fixture cannot be reviewed gold")
        if row["split"] not in {"train", "development", "synthetic_holdout", "excluded"}:
            raise DatasetError(f"{where}: synthetic fixture in real evaluation split")
    if row["review_status"] == "unreviewed" and row["split"] != "excluded":
        raise DatasetError(f"{where}: unreviewed episode cannot enter evaluation or training")
    if row["source_mode"] != "synthetic_fixture" and row["split"] != "excluded":
        if row["rights_status"] != "verified_reusable" or row["review_status"] != "adjudicated":
            raise DatasetError(f"{where}: real episode needs rights and adjudication before use")
    for plan in row["target"]["plans"]:
        if not set(plan["allocations"]).issubset(candidate_ids):
            raise DatasetError(f"{where}: allocation references unknown candidate")
        if not set(plan["evidence_ids"]).issubset(evidence):
            raise DatasetError(f"{where}: plan references unknown evidence")
        allocations = [decimal(v, where + " allocation") for v in plan["allocations"].values()]
        if sum(allocations, Decimal(0)) + decimal(plan["reserve"], where + " plan reserve") != amount:
            raise DatasetError(f"{where}: allocation does not conserve budget")
        if decimal(plan["reserve"], where + " plan reserve") < reserve:
            raise DatasetError(f"{where}: plan violates reserve")
        for candidate in candidates:
            allocation = decimal(plan["allocations"].get(candidate["id"], "0"), where + " allocation")
            if allocation and (candidate["asset"] != need["asset"]
                               or candidate["status"] not in {"active", "synthetic"}
                               or allocation > decimal(candidate["available_cash"], where + " cash")):
                raise DatasetError(f"{where}: allocated to ineligible or illiquid candidate")
    if row["target"]["mode"] == "propose" and not row["target"]["plans"]:
        raise DatasetError(f"{where}: propose has no plans")
    if row["target"]["mode"] != "propose" and row["target"]["plans"]:
        raise DatasetError(f"{where}: non-propose has plans")


def validate(path: Path, schema_path: Path | None = None) -> dict:
    validator = None
    if schema_path is not None:
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError as exc:
            raise DatasetError("install optional validation dependency on the remote host") from exc
        validator = Draft202012Validator(json.loads(schema_path.read_text()),
                                         format_checker=FormatChecker())
    ids = set()
    family_split = {}
    splits = Counter()
    modes = Counter()
    sources = Counter()
    with Path(path).open(encoding="utf-8") as stream:
        for line, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise DatasetError(f"line {line}: JSON object required")
                if validator is not None:
                    errors = list(validator.iter_errors(row))
                    if errors:
                        raise DatasetError(f"line {line}: schema: {errors[0].message}")
                check_episode(row, line)
                eid = row["episode_id"]
                if eid in ids:
                    raise DatasetError(f"line {line}: duplicate episode ID")
                ids.add(eid)
                family = row["scenario_family"]
                prior = family_split.setdefault(family, row["split"])
                if prior != row["split"]:
                    raise DatasetError(f"line {line}: scenario family crosses splits")
                splits[row["split"]] += 1
                modes[row["target"]["mode"]] += 1
                sources[row["source_mode"]] += 1
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, DatasetError):
                    raise
                raise DatasetError(f"line {line}: {exc}") from exc
    if not ids:
        raise DatasetError("empty dataset")
    return {"episodes": len(ids), "families": len(family_split),
            "splits": dict(splits), "modes": dict(modes), "source_modes": dict(sources)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args()
    print(json.dumps(validate(args.dataset, args.schema), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
