"""Reproducible data release manifests. Nothing is promoted silently."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .store import PARSER_VERSION, Store


def build_manifest(store: Store, source_ids: list[str]) -> dict:
    selected = {}
    for source_id in source_ids:
        row = store.latest(source_id)
        if row is None:
            raise ValueError(f"missing source: {source_id}")
        selected[source_id] = {
            "snapshot_id": row["id"], "sha256": row["sha256"],
            "fetched_at": row["fetched_at"], "url": row["source_url"],
            "parser_version": row["parser_version"],
            "facts": len(store.facts_for(row["id"])),
            "markets": len(store.markets_for(row["id"])),
        }
    body = {"schema_version": "0.1.0", "sources": selected,
            "parser_version": PARSER_VERSION, "calculation_version": "0.2.0"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    release_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"release_id": release_id, "created_at": datetime.now(timezone.utc).isoformat(),
            **body, "status": "CANDIDATE_UNREVIEWED"}


def write_manifest(store: Store, source_ids: list[str]) -> Path:
    manifest = build_manifest(store, source_ids)
    releases = store.root / "releases"
    releases.mkdir(exist_ok=True)
    path = releases / f"{manifest['release_id']}.json"
    if not path.exists():
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return path
