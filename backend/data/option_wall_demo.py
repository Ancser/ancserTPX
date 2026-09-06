"""Read-only loader for locally derived option-wall chart snapshots.

Five-minute demo payloads are preferred when they exist.  Older purchased
sessions only have one-minute CBBO and hourly option volume, so their already
derived, point-in-time ML feature rows are exposed as an explicitly labelled
hourly fallback instead of leaving the chart blank or pretending the data has
five-minute volume resolution.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "data" / "research" / "option_wall_demo"
DEFAULT_HOURLY_PATH = (
    Path(__file__).resolve().parents[3]
    / "ancserData" / "qqq_option_ml" / "option_wall_ml_dataset.csv.gz"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mapped_level(anchor: float, bps: float | None) -> float | None:
    if bps is None:
        return None
    return anchor * (1.0 + bps / 10_000.0)


@lru_cache(maxsize=4)
def _load_hourly_rows_cached(
    path_text: str, modified_ns: int, size: int,
) -> tuple[dict[str, Any], ...]:
    """Read only entry-time fields from the research feature artifact."""
    del modified_ns, size  # cache-key material
    path = Path(path_text)
    required = {
        "date", "as_of", "as_of_et", "qqq_spot", "mnq_entry",
        "oi_call_wall_bps", "oi_put_wall_bps", "oi_gamma_flip_bps",
        "dashboard_vol_call_wall_bps", "dashboard_vol_put_wall_bps",
        "quality_valid_contracts",
    }
    snapshots: list[dict[str, Any]] = []
    with gzip.open(path, mode="rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                "Option Wall hourly artifact is missing columns: "
                + ", ".join(sorted(missing))
            )
        for raw in reader:
            day = str(raw.get("date") or "")
            as_of = str(raw.get("as_of") or "")
            if not _DATE_RE.fullmatch(day) or not as_of.startswith(day):
                continue
            qqq_spot = _finite(raw.get("qqq_spot"))
            mnq_spot = _finite(raw.get("mnq_entry"))
            if qqq_spot is None or qqq_spot <= 0 or mnq_spot is None or mnq_spot <= 0:
                continue

            # Match the demo/book schedule: OI at 09:35, then the latest fully
            # completed hourly cumulative-volume bar from 10:00 ET onward.
            use_volume = str(raw.get("as_of_et") or "") != "09:35"
            volume_call = _finite(raw.get("dashboard_vol_call_wall_bps"))
            volume_put = _finite(raw.get("dashboard_vol_put_wall_bps"))
            oi_call = _finite(raw.get("oi_call_wall_bps"))
            oi_put = _finite(raw.get("oi_put_wall_bps"))
            call_bps = volume_call if use_volume and volume_call is not None else oi_call
            put_bps = volume_put if use_volume and volume_put is not None else oi_put
            if call_bps is None or put_bps is None:
                continue

            raw_flip_bps = _finite(raw.get("oi_gamma_flip_bps"))
            stable_flip_bps = (
                raw_flip_bps
                if raw_flip_bps is not None and abs(raw_flip_bps) <= 120.0
                else None
            )
            valid_contracts = _finite(raw.get("quality_valid_contracts"))
            snapshots.append({
                "as_of": as_of,
                "qqq_spot": qqq_spot,
                "mnq_spot": mnq_spot,
                "return_beta": 1.0,
                "wall_source": "hourly_volume" if use_volume else "oi",
                "data_resolution": "hourly_from_1m_cbbo_and_1h_volume",
                "cadence_seconds": 3600,
                "valid_contracts": int(valid_contracts) if valid_contracts is not None else None,
                "call_wall_qqq": _mapped_level(qqq_spot, call_bps),
                "put_wall_qqq": _mapped_level(qqq_spot, put_bps),
                "oi_call_wall_qqq": _mapped_level(qqq_spot, oi_call),
                "oi_put_wall_qqq": _mapped_level(qqq_spot, oi_put),
                "volume_call_wall_qqq": _mapped_level(qqq_spot, volume_call),
                "volume_put_wall_qqq": _mapped_level(qqq_spot, volume_put),
                "call_wall_mnq": _mapped_level(mnq_spot, call_bps),
                "put_wall_mnq": _mapped_level(mnq_spot, put_bps),
                "gamma_flip_mnq": _mapped_level(mnq_spot, stable_flip_bps),
                "gamma_flip_quality": (
                    "stable_local" if stable_flip_bps is not None
                    else "remote_unstable" if raw_flip_bps is not None
                    else "no_root"
                ),
            })
    snapshots.sort(key=lambda row: str(row.get("as_of") or ""))
    return tuple(snapshots)


def _load_hourly_rows(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        stat = path.stat()
    except OSError:
        return []
    return list(_load_hourly_rows_cached(str(path.resolve()), stat.st_mtime_ns, stat.st_size))


def _hourly_payload(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    dates = sorted({str(row["as_of"])[:10] for row in rows})
    if not dates:
        return None
    return {
        "available": True,
        "symbol": "MNQ",
        "underlying": "QQQ",
        "date": dates[0] if len(dates) == 1 else f"{dates[0]}..{dates[-1]}",
        "dates": dates,
        "coverage": {"start": dates[0], "end": dates[-1], "sessions": len(dates)},
        "resolution": "hourly snapshots from 1m option quotes and 1h option volume",
        "source": "Databento OPRA.PILLAR; local point-in-time MNQ anchors",
        "paid_cost_usd": None,
        "model": {
            "name": "QQQ 0DTE GEX proxy v2 hourly fallback",
            "sign_assumption": "calls positive; puts negative",
            "wall_schedule": "OI at 09:35 ET; cumulative hourly Volume GEX from 10:00 ET",
            "mapping": "point-in-time QQQ/MNQ return mapping with beta 1.0",
        },
        "pi_signals": [],
        "snapshots": rows,
        "profiles": {},
        "coverage_by_resolution": {
            "hourly": dates,
            "five_minute": [],
        },
    }


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


def load_option_wall_demo(
    date: str | None = None,
    root: Path | None = None,
    hourly_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return the requested local demo without mutating candle or strategy state."""
    source_root = root or DEFAULT_ROOT
    # An injected demo root (tests/research tools) stays hermetic unless its
    # caller also injects an hourly artifact.  Production calls use both
    # defaults and therefore expose all already-purchased history.
    resolved_hourly = hourly_path if hourly_path is not None else (
        DEFAULT_HOURLY_PATH if root is None else None
    )
    if date:
        if not _DATE_RE.fullmatch(date):
            raise ValueError("date must use YYYY-MM-DD")
        path = _resolve_demo_path(date, source_root)
        if path is not None:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("snapshots"), list):
                raise ValueError(f"invalid option-wall demo payload: {path}")
            return payload
        fallback = [
            row for row in _load_hourly_rows(resolved_hourly)
            if str(row.get("as_of") or "").startswith(date)
        ]
        return _hourly_payload(fallback)

    # The chart layer asks for the default feed without a date.  Return every
    # available session so panning across a week/weekend does not silently stop
    # at the newest demo day.
    paths = _demo_paths(source_root)
    detailed = _aggregate_demos(paths)
    detailed_dates = set(detailed.get("dates", [])) if detailed else set()
    fallback_rows = [
        row for row in _load_hourly_rows(resolved_hourly)
        if str(row.get("as_of") or "")[:10] not in detailed_dates
    ]
    fallback = _hourly_payload(fallback_rows)
    if detailed is None:
        return fallback
    if fallback is None:
        detailed["coverage_by_resolution"] = {
            "hourly": [],
            "five_minute": sorted(detailed_dates),
        }
        return detailed

    result = dict(detailed)
    result["snapshots"] = sorted(
        list(fallback["snapshots"]) + list(detailed["snapshots"]),
        key=lambda row: str(row.get("as_of") or ""),
    )
    dates = sorted(detailed_dates | set(fallback["dates"]))
    result["dates"] = dates
    result["date"] = dates[0] if len(dates) == 1 else f"{dates[0]}..{dates[-1]}"
    result["coverage"] = {
        "start": dates[0], "end": dates[-1], "sessions": len(dates),
    }
    result["resolution"] = (
        "5m snapshots where available; hourly fallback from 1m quotes and 1h volume"
    )
    result["coverage_by_resolution"] = {
        "hourly": fallback["dates"],
        "five_minute": sorted(detailed_dates),
    }
    return result
