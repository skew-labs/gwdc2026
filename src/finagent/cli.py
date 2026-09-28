"""Command-line entrypoints for the approved remote host."""

import argparse
import json
from pathlib import Path

from .collect import collect_all, source_registry
from .contracts import Need
from .planner import compare
from .release import write_manifest
from .store import Store


ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="GWDC financial data runtime")
    parser.add_argument("--data-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="Fetch allowlisted public read-only sources")
    sub.add_parser("release", help="Write an unreviewed release manifest")
    sub.add_parser("quality", help="Audit latest source snapshots for research reads")
    history = sub.add_parser("export-history", help="Export point-in-time, unreviewed observations")
    history.add_argument("--output", type=Path, required=True)
    history.add_argument("--as-of", help="UTC cutoff, e.g. 2026-09-24T03:00:00+00:00")
    plan = sub.add_parser("plan", help="Compare two research-only allocations")
    plan.add_argument("--need-json", required=True,
                      help='Exact user-confirmed JSON, e.g. {"asset":"USDT",...}')
    episode = sub.add_parser("episode-draft", help="Create excluded, unreviewed API episode")
    episode.add_argument("--need-json", required=True)
    episode.add_argument("--output", type=Path, required=True)
    episode.add_argument("--utterance", help="Authored text; not verified user speech")
    serve = sub.add_parser("serve", help="Serve the local review UI and JSON API")
    serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    store = Store(args.data_dir)
    if args.command == "collect":
        value = collect_all(store, ROOT / "config/sources.json")
    elif args.command == "release":
        ids = [source["id"] for source in source_registry(ROOT / "config/sources.json")]
        value = {"manifest": str(write_manifest(store, ids))}
    elif args.command == "quality":
        from .quality import build_quality_report, write_quality_report
        specs = source_registry(ROOT / "config/sources.json")
        value = build_quality_report(store, specs)
        value["report_path"] = str(write_quality_report(store, value))
    elif args.command == "export-history":
        from datetime import datetime
        from .history import write_market_observations
        value = write_market_observations(store, args.output,
            as_of=datetime.fromisoformat(args.as_of) if args.as_of else None)
    elif args.command == "plan":
        value = compare(store, Need.from_json(json.loads(args.need_json)))
    elif args.command == "episode-draft":
        from .episode import draft_episode, write_draft
        record = draft_episode(store, Need.from_json(json.loads(args.need_json)),
                               args.utterance)
        value = {"episode_draft": str(write_draft(args.output, record)),
                 "split": record["split"], "review_status": record["review_status"]}
    else:
        from .server import serve as run_server
        run_server(store, ROOT / "web", args.port)
        return
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
