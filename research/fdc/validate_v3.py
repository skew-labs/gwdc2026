"""Typed episode and counterfactual-family gate for FDC V3 research data."""

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


class DatasetV3Error(ValueError):
    pass


DEPENDENCIES = {"budget", "withdrawal_immediate", "withdrawal_deadline",
                "withdrawal_notice", "risk_total", "risk_shared",
                "market_cash", "source_freshness"}


def exact(value, where: str) -> Decimal:
    if (not isinstance(value, str) or len(value) > 100
            or not re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", value)):
        raise DatasetV3Error(f"{where}: exact nonnegative decimal string required")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise DatasetV3Error(f"{where}: invalid decimal") from exc
    if not parsed.is_finite():
        raise DatasetV3Error(f"{where}: nonfinite value")
    return parsed


def instant(value: str, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DatasetV3Error(f"{where}: invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatasetV3Error(f"{where}: timezone required")
    return parsed


def money(value: dict, unit: str, where: str) -> Decimal:
    if not isinstance(value, dict) or value.get("unit") != unit:
        raise DatasetV3Error(f"{where}: unit mismatch")
    return exact(value.get("value"), where)


def eligible(candidate: dict, need: dict, at: datetime) -> bool:
    withdrawal = need["withdrawal"]
    if candidate["status"] not in {"active", "synthetic"} or candidate["asset"] != need["budget"]["unit"]:
        return False
    if candidate["observed_at"] is None or candidate["valid_until"] is None:
        return False
    if instant(candidate["observed_at"], "observed_at") > at or instant(candidate["valid_until"], "valid_until") < at:
        return False
    if candidate["lock_days"] is None or candidate["notice_days"] is None:
        return False
    if withdrawal["latest_day"] is None or withdrawal["max_notice_days"] is None:
        return False
    return (candidate["lock_days"] <= withdrawal["latest_day"]
            and candidate["notice_days"] <= withdrawal["max_notice_days"])


def check_episode(row: dict, line: int) -> None:
    where = f"line {line}"
    if row["schema_version"] != "3.0.0":
        raise DatasetV3Error(f"{where}: wrong schema version")
    at = instant(row["as_of"], where + " as_of")
    need = row["need"]
    unit = need["budget"]["unit"]
    amount = money(need["budget"], unit, where + " budget")
    immediate = money(need["withdrawal"]["min_immediate"], unit, where + " immediate")
    if amount <= 0 or immediate > amount:
        raise DatasetV3Error(f"{where}: invalid budget or immediate reserve")
    total_cap = exact(need["risk"]["total_fraction"], where + " total risk")
    group_cap = exact(need["risk"]["shared_group_fraction"], where + " shared risk")
    if total_cap > 1 or group_cap > 1:
        raise DatasetV3Error(f"{where}: risk fractions must be <= 1")
    if row["source_mode"] == "synthetic_fixture":
        if row["rights_status"] != "synthetic_generated" or row["review_status"] != "synthetic_unreviewed":
            raise DatasetV3Error(f"{where}: synthetic provenance mismatch")
        if row["target"]["oracle_kind"] != "synthetic_rule":
            raise DatasetV3Error(f"{where}: synthetic label is not synthetic oracle")
    else:
        if (row["split"], row["review_status"], row["rights_status"]) != ("excluded", "unreviewed", "unknown"):
            raise DatasetV3Error(f"{where}: API draft cannot enter training or evaluation")
        if row.get("need_origin") not in {"authored_example", "user_confirmed"} or not row.get("source_release"):
            raise DatasetV3Error(f"{where}: API need origin and source release required")
        if row["target"]["oracle_kind"] != "unreviewed_draft":
            raise DatasetV3Error(f"{where}: API target must remain an unreviewed draft")
    candidates = row["candidates"]
    candidate_by_id = {candidate["id"]: candidate for candidate in candidates}
    if len(candidate_by_id) != len(candidates):
        raise DatasetV3Error(f"{where}: duplicate candidate ID")
    evidence_ids = {item["id"] for item in row["evidence"]}
    if len(evidence_ids) != len(row["evidence"]):
        raise DatasetV3Error(f"{where}: duplicate evidence ID")
    for candidate in candidates:
        if row["source_mode"] == "api_observation":
            if (candidate.get("network") != "tron_mainnet"
                    or candidate.get("protocol_version") != "justlend_v1"
                    or not isinstance(candidate.get("underlying_address"), str)
                    or not candidate["underlying_address"].startswith("T")
                    or not isinstance(candidate.get("underlying_decimals"), int)
                    or not 0 <= candidate["underlying_decimals"] <= 36
                    or not candidate["id"].startswith("T")):
                raise DatasetV3Error(f"{where}: API candidate lacks TRON market identity")
        exact(candidate["supply_apy"], where + " APY")
        money(candidate["available_cash"], candidate["asset"], where + " cash")
        if not set(candidate["evidence_ids"]).issubset(evidence_ids):
            raise DatasetV3Error(f"{where}: candidate has unknown evidence")
        if candidate["observed_at"] is not None:
            if instant(candidate["observed_at"], where + " observed_at") > at:
                raise DatasetV3Error(f"{where}: future candidate observation")
        if candidate["valid_until"] is not None:
            instant(candidate["valid_until"], where + " valid_until")
        ages = [point["age_days"] for point in candidate["history"]]
        if ages != sorted(set(ages), reverse=True):
            raise DatasetV3Error(f"{where}: history is not uniquely oldest-first")
        for point in candidate["history"]:
            exact(point["supply_apy"], where + " history APY")
            money(point["available_cash"], candidate["asset"], where + " history cash")
    target = row["target"]
    mode = target["mode"]
    if mode == "propose" and (not target["plans"] or target["question_fields"] or target["abstain_reasons"]):
        raise DatasetV3Error(f"{where}: invalid propose target")
    if mode == "ask" and (not target["question_fields"] or target["plans"] or target["abstain_reasons"]):
        raise DatasetV3Error(f"{where}: invalid ask target")
    if mode == "abstain" and (not target["abstain_reasons"] or target["plans"] or target["question_fields"]):
        raise DatasetV3Error(f"{where}: invalid abstain target")
    for field in target["question_fields"]:
        if field == "withdrawal.latest_day" and need["withdrawal"]["latest_day"] is not None:
            raise DatasetV3Error(f"{where}: question asks an already known deadline")
        if field == "withdrawal.max_notice_days" and need["withdrawal"]["max_notice_days"] is not None:
            raise DatasetV3Error(f"{where}: question asks an already known notice limit")
    plan_ids = [plan["plan_id"] for plan in target["plans"]]
    if len(set(plan_ids)) != len(plan_ids):
        raise DatasetV3Error(f"{where}: duplicate plan ID")
    for plan in target["plans"]:
        if not set(plan["allocations"]).issubset(candidate_by_id):
            raise DatasetV3Error(f"{where}: unknown allocation candidate")
        if not set(plan["evidence_ids"]).issubset(evidence_ids):
            raise DatasetV3Error(f"{where}: unknown plan evidence")
        if not set(plan["dependency_ids"]).issubset(DEPENDENCIES):
            raise DatasetV3Error(f"{where}: unknown dependency")
        allocations = {key: exact(value, where + " allocation")
                       for key, value in plan["allocations"].items()}
        hold = exact(plan["hold"], where + " hold")
        invested = sum(allocations.values(), Decimal(0))
        if invested + hold != amount or hold < immediate or invested > amount * total_cap:
            raise DatasetV3Error(f"{where}: budget/withdrawal/total-risk violation")
        group_exposure = defaultdict(Decimal)
        for candidate_id, allocation in allocations.items():
            candidate = candidate_by_id[candidate_id]
            if allocation <= 0:
                continue
            if not eligible(candidate, need, at):
                raise DatasetV3Error(f"{where}: ineligible candidate allocated")
            if allocation > money(candidate["available_cash"], unit, where + " cash"):
                raise DatasetV3Error(f"{where}: market cash exceeded")
            if not set(candidate["evidence_ids"]).issubset(plan["evidence_ids"]):
                raise DatasetV3Error(f"{where}: allocation lacks candidate evidence")
            for group in candidate["risk_groups"]:
                group_exposure[group] += allocation
        if any(value > amount * group_cap for value in group_exposure.values()):
            raise DatasetV3Error(f"{where}: shared-risk group cap exceeded")
    if row["source_mode"] == "synthetic_fixture":
        from .synthetic_v3 import label
        if target != label(need, candidates):
            raise DatasetV3Error(f"{where}: synthetic label diverges from its exact oracle")


def _need_leaves(need: dict, prefix: str = "need") -> dict:
    result = {}
    for key, value in need.items():
        path = prefix + "." + key
        if isinstance(value, dict):
            result.update(_need_leaves(value, path))
        else:
            result[path] = value
    return result


def _decision(target: dict) -> tuple:
    plans = tuple((plan["plan_id"], tuple(sorted(plan["allocations"].items())),
                   plan["hold"]) for plan in target["plans"])
    return target["mode"], tuple(target["question_fields"]), tuple(target["abstain_reasons"]), plans


def validate(path: Path, schema_path: Path | None = None) -> dict:
    schema = None
    if schema_path is not None:
        from jsonschema import Draft202012Validator, FormatChecker
        schema = Draft202012Validator(json.loads(Path(schema_path).read_text()),
                                      format_checker=FormatChecker())
    rows = []
    ids = set()
    families = defaultdict(list)
    splits = Counter()
    modes = Counter()
    with Path(path).open(encoding="utf-8") as stream:
        for line, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                if not isinstance(row, dict):
                    raise DatasetV3Error("episode must be an object")
                if schema is not None:
                    errors = list(schema.iter_errors(row))
                    if errors:
                        raise DatasetV3Error("schema: " + errors[0].message)
                check_episode(row, line)
            except (KeyError, TypeError, ValueError) as exc:
                raise DatasetV3Error(f"line {line}: {exc}") from exc
            if row["episode_id"] in ids:
                raise DatasetV3Error(f"line {line}: duplicate episode ID")
            ids.add(row["episode_id"])
            rows.append(row)
            families[row["scenario_family"]].append(row)
            splits[row["split"]] += 1
            modes[row["target"]["mode"]] += 1
    if not rows:
        raise DatasetV3Error("empty dataset")
    pairs = Counter()
    for family, items in families.items():
        if len({item["split"] for item in items}) != 1:
            raise DatasetV3Error(f"family {family}: split leakage")
        if items[0]["source_mode"] != "synthetic_fixture":
            if len(items) != 1 or items[0]["parent_episode_id"] is not None:
                raise DatasetV3Error(f"family {family}: API drafts must be singletons")
            continue
        if len(items) != 2:
            raise DatasetV3Error(f"family {family}: expected base and one counterfactual")
        parents = [item for item in items if item["parent_episode_id"] is None]
        if len(parents) != 1:
            raise DatasetV3Error(f"family {family}: one base required")
        base = parents[0]
        other = next(item for item in items if item is not base)
        if base["changed_field"] is not None or base["expected_pair_effect"] is not None:
            raise DatasetV3Error(f"family {family}: base has change metadata")
        if other["parent_episode_id"] != base["episode_id"]:
            raise DatasetV3Error(f"family {family}: parent reference mismatch")
        for key in ("as_of", "candidates", "evidence", "source_mode", "rights_status"):
            if base[key] != other[key]:
                raise DatasetV3Error(f"family {family}: non-need field changed: {key}")
        first, second = _need_leaves(base["need"]), _need_leaves(other["need"])
        changed = [key for key in first if first[key] != second[key]]
        if changed != [other["changed_field"]]:
            raise DatasetV3Error(f"family {family}: change is not exactly one declared need field")
        effect = "changed" if _decision(base["target"]) != _decision(other["target"]) else "unchanged"
        if effect != other["expected_pair_effect"]:
            raise DatasetV3Error(f"family {family}: effect label disagrees with target")
        pairs[effect] += 1
    return {"episodes": len(rows), "families": len(families), "splits": dict(splits),
            "modes": dict(modes), "counterfactual_pairs": dict(pairs),
            "status": "SYNTHETIC_RESEARCH_OR_EXCLUDED_DRAFT_ONLY"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--schema", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(validate(arguments.dataset, arguments.schema), ensure_ascii=False, indent=2))
