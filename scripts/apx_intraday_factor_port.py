"""APX/Alpaca factor strategy ported to MNQ intraday research.

This is not a literal stock-portfolio copy.  APX is cross-sectional
stock-ranking; TPX has one instrument.  The portable idea is the factor
construction pattern:
  - medium momentum + short pullback
  - rank acceleration
  - volume/participation acceleration
  - drift-regime filter
  - volatility-scaled exits

Signals are evaluated on completed 5m bars and entered at next 5m open.

Run:
  python -m scripts.apx_intraday_factor_port
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, time, timezone
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "apx_intraday_factor"
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


def _bootstrap_ci(values: list[float], seed: int = 1309, n: int = 3000) -> tuple[list[float], float]:
    arr = np.asarray(values, dtype=float)
    if len(arr) < 5:
        return [0.0, 0.0], 0.0
    rng = np.random.default_rng(seed)
    means = rng.choice(arr, size=(n, len(arr)), replace=True).mean(axis=1)
    return (
        [round(float(np.quantile(means, 0.025)), 3), round(float(np.quantile(means, 0.975)), 3)],
        round(float((means > 0).mean()), 4),
    )


def _z(series: pd.Series, window: int = 240) -> pd.Series:
    mu = series.rolling(window, min_periods=max(20, window // 4)).mean()
    sd = series.rolling(window, min_periods=max(20, window // 4)).std()
    return ((series - mu) / sd.replace(0, np.nan)).clip(-5, 5)


def load_5m() -> pd.DataFrame:
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
    return bars


def add_factors(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    c = out["close"]
    ret1 = c.pct_change()
    out["ret3"] = c / c.shift(3) - 1
    out["ret6"] = c / c.shift(6) - 1
    out["ret12"] = c / c.shift(12) - 1
    out["ret48"] = c / c.shift(48) - 1
    out["mom_12_1"] = out["ret48"] - out["ret6"]
    out["pullback3"] = -out["ret3"]
    out["bounce3"] = out["ret3"]
    out["pos_ratio63"] = (ret1 > 0).rolling(63, min_periods=20).mean()
    out["drift_up"] = out["pos_ratio63"] > 0.56
    out["drift_down"] = out["pos_ratio63"] < 0.44

    vol_fast = out["volume"].rolling(12, min_periods=4).mean()
    vol_slow = out["volume"].rolling(63, min_periods=20).mean()
    out["volume_accel"] = vol_fast / vol_slow.replace(0, np.nan) - 1
    out["alpha012"] = np.sign(out["volume"].diff()) * (-c.diff())
    out["spread_proxy"] = (out["high"] - out["low"]) / out["close"]

    # Time-series rank acceleration: current medium-momentum percentile minus
    # the percentile 12 bars ago. APX uses cross-sectional rank acceleration.
    rank_pct = out["mom_12_1"].rolling(240, min_periods=60).rank(pct=True)
    out["rank_accel"] = rank_pct - rank_pct.shift(12)

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

    out["z_mom48"] = _z(out["ret48"])
    out["z_mom_12_1"] = _z(out["mom_12_1"])
    out["z_pullback3"] = _z(out["pullback3"])
    out["z_bounce3"] = _z(out["bounce3"])
    out["z_volaccel"] = _z(out["volume_accel"])
    out["z_alpha012"] = _z(out["alpha012"])
    out["z_rank_accel"] = _z(out["rank_accel"], 120)
    out["z_spread"] = _z(out["spread_proxy"])

    out["score_claude1_long"] = 0.70 * out["z_mom48"] + 0.30 * out["z_pullback3"]
    out["score_claude1_short"] = -0.70 * out["z_mom48"] + 0.30 * out["z_bounce3"]
    out["score_v15s_long"] = 0.70 * out["z_mom_12_1"] + 0.30 * out["z_pullback3"]
    out["score_v15s_short"] = -0.70 * out["z_mom_12_1"] + 0.30 * out["z_bounce3"]
    out["score_rankaccel_long"] = 0.60 * out["z_mom48"] + 0.40 * out["z_rank_accel"]
    out["score_rankaccel_short"] = -0.60 * out["z_mom48"] - 0.40 * out["z_rank_accel"]
    out["score_volrotation_long"] = 0.60 * out["z_mom48"] + 0.40 * out["z_volaccel"]
    out["score_volrotation_short"] = -0.60 * out["z_mom48"] + 0.40 * out["z_volaccel"]
    out["score_alpha012_long"] = 0.50 * out["z_mom48"] + 0.50 * out["z_alpha012"]
    out["score_alpha012_short"] = -0.50 * out["z_mom48"] - 0.50 * out["z_alpha012"]
    return out


PRESETS = {
    "claude1": ("score_claude1_long", "score_claude1_short"),
    "v15s": ("score_v15s_long", "score_v15s_short"),
    "rankaccel": ("score_rankaccel_long", "score_rankaccel_short"),
    "volrotation": ("score_volrotation_long", "score_volrotation_short"),
    "alpha012": ("score_alpha012_long", "score_alpha012_short"),
}


def _pick(indices: list[tuple[int, int]], cooldown: int) -> list[tuple[int, int]]:
    picked = []
    last = -10**9
    for idx, direction in sorted(indices, key=lambda item: item[0]):
        if idx - last >= cooldown:
            picked.append((int(idx), int(direction)))
            last = int(idx)
    return picked


def make_signals(
    df: pd.DataFrame,
    preset: str,
    side: str,
    session: str,
    threshold: float,
    drift_filter: str,
    spread_filter: str,
    cooldown: int,
) -> list[tuple[int, int]]:
    long_col, short_col = PRESETS[preset]
    session_mask = np.ones(len(df), dtype=bool) if session == "ALL" else (df["session"].to_numpy(str) == session)
    long_mask = session_mask & (df[long_col].to_numpy(float) >= threshold)
    short_mask = session_mask & (df[short_col].to_numpy(float) >= threshold)
    if drift_filter == "align":
        long_mask &= df["drift_up"].fillna(False).to_numpy(bool)
        short_mask &= df["drift_down"].fillna(False).to_numpy(bool)
    elif drift_filter == "counter":
        long_mask &= (~df["drift_down"].fillna(False)).to_numpy(bool)
        short_mask &= (~df["drift_up"].fillna(False)).to_numpy(bool)
    if spread_filter == "not_wide":
        wide = df["z_spread"].to_numpy(float) > 1.5
        long_mask &= ~wide
        short_mask &= ~wide
    elif spread_filter == "wide_only":
        wide = df["z_spread"].to_numpy(float) > 1.5
        long_mask &= wide
        short_mask &= wide

    raw = []
    if side in ("long", "both"):
        raw.extend((idx, 1) for idx in np.flatnonzero(long_mask))
    if side in ("short", "both"):
        raw.extend((idx, -1) for idx in np.flatnonzero(short_mask))
    return _pick(raw, cooldown)


def eval_signals(
    df: pd.DataFrame,
    picked: list[tuple[int, int]],
    preset: str,
    side: str,
    session: str,
    threshold: float,
    drift_filter: str,
    spread_filter: str,
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
        risk_points = max(float(atr[idx]) * sl_atr, TICK * 8)
        target_points = max(float(atr[idx]) * tp_atr, TICK * 8)
        stop_price = entry - direction * risk_points
        target_price = entry + direction * target_points
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
                exit_price = stop_price
                exit_idx = j
                tick_result = -risk_points / TICK
                reason = "sl"
                break
            if hit_tp:
                exit_price = target_price
                exit_idx = j
                tick_result = target_points / TICK
                reason = "tp"
                break
        pnl = tick_result * MNQ_TICK_VALUE - ROUND_TURN_COST
        values.append(float(pnl))
        trades.append(
            {
                "preset": preset,
                "signal_time": times.iloc[idx].isoformat(),
                "entry_time": times.iloc[entry_idx].isoformat(),
                "exit_time": times.iloc[exit_idx].isoformat(),
                "session": df.iloc[idx]["session"],
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
    ci, p_pos = _bootstrap_ci(values)
    m.update(
        {
            "preset": preset,
            "side": side,
            "session": session,
            "threshold": threshold,
            "drift_filter": drift_filter,
            "spread_filter": spread_filter,
            "tp_atr": tp_atr,
            "sl_atr": sl_atr,
            "hold_bars": hold_bars,
            "thirds": thirds,
            "bootstrap_mean_ci": ci,
            "bootstrap_p_positive": p_pos,
            "first_signal": trades[0]["signal_time"] if trades else "",
            "last_signal": trades[-1]["signal_time"] if trades else "",
        }
    )
    score = (
        m["pnl"]
        + min(m["profit_factor"], 4.0) * 300.0
        - m["max_dd"] * 1.5
        - abs(m["total_loss"]) * 0.10
        - max(0, m["trades"] - 240) * 2
    )
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
    m["score"] = round(float(score), 2)
    m["verdict"] = "PASS" if not reasons else "FAIL"
    m["reasons"] = ",".join(reasons)
    return m, trades


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = add_factors(load_5m())
    df = df.dropna(subset=["atr14", "z_mom48", "z_mom_12_1", "z_pullback3", "z_rank_accel"]).reset_index(drop=True)
    rows = []
    best_trades = []
    best_score = -1e18
    signal_grid = product(
        ["claude1", "v15s", "rankaccel", "volrotation"],
        ["long", "short", "both"],
        ["RTH", "ASIA", "ALL"],
        [1.5, 2.0],
        ["none", "align"],
        ["none", "not_wide"],
    )
    signal_total = 4 * 3 * 3 * 2 * 2 * 2
    total = signal_total * 18
    i = 0
    for sig in signal_grid:
        preset, side, session, threshold, drift_filter, spread_filter = sig
        for hold_bars in [3, 6, 12]:
            picked = make_signals(df, preset, side, session, threshold, drift_filter, spread_filter, cooldown=max(3, hold_bars))
            if len(picked) < 10:
                continue
            for tp_atr in [1.5, 2.0, 3.0]:
                for sl_atr in [0.75, 1.0]:
                    params = (preset, side, session, threshold, drift_filter, spread_filter, tp_atr, sl_atr, hold_bars)
                    result, trades = eval_signals(df, picked, *params)
                    if result["trades"] >= 10:
                        rows.append(result)
                        if result["score"] > best_score:
                            best_score = result["score"]
                            best_trades = trades
        i += 1
        if i % 50 == 0:
            print(f"signals {i}/{signal_total} rows={len(rows)}", flush=True)

    rows.sort(key=lambda r: (r["score"], r["pnl"], r["profit_factor"]), reverse=True)
    flat = []
    for row in rows:
        item = {k: v for k, v in row.items() if k != "thirds"}
        item["thirds_pnl"] = "/".join(str(x["pnl"]) for x in row.get("thirds", []))
        flat.append(item)
    write_csv(RESULTS_CSV, flat)
    write_csv(TOP_CSV, flat[:100])
    write_csv(BEST_TRADES_CSV, best_trades)

    passes = [r for r in flat if r["verdict"] == "PASS"]
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "bars_5m": int(len(df)),
        "total_variants": total,
        "tested_variants_with_trades": int(len(flat)),
        "passes": int(len(passes)),
        "top": flat[:25],
        "files": {
            "results_csv": str(RESULTS_CSV),
            "top_csv": str(TOP_CSV),
            "best_trades_csv": str(BEST_TRADES_CSV),
            "report_md": str(REPORT_MD),
        },
    }
    LATEST_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# APX Intraday Factor Port",
        "",
        f"Generated: {payload['created_at']}",
        f"5m bars: {len(df)}",
        f"Variants with trades: {len(flat)} / {total}",
        f"PASS count: {len(passes)}",
        "",
        "| verdict | preset | side | session | trades | pnl | dd | pf | win | loss | exp | th | drift | spread | tp/sl/h | thirds | reasons |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---|---|",
    ]
    for row in flat[:35]:
        lines.append(
            f"| {row['verdict']} | {row['preset']} | {row['side']} | {row['session']} | "
            f"{row['trades']} | {row['pnl']} | {row['max_dd']} | {row['profit_factor']} | "
            f"{row['win_rate']} | {row['total_loss']} | {row['expectancy']} | {row['threshold']} | "
            f"{row['drift_filter']} | {row['spread_filter']} | {row['tp_atr']}/{row['sl_atr']}/{row['hold_bars']} | "
            f"{row['thirds_pnl']} | {row['reasons']} |"
        )
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_MD}")
    if flat:
        top = flat[0]
        print(
            "top", top["verdict"], top["preset"], top["side"], top["session"],
            "pnl", top["pnl"], "dd", top["max_dd"], "pf", top["profit_factor"],
            "loss", top["total_loss"], "reasons", top["reasons"],
        )


if __name__ == "__main__":
    main()
