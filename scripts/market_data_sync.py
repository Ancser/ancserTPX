"""Command-line entry point for the canonical MarketData mirror."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import market_data  # noqa: E402
from backend.data.market_data_sync import (  # noqa: E402
    prune_destination,
    sync_tree,
    verify_tree,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror F:/ancserQuant/ancserMarketData to configured backup roots."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="sync once (the default; explicit for scheduled tasks)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="compare every primary file with every configured mirror",
    )
    parser.add_argument(
        "--hash",
        action="store_true",
        help="use SHA-256 during --verify; reads every byte on every destination",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="after syncing, remove files from mirrors that are absent in primary",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    result = verify_tree(hashes=args.hash) if args.verify else sync_tree()
    if args.prune and not args.verify:
        result["prune"] = {
            str(destination): prune_destination(destination)
            for destination in market_data.configured_backup_roots()
        }
    if not args.verbose and isinstance(result.get("files"), list):
        result["file_count"] = len(result["files"])
        result.pop("files", None)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.verify:
        destinations = result.get("destinations") or {}
        return 0 if all(item.get("ok") for item in destinations.values()) else 2
    prune_reports = result.get("prune") or {}
    prune_failed = any(
        bool(report.get("failed"))
        for report in prune_reports.values()
        if isinstance(report, dict)
    )
    return 0 if result.get("status") == "ok" and not result.get("failed") and not prune_failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
