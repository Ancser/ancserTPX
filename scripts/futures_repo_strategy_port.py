"""Open-source futures strategy ideas ported to MNQ intraday research.

Ideas translated, not copied verbatim:
  - CTA style trend following: moving-average/forecast divided by volatility.
  - Quantiacs-style WMA + rate-of-change trend filter.
  - Rank acceleration: rolling percentile of momentum minus its lagged value.

Signals are evaluated on completed bars and entered at the next bar open.

Run:
  python -m scripts.futures_repo_strategy_port
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, time, timezone
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "futures_repo_port"
RESULTS_CSV = OUT_DIR / "results.csv"
TOP_CSV = OUT_DIR / "top_latest.csv"
LATEST_JSON = OUT_DIR / "latest.json"
REPORT_MD = OUT_DIR / "report.md"
BEST_TRADES_CSV = OUT_DIR / "best_trades.csv"
RESULTS_JSONL = OUT_DIR / "results.jsonl"

TICK = 0.25
MNQ_TICK_VALUE = 0.50
ROUND_TURN_COST = 2.48
NY = ZoneInfo("America/New_York")


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


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


def _max_dd(values: list[float]) -> float:
    peak = 0.0
    dd = 0.0
    equity = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def _metrics(values: list[float]) -> dict:
    arr = np.asarray(values, dtype=float)
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    gain = float(wins.sum())
    loss = float(losses.sum())
    return {
        "trades": int(len(arr)),
        "pnl": round(float(arr.sum()), 2),
        "max_dd": round(_max_dd(arr.tolist()), 2),
        "profit_factor": round(gain / abs(loss), 4) if loss < 0 else 999.0,
        "win_rate": round(float((arr > 0).mean()), 4) if len(arr) else 0.0,
        "expectancy": round(float(arr.mean()), 3) if len(arr) else 0.0,
        "total_loss": round(loss, 2),
        "total_gain": round(gain, 2),
    }


def _mean_ci(values: list[float]) -> tuple[list[float], float]:
    arr = np.asarray(values, dtype=float)
    if len(arr) < 5:
        return [0.0, 0.0], 0.0
    mean = float(arr.mean())
    se = float(arr.std(ddof=1) / np.sqrt(len(arr))) if len(arr) > 1 else 0.0
    lower = mean - 1.96 * se
    upper = mean + 1.96 * se
    if se <= 0:
        p_pos = 1.0 if mean > 0 else 0.0
    else:
        # Normal approximation without scipy: P(mean_sample > 0).
        z = mean / se
        p_pos = 0.5 * (1.0 + float(math.erf(z / np.sqrt(2.0))))
    return [round(lower, 3), round(upper, 3)], round(p_pos, 4)


def load_bars(minutes: int) -> pd.DataFrame:
    rows = []
    for c in sorted(candle_store.load("MNQ", 1), key=lambda x: x.timestamp):
        rows.append(
            {
                "timestamp": _utc(c.timestamp),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume or 0),
            }
        )
    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    bars = df.resample(f"{minutes}min", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    bars.dropna(subset=["open", "high", "low", "close"], inplace=True)
    bars.reset_index(inplace=True)
    bars["session"] = bars["timestamp"].map(_session_for)
    bars["trade_date"] = bars["timestamp"].map(_topstep_trade_date)
    return bars


def _wma(series: pd.Series, window: int) -> pd.Series:
    weights = np.arange(1, window + 1, dtype=float)
    return series.rolling(window, min_periods=max(5, window // 2)).apply(
        lambda x: float(np.dot(x, weights[-len(x):]) / weights[-len(x):].sum()),
        raw=True,
    )


def _percent_rank(series: pd.Series, window: int) -> pd.Series:
    return series.rolling(window, min_periods=max(20, window // 4)).rank(pct=True)


def _rolling_norm_forecast(series: pd.Series, window: int = 500) -> pd.Series:
    scale = series.abs().rolling(window, min_periods=max(50, window // 5)).mean()
    return (series * 10.0 / scale.replace(0, np.nan)).clip(-20, 20)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    prev_close = c.shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = tr.rolling(14, min_periods=5).mean()
    atr_safe = out["atr14"].replace(0, np.nan)
    ret1 = c.pct_change()
    out["ret1"] = ret1
    out["ret_z"] = (ret1 - ret1.rolling(120).mean()) / ret1.rolling(120).std().replace(0, np.nan)
    for fast, slow in ((4, 16), (8, 32), (16, 64)):
        ema_fast = c.ewm(span=fast, adjust=False).mean()
        ema_slow = c.ewm(span=slow, adjust=False).mean()
        out[f"ewmac_{fast}_{slow}"] = (ema_fast - ema_slow) / atr_safe
    for fast in (2, 4, 8, 16, 32, 64):
        ema_fast = c.ewm(span=fast, adjust=False).mean()
        ema_slow = c.ewm(span=fast * 4, adjust=False).mean()
        raw = (ema_fast - ema_slow) / atr_safe
        out[f"py_ewmac_{fast}"] = _rolling_norm_forecast(raw)
        out[f"py_mr_{fast}"] = -out[f"py_ewmac_{fast}"]
    for lookback in (20, 40, 80, 160, 320):
        roll = c.rolling(lookback, min_periods=max(10, lookback // 2))
        lo = roll.min()
        hi = roll.max()
        midpoint = (hi + lo) / 2.0
        width = (hi - lo).replace(0, np.nan)
        smooth = max(2, lookback // 4)
        raw = ((c - midpoint) / width).ewm(span=smooth, adjust=False, min_periods=max(2, smooth // 2)).mean()
        out[f"py_breakout_{lookback}"] = _rolling_norm_forecast(raw)
    out["py_open_close"] = _rolling_norm_forecast((out["close"] - out["open"]) / atr_safe)
    out["py_weather"] = np.sign(c.diff()).replace(0, np.nan).ffill().fillna(0) * 10.0
    for window in (12, 24, 48):
        out[f"roc_{window}"] = (c / c.shift(window) - 1.0)
        out[f"wma_{window}"] = _wma(c, window)
        out[f"wma_dist_{window}"] = (c - out[f"wma_{window}"]) / atr_safe
        pr = _percent_rank(out[f"roc_{window}"], 240)
        out[f"rank_{window}"] = pr
        out[f"rank_accel_{window}_6"] = pr - pr.shift(6)
        out[f"rank_accel_{window}_12"] = pr - pr.shift(12)
    for wma_period, roc_period in ((20, 1), (60, 6), (120, 12), (200, 20)):
        ma = _wma(c, wma_period)
        out[f"q_wma_roc_{wma_period}_{roc_period}"] = ma.pct_change(roc_period)
    for window in (24, 48, 96):
        mom = c.pct_change(window)
        rv = ret1.rolling(window, min_periods=max(8, window // 4)).std().replace(0, np.nan)
        out[f"qc_momentum_{window}"] = _rolling_norm_forecast(mom / rv)
    blend_cols = [f"py_ewmac_{x}" for x in (8, 16, 32, 64)] + [f"py_breakout_{x}" for x in (40, 80, 160, 320)]
    out["py_blend"] = _rolling_norm_forecast(out[blend_cols].mean(axis=1))
    out["vol_z"] = (out["volume"] - out["volume"].rolling(60).mean()) / out["volume"].rolling(60).std().replace(0, np.nan)
    reversal_raw = -out["ret_z"]
    out["qc_volume_reversal"] = _rolling_norm_forecast(reversal_raw.where(out["vol_z"] >= 1.0))
    return out


def make_signals(
    df: pd.DataFrame,
    family: str,
    session: str,
    direction_mode: str,
    threshold: float,
    mom_window: int,
    accel_lag: int,
    vol_filter: str,
    cooldown: int,
) -> list[tuple[int, int]]:
    session_mask = np.ones(len(df), dtype=bool) if session == "ALL" else (df["session"].to_numpy(str) == session)
    if family == "ewmac":
        score = df[f"ewmac_{mom_window}"].to_numpy(float)
        long_mask = session_mask & (score >= threshold)
        short_mask = session_mask & (score <= -threshold)
    elif family in ("py_ewmac", "py_mr", "py_breakout", "py_open_close", "py_weather", "py_blend", "qc_momentum", "qc_volume_reversal"):
        if family in ("py_open_close", "py_weather", "py_blend", "qc_volume_reversal"):
            score_name = family
        elif family == "qc_momentum":
            score_name = f"qc_momentum_{mom_window}"
        else:
            score_name = f"{family}_{mom_window}"
        score = df[score_name].to_numpy(float)
        long_mask = session_mask & (score >= threshold)
        short_mask = session_mask & (score <= -threshold)
    elif family == "wma_roc":
        w = mom_window
        score = df[f"wma_dist_{w}"].to_numpy(float)
        roc = df[f"roc_{w}"].to_numpy(float)
        long_mask = session_mask & (score >= threshold) & (roc > 0)
        short_mask = session_mask & (score <= -threshold) & (roc < 0)
    elif family == "quantiacs_state":
        score = df[f"q_wma_roc_{mom_window}"].to_numpy(float)
        long_mask = session_mask & (score >= threshold)
        short_mask = session_mask & (score <= -threshold)
    else:
        w = mom_window
        accel = df[f"rank_accel_{w}_{accel_lag}"].to_numpy(float)
        rank = df[f"rank_{w}"].to_numpy(float)
        long_mask = session_mask & (accel >= threshold) & (rank >= 0.70)
        short_mask = session_mask & (accel <= -threshold) & (rank <= 0.30)

    if vol_filter == "vol_spike":
        vf = df["vol_z"].to_numpy(float) >= 1.0
        long_mask &= vf
        short_mask &= vf
    elif vol_filter == "no_spike":
        vf = df["vol_z"].to_numpy(float) < 2.5
        long_mask &= vf
        short_mask &= vf

    raw = []
    if direction_mode in ("long", "both"):
        raw.extend((idx, 1) for idx in np.flatnonzero(long_mask))
    if direction_mode in ("short", "both"):
        raw.extend((idx, -1) for idx in np.flatnonzero(short_mask))
    raw.sort(key=lambda item: item[0])
    picked = []
    last = -10**9
    for idx, direction in raw:
        if idx - last >= cooldown:
            picked.append((int(idx), int(direction)))
            last = int(idx)
    return picked


def eval_trades(
    df: pd.DataFrame,
    picked: list[tuple[int, int]],
    meta: dict,
    tp_atr: float,
    sl_atr: float,
    hold_bars: int,
) -> tuple[dict, list[dict]]:
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    atr = df["atr14"].to_numpy(float)
    times = pd.to_datetime(df["timestamp"], utc=True)
    values = []
    trades = []
    for idx, direction in picked:
        entry_idx = idx + 1
        if entry_idx >= len(df) or not np.isfinite(atr[idx]) or atr[idx] <= 0:
            continue
        entry = opens[entry_idx]
        risk = max(float(atr[idx]) * sl_atr, TICK * 8)
        target = max(float(atr[idx]) * tp_atr, TICK * 8)
        stop_price = entry - direction * risk
        target_price = entry + direction * target
        end = min(len(df) - 1, entry_idx + hold_bars - 1)
        exit_price = closes[end]
        exit_idx = end
        reason = "time"
        tick_result = direction * (exit_price - entry) / TICK
        for j in range(entry_idx, end + 1):
            if direction > 0:
                hit_sl = lows[j] <= stop_price
                hit_tp = highs[j] >= target_price
            else:
                hit_sl = highs[j] >= stop_price
                hit_tp = lows[j] <= target_price
            if hit_sl:
                tick_result = -risk / TICK
                exit_price = stop_price
                exit_idx = j
                reason = "sl"
                break
            if hit_tp:
                tick_result = target / TICK
                exit_price = target_price
                exit_idx = j
                reason = "tp"
                break
        pnl = tick_result * MNQ_TICK_VALUE - ROUND_TURN_COST
        values.append(float(pnl))
        trades.append(
            {
                **meta,
                "signal_time": times.iloc[idx].isoformat(),
                "entry_time": times.iloc[entry_idx].isoformat(),
                "exit_time": times.iloc[exit_idx].isoformat(),
                "direction": "long" if direction > 0 else "short",
                "entry": round(float(entry), 2),
                "exit": round(float(exit_price), 2),
                "pnl": round(float(pnl), 2),
                "reason": reason,
            }
        )
    m = _metrics(values)
    thirds = []
    if values:
        for part, chunk in enumerate(np.array_split(np.arange(len(values)), 3), start=1):
            item = _metrics([values[int(i)] for i in chunk])
            item["part"] = part
            thirds.append(item)
    ci, p_pos = _mean_ci(values)
    m.update({**meta, "tp_atr": tp_atr, "sl_atr": sl_atr, "hold_bars": hold_bars})
    m["thirds_pnl"] = "/".join(str(x["pnl"]) for x in thirds)
    m["mean_ci"] = ci
    m["p_positive_approx"] = p_pos
    reasons = []
    if m["trades"] < 40:
        reasons.append("sample<40")
    if any(part["pnl"] <= 0 for part in thirds):
        reasons.append("third_negative")
    if ci[0] <= 0:
        reasons.append("mean_ci_lower<=0")
    if m["profit_factor"] < 1.5:
        reasons.append("pf<1.5")
    if abs(m["total_loss"]) > abs(m["pnl"]):
        reasons.append("loss>pnl")
    m["verdict"] = "PASS" if not reasons else "FAIL"
    m["reasons"] = ",".join(reasons)
    m["score"] = round(
        float(m["pnl"] + min(m["profit_factor"], 4.0) * 300 - m["max_dd"] * 1.5 - abs(m["total_loss"]) * 0.1),
        2,
    )
    return m, trades


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_latest(rows: list[dict], best_trades: list[dict]) -> None:
    rows.sort(key=lambda r: (r["score"], r["pnl"], r["profit_factor"]), reverse=True)
    write_csv(RESULTS_CSV, rows)
    write_csv(TOP_CSV, rows[:100])
    write_csv(BEST_TRADES_CSV, best_trades)
    passes = [r for r in rows if r["verdict"] == "PASS"]
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tested_variants_with_trades": len(rows),
        "passes": len(passes),
        "top": rows[:25],
        "files": {
            "results_csv": str(RESULTS_CSV),
            "results_jsonl": str(RESULTS_JSONL),
            "top_csv": str(TOP_CSV),
            "best_trades_csv": str(BEST_TRADES_CSV),
            "report_md": str(REPORT_MD),
        },
    }
    LATEST_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Futures Repo Strategy Port",
        "",
        f"Generated: {payload['created_at']}",
        f"Variants with trades: {len(rows)}",
        f"PASS count: {len(passes)}",
        "",
        "| verdict | tf | family | mode | session | trades | pnl | dd | pf | win | loss | th | tp/sl/h | thirds | reasons |",
        "|---|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for row in rows[:40]:
        lines.append(
            f"| {row['verdict']} | {row['tf']} | {row['family']} | {row['direction_mode']} | {row['session']} | "
            f"{row['trades']} | {row['pnl']} | {row['max_dd']} | {row['profit_factor']} | {row['win_rate']} | "
            f"{row['total_loss']} | {row['threshold']} | {row['tp_atr']}/{row['sl_atr']}/{row['hold_bars']} | "
            f"{row['thirds_pnl']} | {row['reasons']} |"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_JSONL.write_text("", encoding="utf-8")
    rows = []
    best_trades = []
    best_score = -1e18
    written = 0
    for tf in (5, 15, 60):
        df = add_features(load_bars(tf)).reset_index(drop=True)
        configs = []
        for fast_slow in ("4_16", "8_32", "16_64"):
            configs.append(("ewmac", fast_slow, 0))
        for fast in (2, 4, 8, 16, 32, 64):
            configs.append(("py_ewmac", fast, 0))
            configs.append(("py_mr", fast, 0))
        for lookback in (20, 40, 80, 160, 320):
            configs.append(("py_breakout", lookback, 0))
        for family in ("py_open_close", "py_weather", "py_blend", "qc_volume_reversal"):
            configs.append((family, 0, 0))
        for wma_roc in ("20_1", "60_6", "120_12", "200_20"):
            configs.append(("quantiacs_state", wma_roc, 0))
        for w in (24, 48, 96):
            configs.append(("qc_momentum", w, 0))
        for w in (12, 24, 48):
            configs.append(("wma_roc", w, 0))
            configs.append(("rank_accel", w, 6))
            configs.append(("rank_accel", w, 12))

        for family, mom_window, accel_lag in configs:
            if family == "rank_accel":
                thresholds = [0.15, 0.25, 0.35]
            elif family == "quantiacs_state":
                thresholds = [0.0005, 0.001, 0.002, 0.005]
            elif family in ("py_ewmac", "py_mr", "py_breakout", "py_open_close", "py_weather", "py_blend", "qc_momentum", "qc_volume_reversal"):
                thresholds = [5.0, 10.0, 15.0]
            else:
                thresholds = [0.5, 1.0, 1.5]
            for session, direction_mode, threshold, vol_filter, hold_bars in product(
                ["RTH", "ASIA", "ALL"],
                ["long", "short", "both"],
                thresholds,
                ["none", "no_spike", "vol_spike"],
                [3, 6, 12],
            ):
                picked = make_signals(
                    df, family, session, direction_mode, threshold,
                    mom_window, accel_lag, vol_filter, cooldown=max(3, hold_bars),
                )
                if len(picked) < 10:
                    continue
                for tp_atr, sl_atr in ((1.5, 1.0), (2.0, 1.0), (3.0, 1.0), (2.0, 0.75)):
                    meta = {
                        "tf": tf,
                        "family": family,
                        "mom_window": str(mom_window),
                        "accel_lag": accel_lag,
                        "session": session,
                        "direction_mode": direction_mode,
                        "threshold": threshold,
                        "vol_filter": vol_filter,
                    }
                    result, trades = eval_trades(df, picked, meta, tp_atr, sl_atr, hold_bars)
                    if result["trades"] >= 10:
                        rows.append(result)
                        with RESULTS_JSONL.open("a", encoding="utf-8") as fh:
                            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
                        if result["score"] > best_score:
                            best_score = result["score"]
                            best_trades = trades
                        written += 1
                        if written % 2000 == 0:
                            write_latest(rows, best_trades)
                            print(f"checkpoint variants={written}", flush=True)
    write_latest(rows, best_trades)
    print(f"wrote {REPORT_MD}")
    if rows:
        top = rows[0]
        print(
            "top", top["verdict"], top["tf"], top["family"], top["direction_mode"], top["session"],
            "pnl", top["pnl"], "dd", top["max_dd"], "pf", top["profit_factor"],
            "loss", top["total_loss"], "reasons", top["reasons"],
        )


if __name__ == "__main__":
    main()
