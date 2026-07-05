"""5m RSI + KDJ extreme-pressure market-entry study.

Research-only.  Signals are evaluated on completed 5m candles and entered at
the next 5m open as a market order.

Idea tested:
  - long only at a stretched 5m bottom
  - short only at a stretched 5m top
  - both sides

Run:
  python -m scripts.rsi_kdj_extreme_study
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, time, timedelta, timezone
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "rsi_kdj_extreme"
RESULTS_CSV = OUT_DIR / "results.csv"
TOP_CSV = OUT_DIR / "top_latest.csv"
LATEST_JSON = OUT_DIR / "latest.json"
REPORT_MD = OUT_DIR / "report.md"
BEST_TRADES_CSV = OUT_DIR / "best_trades.csv"

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


def _bootstrap_ci(values: list[float], seed: int = 109, n: int = 3000) -> tuple[list[float], float]:
    arr = np.asarray(values, dtype=float)
    if len(arr) < 5:
        return [0.0, 0.0], 0.0
    rng = np.random.default_rng(seed)
    means = rng.choice(arr, size=(n, len(arr)), replace=True).mean(axis=1)
    return (
        [round(float(np.quantile(means, 0.025)), 3), round(float(np.quantile(means, 0.975)), 3)],
        round(float((means > 0).mean()), 4),
    )


def load_5m() -> pd.DataFrame:
    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    rows = []
    for c in candles:
        ts = _utc(c.timestamp)
        rows.append(
            {
                "timestamp": ts,
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume or 0),
            }
        )
    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    bars = df.resample("5min", label="left", closed="left").agg(
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
    ny = bars["timestamp"].map(lambda x: x.astimezone(NY))
    bars["ny_minute"] = ny.map(lambda x: x.hour * 60 + x.minute)
    return bars


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))

    low14 = out["low"].rolling(14).min()
    high14 = out["high"].rolling(14).max()
    rsv = 100 * (close - low14) / (high14 - low14).replace(0, np.nan)
    k_vals = []
    d_vals = []
    k = 50.0
    d = 50.0
    for value in rsv.fillna(50).to_numpy(float):
        k = (2.0 / 3.0) * k + (1.0 / 3.0) * value
        d = (2.0 / 3.0) * d + (1.0 / 3.0) * k
        k_vals.append(k)
        d_vals.append(d)
    out["kdj_k"] = k_vals
    out["kdj_d"] = d_vals
    out["kdj_j"] = 3 * out["kdj_k"] - 2 * out["kdj_d"]

    mid = close.rolling(20).mean()
    sd = close.rolling(20).std()
    out["bb_z"] = (close - mid) / sd.replace(0, np.nan)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    out["ema21_dist_atr"] = (close - ema21) / out["atr14"].replace(0, np.nan)

    roll_low = out["low"].rolling(60).min()
    roll_high = out["high"].rolling(60).max()
    out["pos60"] = (close - roll_low) / (roll_high - roll_low).replace(0, np.nan)
    out["range_ticks"] = (out["high"] - out["low"]) / TICK
    out["volume_z60"] = (out["volume"] - out["volume"].rolling(60).mean()) / out["volume"].rolling(60).std().replace(0, np.nan)
    return out


def _filtered_indices(indices: np.ndarray, cooldown: int) -> list[int]:
    picked: list[int] = []
    last = -10**9
    for idx in indices.tolist():
        if idx - last >= cooldown:
            picked.append(int(idx))
            last = int(idx)
    return picked


def simulate(
    df: pd.DataFrame,
    side: str,
    session: str,
    rsi_low: int,
    k_low: int,
    j_low: int,
    bb_min: float,
    pos_extreme: float,
    stretch_min: float,
    tp_ticks: int,
    sl_ticks: int,
    hold_bars: int,
) -> tuple[dict, list[dict]]:
    work = df
    session_mask = np.ones(len(work), dtype=bool) if session == "ALL" else (work["session"].to_numpy(str) == session)
    long_mask = (
        session_mask
        & (work["rsi14"].to_numpy(float) <= rsi_low)
        & (work["kdj_k"].to_numpy(float) <= k_low)
        & (work["kdj_j"].to_numpy(float) <= j_low)
        & (work["bb_z"].to_numpy(float) <= -bb_min)
        & (work["pos60"].to_numpy(float) <= pos_extreme)
        & (work["ema21_dist_atr"].to_numpy(float) <= -stretch_min)
    )
    short_mask = (
        session_mask
        & (work["rsi14"].to_numpy(float) >= 100 - rsi_low)
        & (work["kdj_k"].to_numpy(float) >= 100 - k_low)
        & (work["kdj_j"].to_numpy(float) >= 100 - j_low)
        & (work["bb_z"].to_numpy(float) >= bb_min)
        & (work["pos60"].to_numpy(float) >= 1.0 - pos_extreme)
        & (work["ema21_dist_atr"].to_numpy(float) >= stretch_min)
    )
    if side == "long":
        candidates = [(idx, 1) for idx in np.flatnonzero(long_mask)]
    elif side == "short":
        candidates = [(idx, -1) for idx in np.flatnonzero(short_mask)]
    else:
        candidates = [(idx, 1) for idx in np.flatnonzero(long_mask)] + [(idx, -1) for idx in np.flatnonzero(short_mask)]
        candidates.sort(key=lambda item: item[0])

    picked: list[tuple[int, int]] = []
    last = -10**9
    cooldown = max(3, hold_bars)
    for idx, direction in candidates:
        if idx - last >= cooldown:
            picked.append((int(idx), int(direction)))
            last = int(idx)

    opens = work["open"].to_numpy(float)
    highs = work["high"].to_numpy(float)
    lows = work["low"].to_numpy(float)
    closes = work["close"].to_numpy(float)
    times = pd.to_datetime(work["timestamp"], utc=True)
    trade_rows: list[dict] = []
    pnls: list[float] = []
    ambiguous = 0
    for idx, direction in picked:
        entry_idx = idx + 1
        if entry_idx >= len(work):
            continue
        entry = opens[entry_idx]
        stop_price = entry - direction * sl_ticks * TICK
        target_price = entry + direction * tp_ticks * TICK
        end = min(len(work) - 1, entry_idx + hold_bars - 1)
        exit_idx = end
        exit_price = closes[end]
        exit_reason = "time"
        tick_result = direction * (exit_price - entry) / TICK
        for j in range(entry_idx, end + 1):
            if direction > 0:
                hit_sl = lows[j] <= stop_price
                hit_tp = highs[j] >= target_price
            else:
                hit_sl = highs[j] >= stop_price
                hit_tp = lows[j] <= target_price
            if hit_sl and hit_tp:
                ambiguous += 1
                tick_result = -sl_ticks
                exit_price = stop_price
                exit_idx = j
                exit_reason = "ambiguous_sl"
                break
            if hit_sl:
                tick_result = -sl_ticks
                exit_price = stop_price
                exit_idx = j
                exit_reason = "sl"
                break
            if hit_tp:
                tick_result = tp_ticks
                exit_price = target_price
                exit_idx = j
                exit_reason = "tp"
                break
        pnl = float(tick_result) * MNQ_TICK_VALUE - ROUND_TURN_COST
        pnls.append(pnl)
        trade_rows.append(
            {
                "signal_time": times.iloc[idx].isoformat(),
                "entry_time": times.iloc[entry_idx].isoformat(),
                "exit_time": times.iloc[exit_idx].isoformat(),
                "session": work.iloc[idx]["session"],
                "direction": "long" if direction > 0 else "short",
                "entry": round(float(entry), 2),
                "exit": round(float(exit_price), 2),
                "pnl": round(float(pnl), 2),
                "exit_reason": exit_reason,
                "rsi14": round(float(work.iloc[idx]["rsi14"]), 2),
                "kdj_k": round(float(work.iloc[idx]["kdj_k"]), 2),
                "kdj_j": round(float(work.iloc[idx]["kdj_j"]), 2),
                "bb_z": round(float(work.iloc[idx]["bb_z"]), 3),
                "pos60": round(float(work.iloc[idx]["pos60"]), 3),
                "ema21_dist_atr": round(float(work.iloc[idx]["ema21_dist_atr"]), 3),
            }
        )
    m = _metrics(pnls)
    thirds = []
    if trade_rows:
        chunks = np.array_split(np.arange(len(trade_rows)), 3)
        for part, chunk in enumerate(chunks, start=1):
            vals = [trade_rows[int(i)]["pnl"] for i in chunk]
            item = _metrics(vals)
            item["part"] = part
            thirds.append(item)
    ci, p_pos = _bootstrap_ci(pnls)
    m.update(
        {
            "side": side,
            "session": session,
            "rsi_low": rsi_low,
            "k_low": k_low,
            "j_low": j_low,
            "bb_min": bb_min,
            "pos_extreme": pos_extreme,
            "stretch_min": stretch_min,
            "tp_ticks": tp_ticks,
            "sl_ticks": sl_ticks,
            "hold_bars": hold_bars,
            "ambiguous": ambiguous,
            "thirds": thirds,
            "bootstrap_mean_ci": ci,
            "bootstrap_p_positive": p_pos,
        }
    )
    score = (
        m["pnl"]
        + min(m["profit_factor"], 4.0) * 300.0
        - m["max_dd"] * 1.5
        - abs(m["total_loss"]) * 0.10
        - max(0, m["trades"] - 240) * 2
    )
    m["score"] = round(float(score), 2)
    reasons = []
    if m["trades"] < 40:
        reasons.append("sample<40")
    if any(part["pnl"] <= 0 for part in thirds):
        reasons.append("third_negative")
    if ci[0] <= 0:
        reasons.append("bootstrap_lower<=0")
    if m["profit_factor"] < 1.5:
        reasons.append("pf<1.5")
    if abs(m["total_loss"]) > abs(m["pnl"]):
        reasons.append("loss>pnl")
    m["verdict"] = "PASS" if not reasons else "FAIL"
    m["reasons"] = ",".join(reasons)
    return m, trade_rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def prepare_exit_pnls(df: pd.DataFrame) -> dict[tuple[int, int, int, int], np.ndarray]:
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    high_s = pd.Series(highs)
    low_s = pd.Series(lows)
    close_s = pd.Series(closes)
    out: dict[tuple[int, int, int, int], np.ndarray] = {}
    for hold_bars in [3, 6]:
        max_high = high_s.shift(-1).iloc[::-1].rolling(hold_bars, min_periods=1).max().iloc[::-1].to_numpy()
        min_low = low_s.shift(-1).iloc[::-1].rolling(hold_bars, min_periods=1).min().iloc[::-1].to_numpy()
        exit_close = close_s.shift(-hold_bars).to_numpy()
        entry = pd.Series(opens).shift(-1).to_numpy()
        for tp_ticks in [40, 80, 120]:
            for sl_ticks in [20, 40]:
                for direction in (1, -1):
                    if direction > 0:
                        stop_hit = (entry - min_low) / TICK >= sl_ticks
                        target_hit = (max_high - entry) / TICK >= tp_ticks
                        expiry_ticks = (exit_close - entry) / TICK
                    else:
                        stop_hit = (max_high - entry) / TICK >= sl_ticks
                        target_hit = (entry - min_low) / TICK >= tp_ticks
                        expiry_ticks = (entry - exit_close) / TICK
                    tick_result = np.where(
                        stop_hit,
                        -float(sl_ticks),
                        np.where(target_hit, float(tp_ticks), expiry_ticks),
                    )
                    pnl = tick_result * MNQ_TICK_VALUE - ROUND_TURN_COST
                    pnl[~np.isfinite(pnl)] = np.nan
                    out[(direction, tp_ticks, sl_ticks, hold_bars)] = pnl
    return out


def candidate_indices(
    df: pd.DataFrame,
    side: str,
    session: str,
    rsi_low: int,
    k_low: int,
    j_low: int,
    bb_min: float,
    pos_extreme: float,
    stretch_min: float,
    cooldown: int,
) -> list[tuple[int, int]]:
    session_mask = np.ones(len(df), dtype=bool) if session == "ALL" else (df["session"].to_numpy(str) == session)
    long_mask = (
        session_mask
        & (df["rsi14"].to_numpy(float) <= rsi_low)
        & (df["kdj_k"].to_numpy(float) <= k_low)
        & (df["kdj_j"].to_numpy(float) <= j_low)
        & (df["bb_z"].to_numpy(float) <= -bb_min)
        & (df["pos60"].to_numpy(float) <= pos_extreme)
        & (df["ema21_dist_atr"].to_numpy(float) <= -stretch_min)
    )
    short_mask = (
        session_mask
        & (df["rsi14"].to_numpy(float) >= 100 - rsi_low)
        & (df["kdj_k"].to_numpy(float) >= 100 - k_low)
        & (df["kdj_j"].to_numpy(float) >= 100 - j_low)
        & (df["bb_z"].to_numpy(float) >= bb_min)
        & (df["pos60"].to_numpy(float) >= 1.0 - pos_extreme)
        & (df["ema21_dist_atr"].to_numpy(float) >= stretch_min)
    )
    if side == "long":
        raw = [(idx, 1) for idx in np.flatnonzero(long_mask)]
    elif side == "short":
        raw = [(idx, -1) for idx in np.flatnonzero(short_mask)]
    else:
        raw = [(idx, 1) for idx in np.flatnonzero(long_mask)] + [(idx, -1) for idx in np.flatnonzero(short_mask)]
        raw.sort(key=lambda item: item[0])

    picked: list[tuple[int, int]] = []
    last = -10**9
    for idx, direction in raw:
        if idx - last >= cooldown:
            picked.append((int(idx), int(direction)))
            last = int(idx)
    return picked


def eval_candidates(
    df: pd.DataFrame,
    picked: list[tuple[int, int]],
    exit_pnls: dict[tuple[int, int, int, int], np.ndarray],
    side: str,
    session: str,
    rsi_low: int,
    k_low: int,
    j_low: int,
    bb_min: float,
    pos_extreme: float,
    stretch_min: float,
    tp_ticks: int,
    sl_ticks: int,
    hold_bars: int,
) -> dict:
    values = []
    times = pd.to_datetime(df["timestamp"], utc=True)
    row_refs = []
    for idx, direction in picked:
        pnl = exit_pnls[(direction, tp_ticks, sl_ticks, hold_bars)][idx]
        if np.isfinite(pnl):
            values.append(float(pnl))
            row_refs.append((idx, direction))
    m = _metrics(values)
    thirds = []
    if values:
        chunks = np.array_split(np.arange(len(values)), 3)
        for part, chunk in enumerate(chunks, start=1):
            vals = [values[int(i)] for i in chunk]
            item = _metrics(vals)
            item["part"] = part
            thirds.append(item)
    ci, p_pos = _bootstrap_ci(values)
    m.update(
        {
            "side": side,
            "session": session,
            "rsi_low": rsi_low,
            "k_low": k_low,
            "j_low": j_low,
            "bb_min": bb_min,
            "pos_extreme": pos_extreme,
            "stretch_min": stretch_min,
            "tp_ticks": tp_ticks,
            "sl_ticks": sl_ticks,
            "hold_bars": hold_bars,
            "ambiguous": 0,
            "thirds": thirds,
            "bootstrap_mean_ci": ci,
            "bootstrap_p_positive": p_pos,
            "first_signal": times.iloc[row_refs[0][0]].isoformat() if row_refs else "",
            "last_signal": times.iloc[row_refs[-1][0]].isoformat() if row_refs else "",
        }
    )
    score = (
        m["pnl"]
        + min(m["profit_factor"], 4.0) * 300.0
        - m["max_dd"] * 1.5
        - abs(m["total_loss"]) * 0.10
        - max(0, m["trades"] - 240) * 2
    )
    m["score"] = round(float(score), 2)
    reasons = []
    if m["trades"] < 40:
        reasons.append("sample<40")
    if any(part["pnl"] <= 0 for part in thirds):
        reasons.append("third_negative")
    if ci[0] <= 0:
        reasons.append("bootstrap_lower<=0")
    if m["profit_factor"] < 1.5:
        reasons.append("pf<1.5")
    if abs(m["total_loss"]) > abs(m["pnl"]):
        reasons.append("loss>pnl")
    m["verdict"] = "PASS" if not reasons else "FAIL"
    m["reasons"] = ",".join(reasons)
    return m


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = add_indicators(load_5m())
    df = df.dropna(subset=["rsi14", "kdj_k", "kdj_j", "bb_z", "pos60", "ema21_dist_atr"]).reset_index(drop=True)

    rows: list[dict] = []
    best_trades: list[dict] = []
    best_score = -1e18
    exit_pnls = prepare_exit_pnls(df)

    signal_grid = product(
        ["long", "short", "both"],
        ["RTH", "ASIA", "EURO", "ALL"],
        [20, 25, 30],
        [15, 20],
        [0, 10, 20],
        [1.5, 2.0],
        [0.10, 0.20],
        [0.0, 0.75, 1.25],
    )
    signal_total = 3 * 4 * 3 * 2 * 3 * 2 * 2 * 3
    done = 0
    for sig_params in signal_grid:
        side, session, rsi_low, k_low, j_low, bb_min, pos_extreme, stretch_min = sig_params
        for hold_bars in [3, 6]:
            picked = candidate_indices(
                df, side, session, rsi_low, k_low, j_low,
                bb_min, pos_extreme, stretch_min, cooldown=max(3, hold_bars),
            )
            if len(picked) < 10:
                continue
            for tp_ticks in [40, 80, 120]:
                for sl_ticks in [20, 40]:
                    result = eval_candidates(
                        df, picked, exit_pnls,
                        side, session, rsi_low, k_low, j_low,
                        bb_min, pos_extreme, stretch_min,
                        tp_ticks, sl_ticks, hold_bars,
                    )
                    if result["trades"] >= 10:
                        rows.append(result)
                        if result["score"] > best_score:
                            best_score = result["score"]
                            best_trades = simulate(
                                df, side, session, rsi_low, k_low, j_low,
                                bb_min, pos_extreme, stretch_min,
                                tp_ticks, sl_ticks, hold_bars,
                            )[1]
        done += 1
        if done % 200 == 0:
            print(f"signals {done}/{signal_total} rows={len(rows)}", flush=True)

    rows.sort(key=lambda r: (r["score"], r["pnl"], r["profit_factor"]), reverse=True)
    flat_rows = []
    for row in rows:
        item = {k: v for k, v in row.items() if k != "thirds"}
        item["thirds_pnl"] = "/".join(str(x["pnl"]) for x in row.get("thirds", []))
        flat_rows.append(item)
    write_csv(RESULTS_CSV, flat_rows)
    write_csv(TOP_CSV, flat_rows[:100])
    write_csv(BEST_TRADES_CSV, best_trades)

    passes = [r for r in rows if r["verdict"] == "PASS"]
    latest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bars_5m": int(len(df)),
        "total_variants": signal_total * 12,
        "tested_variants_with_trades": int(len(rows)),
        "passes": len(passes),
        "top": flat_rows[:25],
        "files": {
            "results_csv": str(RESULTS_CSV),
            "top_csv": str(TOP_CSV),
            "best_trades_csv": str(BEST_TRADES_CSV),
            "report_md": str(REPORT_MD),
        },
    }
    LATEST_JSON.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# RSI + KDJ 5m Extreme Study",
        "",
        f"Generated: {latest['created_at']}",
        f"5m bars: {len(df)}",
        f"Variants with trades: {len(rows)} / {signal_total * 12}",
        f"PASS count: {len(passes)}",
        "",
        "Signal definition: completed 5m RSI/KDJ/BB/position extreme, market entry at next 5m open.",
        "",
        "| verdict | side | session | trades | pnl | dd | pf | win | loss | exp | rsi | k | j | bb | pos | stretch | tp/sl/hold | thirds | reasons |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for row in flat_rows[:30]:
        lines.append(
            f"| {row['verdict']} | {row['side']} | {row['session']} | {row['trades']} | {row['pnl']} | "
            f"{row['max_dd']} | {row['profit_factor']} | {row['win_rate']} | {row['total_loss']} | {row['expectancy']} | "
            f"{row['rsi_low']} | {row['k_low']} | {row['j_low']} | {row['bb_min']} | {row['pos_extreme']} | "
            f"{row['stretch_min']} | {row['tp_ticks']}/{row['sl_ticks']}/{row['hold_bars']} | "
            f"{row['thirds_pnl']} | {row['reasons']} |"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_MD}")
    if flat_rows:
        top = flat_rows[0]
        print(
            "top",
            top["verdict"],
            top["side"],
            top["session"],
            "pnl", top["pnl"],
            "dd", top["max_dd"],
            "pf", top["profit_factor"],
            "loss", top["total_loss"],
            "reasons", top["reasons"],
        )


if __name__ == "__main__":
    main()
