"""Astra: PI-event response matching research.

This is deliberately a research script, not a live strategy.  It keeps the
Discord PI tape as the event source and labels the *future* futures reaction
with a causal-at-entry feature set.  Option-wall features are joined only for
sessions for which the purchased point-in-time QQQ files exist.

Usage::

    python scripts/astra_research.py
    python scripts/astra_research.py --out F:/ancserData/astra_2026

The output directory is outside the project by default.  No broker orders or
paid data requests are made by this script.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import candle_store  # noqa: E402
from backend.data.pi_history import load_rows, parse_ts  # noqa: E402
from backend.live.pi_listener import DIRECTION, SYMBOL_MAP  # noqa: E402
from scripts.option_wall_demo import (  # noqa: E402
    _contract_profile,
    _gamma_flip,
    _map_qqq_to_mnq,
    _profile_by_strike,
    _rolling_return_beta,
    _wall_level,
)

DEFAULT_OUT = Path(r"F:\ancserData\astra_2026")
DEFAULT_OPTION_ROOT = Path(r"F:\ancserData\option_wall_august_2026")
DEFAULT_SEPTEMBER_ROOT = Path(r"F:\ancserData\option_wall_september_2026")

# The direction mapping is the same mapping used by the current PI strategy.
# Keeping it in one place prevents Astra from silently becoming a different PI
# interpretation.
DIRN = dict(DIRECTION)
if not DIRN:  # defensive fallback for an older listener import
    DIRN = {
        "青π": 1, "深蓝圈": 1, "淡蓝圈": 1,
        "粉π": -1, "紫圈": -1,
    }

HORIZONS = (5, 15, 30, 60)


def _utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_record(ts: datetime, equity: str, kind: str, size: Any,
                  pos: Any = None, message_id: Any = None) -> dict[str, Any] | None:
    symbol = str(equity or "").upper()
    future = SYMBOL_MAP.get(symbol)
    if not future or kind not in DIRN:
        return None
    return {
        "ts": _utc(ts),
        "date": _utc(ts).date().isoformat(),
        "equity": symbol,
        "future": future,
        "kind": kind,
        "size": str(size or "?"),
        "pos": pos,
        "direction": DIRN[kind],
        "message_id": str(message_id or ""),
        "source": "canonical_history",
    }


def _load_audit_events() -> list[dict[str, Any]]:
    """Read only durable Discord ``received`` audit rows for research.

    The production backtest still uses ``backend.data.pi_history.load_rows``.
    This extension is explicit because the local history snapshot ends on
    2026-08-07 while the listener audit contains later received messages.
    Duplicate callback rows are collapsed by message/time/equity/kind.
    """
    path = ROOT / "data" / "logs" / "pi_live_signals.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("event") != "received" or not row.get("ts"):
            continue
        kind = str(row.get("kind") or "")
        try:
            ts = parse_ts(row["ts"])
        except Exception:
            continue
        key = (str(row.get("message_id") or ""), ts.isoformat(),
               str(row.get("equity") or ""), kind)
        if key in seen:
            continue
        seen.add(key)
        rec = _event_record(ts, str(row.get("equity") or ""), kind,
                            row.get("size"), row.get("pos"), row.get("message_id"))
        if rec:
            rec["source"] = "discord_audit"
            out.append(rec)
    return out


def load_pi_events(include_audit: bool = True) -> pd.DataFrame:
    """Load the canonical PI history and flatten supported marks."""
    records: list[dict[str, Any]] = []
    for row in load_rows():
        symbol = str(row.get("symbol") or "").upper()
        future = SYMBOL_MAP.get(symbol)
        if not future:
            continue
        try:
            ts = parse_ts(row["ts"])
        except Exception:
            continue
        for mark in row.get("marks") or []:
            rec = _event_record(ts, symbol, mark.get("kind"), mark.get("size"),
                                mark.get("pos"), row.get("id"))
            if rec:
                records.append(rec)
    if include_audit:
        records.extend(_load_audit_events())
    # A message can exist in both the immutable archive and the listener audit.
    # Preserve the canonical copy and remove exact callback duplicates.
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for rec in records:
        key = (rec["message_id"], rec["ts"].isoformat(), rec["equity"], rec["kind"])
        old = unique.get(key)
        if old is None or old.get("source") != "canonical_history":
            unique[key] = rec
    records = list(unique.values())
    if not records:
        return pd.DataFrame(columns=["ts", "date", "equity", "future", "kind",
                                     "size", "pos", "direction"])
    return pd.DataFrame(records).sort_values("ts").reset_index(drop=True)


def _bars_for_events(events: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Range-read the two stored futures series around the PI event window."""
    out: dict[str, pd.DataFrame] = {}
    if events.empty:
        return out
    for symbol in sorted(events["future"].unique()):
        # Do not populate candle_store's long-lived cache during a research
        # run: each full snapshot is multi-million rows.  We only retain the
        # narrow event window below.
        snap = candle_store.load_snapshot(symbol, 1, use_cache=False)
        if not snap.bars:
            out[symbol] = pd.DataFrame()
            continue
        start = _utc(events.loc[events.future == symbol, "ts"].min().to_pydatetime()) - timedelta(minutes=90)
        end = _utc(events.loc[events.future == symbol, "ts"].max().to_pydatetime()) + timedelta(minutes=90)
        bars = candle_store.select_range(snap, start=start, end=end)
        rows = [{
            "ts": _utc(b.timestamp), "open": float(b.open), "high": float(b.high),
            "low": float(b.low), "close": float(b.close), "volume": float(b.volume),
        } for b in bars]
        frame = pd.DataFrame(rows).sort_values("ts").drop_duplicates("ts")
        out[symbol] = frame.reset_index(drop=True)
    return out


def _add_atr(frame: pd.DataFrame) -> pd.DataFrame:
    """Add completed 5-minute ATR blend without using future bars."""
    if frame.empty:
        return frame
    x = frame.copy()
    x = x.set_index("ts")
    five = x.resample("5min", origin="start_day", offset="30min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    prev = five["close"].shift(1)
    tr = pd.concat([
        five["high"] - five["low"],
        (five["high"] - prev).abs(),
        (five["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(14, min_periods=1).mean()
    atr50 = tr.rolling(50, min_periods=1).mean()
    five["atr_blend"] = (atr14 + atr50) / 2.0
    # Resample labels bins at their left edge by default.  Move the feature to
    # the right edge so an event inside 14:15--14:20 cannot see that candle's
    # high/low/close before 14:20.
    five.index = five.index + pd.Timedelta(minutes=5)
    # merge_asof uses only the completed 5m candle at or before the event.
    left = x.reset_index().sort_values("ts")
    right = five[["atr_blend"]].reset_index().sort_values("ts")
    merged = pd.merge_asof(left, right, on="ts", direction="backward")
    return merged.reset_index(drop=True)


def _first_at_or_after(times: np.ndarray, target: pd.Timestamp) -> int | None:
    i = int(np.searchsorted(times, target.to_datetime64(), side="left"))
    return i if i < len(times) else None


def _reaction(event: pd.Series, frame: pd.DataFrame) -> dict[str, Any] | None:
    """Create causal-at-entry features and future reaction labels."""
    if frame.empty:
        return None
    ts = pd.Timestamp(event.ts)
    times = frame["ts"].to_numpy(dtype="datetime64[ns]")
    i0 = _first_at_or_after(times, ts)
    if i0 is None or i0 + 1 >= len(frame):
        return None
    entry = float(frame.iloc[i0].close)
    atr = _safe_float(frame.iloc[i0].atr_blend)
    if atr is None or atr <= 0:
        return None
    direction = int(event.direction)
    result: dict[str, Any] = {"entry_ts": frame.iloc[i0].ts.isoformat(),
                              "entry": entry, "atr_blend": atr}
    for horizon in HORIZONS:
        target = ts + pd.Timedelta(minutes=horizon)
        j = _first_at_or_after(times, target)
        if j is None:
            result[f"r_{horizon}m"] = None
            result[f"mfe_{horizon}m"] = None
            result[f"mae_{horizon}m"] = None
            continue
        path = frame.iloc[i0:j + 1]
        favorable = direction * (path["high"].to_numpy() - entry)
        favorable = np.maximum(favorable, direction * (entry - path["low"].to_numpy()))
        adverse = direction * (path["low"].to_numpy() - entry)
        adverse = np.minimum(adverse, direction * (path["high"].to_numpy() - entry))
        close_move = direction * (float(frame.iloc[j].close) - entry)
        result[f"r_{horizon}m"] = close_move / atr
        result[f"mfe_{horizon}m"] = float(np.max(favorable)) / atr
        result[f"mae_{horizon}m"] = float(np.min(adverse)) / atr
    # Reactions are labels, not entry filters.  The label has a separate
    # numeric score so stars can be audited rather than visually guessed.
    r5, r15, r30, r60 = [result.get(f"r_{h}m") for h in HORIZONS]
    vals = [v for v in (r5, r15, r30, r60) if v is not None]
    if not vals:
        return None
    max_mfe = max(result.get(f"mfe_{h}m") or float("-inf") for h in HORIZONS)
    if (r5 is not None and r5 <= -0.25) or (r15 is not None and r15 <= -0.50):
        reaction = "reversal"
    elif (r5 is not None and r5 >= 0.25 and r15 is not None and r15 >= 0.50):
        reaction = "immediate_continuation"
    elif (r30 is not None and r30 >= 0.50):
        reaction = "delayed_continuation"
    elif max_mfe >= 0.25 and (r60 is None or r60 > -0.25):
        reaction = "impulse_then_fade"
    else:
        reaction = "mixed_or_flat"
    # Stars describe *reaction type*, rather than hindsight PnL magnitude:
    #   3 = immediate continuation, 2 = delayed continuation,
    #   1 = an impulse that faded, 0 = reversal or mixed/flat.
    stars = {
        "immediate_continuation": 3,
        "delayed_continuation": 2,
        "impulse_then_fade": 1,
        "reversal": 0,
        "mixed_or_flat": 0,
    }[reaction]
    result.update({"reaction": reaction, "stars": stars})
    return result


def build_price_dataset(events: pd.DataFrame, bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, event in events.iterrows():
        frame = bars.get(event.future)
        if frame is None or frame.empty:
            continue
        label = _reaction(event, frame)
        if label is None:
            continue
        row = event.to_dict()
        row.update(label)
        rows.append(row)
    return pd.DataFrame(rows)


def _quantile_score(series: pd.Series, value: float) -> float | None:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty or value is None or not np.isfinite(value):
        return None
    return float((s <= value).mean())


def _load_august_option_features(signal_rows: pd.DataFrame,
                                 option_root: Path,
                                 mnq_frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """Build point-in-time option-wall features for Aug 3-7 PI events.

    This intentionally mirrors the existing option-wall proxy: OI before
    10:00 ET, cumulative option volume thereafter.  It is not signed option
    flow and is only joined when all required raw files exist.
    """
    if signal_rows.empty or not option_root.exists():
        return pd.DataFrame()
    defs_path = option_root / "qqq_definition.csv.gz"
    bbo_path = option_root / "raw" / "qqq_bbo_1s.csv.gz"
    if not defs_path.exists() or not bbo_path.exists():
        return pd.DataFrame()
    defs_all = pd.read_csv(defs_path, compression="gzip")
    bbo_all = pd.read_csv(bbo_path, compression="gzip")
    bbo_all["ts"] = pd.to_datetime(bbo_all["ts_recv"], utc=True)
    bbo_all["mid"] = (bbo_all["bid_px_00"] + bbo_all["ask_px_00"]) * 0.5
    bbo_all = bbo_all[bbo_all["mid"].notna()].sort_values("ts")
    if mnq_frame is None or mnq_frame.empty:
        return pd.DataFrame()

    out: list[dict[str, Any]] = []
    # Only QQQ events have a QQQ option chain.  SPY PI events remain in the
    # price-only dataset instead of being incorrectly assigned QQQ walls.
    qqq = signal_rows[(signal_rows["equity"] == "QQQ") &
                      signal_rows["date"].str.startswith("2026-08-")]
    for day_str, group in qqq.groupby("date"):
        day_dir = option_root / "raw" / day_str
        required = [day_dir / f"qqq_0dte_{kind}.csv.gz"
                    for kind in ("statistics", "cbbo_1m", "ohlcv_1m")]
        if not all(p.exists() for p in required):
            continue
        day = date.fromisoformat(day_str)
        defs = defs_all.copy()
        expiry = pd.to_datetime(defs["expiration"], utc=True, errors="coerce")
        defs = defs[(expiry.dt.date.astype(str) == day_str) &
                    defs["instrument_class"].isin(["C", "P"])].copy()
        definitions = {row.raw_symbol: {"strike": float(row.strike_price),
                                       "class": row.instrument_class}
                       for row in defs.itertuples()}
        stats = pd.read_csv(required[0], compression="gzip")
        oi = stats[stats["stat_type"] == 9].groupby("symbol")["quantity"].first().astype(int).to_dict()
        quotes = pd.read_csv(required[1], compression="gzip")
        quotes["ts"] = pd.to_datetime(quotes["ts_recv"], utc=True)
        quotes = quotes.sort_values("ts")
        volume = pd.read_csv(required[2], compression="gzip")
        volume["ts"] = pd.to_datetime(volume["ts_event"], utc=True) + pd.Timedelta(minutes=1)
        volume = volume.groupby(["ts", "symbol"], as_index=False)["volume"].sum().sort_values("ts")
        day_bbo = bbo_all[bbo_all["ts"].dt.date.astype(str) == day_str]
        if day_bbo.empty:
            continue
        qqq_minute = day_bbo.set_index("ts")["mid"].resample("1min").last().dropna()
        day_start = pd.Timestamp(datetime.combine(day, datetime.min.time(), timezone.utc))
        day_end = day_start + pd.Timedelta(days=1)
        mnq = mnq_frame[(mnq_frame["ts"] >= day_start) &
                        (mnq_frame["ts"] < day_end)][["ts", "close"]].copy()
        if mnq.empty:
            continue
        mnq = mnq.set_index("ts")["close"].sort_index()
        latest_quotes: dict[str, dict[str, Any]] = {}
        cumulative_volume: defaultdict[str, int] = defaultdict(int)
        quote_records = quotes.to_dict("records")
        volume_records = volume.to_dict("records")
        qidx = vidx = 0
        expiry_ts = pd.Timestamp(datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=20, minutes=15))
        for _, event in group.sort_values("ts").iterrows():
            as_of = pd.Timestamp(event.ts)
            while qidx < len(quote_records) and quote_records[qidx]["ts"] <= as_of:
                q = quote_records[qidx]
                latest_quotes[q["symbol"]] = {
                    "ts": q["ts"], "bid": q["bid_px_00"], "ask": q["ask_px_00"]
                }
                qidx += 1
            while vidx < len(volume_records) and volume_records[vidx]["ts"] <= as_of:
                v = volume_records[vidx]
                cumulative_volume[v["symbol"]] += int(v["volume"])
                vidx += 1
            qavail = qqq_minute.loc[qqq_minute.index <= as_of]
            mavail = mnq.loc[mnq.index <= as_of]
            if qavail.empty or mavail.empty:
                continue
            qqq_spot, mnq_spot = float(qavail.iloc[-1]), float(mavail.iloc[-1])
            profile = _contract_profile(latest_quotes, cumulative_volume, definitions,
                                       oi, qqq_spot, as_of, expiry_ts)
            grouped = _profile_by_strike(profile)
            if grouped.empty:
                continue
            oi_call = _wall_level(grouped, "C", "oi_gex")
            oi_put = _wall_level(grouped, "P", "oi_gex")
            vol_call = _wall_level(grouped, "C", "volume_gex")
            vol_put = _wall_level(grouped, "P", "volume_gex")
            use_volume = as_of.time() >= datetime.strptime("14:00", "%H:%M").time() and vol_call is not None and vol_put is not None
            call = vol_call if use_volume else oi_call
            put = vol_put if use_volume else oi_put
            years = max((expiry_ts - as_of).total_seconds(), 1.0) / (365 * 24 * 3600)
            flip = _gamma_flip(profile, qqq_spot, years)
            if flip is not None and abs(flip / qqq_spot - 1.0) > 0.012:
                flip = None
            beta = _rolling_return_beta(qqq_minute, mnq, as_of)
            out.append({
                "ts": event.ts,
                "option_date": day_str,
                "option_available": True,
                "wall_source": "volume" if use_volume else "oi",
                "call_wall_qqq": call,
                "put_wall_qqq": put,
                "gamma_flip_qqq": flip,
                "call_wall_mnq": _map_qqq_to_mnq(call, qqq_spot, mnq_spot, beta),
                "put_wall_mnq": _map_qqq_to_mnq(put, qqq_spot, mnq_spot, beta),
                "gamma_flip_mnq": _map_qqq_to_mnq(flip, qqq_spot, mnq_spot, beta),
                "qqq_spot": qqq_spot,
                "mnq_spot": mnq_spot,
                "net_oi_gex": float(profile["oi_gex"].sum()),
                "net_volume_gex": float(profile["volume_gex"].sum()),
            })
    return pd.DataFrame(out)


def _load_derived_option_features(signal_rows: pd.DataFrame,
                                  september_root: Path) -> pd.DataFrame:
    """Join already-built point-in-time September option-wall tapes."""
    if signal_rows.empty or not september_root.exists():
        return pd.DataFrame()
    qqq = signal_rows[(signal_rows["equity"] == "QQQ") &
                      signal_rows["date"].str.startswith("2026-09-")]
    out: list[dict[str, Any]] = []
    for day_str, group in qqq.groupby("date"):
        p = september_root / "raw" / day_str / "derived.json"
        if not p.exists():
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
            snapshots = pd.DataFrame(payload.get("snapshots") or [])
            snapshots["as_of"] = pd.to_datetime(snapshots["as_of"], utc=True)
            snapshots = snapshots.sort_values("as_of")
        except Exception:
            continue
        if snapshots.empty:
            continue
        for _, event in group.sort_values("ts").iterrows():
            ts = pd.Timestamp(event.ts)
            prior = snapshots[snapshots["as_of"] <= ts]
            if prior.empty:
                continue
            snap = prior.iloc[-1]
            out.append({
                "ts": event.ts,
                "option_date": day_str,
                "option_available": True,
                "wall_source": snap.get("wall_source"),
                "call_wall_qqq": snap.get("call_wall_qqq"),
                "put_wall_qqq": snap.get("put_wall_qqq"),
                "gamma_flip_qqq": snap.get("gamma_flip_qqq"),
                "call_wall_mnq": snap.get("call_wall_mnq"),
                "put_wall_mnq": snap.get("put_wall_mnq"),
                "gamma_flip_mnq": snap.get("gamma_flip_mnq"),
                "qqq_spot": snap.get("qqq_spot"),
                "mnq_spot": snap.get("mnq_spot"),
                "net_oi_gex": snap.get("net_oi_gex_1pct"),
                "net_volume_gex": snap.get("net_volume_gex_1pct"),
            })
    return pd.DataFrame(out)


def summarize(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"n": 0}
    def group_stats(key: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for value, g in frame.groupby(key, dropna=False):
            x = pd.to_numeric(g["r_15m"], errors="coerce").dropna()
            result[str(value)] = {
                "n": int(len(g)),
                "hit_15m": float((x > 0).mean()) if len(x) else None,
                "mean_r15_atr": float(x.mean()) if len(x) else None,
                "stars": {str(k): int((g["stars"] == k).sum()) for k in range(4)},
            }
        return result
    return {
        "n": int(len(frame)),
        "days": int(frame["date"].nunique()),
        "hit_5m": float((frame["r_5m"] > 0).mean()),
        "hit_15m": float((frame["r_15m"] > 0).mean()),
        "hit_30m": float((frame["r_30m"] > 0).mean()),
        "mean_r15_atr": float(frame["r_15m"].mean()),
        "mean_r30_atr": float(frame["r_30m"].mean()),
        "stars": {str(k): int((frame["stars"] == k).sum()) for k in range(4)},
        "reaction": frame["reaction"].value_counts(dropna=False).to_dict(),
        "by_kind": group_stats("kind"),
        "by_size": group_stats("size"),
        "by_equity": group_stats("equity"),
    }


def _simple_group(frame: pd.DataFrame, key: str) -> dict[str, Any]:
    if frame.empty or key not in frame:
        return {}
    out: dict[str, Any] = {}
    for value, group in frame.groupby(key, dropna=False):
        r = pd.to_numeric(group["r_15m"], errors="coerce").dropna()
        out[str(value)] = {
            "n": int(len(group)),
            "hit_15m": float((r > 0).mean()) if len(r) else None,
            "mean_r15_atr": float(r.mean()) if len(r) else None,
            "stars": {str(k): int((group["stars"] == k).sum()) for k in range(4)},
        }
    return out


def build_match_analysis(frame: pd.DataFrame) -> dict[str, Any]:
    """Return stability and point-in-time feature matching diagnostics."""
    if frame.empty:
        return {"daily": [], "option": {}}
    daily: list[dict[str, Any]] = []
    for day, group in frame.groupby("date", sort=True):
        daily.append({
            "date": str(day), "n": int(len(group)),
            "hit_15m": float((group["r_15m"] > 0).mean()),
            "mean_r15_atr": float(group["r_15m"].mean()),
            "star0": int((group["stars"] == 0).sum()),
            "star1": int((group["stars"] == 1).sum()),
            "star2": int((group["stars"] == 2).sum()),
            "star3": int((group["stars"] == 3).sum()),
        })
    if "option_available" in frame:
        option_mask = frame["option_available"].astype("boolean").fillna(False)
    else:
        option_mask = pd.Series(False, index=frame.index)
    option = frame[option_mask].copy()
    if option.empty:
        return {"daily": daily, "option": {"n": 0}}
    option["call_wall_mnq"] = pd.to_numeric(option["call_wall_mnq"], errors="coerce")
    option["put_wall_mnq"] = pd.to_numeric(option["put_wall_mnq"], errors="coerce")
    option["net_oi_gex"] = pd.to_numeric(option["net_oi_gex"], errors="coerce")
    option["net_volume_gex"] = pd.to_numeric(option["net_volume_gex"], errors="coerce")
    option["target_wall_mnq"] = np.where(
        option["direction"] > 0, option["call_wall_mnq"], option["put_wall_mnq"]
    )
    option["target_wall_dist_atr"] = (
        option["direction"] * (option["target_wall_mnq"] - option["entry"])
        / option["atr_blend"]
    )
    option["wall_in_direction"] = option["target_wall_dist_atr"] > 0
    option["target_wall_near"] = option["target_wall_dist_atr"].abs() <= 1.5
    option["between_walls"] = (
        (option["entry"] >= option["put_wall_mnq"])
        & (option["entry"] <= option["call_wall_mnq"])
    )
    option["gex_sign"] = np.sign(option["net_oi_gex"])
    correlations: dict[str, float | None] = {}
    for column in ("target_wall_dist_atr", "call_wall_mnq", "put_wall_mnq",
                   "gamma_flip_mnq", "net_oi_gex", "net_volume_gex"):
        if column not in option:
            continue
        pair = option[[column, "r_15m"]].dropna()
        correlations[column] = (
            float(pair.corr(method="spearman").iloc[0, 1])
            if len(pair) >= 3 else None
        )
    return {
        "daily": daily,
        "option": {
            "n": int(len(option)),
            "by_wall_in_direction": _simple_group(option, "wall_in_direction"),
            "by_target_wall_near_1_5_atr": _simple_group(option, "target_wall_near"),
            "by_between_walls": _simple_group(option, "between_walls"),
            "by_gex_sign": _simple_group(option, "gex_sign"),
            "spearman_with_r15": correlations,
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--option-root", type=Path, default=DEFAULT_OPTION_ROOT)
    ap.add_argument("--september-root", type=Path, default=DEFAULT_SEPTEMBER_ROOT)
    ap.add_argument("--no-audit", action="store_true",
                    help="use only the canonical pi_signals.json archive")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    events = load_pi_events(include_audit=not args.no_audit)
    bars = _bars_for_events(events)
    for symbol, frame in list(bars.items()):
        bars[symbol] = _add_atr(frame)
    price = build_price_dataset(events, bars)
    option_aug = _load_august_option_features(price, args.option_root, bars.get("MNQ"))
    option_sep = _load_derived_option_features(price, args.september_root)
    option_frames = [x for x in (option_aug, option_sep) if not x.empty]
    option = pd.concat(option_frames, ignore_index=True) if option_frames else pd.DataFrame()
    if not option.empty:
        option = option.drop_duplicates("ts", keep="last")
        price = price.merge(option, on="ts", how="left")
        for col in ("call_wall_mnq", "put_wall_mnq", "gamma_flip_mnq"):
            price[f"{col}_dist"] = price[col] - price["entry"]
        price["wall_above"] = price["call_wall_mnq"] > price["entry"]
        price["wall_below"] = price["put_wall_mnq"] < price["entry"]
        price["gex_sign"] = np.sign(price["net_oi_gex"])
    price["hour_utc"] = pd.to_datetime(price["ts"], utc=True).dt.hour
    # A transparent null comparison: reverse only the PI direction, keeping
    # the same events and entry prices.  This is not a trading strategy; it is
    # a directional sanity check for the PI tape.
    actual = price["r_15m"].mean() if not price.empty else None
    inverse = (-price["r_15m"]).mean() if not price.empty else None
    report = {
        "name": "Astra PI-response matching research",
        "definition": {
            "source": "backend.data.pi_history.load_rows",
            "price_label": "future direction-signed close move divided by completed 5m ATR blend",
            "stars": {
                "0": "reversal or mixed/flat reaction",
                "1": "initial impulse that faded by the later horizon",
                "2": "delayed continuation (30m confirmation)",
                "3": "immediate continuation (5m + 15m confirmation)",
            },
            "reactions": ["immediate_continuation", "delayed_continuation",
                          "impulse_then_fade", "reversal", "mixed_or_flat"],
            "null": "inverse direction on the same event timestamps; not a tradable benchmark",
        },
        "events_loaded": int(len(events)),
        "events_labelled": int(len(price)),
        "option_feature_events": int(price.get("option_available", pd.Series(dtype=bool)).astype("boolean").fillna(False).sum()),
        "event_sources": price["source"].value_counts(dropna=False).to_dict() if not price.empty else {},
        "price_summary": summarize(price),
        "match_analysis": build_match_analysis(price),
        "direction_sanity": {"actual_mean_r15_atr": actual, "inverse_mean_r15_atr": inverse},
    }
    price.to_csv(args.out / "astra_event_dataset.csv", index=False, encoding="utf-8-sig")
    (args.out / "astra_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    # A compact human-readable summary is useful when the script is run from a
    # terminal without opening the JSON.
    lines = [
        "Astra PI-response research",
        f"events loaded={len(events)} labelled={len(price)} option-feature-events={report['option_feature_events']}",
        f"hit15={report['price_summary'].get('hit_15m')} mean_r15_atr={report['price_summary'].get('mean_r15_atr')}",
        f"stars={report['price_summary'].get('stars')} reactions={report['price_summary'].get('reaction')}",
    ]
    (args.out / "astra_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"outputs: {args.out}")


if __name__ == "__main__":
    main()
