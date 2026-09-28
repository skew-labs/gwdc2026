"""Point-in-time market observations from verified raw snapshots.

Fetch time bounds what the system could have known. The provider did not
publish an observation timestamp, so these are not market-event-time bars.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .store import Store


def market_observations(store: Store, *, as_of: datetime | None = None) -> list[dict]:
    cutoff = as_of or datetime.now(timezone.utc)
    if cutoff.tzinfo is None or cutoff.utcoffset() != timezone.utc.utcoffset(cutoff):
        raise ValueError("as_of must be UTC with timezone")
    rows = []
    # ISO ordering is safe because the collector records UTC +00:00 timestamps.
    for snapshot in store.snapshots_for("justlend_markets_v1", cutoff.isoformat()):
        if snapshot["parser_version"] not in {"0.2.0", "0.3.0"}:
            # Older records stamped the request start, before response arrival.
            # They cannot be used for a strict point-in-time cutoff.
            continue
        fetched = datetime.fromisoformat(snapshot["fetched_at"])
        if fetched.tzinfo is None or fetched > cutoff:
            raise ValueError("invalid or future snapshot time")
        payload = store.payload_for(snapshot)  # fail closed on raw corruption
        facts = {(fact["subject_id"], fact["metric_id"]): fact
                 for fact in store.facts_for(snapshot["id"])}
        for market in store.markets_for(snapshot["id"]):
            if market["underlying_symbol"] not in {"USDT", "USDD"}:
                continue
            address = market["market_address"]
            selected = {}
            for metric in ("supply_apy", "available_cash"):
                fact = facts.get((address, metric))
                if fact is None or fact["quality"] not in {"VALID", "VALID_ZERO"}:
                    raise ValueError(f"missing usable historical fact: {metric}")
                store.verify_fact(payload, fact)
                selected[metric] = {"value": fact["canonical_value"],
                                    "unit": fact["unit"],
                                    "quality": fact["quality"],
                                    "json_path": fact["json_path"]}
            rows.append({
                "schema_version": "1.0.0",
                "source_mode": "api_observation",
                "status": "UNREVIEWED_RESEARCH_OBSERVATION",
                "rights_status": "unknown",
                "source_id": snapshot["source_id"],
                "snapshot_id": snapshot["id"],
                "raw_sha256": snapshot["sha256"],
                "fetched_at": snapshot["fetched_at"],
                "source_observed_at": None,
                "source_time_unknown": True,
                "market": market,
                "facts": selected,
            })
    return rows


def write_market_observations(store: Store, path: Path, *,
                              as_of: datetime | None = None) -> dict:
    cutoff = as_of or datetime.now(timezone.utc)
    rows = market_observations(store, as_of=cutoff)
    if not rows:
        raise ValueError("no verified observations through cutoff")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {"path": str(path), "observations": len(rows),
            "snapshots": len({row["snapshot_id"] for row in rows}),
            "as_of": cutoff.isoformat(),
            "status": "UNREVIEWED_RESEARCH_OBSERVATIONS"}
