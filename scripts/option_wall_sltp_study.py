"""SL/TP exit study for the existing QQQ option-wall walk-forward signals.

Entries are never refit in this module.  It replays the already out-of-sample
signals through observed MNQ one-minute OHLC paths and compares a time exit,
mapped wall exits, and the PI preset's asymmetric ATR-blend geometry.  The
module is research-only and has no order-routing imports.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.intrabar import resolve_same_bar_exit
from scripts.option_wall_article_walk_forward import _add_calendar_feature_columns
from scripts.option_wall_demo import _map_qqq_to_mnq, _rolling_return_beta
from scripts.option_wall_ml_study import (
    DEFAULT_DATA_ROOT,
    RESEARCH_MNQ_FILE,
    _atomic_csv,
    _atomic_json,
    _iso,
)
from scripts.option_wall_walk_forward import _strategy_summary


TICK_SIZE = 0.25
ARTICLE_PROBABILITY_THRESHOLD = 0.55


def _round_tick(value: float) -> float:
    return round(float(value) / TICK_SIZE) * TICK_SIZE


def _active_wall_bps(row: pd.Series, direction: int, role: str) -> tuple[float | None, str | None]:
    """Return an on-side volume wall, falling back to the on-side OI wall.

    The article gives post-10:00 intraday volume walls priority, but an
    unsigned volume wall on the wrong side of spot is not a usable target or
    stop.  In that case only a correctly positioned OI wall may replace it.
    """
    if direction not in {-1, 1} or role not in {"target", "stop"}:
        raise ValueError("direction must be +/-1 and role target/stop")
    want_upper = (direction == 1 and role == "target") or (
        direction == -1 and role == "stop"
    )
    option_side = "call" if want_upper else "put"
    candidates = (
        (f"dashboard_vol_{option_side}_wall_bps", "volume"),
        (f"oi_{option_side}_wall_bps", "oi"),
    )
    for column, source in candidates:
        value = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]
        if not math.isfinite(value):
            continue
        if (want_upper and value > 0) or (not want_upper and value < 0):
            return float(value), source
    return None, None


def _mapped_wall_price(row: pd.Series, wall_bps: float, beta: float) -> float:
    qqq_spot = float(row["qqq_spot"])
    mnq_entry = float(row["mnq_entry"])
    qqq_level = qqq_spot * (1.0 + float(wall_bps) / 10_000.0)
    mapped = _map_qqq_to_mnq(qqq_level, qqq_spot, mnq_entry, beta)
    if mapped is None or not math.isfinite(mapped):
        return math.nan
    return _round_tick(mapped)


def _pi_atr_levels(
    entry: float,
    direction: int,
    atr_blend: float,
    sl_atr_multiple: float,
    tp_atr_multiple: float | None,
) -> tuple[float, float | None]:
    """Create independently-sized PI-style ATR levels on the proper sides."""
    values = (entry, atr_blend, sl_atr_multiple)
    if direction not in {-1, 1} or not all(math.isfinite(float(value)) for value in values):
        raise ValueError("finite entry/ATR/SL and direction +/-1 are required")
    if atr_blend <= 0 or sl_atr_multiple <= 0:
        raise ValueError("ATR and SL multiple must be positive")
    if tp_atr_multiple is not None and (
        not math.isfinite(float(tp_atr_multiple)) or tp_atr_multiple <= 0
    ):
        raise ValueError("TP multiple must be positive or None")
    sl = _round_tick(entry - direction * atr_blend * sl_atr_multiple)
    tp = (
        _round_tick(entry + direction * atr_blend * tp_atr_multiple)
        if tp_atr_multiple is not None else None
    )
    return sl, tp


def _five_minute_bars(mnq: pd.DataFrame) -> pd.DataFrame:
    indexed = mnq.reset_index(drop=True).set_index("ts").sort_index()
    bars = indexed.resample("5min", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
    }).dropna()
    bars["available_at"] = bars.index + pd.Timedelta(minutes=5)
    return bars.reset_index(drop=False)


def _atr_blend_at(five_minute: pd.DataFrame, as_of: pd.Timestamp) -> float | None:
    """Match the current PI strategy's completed-5m simple ATR blend."""
    history = five_minute[five_minute["available_at"] <= as_of]
    if len(history) < 25:
        return None

    def atr(length: int) -> float | None:
        if len(history) < max(7, length // 2):
            return None
        segment = history.tail(length)
        high = segment["high"].to_numpy(dtype=float)
        low = segment["low"].to_numpy(dtype=float)
        close = segment["close"].to_numpy(dtype=float)
        previous = np.r_[close[0], close[:-1]]
        tr = np.maximum.reduce([high - low, np.abs(high - previous), np.abs(low - previous)])
        return float(tr.mean()) if len(tr) else None

    atr14, atr50 = atr(14), atr(50)
    if atr14 is None or atr14 <= 0:
        return None
    return float((atr14 + (atr50 or atr14)) / 2.0)


def _simulate_ohlc_exit(
    path: pd.DataFrame,
    direction: int,
    entry: float,
    sl_price: float | None,
    tp_price: float | None,
) -> dict[str, Any]:
    """Replay one position; the last observed close is the time stop."""
    if path.empty:
        raise ValueError("empty path")
    if direction not in {-1, 1}:
        raise ValueError("direction must be +/-1")
    sl = float(sl_price) if sl_price is not None else math.nan
    tp = float(tp_price) if tp_price is not None else math.nan
    has_sl = math.isfinite(sl)
    has_tp = math.isfinite(tp)

    for elapsed, bar in enumerate(path.itertuples(index=False), 1):
        if direction == 1:
            sl_hit = has_sl and float(bar.low) <= sl
            tp_hit = has_tp and float(bar.high) >= tp
        else:
            sl_hit = has_sl and float(bar.high) >= sl
            tp_hit = has_tp and float(bar.low) <= tp
        if not (sl_hit or tp_hit):
            continue
        if sl_hit and tp_hit:
            reason = resolve_same_bar_exit(float(bar.open), sl, tp)
        else:
            reason = "sl" if sl_hit else "tp"
        if reason == "sl":
            # A stop is a market order.  If a later minute opens through it,
            # use the worse open rather than granting an impossible fill.
            exit_price = min(sl, float(bar.open)) if direction == 1 else max(sl, float(bar.open))
        else:
            # A target is a limit order; exact level is the conservative fill.
            exit_price = tp
        return {
            "exit_price": float(exit_price),
            "exit_reason": reason,
            "bars_held": elapsed,
        }

    last = path.iloc[-1]
    return {
        "exit_price": float(last["close"]),
        "exit_reason": "time",
        "bars_held": int(len(path)),
    }


def _read_mnq(data_root: Path) -> pd.DataFrame:
    path = data_root / "raw_mnq" / RESEARCH_MNQ_FILE
    if not path.is_file():
        raise RuntimeError(f"MNQ one-minute OHLC missing: {path}")
    frame = pd.read_csv(
        path, compression="gzip",
        usecols=["ts_event", "open", "high", "low", "close"],
    )
    frame["ts"] = pd.to_datetime(frame.pop("ts_event"), utc=True, errors="coerce")
    frame = frame.dropna(subset=["ts", "open", "high", "low", "close"])
    frame = frame.sort_values("ts").reset_index(drop=True)
    return frame.set_index("ts", drop=False)


def _read_qqq_sessions(data_root: Path, dates: Sequence[str]) -> dict[str, pd.Series]:
    result: dict[str, pd.Series] = {}
    for day in sorted(set(str(value) for value in dates)):
        path = data_root / "raw" / day / "qqq_ohlcv_1m.csv.gz"
        if not path.is_file():
            continue
        frame = pd.read_csv(path, compression="gzip", usecols=["ts_event", "close"])
        timestamps = pd.to_datetime(frame["ts_event"], utc=True, errors="coerce")
        available = timestamps + pd.Timedelta(minutes=1)
        series = pd.Series(
            pd.to_numeric(frame["close"], errors="coerce").to_numpy(),
            index=available,
        ).dropna().sort_index()
        result[day] = series[~series.index.duplicated(keep="last")]
    return result


def _signal_columns(article: pd.DataFrame, legacy: pd.DataFrame) -> dict[str, np.ndarray]:
    keyed = legacy.copy()
    keyed["as_of_key"] = pd.to_datetime(keyed["as_of"], utc=True).astype(str)
    legacy_map = keyed.set_index("as_of_key")
    article_keys = pd.to_datetime(article["as_of"], utc=True).astype(str)

    def legacy_signal(column: str) -> np.ndarray:
        values = legacy_map[column].reindex(article_keys).fillna(0)
        return values.to_numpy(dtype=int)

    predicted = article["ablation_article_state_prediction"].to_numpy(dtype=int)
    confidence = article["ablation_article_state_confidence"].to_numpy(dtype=float)
    article_state = np.where(
        (predicted != 0) & (confidence >= ARTICLE_PROBABILITY_THRESHOLD), predicted, 0,
    ).astype(int)
    return {
        "primary_model_confidence": legacy_signal("primary_model_confidence_signal"),
        "primary_single_wall_stable": legacy_signal("primary_single_wall_stable_signal"),
        "side_article_state": article_state,
        "side_regime_direction": article["layer_regime_direction_signal"].to_numpy(dtype=int),
        "side_target_confirmed": article["layer_target_confirmed_signal"].to_numpy(dtype=int),
    }


def _levels_for_policy(
    row: pd.Series,
    direction: int,
    policy: str,
    beta: float,
    atr_blend: float | None,
) -> tuple[float | None, float | None, dict[str, Any]]:
    entry = float(row["mnq_entry"])
    metadata: dict[str, Any] = {}
    if policy == "time_only":
        return None, None, metadata

    if policy in {"pi_asymmetric_sl_only", "pi_asymmetric_3r"}:
        if atr_blend is None or not math.isfinite(atr_blend) or atr_blend <= 0:
            return None, None, {"invalid": "atr_unavailable"}
        multiplier = 4.0 if direction == 1 else 1.5
        tp_multiple = multiplier * 3.0 if policy == "pi_asymmetric_3r" else None
        sl, tp = _pi_atr_levels(entry, direction, atr_blend, multiplier, tp_multiple)
        metadata = {"atr_blend": float(atr_blend), "sl_atr_multiple": multiplier}
        if tp is not None:
            metadata["reward_risk"] = 3.0
        return sl, tp, metadata

    target_bps, target_source = _active_wall_bps(row, direction, "target")
    if target_bps is None:
        return None, None, {"invalid": "directional_target_wall_unavailable"}
    tp = _mapped_wall_price(row, target_bps, beta)
    if not math.isfinite(tp):
        return None, None, {"invalid": "mapped_target_unavailable"}
    reward = abs(tp - entry)
    if reward < TICK_SIZE:
        return None, None, {"invalid": "mapped_target_too_close"}

    if policy == "wall_to_wall":
        stop_bps, stop_source = _active_wall_bps(row, direction, "stop")
        if stop_bps is None:
            return None, None, {"invalid": "directional_stop_wall_unavailable"}
        sl = _mapped_wall_price(row, stop_bps, beta)
        metadata["stop_wall_bps"] = stop_bps
        metadata["stop_wall_source"] = stop_source
    elif policy == "wall_tp_equal_risk":
        sl = entry - direction * reward
        metadata["reward_risk"] = 1.0
    elif policy == "wall_tp_2r":
        sl = entry - direction * reward / 2.0
        metadata["reward_risk"] = 2.0
    elif policy == "wall_tp_pi_stop":
        if atr_blend is None or not math.isfinite(atr_blend) or atr_blend <= 0:
            return None, None, {"invalid": "atr_unavailable"}
        multiplier = 4.0 if direction == 1 else 1.5
        sl = entry - direction * float(atr_blend) * multiplier
        metadata["atr_blend"] = float(atr_blend)
        metadata["sl_atr_multiple"] = multiplier
    else:
        raise ValueError(f"unknown policy: {policy}")

    sl = _round_tick(sl)
    if not math.isfinite(sl) or sl == entry:
        return None, None, {"invalid": "mapped_stop_too_close"}
    if direction == 1 and not (sl < entry < tp):
        return None, None, {"invalid": "levels_on_wrong_side"}
    if direction == -1 and not (tp < entry < sl):
        return None, None, {"invalid": "levels_on_wrong_side"}
    metadata.update({
        "target_wall_bps": target_bps,
        "target_wall_source": target_source,
    })
    return float(sl), float(tp), metadata


def _path_for_row(mnq: pd.DataFrame, row: pd.Series, horizon_minutes: int) -> pd.DataFrame:
    as_of = pd.Timestamp(row["as_of"])
    entry = pd.to_numeric(pd.Series([row.get("mnq_entry")]), errors="coerce").iloc[0]
    if not math.isfinite(entry):
        return mnq.iloc[0:0].copy()
    deadline = min(
        as_of + pd.Timedelta(minutes=horizon_minutes),
        pd.Timestamp(row["close_at"]),
    )
    path = mnq.loc[as_of:deadline - pd.Timedelta(nanoseconds=1)].copy()
    if path.empty:
        return path
    entry = float(entry)
    offset = entry - float(path.iloc[0]["open"])
    for column in ("open", "high", "low", "close"):
        path[column] = pd.to_numeric(path[column], errors="coerce") + offset
    return path


def _reconciliation(article: pd.DataFrame, mnq: pd.DataFrame, horizon: int) -> dict[str, Any]:
    stored_column = f"mnq_points_{horizon}m"
    differences: list[float] = []
    for _, row in article.iterrows():
        stored = pd.to_numeric(pd.Series([row.get(stored_column)]), errors="coerce").iloc[0]
        path = _path_for_row(mnq, row, horizon)
        if not math.isfinite(stored) or path.empty:
            continue
        observed = float(path.iloc[-1]["close"] - float(row["mnq_entry"]))
        differences.append(observed - float(stored))
    values = np.asarray(differences, dtype=float)
    return {
        "rows": int(len(values)),
        "exact_fraction": float(np.isclose(values, 0.0).mean()) if len(values) else None,
        "within_one_point_fraction": float((np.abs(values) <= 1.0).mean()) if len(values) else None,
        "mean_absolute_point_difference": float(np.abs(values).mean()) if len(values) else None,
        "maximum_absolute_point_difference": float(np.abs(values).max()) if len(values) else None,
    }


def run_sltp_study(
    data_root: Path = DEFAULT_DATA_ROOT,
    simulations: int = 10_000,
    monte_carlo_horizon: int = 20,
) -> dict[str, Any]:
    article_path = data_root / "option_wall_article_walk_forward_signals.csv.gz"
    legacy_path = data_root / "option_wall_walk_forward_signals.csv.gz"
    if not article_path.is_file() or not legacy_path.is_file():
        raise RuntimeError("walk-forward signal files are missing; run both studies first")
    article = pd.read_csv(article_path, compression="gzip")
    article["as_of"] = pd.to_datetime(article["as_of"], utc=True)
    article["close_at"] = pd.to_datetime(article["close_at"], utc=True)
    article = _add_calendar_feature_columns(article).sort_values("as_of").reset_index(drop=True)
    legacy = pd.read_csv(legacy_path, compression="gzip")
    legacy = legacy[legacy["as_of_et"].astype(str).str.endswith(":00")].copy()
    signals = _signal_columns(article, legacy)

    mnq = _read_mnq(data_root)
    five_minute = _five_minute_bars(mnq)
    qqq_sessions = _read_qqq_sessions(data_root, article["date"].astype(str).unique())
    mnq_close = pd.Series(
        mnq["close"].to_numpy(dtype=float),
        index=mnq["ts"] + pd.Timedelta(minutes=1),
    )
    mnq_close = mnq_close[~mnq_close.index.duplicated(keep="last")].sort_index()
    atr_cache: dict[pd.Timestamp, float | None] = {}
    beta_cache: dict[pd.Timestamp, float] = {}
    for as_of, day in zip(article["as_of"], article["date"].astype(str)):
        atr_cache[as_of] = _atr_blend_at(five_minute, as_of)
        qqq = qqq_sessions.get(day)
        if qqq is None or qqq.empty:
            beta_cache[as_of] = 1.0
        else:
            same_session_mnq = mnq_close.loc[qqq.index.min():as_of]
            estimated = _rolling_return_beta(qqq, same_session_mnq, as_of)
            beta_cache[as_of] = float(estimated) if math.isfinite(estimated) else 1.0

    policies = (
        "time_only",
        "wall_to_wall",
        "wall_tp_equal_risk",
        "wall_tp_2r",
        "wall_tp_pi_stop",
        "pi_asymmetric_sl_only",
        "pi_asymmetric_3r",
    )
    strategy_horizons = {
        "primary_model_confidence": 60,
        "primary_single_wall_stable": 60,
        "side_article_state": 30,
        "side_regime_direction": 30,
        "side_target_confirmed": 30,
    }
    all_sessions = sorted(article["date"].astype(str).unique())
    reports: dict[str, Any] = {}
    trade_rows: list[dict[str, Any]] = []

    for strategy_name, signal in signals.items():
        horizon = strategy_horizons[strategy_name]
        active_indexes = np.flatnonzero(signal != 0)
        strategy_report: dict[str, Any] = {
            "role": "primary" if strategy_name.startswith("primary_") else "side_model",
            "signal_horizon_minutes": horizon,
            "input_signals": int(len(active_indexes)),
            "policies": {},
        }
        for policy in policies:
            rows: list[dict[str, Any]] = []
            skipped: Counter[str] = Counter()
            for index in active_indexes:
                source = article.iloc[int(index)]
                direction = int(signal[index])
                path = _path_for_row(mnq, source, horizon)
                if path.empty:
                    skipped["mnq_path_unavailable"] += 1
                    continue
                as_of = pd.Timestamp(source["as_of"])
                beta = beta_cache[as_of]
                atr_blend = atr_cache[as_of]
                sl, tp, metadata = _levels_for_policy(
                    source, direction, policy, beta, atr_blend,
                )
                if "invalid" in metadata:
                    skipped[str(metadata["invalid"])] += 1
                    continue
                outcome = _simulate_ohlc_exit(
                    path, direction, float(source["mnq_entry"]), sl, tp,
                )
                trade = {
                    "date": str(source["date"]),
                    "as_of": _iso(as_of.to_pydatetime()),
                    "as_of_et": str(source["as_of_et"]),
                    "strategy": strategy_name,
                    "policy": policy,
                    "horizon_minutes": horizon,
                    "direction": direction,
                    "entry_price": float(source["mnq_entry"]),
                    "sl_price": sl,
                    "tp_price": tp,
                    "exit_price": outcome["exit_price"],
                    "exit_reason": outcome["exit_reason"],
                    "bars_held": outcome["bars_held"],
                    "market_points": outcome["exit_price"] - float(source["mnq_entry"]),
                    "beta": beta,
                    **{key: value for key, value in metadata.items() if key != "invalid"},
                }
                rows.append(trade)
                trade_rows.append(trade)
            trades = pd.DataFrame(rows)
            if trades.empty:
                summary = {"trades": 0, "status": "no_eligible_trades"}
            else:
                trade_signals = trades["direction"].to_numpy(dtype=int)
                summary = _strategy_summary(
                    trades,
                    trade_signals,
                    np.ones(len(trades), dtype=bool),
                    all_sessions,
                    simulations,
                    monte_carlo_horizon,
                    points_column="market_points",
                )
                summary["exit_reasons"] = dict(Counter(trades["exit_reason"].astype(str)))
                summary["average_bars_held"] = float(trades["bars_held"].mean())
                summary["median_beta"] = float(trades["beta"].median())
            summary["input_signals"] = int(len(active_indexes))
            summary["eligible_trades"] = int(len(trades))
            summary["skipped"] = dict(skipped)
            strategy_report["policies"][policy] = summary
        reports[strategy_name] = strategy_report

    trades_path = data_root / "option_wall_sltp_trades.csv.gz"
    if trade_rows:
        _atomic_csv(trades_path, pd.DataFrame(trade_rows))
    report: dict[str, Any] = {
        "status": "provisional_research_only",
        "created_at": _iso(pd.Timestamp.now(tz="UTC").to_pydatetime()),
        "entries": "unchanged prior-session-only walk-forward signals",
        "execution": (
            "observed MNQ.v.0 one-minute OHLC, anchored to each stitched MNQ entry; "
            "same-bar SL/TP uses the repository conservative resolver"
        ),
        "wall_mapping": (
            "QQQ wall return × trailing intraday QQQ/MNQ return beta clipped to 0.70-1.30; "
            "post-10:00 volume wall first, correctly-sided OI wall fallback"
        ),
        "pi_reference": (
            "entry unchanged; completed-5m ATR14/ATR50 simple blend; long SL 4x, "
            "short SL 1.5x, TP 3R"
        ),
        "signals": {
            "article": str(article_path),
            "legacy": str(legacy_path),
        },
        "trades_file": str(trades_path),
        "oos_first_session": str(article["date"].min()),
        "oos_last_session": str(article["date"].max()),
        "oos_sessions": int(article["date"].nunique()),
        "path_reconciliation": {
            "30m": _reconciliation(article, mnq, 30),
            "60m": _reconciliation(article, mnq, 60),
        },
        "strategies": reports,
        "warnings": [
            "MNQ.v.0 paths are translated by a constant per-entry offset to the stitched chart coordinate; returns are unchanged.",
            "One-minute OHLC cannot reveal tick order; exact-distance same-bar ambiguity resolves to SL.",
            "No slippage is added beyond repository round-turn commission and fees; stops may receive a worse minute-open fill after gaps.",
            "SL/TP policies are exit comparisons, not independently selected walk-forward hyperparameters.",
        ],
    }
    _atomic_json(data_root / "option_wall_sltp_report.json", report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--simulations", type=int, default=10_000)
    parser.add_argument("--monte-carlo-horizon", type=int, default=20)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_sltp_study(args.data_root, args.simulations, args.monte_carlo_horizon)
    print(json.dumps(report, indent=2, allow_nan=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
