"""Frozen order-flow research candidates as chart events (research only).

The two MNQ MBO leads in docs/RESEARCH_REPORT.md §5.5 exist only as research
output: ``institutional_behavior_frozen_refresh_*_trades.csv.gz`` under
derived/research (fixed rules, next-bar entry, ATR-blend SL/TP, 60-bar cap).
This module reads the newest such file so the chart can show where each
candidate fired and how it ended, next to its aggregate statistics.  Nothing
here computes a signal: it replays the saved rows, and it never feeds a
backtest or the live engine.
"""

from __future__ import annotations

import csv
import gzip
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.backtest.robustness import series_stats, tick_value
from backend.data import market_data
from backend.timebase import UTC

FROZEN_GLOB = "institutional_behavior_frozen_refresh_*_trades.csv.gz"
STRESS_TICKS = 14
# chart layer key -> (family, context, display name)
CANDIDATES: Dict[str, Tuple[str, str, str]] = {
    "of_passive": ("passive_touch_reject", "value_reversion",
                   "PASSIVE REJECT x VALUE REVERSION"),
    "of_turnover": ("mbo_turnover", "cvd_aligned", "MBO TURNOVER x CVD ALIGNED"),
}

_CACHE: Dict[str, Any] = {"key": None, "value": None}


def latest_frozen_trades(root: Optional[Path] = None) -> Optional[Path]:
    root = root or market_data.derived_path("research")
    if not root.is_dir():
        return None
    files = sorted(root.glob(FROZEN_GLOB), key=lambda p: p.name)
    return files[-1] if files else None


def _stats(pnls: List[float]) -> Dict[str, Any]:
    base = series_stats(pnls)
    charge = STRESS_TICKS * tick_value("MNQ")
    stress = series_stats([p - charge for p in pnls])
    return {"n": base["n"], "pnl": round(base["pnl"], 2), "pf": round(base["pf"], 4),
            "win_rate": round(base["win"], 4), "stress_pf": round(stress["pf"], 4),
            "stress_pnl": round(stress["pnl"], 2)}


def _read(path: Path) -> Dict[str, Any]:
    by_type: Dict[str, List[dict]] = {key: [] for key in CANDIDATES}
    evaluation: set = set()
    latest: set = set()
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    lookup = {(family, context): key for key, (family, context, _) in CANDIDATES.items()}
    for row in rows:
        key = lookup.get((row.get("family"), row.get("context")))
        if key is None:
            continue
        ident = (key, row.get("signal_epoch"), row.get("direction"))
        split = row.get("split")
        if split == "evaluation":
            evaluation.add(ident)
        elif split == "latest_two_sessions":
            latest.add(ident)
        elif split == "all":
            by_type[key].append(row)

    events: List[dict] = []
    summary: Dict[str, Any] = {}
    for key, rows_for_type in by_type.items():
        family, context, name = CANDIDATES[key]
        pnls_all: List[float] = []
        pnls_eval: List[float] = []
        for row in rows_for_type:
            ident = (key, row.get("signal_epoch"), row.get("direction"))
            phase = ("latest" if ident in latest
                     else "evaluation" if ident in evaluation else "discovery")
            pnl = float(row["net_pnl"])
            pnls_all.append(pnl)
            if ident in evaluation:
                pnls_eval.append(pnl)
            epoch = int(float(row["signal_epoch"]))
            events.append({
                "type": key,
                "time": datetime.fromtimestamp(epoch, UTC).isoformat(),
                "date": row.get("date"),
                "direction": "long" if float(row["direction"]) > 0 else "short",
                "entry": float(row["entry_price"]),
                "sl": float(row["sl_price"]),
                "tp": float(row["tp_price"]),
                "exit": float(row["exit_price"]),
                "exit_reason": row.get("exit_reason"),
                "bars_held": int(float(row.get("bars_held") or 0)),
                "net_pnl": round(pnl, 2),
                "rule": row.get("rule"),
                "phase": phase,
            })
        dates = sorted(r.get("date") or "" for r in rows_for_type)
        summary[key] = {
            "name": name, "family": family, "context": context,
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "all": _stats(pnls_all),
            "evaluation": _stats(pnls_eval),
        }
    events.sort(key=lambda e: (e["time"], e["type"]))
    return {"available": True, "source": path.name, "events": events, "summary": summary,
            "note": "Research replay of saved rows (MBO days only); not a live signal."}


def load_orderflow_research_events(root: Optional[Path] = None) -> Dict[str, Any]:
    path = latest_frozen_trades(root)
    if path is None:
        return {"available": False, "events": [], "summary": {},
                "note": "No frozen order-flow research output found."}
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _CACHE["key"] != key:
        _CACHE.update(key=key, value=_read(path))
    return _CACHE["value"]
