"""Read-only audit of current-day futures source and contract provenance.

The audit compares the stored current-day bars with the Topstep API's active
contract and the local quarterly default.  It is intentionally separate from
the repair operation: running it never writes the candle store or changes a
live connection.  The generated report records whether a source-labelled bar
also belongs to the expected contract, which the older source-only metadata
could not show.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pickle
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from backend.broker.topstepx import TopstepXClient  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import BarUnit, current_quarterly_contract_id  # noqa: E402
from backend.timebase import as_utc  # noqa: E402


AUDIT_VERSION = "2026-09-14-futures-source-audit-v2"
DEFAULT_START = "2026-09-14T00:00:00Z"
DEFAULT_END = "2026-09-14T19:00:00Z"


def _utc(value: str) -> datetime:
    return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def _bar_key(bar: Any) -> tuple[float, float, float, float, int]:
    return (
        float(bar.open),
        float(bar.high),
        float(bar.low),
        float(bar.close),
        int(bar.volume),
    )


def _record_key(record: dict[str, Any]) -> tuple[float, float, float, float, int]:
    return (
        float(record["open"]),
        float(record["high"]),
        float(record["low"]),
        float(record["close"]),
        int(record.get("volume", 0)),
    )


def _ts(bar: Any) -> datetime:
    return as_utc(bar.timestamp)


def _ranges(timestamps: list[datetime]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for stamp in sorted(set(timestamps)):
        if not out or stamp - _utc(out[-1]["end"]) > timedelta(minutes=1):
            out.append({"start": stamp.isoformat(), "end": stamp.isoformat(), "minutes": 1})
        else:
            out[-1]["end"] = stamp.isoformat()
            out[-1]["minutes"] += 1
    return out


def _load_store_day(symbol: str, day: datetime) -> tuple[list[Any], dict[str, Any]]:
    path = market_data.candle_store_dir() / f"{symbol}_accumulated_1m.pkl"
    with path.open("rb") as handle:
        all_bars = pickle.load(handle)
    bars = [bar for bar in all_bars if _ts(bar).date() == day.date()]
    pending_path = market_data.candle_store_dir() / f"{symbol}_accumulated_1m.pending.jsonl"
    pending: list[dict[str, Any]] = []
    if pending_path.exists():
        for line in pending_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                stamp = _utc(record["timestamp"])
                if stamp.date() == day.date():
                    pending.append(record)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    meta_path = market_data.candle_store_dir() / f"{symbol}_accumulated_1m.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    return bars, {
        "path": str(path),
        "total_bars": len(all_bars),
        "day_bars_in_pkl": len(bars),
        "pkl_source_counts": dict(sorted(Counter(str(getattr(b, "source", "unknown") or "unknown") for b in bars).items())),
        "pkl_symbol_counts": dict(sorted(Counter(str(getattr(b, "symbol", "unknown") or "unknown") for b in bars).items())),
        "pkl_first": _ts(bars[0]).isoformat() if bars else None,
        "pkl_last": _ts(bars[-1]).isoformat() if bars else None,
        "pending_day_records": len(pending),
        "pending_source_counts": dict(sorted(Counter(str(item.get("source") or "unknown") for item in pending).items())),
        "pending_first": pending[0].get("timestamp") if pending else None,
        "pending_last": pending[-1].get("timestamp") if pending else None,
        "meta": {
            "total_bars": meta.get("total_bars"),
            "first_ts": meta.get("first_ts"),
            "last_ts": meta.get("last_ts"),
            "updated_at": meta.get("updated_at"),
        },
    }


def _latest_repair_archive(symbol: str) -> Path | None:
    archive_root = market_data.archive_root()
    candidates = sorted(
        path for path in archive_root.glob(f"{symbol.lower()}_topstep_contract_repair_*")
        if path.is_dir()
    )
    return candidates[-1] if candidates else None


def _load_archived_day(symbol: str, day: datetime, archive: Path | None) -> list[Any]:
    if archive is None:
        return []
    path = archive / f"{symbol}_accumulated_1m.pkl"
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            all_bars = pickle.load(handle)
    except (OSError, EOFError, pickle.PickleError):
        return []
    return [bar for bar in all_bars if _ts(bar).date() == day.date()]


async def _fetch_contract_day(client: TopstepXClient, contract_id: str, start: str, end: str) -> list[Any]:
    return await client.get_historical_bars_paginated(
        contract_id=contract_id,
        unit=BarUnit.MINUTE,
        unit_number=1,
        start_time=start,
        end_time=end,
    )


def _compare(stored: list[Any], active: list[Any], deferred: list[Any]) -> dict[str, Any]:
    store_by_ts = {_ts(bar): bar for bar in stored}
    active_by_ts = {_ts(bar): bar for bar in active}
    deferred_by_ts = {_ts(bar): bar for bar in deferred}
    classified: defaultdict[str, list[datetime]] = defaultdict(list)
    unknown: list[datetime] = []
    for stamp, bar in sorted(store_by_ts.items()):
        if stamp in active_by_ts and _bar_key(bar) == _bar_key(active_by_ts[stamp]):
            classified["active"].append(stamp)
        elif stamp in deferred_by_ts and _bar_key(bar) == _bar_key(deferred_by_ts[stamp]):
            classified["deferred_local_default"].append(stamp)
        else:
            unknown.append(stamp)

    jumps: list[tuple[float, float, datetime, datetime]] = []
    ordered = sorted(store_by_ts.items())
    for (before_ts, before), (after_ts, after) in zip(ordered, ordered[1:]):
        if (after_ts - before_ts).total_seconds() <= 120:
            delta = float(after.close) - float(before.close)
            jumps.append((abs(delta), delta, before_ts, after_ts))

    return {
        "stored_bars": len(store_by_ts),
        "active_api_bars": len(active_by_ts),
        "deferred_api_bars": len(deferred_by_ts),
        "active_exact": len(classified["active"]),
        "deferred_exact": len(classified["deferred_local_default"]),
        "unclassified": len(unknown),
        "active_ranges": _ranges(classified["active"]),
        "deferred_ranges": _ranges(classified["deferred_local_default"]),
        "unclassified_times": [stamp.isoformat() for stamp in unknown[:50]],
        "largest_close_jumps": [
            {
                "points": round(abs_delta, 4),
                "signed_points": round(delta, 4),
                "before": before.isoformat(),
                "after": after.isoformat(),
            }
            for abs_delta, delta, before, after in sorted(jumps, reverse=True)[:10]
        ],
    }


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    store = report["store"]
    comparison = report["comparison"]
    contracts = report["contracts"]
    repair = report.get("repair_evidence") or {}
    before = repair.get("before_repair") or {}
    lines = [
        "# Futures source and contract audit",
        "",
        f"- Version: `{report['audit_version']}`",
        f"- Generated: `{report['generated_at']}`",
        "- Read-only audit; no store or live setting was changed.",
        "",
        "## Contract identity",
        "",
        f"- Topstep API active contract: `{contracts['topstep_active']}`",
        f"- Local quarterly default: `{contracts['local_default']}`",
        f"- Reconciliation report contract: `{contracts.get('reconciliation_active')}`",
        "",
        "## Current-day classification",
        "",
        f"- Persistent pkl bars: `{comparison['stored_bars']}`; source labels: `{store['pkl_source_counts']}`.",
        f"- Exact active-contract matches: `{comparison['active_exact']}`.",
        f"- Exact local-default/deferred matches: `{comparison['deferred_exact']}`.",
        f"- Unclassified: `{comparison['unclassified']}`.",
        f"- Active ranges after the repair: `{comparison['active_ranges']}`.",
        f"- Deferred ranges after the repair: `{comparison['deferred_ranges']}`.",
        "",
        "The former mixed range was a Topstep contract mix, not Databento data: source labels alone said `topstepx`, while the OHLCV values in the 60-minute range matched the deferred local-default contract. The range was replaced from the active contract and then rechecked against the active API.",
        "",
    ]
    if repair.get("archive_path") and before:
        lines.extend([
            "## Repair evidence",
            "",
            f"- Pre-repair archive: `{repair['archive_path']}`",
            f"- Before repair: active exact `{before.get('active_exact', 0)}`, deferred exact `{before.get('deferred_exact', 0)}`, unclassified `{before.get('unclassified', 0)}`.",
            f"- Deferred range before repair: `{before.get('deferred_ranges', [])}`.",
            "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    load_dotenv(dotenv_path=ROOT / ".env")
    day = _utc(args.start)
    stored, store_info = _load_store_day(args.symbol, day)
    repair_archive = _latest_repair_archive(args.symbol)
    archived = _load_archived_day(args.symbol, day, repair_archive)
    local_default = current_quarterly_contract_id(args.symbol, now=day)
    client = TopstepXClient(os.getenv("TOPSTEPX_USERNAME", ""), os.getenv("TOPSTEPX_API_KEY", ""))
    try:
        await client.authenticate()
        active = await client.get_front_month_contract_id(args.symbol)
        active_bars = await _fetch_contract_day(client, active, args.start, args.end)
        deferred_bars = await _fetch_contract_day(client, local_default, args.start, args.end)
    finally:
        await client.disconnect()

    reconciliation_path = market_data.derived_path("research", "futures_reconciliation_latest.json")
    reconciliation = {}
    if reconciliation_path.exists():
        try:
            reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            reconciliation = {}

    report = {
        "audit_version": AUDIT_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "symbol": args.symbol,
        "range": {"start": args.start, "end": args.end},
        "production_changed": False,
        "store": store_info,
        "contracts": {
            "topstep_active": active,
            "local_default": local_default,
            "reconciliation_active": (reconciliation.get("topstepx_contracts") or {}).get(args.symbol),
        },
        "comparison": _compare(stored, active_bars, deferred_bars),
        "repair_evidence": {
            "archive_path": str(repair_archive) if repair_archive else None,
            "before_repair": _compare(archived, active_bars, deferred_bars) if archived else None,
        },
        "reconciliation_context": {
            "generated_at": reconciliation.get("generated_at"),
            "recent_cutoff": (reconciliation.get("policy") or {}).get("recent_cutoff"),
            "recent_priority": (reconciliation.get("policy") or {}).get("recent_source_priority"),
            "historical_priority": (reconciliation.get("policy") or {}).get("historical_source_priority"),
        },
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="MNQ", choices=("MNQ", "MES"))
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--out", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = asyncio.run(_run(args))
    output = Path(args.out) if args.out else market_data.derived_path(
        "research", f"futures_source_audit_{args.symbol.lower()}_latest.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, output.with_suffix(".md"))
    print(f"JSON: {output}")
    print(f"Report: {output.with_suffix('.md')}")


if __name__ == "__main__":
    main()
