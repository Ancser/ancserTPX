"""Read-only loader for locally derived option-wall demo snapshots."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "data" / "research" / "option_wall_demo"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _resolve_demo_path(date: str | None, root: Path) -> Path | None:
    if date:
        if not _DATE_RE.fullmatch(date):
            raise ValueError("date must use YYYY-MM-DD")
        candidate = root / date / "derived.json"
        return candidate if candidate.is_file() else None
    candidates = sorted(root.glob("*/derived.json"), reverse=True)
    return candidates[0] if candidates else None


def _demo_paths(root: Path) -> list[Path]:
    """Return valid date folders in chronological order.

    ``glob`` is deliberately kept behind this small helper so the aggregate
    response and the explicit-date response share the same filesystem boundary.
    The loader only reads derived JSON; raw option exports remain outside the
    application tree.
    """
    return sorted(
        (path for path in root.glob("*/derived.json") if path.is_file()),
        key=lambda path: path.parent.name,
    )


def _aggregate_demos(paths: list[Path]) -> dict[str, Any] | None:
    """Merge one compact option-wall payload per session into one chart feed."""
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("snapshots"), list):
            loaded.append((path, payload))
    payloads = [payload for _, payload in loaded]
    if not payloads:
        return None

    newest = payloads[-1]
    dates = [str(payload.get("date") or path.parent.name) for path, payload in loaded]
    # A duplicate/legacy folder should not make the coverage label ambiguous.
    dates = sorted({date for date in dates if _DATE_RE.fullmatch(date)})

    snapshots: list[dict[str, Any]] = []
    seen_snapshots: set[tuple[Any, ...]] = set()
    for payload in payloads:
        for row in payload.get("snapshots", []):
            if not isinstance(row, dict):
                continue
            key = (row.get("as_of"), row.get("call_wall_mnq"), row.get("put_wall_mnq"))
            if key in seen_snapshots:
                continue
            seen_snapshots.add(key)
            snapshots.append(row)
    snapshots.sort(key=lambda row: str(row.get("as_of") or ""))

    pi_signals: list[dict[str, Any]] = []
    seen_pi: set[tuple[Any, ...]] = set()
    for payload in payloads:
        for row in payload.get("pi_signals", []):
            if not isinstance(row, dict):
                continue
            key = (
                row.get("ts"), row.get("side"), row.get("kind"),
                row.get("size"), row.get("position"),
            )
            if key in seen_pi:
                continue
            seen_pi.add(key)
            pi_signals.append(row)
    pi_signals.sort(key=lambda row: str(row.get("ts") or ""))

    profiles: dict[str, Any] = {}
    for payload in payloads:
        if isinstance(payload.get("profiles"), dict):
            profiles.update(payload["profiles"])

    # Per-day builders may repeat the purchase manifest cost.  Sum distinct
    # positive values so the label is not multiplied by the number of sessions.
    costs: set[float] = set()
    for payload in payloads:
        try:
            cost = float(payload.get("paid_cost_usd"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(cost) and cost > 0:
            costs.add(round(cost, 12))

    aggregate = dict(newest)
    aggregate["date"] = dates[0] if len(dates) == 1 else f"{dates[0]}..{dates[-1]}"
    aggregate["dates"] = dates
    aggregate["coverage"] = {
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "sessions": len(dates),
    }
    aggregate["paid_cost_usd"] = round(sum(costs), 12)
    aggregate["snapshots"] = snapshots
    aggregate["pi_signals"] = pi_signals
    aggregate["profiles"] = profiles
    return aggregate


def load_option_wall_demo(date: str | None = None, root: Path | None = None) -> dict[str, Any] | None:
    """Return the requested local demo without mutating candle or strategy state."""
    source_root = root or DEFAULT_ROOT
    if date:
        path = _resolve_demo_path(date, source_root)
        if path is None:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), list):
            raise ValueError(f"invalid option-wall demo payload: {path}")
        return payload

    # The chart layer asks for the default feed without a date.  Return every
    # available session so panning across a week/weekend does not silently stop
    # at the newest demo day.
    paths = _demo_paths(source_root)
    return _aggregate_demos(paths)
