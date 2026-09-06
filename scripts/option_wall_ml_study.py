"""Point-in-time QQQ option-wall ML research.

This script is deliberately independent from the ancserTPX strategy and live
execution paths.  It can acquire a resumable, cost-capped historical dataset,
build opening/hourly option-wall features, and train chronological direction
classifiers.  Nothing in this file can place an order.

The expensive OPRA ``ohlcv-1m`` request used by the chart demo is not required
for hourly research.  This study uses ``ohlcv-1h`` for cumulative volume GEX,
``cbbo-1m`` for point-in-time IV/gamma, and official OI statistics.  Every
feature row is stamped at the first instant at which all of its inputs were
available; labels only use later QQQ/MNQ bars.

Examples (``end`` is exclusive)::

    python scripts/option_wall_ml_study.py acquire \
        --start 2025-09-04 --end 2026-09-04 --max-cost 65
    python scripts/option_wall_ml_study.py build
    python scripts/option_wall_ml_study.py train
    python scripts/option_wall_ml_study.py all \
        --start 2025-09-04 --end 2026-09-04 --max-cost 65

Raw/licensed output and fitted artifacts default to
``F:/ancserQuant/ancserData/qqq_option_ml`` and remain outside the repository.
MNQ labels default to the same stitched multi-year 1-minute store used by
ancserTPX; a separately acquired raw continuous file is only a fallback.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys
import time as time_module
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import joblib
import numpy as np
import pandas as pd
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks, peak_prominences
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, f1_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.db.models import get_commission_rt, get_fees_rt, get_point_value
from scripts.option_wall_demo import (
    _contract_profile,
    _gamma_flip,
    _gamma_flip_from_profile,
    _gamma_price_profile,
    _profile_by_strike,
)


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
DEFAULT_DATA_ROOT = ROOT.parent / "ancserData" / "qqq_option_ml"
DEFAULT_MNQ_PATH = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
RESEARCH_MNQ_FILE = "mnq_v0_ohlcv_1m.csv.gz"
FEATURE_VERSION = 2
SCHEMA_FILES = {
    "statistics": "qqq_0dte_statistics.csv.gz",
    "cbbo-1m": "qqq_0dte_cbbo_1m.csv.gz",
    "ohlcv-1h": "qqq_0dte_ohlcv_1h.csv.gz",
    "qqq-ohlcv-1m": "qqq_ohlcv_1m.csv.gz",
}
NON_FEATURE_COLUMNS = {
    "feature_version", "date", "as_of", "as_of_et", "future_60m_at", "close_at",
    "future_30m_at", "label_30m", "label_60m", "label_close",
    "target_expansion_30m", "target_wall_first_30m",
    "target_wall_hit_minutes_30m", "qqq_future_return_bps_30m",
    "qqq_future_max_up_bps_30m", "qqq_future_max_down_bps_30m",
    "qqq_future_range_bps_30m", "qqq_future_directional_efficiency_30m",
    "qqq_future_return_bps_60m",
    "qqq_future_return_bps_close", "mnq_entry", "mnq_exit_60m",
    "mnq_exit_30m", "mnq_exit_close", "mnq_points_30m", "mnq_points_60m",
    "mnq_points_close",
}


def _peak_feature_names(prefix: str) -> set[str]:
    names = {
        f"{prefix}_gross_log", f"{prefix}_call_share", f"{prefix}_net_balance",
        f"{prefix}_upper_share", f"{prefix}_lower_share", f"{prefix}_side_imbalance",
        f"{prefix}_center_bps", f"{prefix}_dispersion_bps",
        f"{prefix}_peak_count_20pct",
    }
    for rank in range(1, 4):
        names.update({
            f"{prefix}_peak{rank}_bps", f"{prefix}_peak{rank}_share",
            f"{prefix}_peak{rank}_prominence",
        })
    return names


# Keep the original 42-feature experiment reproducible after richer columns are
# added to the same point-in-time dataset.  New research must opt into a named
# family rather than silently changing the old wall-only baseline.
LEGACY_WALL_FEATURES = frozenset(
    {"minutes_since_open", "minutes_to_close", "quality_valid_contracts",
     "oi_call_wall_bps", "oi_put_wall_bps", "oi_gamma_flip_bps"}
    | _peak_feature_names("oi")
    | _peak_feature_names("vol")
)
LEGACY_PRICE_FEATURES = frozenset({
    "price_return_from_open_bps", "price_return_30m_bps",
    "price_return_60m_bps", "price_realized_vol_60m_bps",
})
ABLATION_FEATURE_SETS = (
    "legacy_wall", "dashboard", "article_state", "combined_0dte",
)


@dataclass(frozen=True)
class RequestSpec:
    name: str
    dataset: str
    schema: str
    start: str
    end: str
    symbols: str | list[str]
    stype_in: str
    output: Path

    def kwargs(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "schema": self.schema,
            "start": self.start,
            "end": self.end,
            "symbols": self.symbols,
            "stype_in": self.stype_in,
        }


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _session_bounds(day: date) -> tuple[datetime, datetime, datetime]:
    open_et = datetime.combine(day, time(9, 30), tzinfo=NY)
    close_et = datetime.combine(day, time(16, 0), tzinfo=NY)
    option_close_et = datetime.combine(day, time(16, 15), tzinfo=NY)
    return open_et.astimezone(UTC), close_et.astimezone(UTC), option_close_et.astimezone(UTC)


def _sample_times(day: date) -> list[datetime]:
    # 09:35 avoids the least reliable opening quotes.  Whole-hour samples make
    # UTC-clock OHLCV-1h bars causal: a bar is included only after its end.
    values = [time(9, 35), time(10), time(11), time(12), time(13), time(14), time(15)]
    return [datetime.combine(day, value, tzinfo=NY).astimezone(UTC) for value in values]


def _load_dotenv_key(env_path: Path) -> str:
    value = os.getenv("DATABENTO_API_KEY", "").strip()
    if value:
        return value
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith("#") or "=" not in line:
                continue
            key, raw = line.split("=", 1)
            if key.strip() == "DATABENTO_API_KEY":
                value = raw.strip().strip('"').strip("'")
                if value:
                    return value
    raise RuntimeError("DATABENTO_API_KEY is missing from the environment and .env")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp.gz")
    frame.to_csv(temp, index=False, compression="gzip")
    os.replace(temp, path)


def _normalise_databento_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.reset_index() if frame.index.names != [None] else frame.copy()
    # Some SDK versions expose the mapped symbol as an index level.
    if "symbol" not in result.columns and "raw_symbol" in result.columns:
        result["symbol"] = result["raw_symbol"]
    return result


def _download_frame(client: Any, spec: RequestSpec, retries: int = 4) -> pd.DataFrame:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            store = client.timeseries.get_range(**spec.kwargs())
            return _normalise_databento_frame(store.to_df())
        except Exception as exc:  # network/provider errors are retried, logic errors surface later
            last_error = exc
            if attempt + 1 < retries:
                time_module.sleep(min(2 ** attempt, 8))
    assert last_error is not None
    raise last_error


def _definition_spec(day: date, folder: Path) -> RequestSpec:
    # Definitions and OI are normally published before the cash open.  A broad
    # UTC window is intentionally used so the same rule survives EST and EDT.
    # Databento definition snapshots require a UTC-midnight start; beginning
    # later can omit instruments that were already effective at midnight.
    start = datetime.combine(day, time(0, 0), tzinfo=UTC)
    end = _session_bounds(day)[0]
    return RequestSpec(
        name="definition", dataset="OPRA.PILLAR", schema="definition",
        start=_iso(start), end=_iso(end), symbols="QQQ.OPT", stype_in="parent",
        output=folder / "qqq_definition.csv.gz",
    )


def _zero_dte_symbols(definitions: pd.DataFrame, day: date) -> list[str]:
    required = {"expiration", "instrument_class", "raw_symbol"}
    missing = required - set(definitions.columns)
    if missing:
        raise ValueError(f"definition data missing columns: {sorted(missing)}")
    expiry = pd.to_datetime(definitions["expiration"], utc=True, errors="coerce")
    rows = definitions[
        (expiry.dt.date == day)
        & definitions["instrument_class"].isin(["C", "P"])
    ]
    return sorted(rows["raw_symbol"].dropna().astype(str).unique().tolist())


def _daily_specs(day: date, folder: Path, symbols: list[str], with_hourly_volume: bool) -> list[RequestSpec]:
    open_utc, close_utc, _ = _session_bounds(day)
    stats_start = open_utc - timedelta(hours=5)
    specs = [
        RequestSpec(
            "statistics", "OPRA.PILLAR", "statistics", _iso(stats_start), _iso(open_utc),
            symbols, "raw_symbol", folder / SCHEMA_FILES["statistics"],
        ),
        RequestSpec(
            "cbbo-1m", "OPRA.PILLAR", "cbbo-1m", _iso(open_utc), _iso(close_utc),
            symbols, "raw_symbol", folder / SCHEMA_FILES["cbbo-1m"],
        ),
    ]
    if with_hourly_volume:
        volume_start = open_utc.replace(minute=0, second=0, microsecond=0)
        specs.append(RequestSpec(
            "ohlcv-1h", "OPRA.PILLAR", "ohlcv-1h", _iso(volume_start), _iso(close_utc),
            symbols, "raw_symbol", folder / SCHEMA_FILES["ohlcv-1h"],
        ))
    specs.append(RequestSpec(
        "qqq-ohlcv-1m", "EQUS.MINI", "ohlcv-1m", _iso(open_utc), _iso(close_utc),
        "QQQ", "raw_symbol", folder / SCHEMA_FILES["qqq-ohlcv-1m"],
    ))
    return specs


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": 1, "requests": [], "days": {}, "incremental_quoted_cost": 0.0}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid acquisition manifest: {path}")
    payload.setdefault("requests", [])
    payload.setdefault("days", {})
    payload.setdefault("incremental_quoted_cost", 0.0)
    return payload


def _record_request(manifest_path: Path, manifest: dict[str, Any], spec: RequestSpec,
                    quoted_cost: float, records: int, status: str, rows: int = 0,
                    error: str | None = None) -> None:
    row = {
        "day": spec.output.parent.name,
        "name": spec.name,
        "dataset": spec.dataset,
        "schema": spec.schema,
        "start": spec.start,
        "end": spec.end,
        "file": str(spec.output),
        "quoted_cost": quoted_cost,
        "quoted_records": records,
        "downloaded_rows": rows,
        "status": status,
        "updated_at": _iso(datetime.now(UTC)),
    }
    if error:
        row["error"] = error
    manifest["requests"].append(row)
    manifest["incremental_quoted_cost"] = round(
        float(manifest.get("incremental_quoted_cost", 0.0))
        + (quoted_cost if status in {"reserved", "downloaded", "failed_after_request"} else 0.0),
        12,
    )
    _atomic_json(manifest_path, manifest)


def _is_closed_session_metadata_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "symbology_invalid_request" in message
        and "none of the symbols could be resolved" in message
    )


def acquire_history(data_root: Path, start: date, end: date, max_cost: float,
                    with_hourly_volume: bool = True, max_days: int | None = None) -> dict[str, Any]:
    """Download the most recent contiguous weekday history without exceeding ``max_cost``.

    The loop runs newest-to-oldest.  Each provider request is quoted first and
    reserved in the manifest before bytes are requested.  An interrupted file
    is never silently retried because the original stream may already have
    incurred usage fees.
    """
    import databento as db

    if end <= start:
        raise ValueError("end must be after start")
    if max_cost <= 0:
        raise ValueError("max_cost must be positive")
    client = db.Historical(_load_dotenv_key(ROOT / ".env"))
    raw_root = data_root / "raw"
    manifest_path = data_root / "acquisition_manifest.json"
    manifest = _load_manifest(manifest_path)
    spent = 0.0
    completed_days = 0

    days = [ts.date() for ts in pd.date_range(start, end - timedelta(days=1), freq="B")]
    for day in reversed(days):
        if max_days is not None and completed_days >= max_days:
            break
        # OPRA intraday records become historical roughly 24 hours after the
        # session begins.  Skip (rather than fail) a newest session that still
        # requires a live license; a later resume will pick it up.
        open_utc = _session_bounds(day)[0]
        if open_utc > datetime.now(UTC) - timedelta(hours=24):
            manifest["days"][day.isoformat()] = {
                "status": "historical_delay_wait",
                "retry_after": _iso(open_utc + timedelta(hours=24)),
            }
            _atomic_json(manifest_path, manifest)
            continue
        folder = raw_root / day.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        definition = _definition_spec(day, folder)
        if not definition.output.is_file():
            try:
                def_cost = float(client.metadata.get_cost(**definition.kwargs()))
                def_records = int(client.metadata.get_record_count(**definition.kwargs()))
            except Exception as exc:
                if _is_closed_session_metadata_error(exc):
                    manifest["days"][day.isoformat()] = {
                        "status": "market_closed", "provider_message": str(exc),
                    }
                    _atomic_json(manifest_path, manifest)
                    print(f"{day} market closed; skipped", flush=True)
                    continue
                raise
            if def_records == 0:
                manifest["days"][day.isoformat()] = {"status": "no_definition_records"}
                _atomic_json(manifest_path, manifest)
                continue
            if spent + def_cost > max_cost:
                break
            _record_request(manifest_path, manifest, definition, def_cost, def_records, "reserved")
            spent += def_cost
            try:
                frame = _download_frame(client, definition)
                _atomic_csv(definition.output, frame)
            except Exception as exc:
                _record_request(manifest_path, manifest, definition, 0.0, def_records,
                                "failed_after_request", error=str(exc))
                raise
        definitions = pd.read_csv(definition.output, compression="gzip")
        symbols = _zero_dte_symbols(definitions, day)
        if not symbols:
            manifest["days"][day.isoformat()] = {"status": "no_0dte_symbols"}
            _atomic_json(manifest_path, manifest)
            continue

        specs = _daily_specs(day, folder, symbols, with_hourly_volume)
        pending = [spec for spec in specs if not spec.output.is_file()]
        quote_rows: list[tuple[RequestSpec, float, int]] = []
        for spec in pending:
            cost = float(client.metadata.get_cost(**spec.kwargs()))
            count = int(client.metadata.get_record_count(**spec.kwargs()))
            quote_rows.append((spec, cost, count))
        day_cost = sum(cost for _, cost, _ in quote_rows)
        if spent + day_cost > max_cost:
            manifest["days"][day.isoformat()] = {
                "status": "budget_stop", "quoted_cost": day_cost,
                "remaining": max_cost - spent,
            }
            _atomic_json(manifest_path, manifest)
            break

        for spec, cost, count in quote_rows:
            _record_request(manifest_path, manifest, spec, cost, count, "reserved")
            spent += cost
            try:
                frame = _download_frame(client, spec)
                _atomic_csv(spec.output, frame)
                _record_request(manifest_path, manifest, spec, 0.0, count,
                                "downloaded", rows=len(frame))
            except Exception as exc:
                _record_request(manifest_path, manifest, spec, 0.0, count,
                                "failed_after_request", error=str(exc))
                raise
        manifest["days"][day.isoformat()] = {
            "status": "complete",
            "symbols": len(symbols),
            "incremental_quoted_cost": round(day_cost, 12),
        }
        _atomic_json(manifest_path, manifest)
        completed_days += 1
        print(f"{day} complete | {len(symbols)} contracts | run quote ${spent:.4f}", flush=True)

    result = {
        "completed_days_this_run": completed_days,
        "incremental_quoted_cost_this_run": round(spent, 12),
        "max_cost": max_cost,
        "data_root": str(data_root),
    }
    manifest["last_run"] = result | {"finished_at": _iso(datetime.now(UTC))}
    _atomic_json(manifest_path, manifest)
    return result


def acquire_mnq_history(data_root: Path, start: date, end: date, max_cost: float) -> dict[str, Any]:
    """Acquire the independent MNQ volume-roll series used only for trade scoring."""
    import databento as db

    output = data_root / "raw_mnq" / RESEARCH_MNQ_FILE
    if output.is_file():
        return {"status": "reused", "file": str(output), "incremental_quoted_cost": 0.0}
    client = db.Historical(_load_dotenv_key(ROOT / ".env"))
    spec = RequestSpec(
        "mnq-ohlcv-1m", "GLBX.MDP3", "ohlcv-1m",
        date.isoformat(start), date.isoformat(end), ["MNQ.v.0"], "continuous", output,
    )
    cost = float(client.metadata.get_cost(**spec.kwargs()))
    records = int(client.metadata.get_record_count(**spec.kwargs()))
    if cost > max_cost:
        raise RuntimeError(f"MNQ quote ${cost:.4f} exceeds --max-cost ${max_cost:.4f}")
    manifest_path = data_root / "mnq_acquisition_manifest.json"
    manifest = {
        "version": 1, "status": "reserved", "quoted_cost": cost,
        "quoted_records": records, "request": asdict_request(spec),
        "updated_at": _iso(datetime.now(UTC)),
    }
    _atomic_json(manifest_path, manifest)
    try:
        frame = _download_frame(client, spec)
        _atomic_csv(output, frame)
    except Exception as exc:
        manifest.update(status="failed_after_request", error=str(exc),
                        updated_at=_iso(datetime.now(UTC)))
        _atomic_json(manifest_path, manifest)
        raise
    manifest.update(status="complete", downloaded_rows=len(frame),
                    updated_at=_iso(datetime.now(UTC)))
    _atomic_json(manifest_path, manifest)
    return {"status": "complete", "file": str(output),
            "rows": len(frame), "incremental_quoted_cost": cost}


def asdict_request(spec: RequestSpec) -> dict[str, Any]:
    """Serialize request metadata without leaking credentials or huge symbol lists."""
    return {
        "name": spec.name, "dataset": spec.dataset, "schema": spec.schema,
        "start": spec.start, "end": spec.end, "stype_in": spec.stype_in,
        "symbols": spec.symbols if isinstance(spec.symbols, str) else list(spec.symbols),
        "output": str(spec.output),
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _peak_summary(profile: pd.DataFrame, spot: float, column: str, prefix: str,
                  max_moneyness: float = 0.04, smooth_sigma: float = 1.0) -> dict[str, float]:
    grouped = _profile_by_strike(profile)
    if grouped.empty or column not in grouped.columns:
        return {}
    pivot = grouped.pivot_table(index="strike", columns="class", values=column,
                                aggfunc="sum", fill_value=0.0)
    call = pivot.get("C", pd.Series(0.0, index=pivot.index)).astype(float).clip(lower=0.0)
    put = pivot.get("P", pd.Series(0.0, index=pivot.index)).astype(float).abs()
    strikes = pivot.index.to_numpy(dtype=float)
    mask = np.abs(strikes / spot - 1.0) <= max_moneyness
    strikes, calls, puts = strikes[mask], call.to_numpy()[mask], put.to_numpy()[mask]
    if len(strikes) < 3:
        return {}
    order = np.argsort(strikes)
    strikes, calls, puts = strikes[order], calls[order], puts[order]
    depth = calls + puts
    gross = float(depth.sum())
    if gross <= 0:
        return {}

    positive_steps = np.diff(strikes)
    positive_steps = positive_steps[positive_steps > 0]
    step = float(np.clip(np.median(positive_steps), 0.25, 1.0)) if len(positive_steps) else 1.0
    grid = np.arange(strikes.min(), strikes.max() + step * 0.5, step)
    grid_depth = np.interp(grid, strikes, depth, left=0.0, right=0.0)
    curve = gaussian_filter1d(grid_depth / gross, smooth_sigma, mode="nearest")
    found, _ = find_peaks(curve)
    if not len(found) or int(np.argmax(curve)) not in found:
        found = np.unique(np.append(found, int(np.argmax(curve))))
    prominences = peak_prominences(curve, found)[0] if len(found) else np.array([])
    peaks = sorted(
        (
            {
                "strike": float(grid[idx]),
                "share": float(curve[idx]),
                "prominence": float(prominences[pos]),
            }
            for pos, idx in enumerate(found)
        ),
        key=lambda row: (row["share"], row["prominence"]),
        reverse=True,
    )

    upper = float(depth[strikes > spot].sum())
    lower = float(depth[strikes < spot].sum())
    call_total, put_total = float(calls.sum()), float(puts.sum())
    center = float(np.dot(strikes, depth) / gross)
    variance = float(np.dot((strikes - center) ** 2, depth) / gross)
    strongest = peaks[0]
    result = {
        f"{prefix}_gross_log": math.log1p(gross),
        f"{prefix}_call_share": call_total / gross,
        f"{prefix}_net_balance": (call_total - put_total) / gross,
        f"{prefix}_upper_share": upper / gross,
        f"{prefix}_lower_share": lower / gross,
        f"{prefix}_side_imbalance": (upper - lower) / max(upper + lower, 1e-12),
        f"{prefix}_center_bps": (center / spot - 1.0) * 10_000.0,
        f"{prefix}_dispersion_bps": math.sqrt(max(variance, 0.0)) / spot * 10_000.0,
        f"{prefix}_peak_count_20pct": float(sum(p["share"] >= strongest["share"] * 0.20 for p in peaks)),
    }
    for rank in range(3):
        peak = peaks[rank] if rank < len(peaks) else None
        result[f"{prefix}_peak{rank + 1}_bps"] = (
            (peak["strike"] / spot - 1.0) * 10_000.0 if peak else 0.0
        )
        result[f"{prefix}_peak{rank + 1}_share"] = peak["share"] if peak else 0.0
        result[f"{prefix}_peak{rank + 1}_prominence"] = peak["prominence"] if peak else 0.0
    return result


def _signed_log1p(value: float) -> float:
    return math.copysign(math.log1p(abs(float(value))), float(value))


def extract_dashboard_features(
    profile: pd.DataFrame,
    spot: float,
    as_of: datetime,
    expiry: datetime,
    gamma_profiles: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
) -> dict[str, float]:
    """Expose the non-redundant quantities behind a GEX dashboard.

    Dollar GEX is compressed with a signed/log transform for modelling.  OI is
    the overnight position map; volume remains an unsigned activity proxy.
    """
    result: dict[str, float] = {}
    if profile.empty:
        return result
    scoped = profile[
        np.abs(pd.to_numeric(profile["strike"], errors="coerce") / spot - 1.0) <= 0.04
    ].copy()
    if scoped.empty:
        return result

    for name, column in (("oi", "oi_gex"), ("vol", "volume_gex")):
        if column not in scoped.columns:
            continue
        calls = pd.to_numeric(
            scoped.loc[scoped["class"] == "C", column], errors="coerce",
        ).clip(lower=0.0)
        puts = pd.to_numeric(
            scoped.loc[scoped["class"] == "P", column], errors="coerce",
        ).abs()
        call_total = float(calls.sum())
        put_total = float(puts.sum())
        gross = call_total + put_total
        net = call_total - put_total
        result.update({
            f"dashboard_{name}_call_gex_log": math.log1p(call_total),
            f"dashboard_{name}_put_gex_log": math.log1p(put_total),
            f"dashboard_{name}_net_gex_signed_log": _signed_log1p(net),
            f"dashboard_{name}_total_gex_log": math.log1p(gross),
            f"dashboard_{name}_call_put_ratio_log": math.log(
                (call_total + 1.0) / (put_total + 1.0)
            ),
        })

    oi = pd.to_numeric(scoped.get("oi", 0.0), errors="coerce").fillna(0.0).clip(lower=0.0)
    call_oi = float(oi[scoped["class"].to_numpy() == "C"].sum())
    put_oi = float(oi[scoped["class"].to_numpy() == "P"].sum())
    total_oi = call_oi + put_oi
    result.update({
        "dashboard_call_oi_log": math.log1p(call_oi),
        "dashboard_put_oi_log": math.log1p(put_oi),
        "dashboard_total_oi_log": math.log1p(total_oi),
        "dashboard_call_oi_share": call_oi / total_oi if total_oi > 0 else 0.5,
    })

    grouped = _profile_by_strike(profile)
    if not grouped.empty and "volume_gex" in grouped.columns:
        calls = grouped[grouped["class"] == "C"]
        puts = grouped[grouped["class"] == "P"]
        gross_volume = float(pd.to_numeric(grouped["volume_gex"], errors="coerce").abs().sum())
        if gross_volume > 0 and not calls.empty:
            row = calls.loc[calls["volume_gex"].idxmax()]
            result["dashboard_vol_call_wall_bps"] = (float(row["strike"]) / spot - 1.0) * 10_000.0
            result["dashboard_vol_call_wall_share"] = (
                abs(float(row["volume_gex"])) / gross_volume if gross_volume > 0 else 0.0
            )
        if gross_volume > 0 and not puts.empty:
            row = puts.loc[puts["volume_gex"].idxmin()]
            result["dashboard_vol_put_wall_bps"] = (float(row["strike"]) / spot - 1.0) * 10_000.0
            result["dashboard_vol_put_wall_share"] = (
                abs(float(row["volume_gex"])) / gross_volume if gross_volume > 0 else 0.0
            )

    years = max((expiry - as_of).total_seconds(), 1.0) / (365.0 * 24.0 * 3600.0)
    if gamma_profiles is not None and "volume" in gamma_profiles:
        volume_flip = _gamma_flip_from_profile(*gamma_profiles["volume"], spot)
    else:
        volume_flip = _gamma_flip(profile, spot, years, weight_column="volume")
    result["dashboard_vol_gamma_flip_proxy_bps"] = (
        (float(volume_flip) / spot - 1.0) * 10_000.0
        if volume_flip is not None else math.nan
    )
    return result


def _gamma_profile_shape(
    profile: pd.DataFrame,
    spot: float,
    years: float,
    weight_column: str,
    prefix: str,
    prepared: tuple[np.ndarray, np.ndarray] | None = None,
) -> dict[str, float]:
    grid, totals = (
        prepared if prepared is not None
        else _gamma_price_profile(profile, spot, years, weight_column)
    )
    if not len(grid):
        return {}
    x_bps = (grid / spot - 1.0) * 10_000.0
    center_i = int(np.argmin(np.abs(x_bps)))
    left_i = int(np.argmin(np.abs(x_bps + 50.0)))
    right_i = int(np.argmin(np.abs(x_bps - 50.0)))
    gex_column = "oi_gex" if weight_column == "oi" else "volume_gex"
    gross = float(pd.to_numeric(profile.get(gex_column, 0.0), errors="coerce").abs().sum())
    scale = max(gross, 1e-12)
    signs = np.sign(totals)
    crossings = int(np.sum(signs[1:] * signs[:-1] < 0))
    return {
        f"article_{prefix}_profile_slope_100bps": float(
            (totals[right_i] - totals[left_i]) / scale
        ),
        f"article_{prefix}_profile_curvature_50bps": float(
            (totals[right_i] - 2.0 * totals[center_i] + totals[left_i]) / scale
        ),
        f"article_{prefix}_profile_zero_crossings": float(crossings),
    }


def extract_article_option_features(
    profile: pd.DataFrame,
    spot: float,
    as_of: datetime,
    expiry: datetime,
    gamma_profiles: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    if profile.empty:
        return result
    years = max((expiry - as_of).total_seconds(), 1.0) / (365.0 * 24.0 * 3600.0)
    prepared = gamma_profiles or {}
    result.update(_gamma_profile_shape(
        profile, spot, years, "oi", "oi_gex", prepared.get("oi"),
    ))
    result.update(_gamma_profile_shape(
        profile, spot, years, "volume", "vol_gex", prepared.get("volume"),
    ))

    scoped = profile[
        np.abs(pd.to_numeric(profile["strike"], errors="coerce") / spot - 1.0) <= 0.015
    ].copy()
    scoped["iv"] = pd.to_numeric(scoped.get("iv"), errors="coerce")
    scoped = scoped[scoped["iv"].notna() & (scoped["iv"] > 0)]
    if scoped.empty:
        return result
    weights = pd.to_numeric(scoped.get("oi", 0.0), errors="coerce").fillna(0.0).clip(lower=0.0)
    if float(weights.sum()) <= 0:
        weights = pd.Series(np.ones(len(scoped)), index=scoped.index)
    atm = scoped.iloc[np.argsort(np.abs(scoped["strike"].to_numpy(dtype=float) / spot - 1.0))[:8]]
    result["article_iv_atm_pct"] = float(atm["iv"].median() * 100.0)
    weighted_mean = float(np.average(scoped["iv"], weights=weights))
    result["article_iv_dispersion_pct"] = float(
        math.sqrt(np.average((scoped["iv"] - weighted_mean) ** 2, weights=weights)) * 100.0
    )
    downside = scoped[(scoped["class"] == "P") & (scoped["strike"] <= spot)]
    upside = scoped[(scoped["class"] == "C") & (scoped["strike"] >= spot)]
    if not downside.empty and not upside.empty:
        result["article_iv_downside_minus_upside_pct"] = float(
            downside["iv"].median() * 100.0 - upside["iv"].median() * 100.0
        )
    return result


def _article_price_features(
    qqq: pd.DataFrame,
    as_of: datetime,
    open_utc: datetime,
    spot: float,
    wall_features: dict[str, float],
) -> dict[str, float]:
    available = qqq[
        (qqq["available_at"] <= pd.Timestamp(as_of))
        & (qqq["available_at"] > pd.Timestamp(open_utc))
    ].copy()
    if available.empty:
        return {}
    for column in ("high", "low", "close", "volume"):
        available[column] = pd.to_numeric(available[column], errors="coerce")
    available = available.dropna(subset=["high", "low", "close"])
    if available.empty:
        return {}
    volume = available["volume"].fillna(0.0).clip(lower=1.0)
    typical = (available["high"] + available["low"] + available["close"]) / 3.0
    cumulative_volume = volume.cumsum()
    vwap_series = (typical * volume).cumsum() / cumulative_volume
    vwap = float(vwap_series.iloc[-1])
    weighted_variance = float(np.average((typical - vwap) ** 2, weights=volume))

    def trailing(minutes: int) -> pd.DataFrame:
        return available[available["available_at"] > pd.Timestamp(as_of - timedelta(minutes=minutes))]

    def return_bps(minutes: int) -> float:
        rows = trailing(minutes)
        if rows.empty:
            return math.nan
        return (spot / float(rows.iloc[0]["close"]) - 1.0) * 10_000.0

    fifteen = trailing(15)
    closes_15 = fifteen["close"].to_numpy(dtype=float)
    path_length = float(np.abs(np.diff(closes_15)).sum()) if len(closes_15) > 1 else 0.0
    efficiency = (
        abs(float(closes_15[-1] - closes_15[0])) / path_length
        if path_length > 0 else 0.0
    )
    earlier = available[available["available_at"] <= pd.Timestamp(as_of - timedelta(minutes=15))]
    earlier_vwap = float(vwap_series.loc[earlier.index[-1]]) if not earlier.empty else float(vwap_series.iloc[0])
    session_high = float(available["high"].max())
    session_low = float(available["low"].min())
    session_range = session_high - session_low
    result = {
        "article_price_vwap_distance_bps": (spot / vwap - 1.0) * 10_000.0,
        "article_price_vwap_slope_15m_bps": (vwap / earlier_vwap - 1.0) * 10_000.0,
        "article_price_vwap_std_bps": math.sqrt(max(weighted_variance, 0.0)) / spot * 10_000.0,
        "article_price_return_5m_bps": return_bps(5),
        "article_price_return_15m_bps": return_bps(15),
        "article_price_trend_efficiency_15m": efficiency,
        "article_price_session_range_position": (
            (spot - session_low) / session_range if session_range > 0 else 0.5
        ),
    }
    ten = trailing(10)
    if not ten.empty:
        result["article_price_above_vwap_fraction_10m"] = float(
            (ten["close"] > vwap_series.loc[ten.index]).mean()
        )

    def level_from_bps(name: str) -> float | None:
        value = _safe_float(wall_features.get(name), math.nan)
        return spot * (1.0 + value / 10_000.0) if math.isfinite(value) else None

    levels = {
        "flip": level_from_bps("oi_gamma_flip_bps"),
        "call_wall": level_from_bps("oi_call_wall_bps"),
        "put_wall": level_from_bps("oi_put_wall_bps"),
    }
    for minutes in (3, 5, 10):
        rows = trailing(minutes)
        if rows.empty:
            continue
        closes = rows["close"]
        if levels["flip"] is not None:
            result[f"article_price_above_flip_fraction_{minutes}m"] = float(
                (closes > levels["flip"]).mean()
            )
        if levels["call_wall"] is not None:
            result[f"article_price_above_call_wall_fraction_{minutes}m"] = float(
                (closes > levels["call_wall"]).mean()
            )
        if levels["put_wall"] is not None:
            result[f"article_price_below_put_wall_fraction_{minutes}m"] = float(
                (closes < levels["put_wall"]).mean()
            )

    flip = levels["flip"]
    price_side = 1 if spot > vwap else -1 if spot < vwap else 0
    flip_side = 1 if flip is not None and spot > flip else -1 if flip is not None and spot < flip else 0
    result["article_gvp_price_flip_alignment"] = (
        float(price_side) if price_side != 0 and price_side == flip_side else 0.0
    )
    return result


def _future_path_targets(
    qqq: pd.DataFrame,
    as_of: datetime,
    horizon_at: datetime,
    spot: float,
    call_wall_bps: float | None,
    put_wall_bps: float | None,
    deadband_bps: float,
) -> dict[str, float | int]:
    future = qqq[
        (qqq["available_at"] > pd.Timestamp(as_of))
        & (qqq["available_at"] <= pd.Timestamp(horizon_at))
    ].copy()
    if future.empty:
        return {}
    for column in ("high", "low", "close"):
        future[column] = pd.to_numeric(future[column], errors="coerce")
    future = future.dropna(subset=["high", "low", "close"])
    if future.empty:
        return {}
    endpoint = float(future.iloc[-1]["close"])
    end_return = (endpoint / spot - 1.0) * 10_000.0
    max_up = (float(future["high"].max()) / spot - 1.0) * 10_000.0
    max_down = (float(future["low"].min()) / spot - 1.0) * 10_000.0
    closes = np.r_[spot, future["close"].to_numpy(dtype=float)]
    path_length = float(np.abs(np.diff(closes)).sum())
    efficiency = abs(endpoint - spot) / path_length if path_length > 0 else 0.0
    direction = 1 if end_return > deadband_bps else -1 if end_return < -deadband_bps else 0

    call_wall = (
        spot * (1.0 + float(call_wall_bps) / 10_000.0)
        if call_wall_bps is not None and math.isfinite(float(call_wall_bps))
        and float(call_wall_bps) > 0 else None
    )
    put_wall = (
        spot * (1.0 + float(put_wall_bps) / 10_000.0)
        if put_wall_bps is not None and math.isfinite(float(put_wall_bps))
        and float(put_wall_bps) < 0 else None
    )
    first_wall = 0
    hit_minutes = math.nan
    for row in future.itertuples():
        call_hit = call_wall is not None and float(row.high) >= call_wall
        put_hit = put_wall is not None and float(row.low) <= put_wall
        if call_hit or put_hit:
            if call_hit != put_hit:
                first_wall = 1 if call_hit else -1
            hit_minutes = (
                pd.Timestamp(row.available_at) - pd.Timestamp(as_of)
            ).total_seconds() / 60.0
            break
    return {
        "qqq_future_return_bps_30m": end_return,
        "qqq_future_max_up_bps_30m": max_up,
        "qqq_future_max_down_bps_30m": max_down,
        "qqq_future_range_bps_30m": max_up - max_down,
        "qqq_future_directional_efficiency_30m": efficiency,
        "label_30m": direction,
        "target_expansion_30m": int(direction != 0),
        "target_wall_first_30m": first_wall,
        "target_wall_hit_minutes_30m": hit_minutes,
    }


def add_article_temporal_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add only same-session lags; the first observation has no confirmation."""
    if frame.empty:
        return frame.copy()
    ordered = frame.sort_values(["date", "as_of"]).copy()
    grouped = ordered.groupby("date", sort=False)
    previous_time = grouped["as_of"].shift(1)
    elapsed = (
        pd.to_datetime(ordered["as_of"], utc=True) - pd.to_datetime(previous_time, utc=True)
    ).dt.total_seconds() / 60.0
    ordered["article_has_previous_snapshot"] = previous_time.notna().astype(float)
    ordered["article_minutes_since_previous_snapshot"] = elapsed

    spot = pd.to_numeric(ordered["qqq_spot"], errors="coerce")
    for column in (
        "oi_call_wall_bps", "oi_put_wall_bps",
        "dashboard_vol_call_wall_bps", "dashboard_vol_put_wall_bps",
    ):
        if column not in ordered:
            continue
        level = spot * (1.0 + pd.to_numeric(ordered[column], errors="coerce") / 10_000.0)
        previous_level = level.groupby(ordered["date"], sort=False).shift(1)
        migration = (level / previous_level - 1.0) * 10_000.0
        name = column.removesuffix("_bps")
        ordered[f"article_{name}_migration_bps_per_hour"] = migration * 60.0 / elapsed

    for column in (
        "oi_gross_log", "vol_gross_log", "oi_net_balance", "vol_net_balance",
        "oi_peak1_share", "vol_peak1_share", "dashboard_vol_call_wall_share",
        "dashboard_vol_put_wall_share",
    ):
        if column in ordered:
            ordered[f"article_{column}_delta"] = ordered[column] - grouped[column].shift(1)

    if {"oi_call_wall_bps", "oi_put_wall_bps"}.issubset(ordered.columns):
        ordered["article_oi_wall_width_bps"] = (
            ordered["oi_call_wall_bps"] - ordered["oi_put_wall_bps"]
        )
        ordered["article_oi_wall_width_delta_bps"] = (
            ordered["article_oi_wall_width_bps"]
            - grouped["article_oi_wall_width_bps"].shift(1)
        )
    for side in ("call", "put"):
        vol_column = f"dashboard_vol_{side}_wall_bps"
        oi_column = f"oi_{side}_wall_bps"
        if vol_column in ordered and oi_column in ordered:
            ordered[f"article_oi_vol_{side}_wall_gap_bps"] = (
                ordered[vol_column] - ordered[oi_column]
            )

    migrations = [
        column for column in ordered.columns
        if column.endswith("wall_migration_bps_per_hour") and "oi_" in column
    ]
    if len(migrations) >= 2 and "article_gvp_price_flip_alignment" in ordered:
        call_move = np.sign(pd.to_numeric(ordered[migrations[0]], errors="coerce"))
        put_move = np.sign(pd.to_numeric(ordered[migrations[1]], errors="coerce"))
        structure = np.where(call_move == put_move, call_move, 0.0)
        price_alignment = pd.to_numeric(
            ordered["article_gvp_price_flip_alignment"], errors="coerce",
        ).fillna(0.0).to_numpy()
        ordered["article_gvp_full_alignment"] = np.where(
            (structure != 0) & (structure == price_alignment), structure, 0.0,
        )
    return ordered


def extract_wall_features(
    profile: pd.DataFrame,
    spot: float,
    as_of: datetime,
    expiry: datetime,
    gamma_profile: tuple[np.ndarray, np.ndarray] | None = None,
) -> dict[str, float]:
    features = _peak_summary(profile, spot, "oi_gex", "oi")
    volume = _peak_summary(profile, spot, "volume_gex", "vol")
    features.update(volume)
    grouped = _profile_by_strike(profile)
    if not grouped.empty:
        calls = grouped[grouped["class"] == "C"]
        puts = grouped[grouped["class"] == "P"]
        if not calls.empty:
            features["oi_call_wall_bps"] = (
                float(calls.loc[calls["oi_gex"].idxmax(), "strike"]) / spot - 1.0
            ) * 10_000.0
        if not puts.empty:
            features["oi_put_wall_bps"] = (
                float(puts.loc[puts["oi_gex"].idxmin(), "strike"]) / spot - 1.0
            ) * 10_000.0
    years = max((expiry - as_of).total_seconds(), 1.0) / (365.0 * 24.0 * 3600.0)
    flip = (
        _gamma_flip_from_profile(*gamma_profile, spot)
        if gamma_profile is not None else _gamma_flip(profile, spot, years)
    )
    features["oi_gamma_flip_bps"] = (
        (float(flip) / spot - 1.0) * 10_000.0 if flip is not None else math.nan
    )
    features["quality_valid_contracts"] = float(len(profile))
    return features


def _column_time(frame: pd.DataFrame, candidates: Sequence[str]) -> pd.Series:
    for name in candidates:
        if name in frame.columns:
            return pd.to_datetime(frame[name], utc=True, errors="coerce")
    raise ValueError(f"none of the timestamp columns exist: {candidates}")


def _qqq_bars(folder: Path) -> pd.DataFrame:
    frame = pd.read_csv(folder / SCHEMA_FILES["qqq-ohlcv-1m"], compression="gzip")
    frame["available_at"] = _column_time(frame, ["ts_event", "ts_recv"]) + pd.Timedelta(minutes=1)
    return frame.sort_values("available_at")


def _price_at_or_before(frame: pd.DataFrame, at: datetime) -> float | None:
    rows = frame[frame["available_at"] <= pd.Timestamp(at)]
    if rows.empty:
        return None
    return _safe_float(rows.iloc[-1]["close"], math.nan)


def _fresh_price_at(frame: pd.DataFrame, at: datetime, max_age_minutes: float = 2.0) -> float | None:
    rows = frame[frame["available_at"] <= pd.Timestamp(at)]
    if rows.empty:
        return None
    latest = rows.iloc[-1]
    age = pd.Timestamp(at) - pd.Timestamp(latest["available_at"])
    if age > pd.Timedelta(minutes=max_age_minutes):
        return None
    return _safe_float(latest["close"], math.nan)


def _price_features(qqq: pd.DataFrame, as_of: datetime, open_utc: datetime) -> dict[str, float]:
    now = _price_at_or_before(qqq, as_of)
    opened = _price_at_or_before(qqq, open_utc + timedelta(minutes=1))
    back_30 = _price_at_or_before(qqq, as_of - timedelta(minutes=30))
    back_60 = _price_at_or_before(qqq, as_of - timedelta(minutes=60))
    rows = qqq[(qqq["available_at"] <= pd.Timestamp(as_of))
               & (qqq["available_at"] > pd.Timestamp(as_of - timedelta(minutes=60)))]
    returns = np.log(pd.to_numeric(rows["close"], errors="coerce")).diff().dropna()
    def bps(base: float | None) -> float:
        if now is None or base is None or not math.isfinite(base) or base <= 0:
            return math.nan
        return (now / base - 1.0) * 10_000.0
    return {
        "price_return_from_open_bps": bps(opened),
        "price_return_30m_bps": bps(back_30),
        "price_return_60m_bps": bps(back_60),
        "price_realized_vol_60m_bps": float(returns.std(ddof=0) * 10_000.0) if len(returns) else math.nan,
    }


def _load_mnq_points(path: Path, start: date, end: date) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    with path.open("rb") as handle:
        bars = pickle.load(handle)
    rows = []
    for bar in bars:
        ts = pd.Timestamp(bar.timestamp)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        if start <= ts.date() < end:
            rows.append({"ts": ts, "open": float(bar.open), "close": float(bar.close)})
    del bars
    return pd.DataFrame(rows).sort_values("ts") if rows else pd.DataFrame()


def _load_research_mnq_csv(path: Path, start: date, end: date) -> pd.DataFrame:
    if not path.is_file():
        return pd.DataFrame()
    frame = pd.read_csv(path, compression="gzip")
    frame["ts"] = _column_time(frame, ["ts_event", "ts_recv"])
    frame = frame[(frame["ts"].dt.date >= start) & (frame["ts"].dt.date < end)]
    return frame[["ts", "open", "close"]].sort_values("ts")


def _mnq_trade_prices(mnq: pd.DataFrame, as_of: datetime, exit_at: datetime) -> tuple[float, float] | None:
    if mnq.empty:
        return None
    after = mnq[mnq["ts"] >= pd.Timestamp(as_of)]
    before_exit = mnq[mnq["ts"] < pd.Timestamp(exit_at)]
    if after.empty or before_exit.empty:
        return None
    entry_row = after.iloc[0]
    exit_row = before_exit.iloc[-1]
    if entry_row["ts"] >= pd.Timestamp(exit_at):
        return None
    if pd.Timestamp(entry_row["ts"]) > pd.Timestamp(as_of) + pd.Timedelta(minutes=2):
        return None
    if pd.Timestamp(exit_row["ts"]) < pd.Timestamp(exit_at) - pd.Timedelta(minutes=2):
        return None
    if pd.Timestamp(entry_row["ts"]).tz_convert(NY).date() != pd.Timestamp(as_of).tz_convert(NY).date():
        return None
    return float(entry_row["open"]), float(exit_row["close"])


def build_day_rows(folder: Path, mnq: pd.DataFrame | None = None,
                   label_deadband_bps: float = 10.0) -> list[dict[str, Any]]:
    day = date.fromisoformat(folder.name)
    required = [folder / "qqq_definition.csv.gz"] + [
        folder / SCHEMA_FILES[name] for name in ("statistics", "cbbo-1m", "qqq-ohlcv-1m")
    ]
    if not all(path.is_file() for path in required):
        return []
    definitions_df = pd.read_csv(required[0], compression="gzip")
    expiry_series = pd.to_datetime(definitions_df["expiration"], utc=True, errors="coerce")
    definitions_df = definitions_df[
        (expiry_series.dt.date == day)
        & definitions_df["instrument_class"].isin(["C", "P"])
    ].copy()
    definitions = {
        str(row.raw_symbol): {"strike": float(row.strike_price), "class": str(row.instrument_class)}
        for row in definitions_df.itertuples()
    }
    if not definitions:
        return []

    stats = pd.read_csv(folder / SCHEMA_FILES["statistics"], compression="gzip")
    oi_rows = stats[stats["stat_type"] == 9]
    open_interest = oi_rows.groupby("symbol")["quantity"].first().astype(int).to_dict()

    quotes = pd.read_csv(folder / SCHEMA_FILES["cbbo-1m"], compression="gzip")
    quotes["ts"] = _column_time(quotes, ["ts_recv", "ts_event"])
    quotes = quotes.sort_values("ts")
    quote_records = quotes.to_dict("records")

    volume_path = folder / SCHEMA_FILES["ohlcv-1h"]
    volume_records: list[dict[str, Any]] = []
    if volume_path.is_file():
        volume = pd.read_csv(volume_path, compression="gzip")
        volume["available_at"] = _column_time(volume, ["ts_event", "ts_recv"]) + pd.Timedelta(hours=1)
        volume = volume.groupby(["available_at", "symbol"], as_index=False)["volume"].sum()
        volume_records = volume.sort_values("available_at").to_dict("records")

    qqq = _qqq_bars(folder)
    open_utc, close_utc, option_close_utc = _session_bounds(day)
    expiry = option_close_utc
    if qqq.empty:
        return []
    actual_close_at = min(pd.Timestamp(close_utc), pd.Timestamp(qqq["available_at"].max()))
    close_at = actual_close_at.to_pydatetime()
    close_spot = _price_at_or_before(qqq, close_at)
    if close_spot is None:
        return []

    latest_quotes: dict[str, dict[str, Any]] = {}
    cumulative_volume: defaultdict[str, int] = defaultdict(int)
    quote_idx = volume_idx = 0
    rows: list[dict[str, Any]] = []
    for as_of in _sample_times(day):
        while quote_idx < len(quote_records) and quote_records[quote_idx]["ts"] <= pd.Timestamp(as_of):
            row = quote_records[quote_idx]
            latest_quotes[str(row["symbol"])] = {
                "ts": row["ts"], "bid": row.get("bid_px_00"), "ask": row.get("ask_px_00"),
            }
            quote_idx += 1
        while (volume_idx < len(volume_records)
               and volume_records[volume_idx]["available_at"] <= pd.Timestamp(as_of)):
            row = volume_records[volume_idx]
            cumulative_volume[str(row["symbol"])] += int(row["volume"])
            volume_idx += 1
        if pd.Timestamp(as_of) > actual_close_at:
            continue
        spot = _fresh_price_at(qqq, as_of)
        if spot is None:
            continue
        profile = _contract_profile(
            latest_quotes, cumulative_volume, definitions, open_interest,
            spot, pd.Timestamp(as_of), pd.Timestamp(expiry),
        )
        if profile.empty:
            continue
        years = max(
            (pd.Timestamp(expiry) - pd.Timestamp(as_of)).total_seconds(), 1.0,
        ) / (365.0 * 24.0 * 3600.0)
        gamma_profiles = {
            name: _gamma_price_profile(profile, spot, years, name)
            for name in ("oi", "volume")
        }
        future_30m_at = min(as_of + timedelta(minutes=30), close_at)
        future_60m_at = min(as_of + timedelta(minutes=60), close_at)
        future_60m_spot = _price_at_or_before(qqq, future_60m_at)
        if future_60m_spot is None:
            continue
        ret_60m = (future_60m_spot / spot - 1.0) * 10_000.0
        ret_close = (close_spot / spot - 1.0) * 10_000.0
        def label(value: float) -> int:
            return 1 if value > label_deadband_bps else -1 if value < -label_deadband_bps else 0
        result: dict[str, Any] = {
            "feature_version": FEATURE_VERSION,
            "date": day.isoformat(),
            "as_of": _iso(as_of),
            "as_of_et": as_of.astimezone(NY).strftime("%H:%M"),
            "future_30m_at": _iso(future_30m_at),
            "future_60m_at": _iso(future_60m_at),
            "close_at": _iso(close_at),
            "qqq_spot": spot,
            "qqq_future_return_bps_60m": ret_60m,
            "qqq_future_return_bps_close": ret_close,
            "label_60m": label(ret_60m),
            "label_close": label(ret_close),
            "minutes_since_open": (as_of - open_utc).total_seconds() / 60.0,
            "minutes_to_close": (close_at - as_of).total_seconds() / 60.0,
        }
        wall_features = extract_wall_features(
            profile, spot, as_of, expiry, gamma_profiles["oi"],
        )
        result.update(wall_features)
        result.update(extract_dashboard_features(
            profile, spot, as_of, expiry, gamma_profiles,
        ))
        result.update(extract_article_option_features(
            profile, spot, as_of, expiry, gamma_profiles,
        ))
        result.update(_price_features(qqq, as_of, open_utc))
        result.update(_article_price_features(qqq, as_of, open_utc, spot, wall_features))
        result.update(_future_path_targets(
            qqq, as_of, future_30m_at, spot,
            wall_features.get("oi_call_wall_bps"),
            wall_features.get("oi_put_wall_bps"),
            label_deadband_bps,
        ))
        if mnq is not None and not mnq.empty:
            trade_30 = _mnq_trade_prices(mnq, as_of, future_30m_at)
            trade_60 = _mnq_trade_prices(mnq, as_of, future_60m_at)
            trade_close = _mnq_trade_prices(mnq, as_of, close_at)
            if trade_30:
                result["mnq_entry"], result["mnq_exit_30m"] = trade_30
                result["mnq_points_30m"] = trade_30[1] - trade_30[0]
            if trade_60:
                result["mnq_entry"], result["mnq_exit_60m"] = trade_60
                result["mnq_points_60m"] = trade_60[1] - trade_60[0]
            if trade_close:
                result.setdefault("mnq_entry", trade_close[0])
                result["mnq_exit_close"] = trade_close[1]
                result["mnq_points_close"] = trade_close[1] - trade_close[0]
        rows.append(result)
    if not rows:
        return []
    return add_article_temporal_features(pd.DataFrame(rows)).to_dict("records")


def build_dataset(data_root: Path, mnq_path: Path | None = DEFAULT_MNQ_PATH,
                  label_deadband_bps: float = 10.0) -> pd.DataFrame:
    folders = sorted(path for path in (data_root / "raw").glob("????-??-??") if path.is_dir())
    if not folders:
        raise RuntimeError(f"no daily raw folders found under {data_root / 'raw'}")
    start, end = date.fromisoformat(folders[0].name), date.fromisoformat(folders[-1].name) + timedelta(days=1)
    research_mnq = data_root / "raw_mnq" / RESEARCH_MNQ_FILE
    if mnq_path is not None and mnq_path.is_file():
        # Use ancserTPX's adjusted/stitched coordinate by default so the
        # research P&L is comparable with the system's existing backtests.
        mnq = _load_mnq_points(mnq_path, start, end)
        print(f"MNQ source: ancserTPX store {mnq_path}", flush=True)
    elif research_mnq.is_file():
        mnq = _load_research_mnq_csv(research_mnq, start, end)
        print(f"MNQ source: raw research fallback {research_mnq}", flush=True)
    else:
        mnq = pd.DataFrame()
    rows: list[dict[str, Any]] = []
    feature_root = data_root / "features"
    feature_root.mkdir(parents=True, exist_ok=True)
    for folder in folders:
        output = feature_root / f"{folder.name}.csv.gz"
        if output.is_file():
            cached = pd.read_csv(output, compression="gzip")
            if ("feature_version" in cached.columns
                    and set(cached["feature_version"].dropna().astype(int)) == {FEATURE_VERSION}):
                day_rows = cached.to_dict("records")
            else:
                day_rows = build_day_rows(folder, None, label_deadband_bps)
                if day_rows:
                    _atomic_csv(output, pd.DataFrame(day_rows))
        else:
            day_rows = build_day_rows(folder, None, label_deadband_bps)
            if day_rows:
                _atomic_csv(output, pd.DataFrame(day_rows))
        rows.extend(day_rows)
        if day_rows:
            print(f"{folder.name} features: {len(day_rows)}", flush=True)
    if not rows:
        raise RuntimeError("no feature rows could be built")
    dataset = pd.DataFrame(rows).sort_values(["date", "as_of"]).reset_index(drop=True)
    deadband = float(label_deadband_bps)
    for horizon in ("30m", "60m", "close"):
        returns = pd.to_numeric(dataset[f"qqq_future_return_bps_{horizon}"], errors="coerce")
        dataset[f"label_{horizon}"] = np.select(
            [returns > deadband, returns < -deadband], [1, -1], default=0,
        ).astype(int)
    dataset["target_expansion_30m"] = (dataset["label_30m"] != 0).astype(int)
    if not mnq.empty:
        for idx, row in dataset.iterrows():
            as_of = pd.Timestamp(row["as_of"]).to_pydatetime()
            for horizon, exit_column in (
                ("30m", "future_30m_at"), ("60m", "future_60m_at"),
                ("close", "close_at"),
            ):
                exit_at = pd.Timestamp(row[exit_column]).to_pydatetime()
                trade = _mnq_trade_prices(mnq, as_of, exit_at)
                if trade:
                    dataset.loc[idx, "mnq_entry"] = trade[0]
                    dataset.loc[idx, f"mnq_exit_{horizon}"] = trade[1]
                    dataset.loc[idx, f"mnq_points_{horizon}"] = trade[1] - trade[0]
    _atomic_csv(data_root / "option_wall_ml_dataset.csv.gz", dataset)
    return dataset


def chronological_split(frame: pd.DataFrame, train_fraction: float = 0.60,
                        validation_fraction: float = 0.20) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = sorted(frame["date"].astype(str).unique())
    if len(dates) < 20:
        raise ValueError("at least 20 independent sessions are required")
    train_end = max(1, int(len(dates) * train_fraction))
    validation_end = max(train_end + 1, int(len(dates) * (train_fraction + validation_fraction)))
    validation_end = min(validation_end, len(dates) - 1)
    train_dates = set(dates[:train_end])
    validation_dates = set(dates[train_end:validation_end])
    test_dates = set(dates[validation_end:])
    return (
        frame[frame["date"].astype(str).isin(train_dates)].copy(),
        frame[frame["date"].astype(str).isin(validation_dates)].copy(),
        frame[frame["date"].astype(str).isin(test_dates)].copy(),
    )


def _feature_columns(frame: pd.DataFrame, include_price: bool) -> list[str]:
    allowed = set(LEGACY_WALL_FEATURES)
    if include_price:
        allowed.update(LEGACY_PRICE_FEATURES)
    return sorted(
        column for column in allowed
        if column in frame.columns and pd.api.types.is_numeric_dtype(frame[column])
    )


def ablation_feature_columns(frame: pd.DataFrame, feature_set: str) -> list[str]:
    if feature_set not in ABLATION_FEATURE_SETS:
        raise ValueError(f"unknown feature set: {feature_set}")
    allowed = set(LEGACY_WALL_FEATURES)
    if feature_set in {"dashboard", "combined_0dte"}:
        allowed.update(column for column in frame.columns if column.startswith("dashboard_"))
    if feature_set in {"article_state", "combined_0dte"}:
        allowed.update(column for column in frame.columns if column.startswith("article_"))
    return sorted(
        column for column in allowed
        if column in frame.columns and pd.api.types.is_numeric_dtype(frame[column])
    )


def _profit_factor(values: Iterable[float]) -> float:
    values = list(values)
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return gains / losses if losses > 0 else math.inf if gains > 0 else 0.0


def _mnq_pnl_summary(points: np.ndarray, signals: np.ndarray,
                     sessions: np.ndarray) -> dict[str, Any]:
    points = np.asarray(points, dtype=float)
    signals = np.asarray(signals, dtype=int)
    sessions = np.asarray(sessions, dtype=str)
    valid = np.isfinite(points) & np.isin(signals, [-1, 1])
    points, signals, sessions = points[valid], signals[valid], sessions[valid]
    cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
    pnl = signals * points * get_point_value("MNQ") - cost
    equity = np.cumsum(pnl)
    running_peak = np.maximum.accumulate(np.r_[0.0, equity])[1:]
    result: dict[str, Any] = {
        "trades": int(len(pnl)),
        "net_pnl": float(pnl.sum()),
        "pf": _profit_factor(pnl),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
        "max_drawdown": float((equity - running_peak).min()) if len(pnl) else None,
        "round_turn_cost": cost,
        "directions": {
            "long": int((signals == 1).sum()),
            "short": int((signals == -1).sum()),
        },
    }
    side_results: dict[str, Any] = {}
    for side, name in ((1, "long"), (-1, "short")):
        side_pnl = pnl[signals == side]
        side_results[name] = {
            "trades": int(len(side_pnl)),
            "net_pnl": float(side_pnl.sum()),
            "pf": _profit_factor(side_pnl),
            "win_rate": float((side_pnl > 0).mean()) if len(side_pnl) else None,
        }
    result["by_direction"] = side_results
    result["by_session_pnl"] = {
        str(session): float(pnl[sessions == session].sum())
        for session in dict.fromkeys(sessions)
    }
    return result


def _hourly_forced_direction_report(model: Pipeline, frame: pd.DataFrame,
                                    features: list[str],
                                    probability_threshold: float) -> dict[str, Any]:
    required = {"as_of_et", "date", "mnq_points_60m"}
    if not required.issubset(frame.columns):
        return {"status": "unavailable", "reason": "hourly MNQ columns missing"}
    hourly = frame[frame["as_of_et"].astype(str).str.endswith(":00")].copy()
    points = pd.to_numeric(hourly["mnq_points_60m"], errors="coerce").to_numpy(dtype=float)
    if hourly.empty or not np.isfinite(points).any():
        return {"status": "unavailable", "reason": "no complete hourly MNQ trades"}
    probabilities = np.asarray(model.predict_proba(hourly[features]), dtype=float)
    classes = np.asarray(model.classes_, dtype=int)
    class_index = {int(value): idx for idx, value in enumerate(classes)}
    if -1 not in class_index or 1 not in class_index:
        return {"status": "unavailable", "reason": "training split lacks a direction class"}
    predicted = np.asarray(model.predict(hourly[features]), dtype=int)
    confidence = probabilities.max(axis=1)
    forced = np.where(
        probabilities[:, class_index[1]] >= probabilities[:, class_index[-1]], 1, -1,
    )
    sessions = hourly["date"].astype(str).to_numpy()
    confident = (predicted != 0) & (confidence >= probability_threshold)
    non_neutral = predicted != 0
    return {
        "status": "provisional_research_only",
        "hours": "10:00-15:00 ET",
        "forced_rule": "ignore neutral; choose the larger of P(long) and P(short)",
        "forced_every_hour": _mnq_pnl_summary(points, forced, sessions),
        "model_non_neutral_no_confidence_gate": _mnq_pnl_summary(
            points[non_neutral], predicted[non_neutral], sessions[non_neutral],
        ),
        "confidence_gated": _mnq_pnl_summary(
            points[confident], predicted[confident], sessions[confident],
        ),
        "baselines": {
            "always_long": _mnq_pnl_summary(points, np.ones(len(points), dtype=int), sessions),
            "always_short": _mnq_pnl_summary(points, -np.ones(len(points), dtype=int), sessions),
        },
    }


def _classification_report(model: Pipeline, frame: pd.DataFrame, features: list[str],
                           label_column: str, return_column: str,
                           probability_threshold: float = 0.55) -> dict[str, Any]:
    x = frame[features]
    y = frame[label_column].astype(int).to_numpy()
    pred = model.predict(x).astype(int)
    probabilities = model.predict_proba(x)
    confidence = probabilities.max(axis=1)
    active = (pred != 0) & (confidence >= probability_threshold)
    returns = pd.to_numeric(frame[return_column], errors="coerce").to_numpy(dtype=float)
    qqq_strategy = pred[active] * returns[active]
    result: dict[str, Any] = {
        "rows": len(frame),
        "sessions": int(frame["date"].nunique()),
        "accuracy": float(accuracy_score(y, pred)),
        "balanced_accuracy": float(balanced_accuracy_score(y, pred)),
        "f1_macro": float(f1_score(y, pred, average="macro", zero_division=0)),
        "confusion_matrix_labels_-1_0_1": confusion_matrix(y, pred, labels=[-1, 0, 1]).tolist(),
        "active_predictions": int(active.sum()),
        "coverage": float(active.mean()),
        "active_accuracy": float((pred[active] == y[active]).mean()) if active.any() else None,
        "qqq_direction_pf_bps": _profit_factor(qqq_strategy),
        "qqq_direction_total_bps": float(np.nansum(qqq_strategy)),
    }
    mnq_column = "mnq_points_60m" if label_column == "label_60m" else "mnq_points_close"
    if mnq_column in frame.columns:
        points = pd.to_numeric(frame[mnq_column], errors="coerce").to_numpy(dtype=float)
        valid = active & np.isfinite(points)
        cost = get_commission_rt("MNQ") + get_fees_rt("MNQ")
        pnl = pred[valid] * points[valid] * get_point_value("MNQ") - cost
        result.update({
            "mnq_trades": int(valid.sum()),
            "mnq_net_pnl": float(pnl.sum()),
            "mnq_pf": _profit_factor(pnl),
            "mnq_win_rate": float((pnl > 0).mean()) if len(pnl) else None,
            "mnq_round_turn_cost": cost,
        })
    return result


def train_models(data_root: Path, probability_threshold: float = 0.55) -> dict[str, Any]:
    dataset_path = data_root / "option_wall_ml_dataset.csv.gz"
    if not dataset_path.is_file():
        raise RuntimeError(f"dataset missing: {dataset_path}; run build first")
    frame = pd.read_csv(dataset_path, compression="gzip")
    train, validation, test = chronological_split(frame)
    output_root = data_root / "models"
    output_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "created_at": _iso(datetime.now(UTC)),
        "dataset": str(dataset_path),
        "splits": {
            "train": [str(train["date"].min()), str(train["date"].max()), int(train["date"].nunique())],
            "validation": [str(validation["date"].min()), str(validation["date"].max()), int(validation["date"].nunique())],
            "test": [str(test["date"].min()), str(test["date"].max()), int(test["date"].nunique())],
        },
        "probability_threshold": probability_threshold,
        "models": {},
    }
    for horizon, label_column, return_column in [
        ("60m", "label_60m", "qqq_future_return_bps_60m"),
        ("close", "label_close", "qqq_future_return_bps_close"),
    ]:
        for feature_set, include_price in [("wall_only", False), ("wall_plus_price", True)]:
            features = _feature_columns(frame, include_price)
            candidates: dict[str, Pipeline] = {
                "logistic": Pipeline([
                    ("imputer", SimpleImputer(strategy="median")),
                    ("scale", StandardScaler()),
                    ("model", LogisticRegression(max_iter=2000, class_weight="balanced", random_state=42)),
                ]),
                "hist_gradient_boosting": Pipeline([
                    ("imputer", SimpleImputer(strategy="median")),
                    ("model", HistGradientBoostingClassifier(
                        max_iter=180, learning_rate=0.05, max_leaf_nodes=15,
                        min_samples_leaf=20, l2_regularization=1.0, random_state=42,
                    )),
                ]),
            }
            trained: dict[str, Pipeline] = {}
            validation_scores: dict[str, float] = {}
            for name, candidate in candidates.items():
                candidate.fit(train[features], train[label_column].astype(int))
                trained[name] = candidate
                validation_scores[name] = balanced_accuracy_score(
                    validation[label_column].astype(int), candidate.predict(validation[features])
                )
            winner_name = max(validation_scores, key=validation_scores.get)
            winner = trained[winner_name]
            key = f"{horizon}_{feature_set}"
            artifact = {
                "model": winner,
                "features": features,
                "horizon": horizon,
                "feature_set": feature_set,
                "label_column": label_column,
                "trained_through": str(train["date"].max()),
                "validated_through": str(validation["date"].max()),
                "probability_threshold": probability_threshold,
            }
            joblib.dump(artifact, output_root / f"{key}.joblib")
            model_report = {
                "selected_algorithm": winner_name,
                "validation_candidate_balanced_accuracy": validation_scores,
                "feature_count": len(features),
                "validation": _classification_report(
                    winner, validation, features, label_column, return_column, probability_threshold,
                ),
                "test": _classification_report(
                    winner, test, features, label_column, return_column, probability_threshold,
                ),
            }
            if horizon == "60m":
                model_report["hourly_test_mnq"] = _hourly_forced_direction_report(
                    winner, test, features, probability_threshold,
                )
            report["models"][key] = model_report
    _atomic_json(data_root / "option_wall_ml_report.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["acquire", "acquire-mnq", "build", "train", "all"])
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--start", type=date.fromisoformat,
                        default=date.today() - timedelta(days=365))
    parser.add_argument("--end", type=date.fromisoformat, default=date.today())
    parser.add_argument("--max-cost", type=float, default=65.0,
                        help="hard cap for newly quoted provider requests in this run")
    parser.add_argument("--max-days", type=int, default=None,
                        help="optional smoke/resume limit")
    parser.add_argument("--no-hourly-volume", action="store_true")
    parser.add_argument("--mnq-path", type=Path, default=DEFAULT_MNQ_PATH)
    parser.add_argument("--no-mnq", action="store_true")
    parser.add_argument("--label-deadband-bps", type=float, default=10.0)
    parser.add_argument("--probability-threshold", type=float, default=0.55)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.data_root.mkdir(parents=True, exist_ok=True)
    if args.mode in {"acquire", "all"}:
        result = acquire_history(
            args.data_root, args.start, args.end, args.max_cost,
            with_hourly_volume=not args.no_hourly_volume,
            max_days=args.max_days,
        )
        print(json.dumps(result, indent=2))
    if args.mode == "acquire-mnq":
        result = acquire_mnq_history(args.data_root, args.start, args.end, args.max_cost)
        print(json.dumps(result, indent=2))
    if args.mode in {"build", "all"}:
        dataset = build_dataset(
            args.data_root,
            None if args.no_mnq else args.mnq_path,
            args.label_deadband_bps,
        )
        print(f"dataset rows={len(dataset)} sessions={dataset['date'].nunique()}")
    if args.mode in {"train", "all"}:
        report = train_models(args.data_root, args.probability_threshold)
        print(json.dumps(report, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
