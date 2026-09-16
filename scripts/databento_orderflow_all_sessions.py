"""Build compact all-session MBO caches from already-owned raw DBN days.

This command is deliberately local-only: it never calls Databento and never
overwrites the strategy-facing RTH cache.  Each source file is a complete UTC
calendar-day MBO request, so the derived cache keeps the same boundary and
labels each minute with the shared New-York ASIA/EURO/PRE/RTH/AH session.

Examples::

    python scripts/databento_orderflow_all_sessions.py \
        --start 2026-08-07 --end 2026-09-12
    python scripts/databento_orderflow_all_sessions.py \
        --start 2026-08-07 --end 2026-09-12 --force

Missing raw days are reported, not fabricated.  Historical MBO that is not
already on the primary market-data drive still needs a separately authorised
Databento download before this builder can process it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data, orderflow  # noqa: E402
from scripts.databento_orderflow_download import (  # noqa: E402
    DATASET,
    _base_symbol,
    _instant,
)


def _raw_day_sources(
    source_root: Path,
    symbol: str,
) -> dict[date, list[tuple[str, Path]]]:
    """Index valid one-UTC-day MBO files, retaining contract identity."""
    requested = str(symbol or "auto").strip().upper()
    result: dict[date, list[tuple[str, Path]]] = defaultdict(list)
    if not source_root.is_dir():
        return result
    for metadata_path in source_root.rglob("metadata.json"):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        query = payload.get("query") or payload.get("request") or {}
        if not isinstance(query, dict) or str(query.get("schema") or "") != "mbo":
            continue
        if str(query.get("dataset") or DATASET) != DATASET:
            continue
        symbols = query.get("symbols") or []
        if isinstance(symbols, str):
            symbols = [symbols]
        raw_symbols = [str(value).upper() for value in symbols if str(value).strip()]
        if not raw_symbols:
            continue
        if requested != "AUTO" and requested not in raw_symbols:
            continue
        if requested == "AUTO" and not any(_base_symbol(value) == "MNQ" for value in raw_symbols):
            continue
        start = _instant(query.get("start"))
        end = _instant(query.get("end"))
        if start is None or end is None or end.date() != start.date() + timedelta(days=1):
            continue
        raw_symbol = requested if requested != "AUTO" else next(
            value for value in raw_symbols if _base_symbol(value) == "MNQ"
        )
        files = sorted(metadata_path.parent.glob("*.dbn.zst"))
        if not files:
            files = sorted(metadata_path.parent.glob("*.dbn"))
        for path in files:
            if path.is_file() and path.stat().st_size > 0:
                result[start.date()].append((raw_symbol, path))
                break
    for day in result:
        result[day] = sorted(
            result[day],
            key=lambda item: (item[0], str(item[1]).lower()),
        )
    return result


def _choose_source(
    candidates: list[tuple[str, Path]],
    symbol: str,
) -> tuple[str, Path] | None:
    if not candidates:
        return None
    requested = str(symbol or "auto").strip().upper()
    if requested != "AUTO":
        for candidate in candidates:
            if candidate[0] == requested:
                return candidate
        return None
    configured = os.environ.get("DATABENTO_MNQ_SYMBOL", "").strip().upper()
    if configured:
        for candidate in candidates:
            if candidate[0] == configured:
                return candidate
    # The current raw inventory normally has one contract per UTC day.  If a
    # rollover left two files, keep the result deterministic and report the
    # conflict rather than silently blending separate contract books.
    return candidates[0]


def _valid_all_cache(path: Path, source: Path) -> bool:
    try:
        payload = orderflow.read_footprint_cache(path)
        meta = payload.get("meta") or {}
        return (
            int(meta.get("schema_version") or 0) == orderflow.CACHE_SCHEMA_VERSION
            and str(meta.get("session") or "").upper() == "ALL"
            and str(meta.get("source_file") or "") == source.name
        )
    except (FileNotFoundError, OSError, ValueError, EOFError, TypeError):
        return False


def build_local_caches(
    start: date,
    end: date,
    *,
    symbol: str = "auto",
    root: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Build all-session caches for the requested UTC source-day range."""
    primary = market_data.ensure_layout() if root is None else Path(root).resolve()
    sources = _raw_day_sources(market_data.orderflow_root(primary), symbol)
    report: dict[str, Any] = {
        "version": 1,
        "dataset": DATASET,
        "schema": "mbo",
        "symbol": str(symbol or "auto").upper(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "primary": str(primary),
        "built": [],
        "skipped": [],
        "missing": [],
        "conflicts": [],
        "errors": [],
    }
    current = start
    while current < end:
        candidates = sources.get(current, [])
        chosen = _choose_source(candidates, symbol)
        if chosen is None:
            if candidates:
                report["conflicts"].append({
                    "calendar_date": current.isoformat(),
                    "candidates": [
                        {"symbol": raw, "path": str(path)}
                        for raw, path in candidates
                    ],
                })
            else:
                report["missing"].append(current.isoformat())
            current += timedelta(days=1)
            continue
        raw_symbol, source = chosen
        output = orderflow.all_session_footprint_cache_path(
            current.isoformat(), _base_symbol(raw_symbol), root=primary,
        )
        if output.is_file() and not force and _valid_all_cache(output, source):
            report["skipped"].append({
                "calendar_date": current.isoformat(),
                "symbol": raw_symbol,
                "source": str(source),
                "output": str(output),
            })
            current += timedelta(days=1)
            continue
        try:
            _, payload = orderflow.build_all_session_footprint_file(
                source,
                current.isoformat(),
                symbol=_base_symbol(raw_symbol),
                output_path=output,
            )
            report["built"].append({
                "calendar_date": current.isoformat(),
                "symbol": raw_symbol,
                "source": str(source),
                "output": str(output),
                "bars": len(payload.get("bars") or []),
            })
        except Exception as exc:  # keep independent UTC days auditable
            report["errors"].append({
                "calendar_date": current.isoformat(),
                "source": str(source),
                "error": f"{type(exc).__name__}: {exc}",
            })
        current += timedelta(days=1)

    report["summary"] = {
        "requested_days": (end - start).days,
        "built": len(report["built"]),
        "skipped": len(report["skipped"]),
        "missing": len(report["missing"]),
        "conflicts": len(report["conflicts"]),
        "errors": len(report["errors"]),
    }
    output_root = market_data.derived_path("orderflow", root=primary)
    output_root.mkdir(parents=True, exist_ok=True)
    latest = output_root / "all_session_backfill_latest.json"
    temporary = latest.with_name(latest.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    temporary.replace(latest)
    report["report_path"] = str(latest)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="inclusive UTC date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="exclusive UTC date YYYY-MM-DD")
    parser.add_argument(
        "--symbol", default="auto",
        help="raw Databento symbol, or auto to use the local MNQ inventory",
    )
    parser.add_argument("--force", action="store_true", help="rebuild valid ALL caches")
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end <= start:
        parser.error("--end must be later than --start")
    report = build_local_caches(start, end, symbol=args.symbol, force=args.force)
    summary = report["summary"]
    print(
        f"ALL-session cache complete | built {summary['built']} | "
        f"skipped {summary['skipped']} | missing {summary['missing']} | "
        f"conflicts {summary['conflicts']} | errors {summary['errors']}"
    )
    print(f"Report: {report['report_path']}")
    return 0 if not report["errors"] and not report["conflicts"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
