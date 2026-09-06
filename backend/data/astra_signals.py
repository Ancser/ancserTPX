"""Read-only loader for the Astra research event tape.

Astra is intentionally kept separate from the production PI history.  The
CSV is produced by ``scripts/astra_research.py`` and may live outside the
project because it contains the locally purchased/researched option-wall
join.  Missing files simply make the optional chart layer unavailable.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_DATASET = Path(r"F:\ancserData\astra_2026\astra_event_dataset.csv")


def _path() -> Path:
    return Path(os.getenv("ASTRA_DATASET", str(DEFAULT_DATASET)))


def _utc(value: str) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _float(value: str) -> Optional[float]:
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _int(value: str) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def load_rows(symbol: str = "", start: str = "", end: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return chart-safe Astra rows and source metadata."""
    path = _path()
    meta: dict[str, Any] = {
        "available": path.exists(),
        "dataset": str(path),
        "version": "astra_2026_event_tape_v1",
    }
    if not path.exists():
        return [], meta
    lo = _utc(start) if start else None
    hi = _utc(end) if end else None
    wanted = str(symbol or "").upper()
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            ts = _utc(raw.get("ts", ""))
            if ts is None or (lo and ts < lo) or (hi and ts > hi):
                continue
            future = str(raw.get("future") or "").upper()
            equity = str(raw.get("equity") or "").upper()
            if wanted:
                if wanted.startswith("MNQ") and future != "MNQ":
                    continue
                if wanted.startswith("MES") and future != "MES":
                    continue
            out.append({
                "ts": ts.isoformat(),
                "entry_ts": _utc(raw.get("entry_ts", "")).isoformat()
                if _utc(raw.get("entry_ts", "")) else ts.isoformat(),
                "symbol": equity,
                "equity": equity,
                "future": future,
                "kind": raw.get("kind") or "",
                "size": raw.get("size") or "?",
                "pos": raw.get("pos") or "",
                "direction": _int(raw.get("direction", "")) or 0,
                "source": raw.get("source") or "",
                "message_id": raw.get("message_id") or "",
                # ``stars`` and ``reaction`` are future labels.  They are sent
                # only for visual audit, never interpreted by the strategy.
                "stars": _int(raw.get("stars", "")),
                "reaction": raw.get("reaction") or "",
                "option_available": _bool(raw.get("option_available", "")),
                "wall_above": _bool(raw.get("wall_above", "")),
                "wall_below": _bool(raw.get("wall_below", "")),
                "gex_sign": raw.get("gex_sign") or "",
                "call_wall_mnq": _float(raw.get("call_wall_mnq", "")),
                "put_wall_mnq": _float(raw.get("put_wall_mnq", "")),
                "gamma_flip_mnq": _float(raw.get("gamma_flip_mnq", "")),
                "entry": _float(raw.get("entry", "")),
                "atr_blend": _float(raw.get("atr_blend", "")),
            })
    out.sort(key=lambda row: row["ts"])
    meta.update({
        "total": len(out),
        "canonical": sum(row["source"] == "canonical_history" for row in out),
        "discord_audit": sum(row["source"] == "discord_audit" for row in out),
        "option_feature_rows": sum(bool(row["option_available"]) for row in out),
        "futures": sorted({row["future"] for row in out}),
    })
    return out, meta
