"""Full MNQ/ES signal factor research.

Research-only. It does not touch live engines, broker orders, presets, or the
running server.

Core assumptions:
- Signals are evaluated on completed 5-minute bars.
- Entries are market orders at the next 5-minute open.
- PnL is normalized to one contract of the requested symbol (1 MNQ by default).
- Research outputs are written under scratchpad/full_signal_factor_research/.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import Candle
from backend.strategy.consolidation import ClockBucketZoneDetector


OUT_ROOT = ROOT / "scratchpad" / "full_signal_factor_research"
UTC = timezone.utc
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    point_value: float
    tick_size: float
    round_turn_cost: float


@dataclass(frozen=True)
class ExitConfig:
    config_id: str
    signal_family: str
    side_mode: str
    sl_rule: str
    tp_rule: str
    sl_value: float
    tp_value: float
    max_hold_bars: int


@dataclass
class SimTrade:
    config_id: str
    signal_family: str
    side_mode: str
    signal_id: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry: float
    exit: float
    sl: float
    tp: float
    exit_reason: str
    hold_bars: int
    pnl: float
    gross_pnl: float
    session: str
    trade_date: str
    week: str
    month: str


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _spec_for_symbol(symbol: str) -> ContractSpec:
    sym = symbol.upper().replace("/", "").replace("=F", "")
    if sym == "MNQ":
        return ContractSpec("MNQ", 2.0, 0.25, 1.24)
    if sym == "NQ":
        return ContractSpec("NQ", 20.0, 0.25, 3.80)
    if sym == "MES":
        return ContractSpec("MES", 5.0, 0.25, 1.24)
    if sym == "ES":
        return ContractSpec("ES", 50.0, 0.25, 3.80)
    return ContractSpec(sym, 1.0, 0.01, 0.0)


def _session_for(ts: datetime) -> str:
    ts = _utc(ts)
    tod = ts.time()
    if tod >= time(22, 0) or tod < time(7, 0):
        return "ASIA"
    if time(7, 0) <= tod < time(11, 0):
        return "EURO"
    if time(11, 0) <= tod < time(13, 30):
        return "PRE"
    if time(13, 30) <= tod < time(20, 0):
        return "RTH"
    return "AH"


def _period_week(ts: datetime) -> str:
    iso = _utc(ts).date().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _period_month(ts: datetime) -> str:
    dt = _utc(ts)
    return f"{dt.year}-{dt.month:02d}"


def _round_to_tick(price: float, tick: float) -> float:
    return round(float(price) / tick) * tick


def _load_candles(symbol: str, max_bars: int = 0) -> list[Candle]:
    bars = sorted(candle_store.load(symbol.upper(), 1), key=lambda c: _utc(c.timestamp))
    if max_bars and max_bars > 0:
        bars = bars[-max_bars:]
    return bars


def candles_to_df(candles: list[Candle]) -> pd.DataFrame:
    rows = [
        {
            "timestamp": _utc(c.timestamp),
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(c.volume or 0),
        }
        for c in candles
    ]
    return pd.DataFrame(rows).set_index("timestamp").sort_index()


def aggregate_5m(candles: list[Candle]) -> pd.DataFrame:
    bars = BacktestEngine.aggregate_1m_to_5m(candles)
    rows = [
        {
            "timestamp": _utc(c.timestamp),
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": float(c.volume or 0),
        }
        for c in bars
    ]
    df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    if df.empty:
        return df
    df["session"] = df["timestamp"].map(_session_for)
    df["trade_date"] = df["timestamp"].map(lambda x: str(_topstep_trade_date(_utc(x))))
    df["week"] = df["timestamp"].map(_period_week)
    df["month"] = df["timestamp"].map(_period_month)
    return df


def _rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def _bcwsma(values: pd.Series, length: int, multiplier: int) -> pd.Series:
    out = []
    prev = 0.0
    for raw in values.fillna(0.0).to_numpy(float):
        prev = (multiplier * raw + (length - multiplier) * prev) / float(length)
        out.append(prev)
    return pd.Series(out, index=values.index)


def add_factors(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["tr"] = tr
    out["atr14"] = tr.rolling(14, min_periods=7).mean()
    out["atr50"] = tr.rolling(50, min_periods=25).mean()
    out["range15"] = (
        out["high"].rolling(3, min_periods=3).max()
        - out["low"].rolling(3, min_periods=3).min()
    )

    low9 = out["low"].rolling(9, min_periods=9).min()
    high9 = out["high"].rolling(9, min_periods=9).max()
    rsv = 100.0 * (close - low9) / (high9 - low9).replace(0, np.nan)
    k = _bcwsma(rsv, 3, 1)
    d = _bcwsma(k, 3, 1)
    out["kdj_j"] = 3.0 * k - 2.0 * d

    delta = close.diff()
    up = _rma(delta.clip(lower=0), 14)
    down = _rma((-delta.clip(upper=0)), 14)
    rs = up / down.replace(0, np.nan)
    out["rsi14"] = np.where(
        down == 0,
        100.0,
        np.where(up == 0, 0.0, 100.0 - (100.0 / (1.0 + rs))),
    )
    out["ifb_short"] = (
        (out["kdj_j"] > 80)
        & (out["kdj_j"] < out["kdj_j"].shift(1))
        & (close > close.shift(1))
        & (out["rsi14"] > 60)
    )
    out["ifb_long"] = (
        (out["kdj_j"] < 20)
        & (out["kdj_j"] > out["kdj_j"].shift(1))
        & (close < close.shift(1))
        & (out["rsi14"] < 40)
    )

    roc = 100.0 * (close - close.shift(1)) / close.shift(1).replace(0, np.nan)
    pmo = (10.0 * roc.ewm(span=100, adjust=False).mean()).ewm(span=50, adjust=False).mean()
    pmo_sig = pmo.ewm(span=10, adjust=False).mean()
    out["pmo"] = pmo
    out["pmo_signal"] = pmo_sig
    p = pmo - pmo_sig
    q = pmo_sig - pmo
    crossunder = (pmo < pmo_sig) & (pmo.shift(1) >= pmo_sig.shift(1))
    crossover = (pmo > pmo_sig) & (pmo.shift(1) <= pmo_sig.shift(1))
    out["pmo_normal_short"] = (pmo > 0.06) & crossunder
    out["pmo_normal_long"] = (pmo < -0.10) & crossover
    out["pmo_early_short"] = (pmo_sig > 0.06) & (p < p.shift(1)) & (pmo > pmo_sig) & (p.shift(1) < p.shift(2))
    out["pmo_early_long"] = (pmo_sig < -0.10) & (q < q.shift(1)) & (pmo < pmo_sig) & (q.shift(1) < q.shift(2))

    lookback = 40
    atr = out["atr14"].replace(0, np.nan)
    out["mom_norm"] = (close - close.shift(lookback)) / (atr * math.sqrt(lookback))
    mean = close.ewm(span=12, adjust=False).mean()
    out["rev_mean"] = mean
    out["rev_z"] = (close - mean) / atr
    out["mrev_long"] = (out["mom_norm"] >= 0.4) & (out["rev_z"] <= -1.1)
    out["mrev_short"] = (out["mom_norm"] <= -0.4) & (out["rev_z"] >= 1.1)
    return out


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


def build_signal_events(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add_row(i: int, family: str, subtype: str, direction: int):
        if i + 1 >= len(df):
            return
        src = df.iloc[i]
        ent = df.iloc[i + 1]
        atr = _safe_float(src.get("atr14"), 0.0)
        if atr <= 0:
            return
        side = "long" if direction > 0 else "short"
        sid = f"{family}:{subtype}:{side}:{ent.timestamp.isoformat()}"
        rows.append(
            {
                "signal_id": sid,
                "signal_family": family,
                "signal_subtype": subtype,
                "direction": direction,
                "side": side,
                "signal_time": src.timestamp,
                "entry_time": ent.timestamp,
                "source_i": i,
                "entry_i": i + 1,
                "entry": float(ent.open),
                "source_close": float(src.close),
                "atr14": atr,
                "atr50": _safe_float(src.get("atr50"), atr),
                "range15": _safe_float(src.get("range15"), atr),
                "session": str(src.session),
                "trade_date": str(ent.trade_date),
                "week": str(ent.week),
                "month": str(ent.month),
                "pmo": _safe_float(src.get("pmo"), np.nan),
                "pmo_signal": _safe_float(src.get("pmo_signal"), np.nan),
                "kdj_j": _safe_float(src.get("kdj_j"), np.nan),
                "rsi14": _safe_float(src.get("rsi14"), np.nan),
                "mom_norm": _safe_float(src.get("mom_norm"), np.nan),
                "rev_z": _safe_float(src.get("rev_z"), np.nan),
            }
        )

    for i in range(len(df) - 1):
        row = df.iloc[i]
        pmo_subtypes_long = []
        pmo_subtypes_short = []
        if bool(row.get("pmo_normal_long")):
            pmo_subtypes_long.append("normal")
        if bool(row.get("pmo_early_long")):
            pmo_subtypes_long.append("early")
        if bool(row.get("pmo_normal_short")):
            pmo_subtypes_short.append("normal")
        if bool(row.get("pmo_early_short")):
            pmo_subtypes_short.append("early")
        if pmo_subtypes_long:
            add_row(i, "emapmo", "+".join(pmo_subtypes_long), 1)
        if pmo_subtypes_short:
            add_row(i, "emapmo", "+".join(pmo_subtypes_short), -1)
        if bool(row.get("ifb_long")):
            add_row(i, "icefishball", "kdjma", 1)
        if bool(row.get("ifb_short")):
            add_row(i, "icefishball", "kdjma", -1)
        if bool(row.get("mrev_long")):
            add_row(i, "momentum_reversion", "m200r5_0362", 1)
        if bool(row.get("mrev_short")):
            add_row(i, "momentum_reversion", "m200r5_0362", -1)

    events = pd.DataFrame(rows)
    if events.empty:
        return events
    events.sort_values(["entry_time", "signal_family", "side"], inplace=True)
    events.reset_index(drop=True, inplace=True)
    add_cluster_features(events)
    return events


def add_cluster_features(events: pd.DataFrame, window_bars: int = 1) -> None:
    by_i: dict[int, list[dict[str, Any]]] = {}
    for rec in events.to_dict("records"):
        by_i.setdefault(int(rec["entry_i"]), []).append(rec)
    sizes = []
    kinds = []
    labels = []
    for rec in events.to_dict("records"):
        i = int(rec["entry_i"])
        cluster = []
        for j in range(i - window_bars, i + window_bars + 1):
            cluster.extend(by_i.get(j, []))
        fams = [str(x["signal_family"]) for x in cluster]
        unique = sorted(set(fams))
        sizes.append(len(cluster))
        labels.append("+".join(unique))
        if len(cluster) <= 1:
            kinds.append("solo")
        elif len(unique) == 1:
            kinds.append("same_family")
        elif len(unique) >= 3:
            kinds.append("all3_mixed")
        else:
            kinds.append("mixed")
    events["cluster_size"] = sizes
    events["cluster_kind"] = kinds
    events["cluster_families"] = labels


def build_zone_context(
    candles_1m: list[Candle],
    events: pd.DataFrame,
    tfs: list[str],
    spec: ContractSpec,
) -> pd.DataFrame:
    if events.empty:
        return events
    entry_times = set(np.array(pd.to_datetime(events["entry_time"], utc=True).dt.to_pydatetime()))
    detectors = {
        tf: ClockBucketZoneDetector(
            area_timeframe=tf,
            value_area_pct=0.80,
            tick_size=spec.tick_size,
            max_recent=8,
            recalc_active_each_bar=False,
        )
        for tf in tfs
    }
    snapshots: dict[datetime, dict[str, Any]] = {}
    for candle in sorted(candles_1m, key=lambda c: _utc(c.timestamp)):
        ts = _utc(candle.timestamp)
        for detector in detectors.values():
            detector.update(candle)
        if ts not in entry_times:
            continue
        price = float(candle.open)
        snap: dict[str, Any] = {}
        for tf, detector in detectors.items():
            zone = detector.get_active_zone()
            prefix = f"zone_{tf}_"
            if zone is None:
                snap[prefix + "state"] = "none"
                continue
            width = max(0.0, float(zone.high_100) - float(zone.low_100))
            if price > float(zone.high_100):
                state = "breakout_full_up"
            elif price < float(zone.low_100):
                state = "breakout_full_down"
            elif price > float(zone.vah_80):
                state = "above_vah"
            elif price < float(zone.val_80):
                state = "below_val"
            else:
                state = "inside_va"
            snap.update(
                {
                    prefix + "id": zone.zone_id,
                    prefix + "state": state,
                    prefix + "poc": round(float(zone.poc), 4),
                    prefix + "vah": round(float(zone.vah_80), 4),
                    prefix + "val": round(float(zone.val_80), 4),
                    prefix + "high": round(float(zone.high_100), 4),
                    prefix + "low": round(float(zone.low_100), 4),
                    prefix + "width": round(width, 4),
                    prefix + "age_min": int((ts - _utc(zone.left_at or zone.formed_at)).total_seconds() // 60),
                }
            )
        snapshots[ts] = snap

    zone_rows = []
    for ts in np.array(pd.to_datetime(events["entry_time"], utc=True).dt.to_pydatetime()):
        zone_rows.append(snapshots.get(_utc(ts), {}))
    zone_df = pd.DataFrame(zone_rows)
    return pd.concat([events.reset_index(drop=True), zone_df.reset_index(drop=True)], axis=1)


def add_forward_outcomes(df: pd.DataFrame, events: pd.DataFrame, horizons: Iterable[int]) -> pd.DataFrame:
    if events.empty:
        return events
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    rows = events.copy()
    for h in horizons:
        correct = []
        ret_points = []
        mfe_points = []
        mae_points = []
        peak_capture = []
        wrong_then_right = []
        for rec in events.to_dict("records"):
            entry_i = int(rec["entry_i"])
            direction = int(rec["direction"])
            entry = float(rec["entry"])
            end_i = min(len(df) - 1, entry_i + int(h))
            if end_i <= entry_i:
                correct.append(np.nan)
                ret_points.append(np.nan)
                mfe_points.append(np.nan)
                mae_points.append(np.nan)
                peak_capture.append(np.nan)
                wrong_then_right.append(np.nan)
                continue
            segment_high = float(np.max(highs[entry_i:end_i + 1]))
            segment_low = float(np.min(lows[entry_i:end_i + 1]))
            ret = (float(closes[end_i]) - entry) * direction
            mfe = (segment_high - entry) if direction > 0 else (entry - segment_low)
            mae = (entry - segment_low) if direction > 0 else (segment_high - entry)
            atr = max(float(rec["atr14"]), 1e-9)
            correct.append(1 if ret > 0 else 0)
            ret_points.append(ret)
            mfe_points.append(mfe)
            mae_points.append(mae)
            peak_capture.append(1 if (mfe >= atr and mae <= 0.5 * atr) else 0)

            short_end = min(len(df) - 1, entry_i + max(1, int(h) // 4))
            short_ret = (float(closes[short_end]) - entry) * direction
            wrong_then_right.append(1 if short_ret < 0 and ret > 0 else 0)

        rows[f"h{h}_correct"] = correct
        rows[f"h{h}_ret_points"] = ret_points
        rows[f"h{h}_mfe_points"] = mfe_points
        rows[f"h{h}_mae_points"] = mae_points
        rows[f"h{h}_peak_capture"] = peak_capture
        rows[f"h{h}_wrong_short_right_long"] = wrong_then_right
    return rows


def _risk_width(rec: dict[str, Any], rule: str, value: float, tick: float) -> float:
    if rule == "fixed":
        width = float(value)
    elif rule == "atr":
        width = float(rec["atr14"]) * float(value)
    elif rule == "atr_blend":
        width = ((float(rec["atr14"]) + float(rec.get("atr50") or rec["atr14"])) / 2.0) * float(value)
    elif rule == "range15_pct":
        width = max(float(rec["range15"]), float(rec["atr14"])) * float(value)
    else:
        width = float(rec["atr14"]) * float(value)
    return max(tick, width)


def _exit_hit(direction: int, high: float, low: float, sl: float, tp: float) -> tuple[float, str] | None:
    if direction > 0:
        hit_sl = low <= sl
        hit_tp = high >= tp
        if hit_sl:
            return sl, "sl" if not hit_tp else "sl_same_bar"
        if hit_tp:
            return tp, "tp"
    else:
        hit_sl = high >= sl
        hit_tp = low <= tp
        if hit_sl:
            return sl, "sl" if not hit_tp else "sl_same_bar"
        if hit_tp:
            return tp, "tp"
    return None


def simulate_config(
    df: pd.DataFrame,
    event_records: list[dict[str, Any]],
    spec: ContractSpec,
    cfg: ExitConfig,
) -> list[SimTrade]:
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    times = df["timestamp"].to_list()
    sessions = df["session"].to_list()
    trade_dates = df["trade_date"].to_list()
    weeks = df["week"].to_list()
    months = df["month"].to_list()

    if not event_records:
        return []

    trades: list[SimTrade] = []
    next_allowed_i = -1
    for rec in event_records:
        entry_i = int(rec["entry_i"])
        if entry_i <= next_allowed_i or entry_i >= len(df):
            continue
        direction = int(rec["direction"])
        entry = _round_to_tick(float(rec["entry"]), spec.tick_size)
        sl_w = _risk_width(rec, cfg.sl_rule, cfg.sl_value, spec.tick_size)
        tp_w = _risk_width(rec, cfg.tp_rule, cfg.tp_value, spec.tick_size)
        if direction > 0:
            sl = _round_to_tick(entry - sl_w, spec.tick_size)
            tp = _round_to_tick(entry + tp_w, spec.tick_size)
        else:
            sl = _round_to_tick(entry + sl_w, spec.tick_size)
            tp = _round_to_tick(entry - tp_w, spec.tick_size)
        if sl == entry or tp == entry:
            continue

        max_j = min(len(df) - 1, entry_i + int(cfg.max_hold_bars))
        exit_price = float(closes[max_j])
        exit_reason = "time"
        exit_j = max_j
        for j in range(entry_i, max_j + 1):
            hit = _exit_hit(direction, float(highs[j]), float(lows[j]), sl, tp)
            if hit is not None:
                exit_price, exit_reason = hit
                exit_j = j
                break
        gross = (float(exit_price) - entry) * direction * spec.point_value
        pnl = gross - spec.round_turn_cost
        trades.append(
            SimTrade(
                config_id=cfg.config_id,
                signal_family=cfg.signal_family,
                side_mode=cfg.side_mode,
                signal_id=str(rec["signal_id"]),
                direction="long" if direction > 0 else "short",
                entry_time=_utc(times[entry_i]),
                exit_time=_utc(times[exit_j]),
                entry=entry,
                exit=float(exit_price),
                sl=sl,
                tp=tp,
                exit_reason=exit_reason,
                hold_bars=exit_j - entry_i,
                pnl=pnl,
                gross_pnl=gross,
                session=str(sessions[entry_i]),
                trade_date=str(trade_dates[entry_i]),
                week=str(weeks[entry_i]),
                month=str(months[entry_i]),
            )
        )
        next_allowed_i = exit_j
    return trades


def _metrics(trades: list[SimTrade]) -> dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "pnl": 0.0,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "expectancy": 0.0,
            "max_dd": 0.0,
            "total_gain": 0.0,
            "total_loss": 0.0,
        }
    pnl = np.array([t.pnl for t in trades], dtype=float)
    gains = pnl[pnl > 0].sum()
    losses = pnl[pnl < 0].sum()
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.insert(eq, 0, 0.0))[1:]
    dd = peak - eq
    return {
        "trades": int(len(trades)),
        "pnl": round(float(pnl.sum()), 2),
        "profit_factor": round(float(gains / abs(losses)), 4) if losses < 0 else (999.0 if gains > 0 else 0.0),
        "win_rate": round(float((pnl > 0).mean()), 4),
        "expectancy": round(float(pnl.mean()), 4),
        "max_dd": round(float(dd.max()) if len(dd) else 0.0, 2),
        "total_gain": round(float(gains), 2),
        "total_loss": round(float(losses), 2),
    }


def _period_stats(trades: list[SimTrade], field: str) -> dict[str, Any]:
    if not trades:
        return {"periods": 0, "positive_periods": 0, "positive_rate": 0.0, "min_pnl": 0.0, "max_pnl": 0.0, "std_pnl": 0.0}
    rows: dict[str, float] = {}
    for t in trades:
        key = getattr(t, field)
        rows[key] = rows.get(key, 0.0) + float(t.pnl)
    vals = np.array(list(rows.values()), dtype=float)
    return {
        "periods": int(len(vals)),
        "positive_periods": int((vals > 0).sum()),
        "positive_rate": round(float((vals > 0).mean()), 4),
        "min_pnl": round(float(vals.min()), 2),
        "max_pnl": round(float(vals.max()), 2),
        "std_pnl": round(float(vals.std(ddof=0)), 2),
    }


def _wf_stats(trades: list[SimTrade]) -> dict[str, Any]:
    if not trades:
        return {"wf_folds": 0, "wf_positive": 0, "wf_all_positive": False, "wf_pnls": []}
    ordered = sorted(trades, key=lambda t: t.entry_time)
    folds = np.array_split(np.array(ordered, dtype=object), 3)
    pnls = [round(float(sum(t.pnl for t in fold)), 2) for fold in folds if len(fold)]
    return {
        "wf_folds": len(pnls),
        "wf_positive": int(sum(1 for x in pnls if x > 0)),
        "wf_all_positive": bool(pnls and all(x > 0 for x in pnls)),
        "wf_pnls": pnls,
    }


def make_exit_grid(events: pd.DataFrame, grid_level: str = "standard") -> list[ExitConfig]:
    families = sorted(events["signal_family"].unique().tolist()) if not events.empty else []
    side_modes = ["all", "long_only", "short_only"]
    if grid_level == "full":
        hold_bars = [3, 6, 12, 24, 48]
        fixed_sls = [10, 15, 20, 30, 40, 60, 80]
        fixed_tps = [10, 15, 20, 30, 40, 60, 80, 120]
        atr_sls = [0.75, 1.0, 1.25, 1.5, 2.0, 2.5]
        atr_tps = [0.75, 1.0, 1.5, 2.0, 3.0, 4.0]
        range_sls = [0.5, 0.75, 1.0, 1.25]
        range_tps = [0.5, 1.0, 1.5, 2.0]
    elif grid_level == "quick":
        hold_bars = [6, 12, 24]
        fixed_sls = [20, 40, 60]
        fixed_tps = [20, 60, 120]
        atr_sls = [1.0, 1.5, 2.5]
        atr_tps = [1.0, 2.0, 4.0]
        range_sls = [0.75, 1.25]
        range_tps = [1.0, 2.0]
    else:
        hold_bars = [6, 12, 24, 48]
        fixed_sls = [15, 30, 60, 80]
        fixed_tps = [15, 30, 60, 120]
        atr_sls = [1.0, 1.5, 2.0, 2.5]
        atr_tps = [1.0, 1.5, 2.0, 4.0]
        range_sls = [0.75, 1.0, 1.25]
        range_tps = [1.0, 1.5, 2.0]
    configs: list[ExitConfig] = []
    idx = 1
    for family in families:
        for side_mode in side_modes:
            for hold in hold_bars:
                for sl in fixed_sls:
                    for tp in fixed_tps:
                        configs.append(ExitConfig(f"X{idx:05d}", family, side_mode, "fixed", "fixed", sl, tp, hold))
                        idx += 1
                for rule in ("atr", "atr_blend"):
                    for sl in atr_sls:
                        for tp in atr_tps:
                            configs.append(ExitConfig(f"X{idx:05d}", family, side_mode, rule, rule, sl, tp, hold))
                            idx += 1
                for sl in range_sls:
                    for tp in range_tps:
                        configs.append(ExitConfig(f"X{idx:05d}", family, side_mode, "range15_pct", "range15_pct", sl, tp, hold))
                        idx += 1
    return configs


def _records_by_family_side(events: pd.DataFrame) -> dict[tuple[str, str], list[dict[str, Any]]]:
    records = events.to_dict("records")
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for family in sorted(events["signal_family"].unique().tolist()) if not events.empty else []:
        fam = [r for r in records if str(r["signal_family"]) == family]
        out[(family, "all")] = fam
        out[(family, "long_only")] = [r for r in fam if int(r["direction"]) > 0]
        out[(family, "short_only")] = [r for r in fam if int(r["direction"]) < 0]
    return out


def run_exit_grid(
    df: pd.DataFrame,
    events: pd.DataFrame,
    spec: ContractSpec,
    grid_level: str,
) -> tuple[pd.DataFrame, list[SimTrade], dict[str, Any]]:
    rows = []
    trades_by_cfg: dict[str, list[SimTrade]] = {}
    record_map = _records_by_family_side(events)
    for cfg in make_exit_grid(events, grid_level):
        trades = simulate_config(df, record_map.get((cfg.signal_family, cfg.side_mode), []), spec, cfg)
        m = _metrics(trades)
        if m["trades"] <= 0:
            continue
        wf = _wf_stats(trades)
        week = _period_stats(trades, "week")
        day = _period_stats(trades, "trade_date")
        month = _period_stats(trades, "month")
        row = {
            **asdict(cfg),
            **m,
            **wf,
            "day_positive_rate": day["positive_rate"],
            "week_positive_rate": week["positive_rate"],
            "month_positive_rate": month["positive_rate"],
            "week_std_pnl": week["std_pnl"],
        }
        rows.append(row)
        trades_by_cfg[cfg.config_id] = trades
    grid = pd.DataFrame(rows)
    if grid.empty:
        return grid, [], {}
    grid.sort_values(
        ["wf_all_positive", "profit_factor", "expectancy", "pnl", "trades"],
        ascending=[False, False, False, False, False],
        inplace=True,
    )
    best_id = str(grid.iloc[0]["config_id"])
    best_trades = trades_by_cfg[best_id]
    return grid, best_trades, asdict(make_config_lookup(grid.iloc[0]))


def make_config_lookup(row: pd.Series) -> ExitConfig:
    return ExitConfig(
        config_id=str(row["config_id"]),
        signal_family=str(row["signal_family"]),
        side_mode=str(row["side_mode"]),
        sl_rule=str(row["sl_rule"]),
        tp_rule=str(row["tp_rule"]),
        sl_value=float(row["sl_value"]),
        tp_value=float(row["tp_value"]),
        max_hold_bars=int(row["max_hold_bars"]),
    )


def run_entry_exit_combos(df: pd.DataFrame, events: pd.DataFrame, spec: ContractSpec) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame()
    families = sorted(events["signal_family"].unique().tolist())
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    times = df["timestamp"].to_list()
    rows = []
    event_records = sorted(events.to_dict("records"), key=lambda r: int(r["entry_i"]))
    by_exit: dict[tuple[str, int], list[int]] = {}
    for r in event_records:
        by_exit.setdefault((str(r["signal_family"]), -int(r["direction"])), []).append(int(r["entry_i"]))

    for entry_family in families:
        entries = [r for r in event_records if str(r["signal_family"]) == entry_family]
        for exit_family in families:
            for max_hold in (6, 12, 24, 48):
                trades: list[SimTrade] = []
                next_allowed = -1
                for rec in entries:
                    entry_i = int(rec["entry_i"])
                    if entry_i <= next_allowed or entry_i >= len(df):
                        continue
                    direction = int(rec["direction"])
                    entry = _round_to_tick(float(rec["entry"]), spec.tick_size)
                    sl_w = max(spec.tick_size, float(rec["atr14"]) * 1.5)
                    sl = _round_to_tick(entry - sl_w, spec.tick_size) if direction > 0 else _round_to_tick(entry + sl_w, spec.tick_size)
                    max_j = min(len(df) - 1, entry_i + max_hold)
                    exit_j = max_j
                    exit_price = float(closes[max_j])
                    exit_reason = "time"
                    opposite_indices = by_exit.get((exit_family, direction * -1), [])
                    for j in range(entry_i, max_j + 1):
                        hit_sl = lows[j] <= sl if direction > 0 else highs[j] >= sl
                        if hit_sl:
                            exit_j = j
                            exit_price = sl
                            exit_reason = "protective_sl"
                            break
                        if j in opposite_indices and j > entry_i:
                            exit_j = j
                            exit_price = float(opens[j])
                            exit_reason = f"exit_signal:{exit_family}"
                            break
                    gross = (exit_price - entry) * direction * spec.point_value
                    pnl = gross - spec.round_turn_cost
                    trades.append(
                        SimTrade(
                            config_id=f"EE:{entry_family}->{exit_family}:{max_hold}",
                            signal_family=entry_family,
                            side_mode="all",
                            signal_id=str(rec["signal_id"]),
                            direction="long" if direction > 0 else "short",
                            entry_time=_utc(times[entry_i]),
                            exit_time=_utc(times[exit_j]),
                            entry=entry,
                            exit=exit_price,
                            sl=sl,
                            tp=0.0,
                            exit_reason=exit_reason,
                            hold_bars=exit_j - entry_i,
                            pnl=pnl,
                            gross_pnl=gross,
                            session=str(df.iloc[entry_i].session),
                            trade_date=str(df.iloc[entry_i].trade_date),
                            week=str(df.iloc[entry_i].week),
                            month=str(df.iloc[entry_i].month),
                        )
                    )
                    next_allowed = exit_j
                m = _metrics(trades)
                if m["trades"] > 0:
                    rows.append(
                        {
                            "entry_signal": entry_family,
                            "exit_signal": exit_family,
                            "max_hold_bars": max_hold,
                            **m,
                            **_wf_stats(trades),
                            "week_positive_rate": _period_stats(trades, "week")["positive_rate"],
                        }
                    )
    out = pd.DataFrame(rows)
    if not out.empty:
        out.sort_values(["wf_all_positive", "profit_factor", "expectancy"], ascending=[False, False, False], inplace=True)
    return out


def summarize_accuracy(events: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    rows = []
    groups = [
        ["signal_family"],
        ["signal_family", "side"],
        ["signal_family", "session"],
        ["signal_family", "cluster_kind"],
        ["signal_family", "week"],
        ["signal_family", "month"],
    ]
    for group in groups:
        for keys, sub in events.groupby(group):
            if not isinstance(keys, tuple):
                keys = (keys,)
            base = {name: val for name, val in zip(group, keys)}
            base["group"] = "+".join(group)
            base["signals"] = int(len(sub))
            for h in horizons:
                col = f"h{h}_correct"
                peak = f"h{h}_peak_capture"
                wrong_right = f"h{h}_wrong_short_right_long"
                if col in sub:
                    base[f"h{h}_direction_acc"] = round(float(sub[col].mean()), 4)
                    base[f"h{h}_avg_ret_points"] = round(float(sub[f"h{h}_ret_points"].mean()), 4)
                    base[f"h{h}_peak_capture_rate"] = round(float(sub[peak].mean()), 4)
                    base[f"h{h}_wrong_short_right_long_rate"] = round(float(sub[wrong_right].mean()), 4)
            rows.append(base)
    return pd.DataFrame(rows)


def summarize_clusters(events: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    rows = []
    for keys, sub in events.groupby(["cluster_kind", "cluster_families", "signal_family"]):
        row = {
            "cluster_kind": keys[0],
            "cluster_families": keys[1],
            "signal_family": keys[2],
            "signals": int(len(sub)),
            "avg_cluster_size": round(float(sub["cluster_size"].mean()), 3),
        }
        for h in horizons:
            if f"h{h}_correct" in sub:
                row[f"h{h}_direction_acc"] = round(float(sub[f"h{h}_correct"].mean()), 4)
                row[f"h{h}_peak_capture_rate"] = round(float(sub[f"h{h}_peak_capture"].mean()), 4)
        rows.append(row)
    out = pd.DataFrame(rows)
    if not out.empty:
        out.sort_values(["h12_direction_acc", "signals"], ascending=[False, False], inplace=True)
    return out


def write_trades(path: Path, trades: list[SimTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        if not trades:
            fh.write("")
            return
        writer = csv.DictWriter(fh, fieldnames=list(asdict(trades[0]).keys()))
        writer.writeheader()
        for trade in trades:
            row = asdict(trade)
            row["entry_time"] = trade.entry_time.isoformat()
            row["exit_time"] = trade.exit_time.isoformat()
            writer.writerow(row)


def data_audit(symbols: list[str]) -> list[dict[str, Any]]:
    rows = []
    for symbol in symbols:
        bars = _load_candles(symbol)
        if not bars:
            rows.append({"symbol": symbol.upper(), "bars": 0, "first": None, "last": None, "stale_days": None})
            continue
        first = _utc(bars[0].timestamp)
        last = _utc(bars[-1].timestamp)
        stale_days = (datetime.now(UTC) - last).total_seconds() / 86400.0
        rows.append(
            {
                "symbol": symbol.upper(),
                "bars": len(bars),
                "first": first.isoformat(),
                "last": last.isoformat(),
                "stale_days": round(stale_days, 2),
            }
        )
    return rows


def run_symbol(symbol: str, args, run_dir: Path) -> dict[str, Any]:
    spec = _spec_for_symbol(symbol)
    candles = _load_candles(symbol, args.max_bars)
    sym_dir = run_dir / spec.symbol
    sym_dir.mkdir(parents=True, exist_ok=True)
    if not candles:
        return {"symbol": spec.symbol, "available": False, "message": "no local 1m candle store"}

    df5 = add_factors(aggregate_5m(candles))
    events = build_signal_events(df5)
    if args.include_zones and not events.empty:
        zone_tfs = [x.strip() for x in args.zone_tfs.split(",") if x.strip()]
        events = build_zone_context(candles, events, zone_tfs, spec)

    horizons = [1, 3, 6, 12, 24, 48]
    events = add_forward_outcomes(df5, events, horizons)
    signals_path = sym_dir / "signals.csv"
    events.to_csv(signals_path, index=False)

    accuracy = summarize_accuracy(events, horizons)
    accuracy.to_csv(sym_dir / "horizon_accuracy.csv", index=False)
    clusters = summarize_clusters(events, horizons)
    clusters.to_csv(sym_dir / "cluster_summary.csv", index=False)

    grid, best_trades, best_cfg = run_exit_grid(df5, events, spec, args.grid_level)
    grid.to_csv(sym_dir / "exit_grid.csv", index=False)
    write_trades(sym_dir / "best_trades.csv", best_trades)

    combos = run_entry_exit_combos(df5, events, spec)
    combos.to_csv(sym_dir / "entry_exit_combos.csv", index=False)

    period_rows = []
    if best_trades:
        for period_field in ("trade_date", "week", "month"):
            p = _period_stats(best_trades, period_field)
            period_rows.append({"config_id": best_trades[0].config_id, "period": period_field, **p})
    pd.DataFrame(period_rows).to_csv(sym_dir / "period_stability.csv", index=False)

    more_trades = {}
    if not grid.empty and len(grid) >= 3:
        more_trades = {
            "corr_trades_pf": round(float(grid["trades"].corr(grid["profit_factor"])), 4),
            "corr_trades_expectancy": round(float(grid["trades"].corr(grid["expectancy"])), 4),
            "corr_trades_pnl": round(float(grid["trades"].corr(grid["pnl"])), 4),
        }

    best_grid = grid.head(20).to_dict("records") if not grid.empty else []
    best_combo = combos.head(20).to_dict("records") if not combos.empty else []
    summary = {
        "symbol": spec.symbol,
        "available": True,
        "one_contract_point_value": spec.point_value,
        "tick_size": spec.tick_size,
        "round_turn_cost": spec.round_turn_cost,
        "bars_1m": len(candles),
        "bars_5m": int(len(df5)),
        "data_start": _utc(candles[0].timestamp).isoformat(),
        "data_end": _utc(candles[-1].timestamp).isoformat(),
        "signals": int(len(events)),
        "signals_by_family": events["signal_family"].value_counts().to_dict() if not events.empty else {},
        "grid_level": args.grid_level,
        "best_exit_config": best_cfg,
        "best_exit_metrics": _metrics(best_trades),
        "best_exit_wf": _wf_stats(best_trades),
        "best_exit_week_stability": _period_stats(best_trades, "week"),
        "more_trades_relationship": more_trades,
        "top_exit_grid": best_grid,
        "top_entry_exit_combos": best_combo,
        "outputs": {
            "signals": str(signals_path),
            "horizon_accuracy": str(sym_dir / "horizon_accuracy.csv"),
            "cluster_summary": str(sym_dir / "cluster_summary.csv"),
            "exit_grid": str(sym_dir / "exit_grid.csv"),
            "entry_exit_combos": str(sym_dir / "entry_exit_combos.csv"),
            "period_stability": str(sym_dir / "period_stability.csv"),
            "best_trades": str(sym_dir / "best_trades.csv"),
        },
    }
    (sym_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="MNQ", help="Comma-separated symbols to research, e.g. MNQ,ES")
    parser.add_argument("--audit-symbols", default="MNQ,ES,MES", help="Symbols for local store audit")
    parser.add_argument("--max-bars", type=int, default=0, help="Use only the most recent N 1m bars; 0 = all")
    parser.add_argument("--include-zones", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--zone-tfs", default="15m,30m,1h,2h,4h")
    parser.add_argument("--grid-level", choices=["quick", "standard", "full"], default="standard")
    parser.add_argument("--print-json", action="store_true")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")
    name = f"{stamp}_{args.tag}" if args.tag else stamp
    run_dir = OUT_ROOT / name
    run_dir.mkdir(parents=True, exist_ok=True)

    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    audit_symbols = [x.strip().upper() for x in args.audit_symbols.split(",") if x.strip()]
    audit = data_audit(audit_symbols)
    pd.DataFrame(audit).to_csv(run_dir / "data_audit.csv", index=False)

    summaries = []
    for symbol in symbols:
        print(f"[research] {symbol} start")
        summaries.append(run_symbol(symbol, args, run_dir))
        print(f"[research] {symbol} done")

    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "run_dir": str(run_dir),
        "symbols": symbols,
        "data_audit": audit,
        "summaries": summaries,
        "assumptions": {
            "entry": "market at next completed 5m open",
            "live_parity": "signals use last completed candle only",
            "pnl_contract_size": "one contract",
            "same_bar_resolution": "conservative SL first when SL and TP hit same bar",
        },
    }
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (OUT_ROOT / "latest.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if args.print_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"report={run_dir / 'report.json'}")
        for summary in summaries:
            print(
                f"{summary.get('symbol')} signals={summary.get('signals')} "
                f"best={summary.get('best_exit_config', {}).get('config_id')} "
                f"pf={summary.get('best_exit_metrics', {}).get('profit_factor')} "
                f"pnl={summary.get('best_exit_metrics', {}).get('pnl')} "
                f"trades={summary.get('best_exit_metrics', {}).get('trades')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
