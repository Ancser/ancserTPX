"""MNQ London sweep -> HTF reversion -> Asia target study.

Research-only script. It does not touch live engines, broker state, orders,
presets, or the running server.

Idea:
- Build the Asia range from 22:00-07:00 UTC.
- During London/EURO (07:00-11:00 UTC), wait for a sweep of Asia high/low.
- Wait for a completed higher-timeframe candle to close back inside the Asia
  range as a reversion confirmation.
- Main setup: sweep Asia high -> short, target Asia low.
- Symmetric setup is included for comparison: sweep Asia low -> long, target
  Asia high.
- Entry is the next 5m open after the completed HTF confirmation candle.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, date, time, timedelta, timezone
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from backend.data import candle_store


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "scratchpad" / "london_sweep_asia_target_mnq"

TICK = 0.25
POINT_VALUE = 2.0
ROUND_TURN_COST = 1.24


@dataclass(frozen=True)
class Params:
    param_id: str
    side_mode: str
    htf_minutes: int
    sweep_buffer_ticks: int
    reclaim_buffer_ticks: int
    sl_buffer_ticks: int
    min_asia_range_atr: float
    max_sweep_atr: float
    max_hold_bars: int


@dataclass
class Trade:
    param_id: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry: float
    exit: float
    sl: float
    tp: float
    pnl: float
    gross_pnl: float
    exit_reason: str
    hold_bars: int
    asia_key: str
    asia_high: float
    asia_low: float
    sweep_price: float
    confirm_time: datetime


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _round_to_tick(price: float) -> float:
    return round(price / TICK) * TICK


def _asia_key(ts: datetime) -> date:
    ts = _utc(ts)
    if ts.time() >= time(22, 0):
        return ts.date() + timedelta(days=1)
    return ts.date()


def _is_asia(ts: datetime) -> bool:
    tod = _utc(ts).time()
    return tod >= time(22, 0) or tod < time(7, 0)


def _is_london(ts: datetime) -> bool:
    tod = _utc(ts).time()
    return time(7, 0) <= tod < time(11, 0)


def load_5m() -> pd.DataFrame:
    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit("No MNQ candle store found.")
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
    bars["asia_key"] = bars["timestamp"].map(lambda ts: str(_asia_key(ts)))
    bars["is_asia"] = bars["timestamp"].map(_is_asia)
    bars["is_london"] = bars["timestamp"].map(_is_london)

    prev_close = bars["close"].shift(1)
    tr = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close).abs(),
            (bars["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    bars["atr14"] = tr.rolling(14, min_periods=7).mean()
    return bars


def asia_ranges(df: pd.DataFrame) -> dict[str, dict]:
    ranges = {}
    asia = df[df["is_asia"]]
    for key, group in asia.groupby("asia_key"):
        if len(group) < 60:
            continue
        ranges[str(key)] = {
            "high": float(group["high"].max()),
            "low": float(group["low"].min()),
            "start": group.iloc[0]["timestamp"],
            "end": group.iloc[-1]["timestamp"],
            "bars": int(len(group)),
            "atr": float(group["atr14"].dropna().median()) if not group["atr14"].dropna().empty else 0.0,
        }
    return ranges


def aggregate_htf(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    work = df.set_index("timestamp").sort_index()
    htf = work.resample(f"{minutes}min", label="left", closed="left").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        atr14=("atr14", "last"),
    )
    htf.dropna(subset=["open", "high", "low", "close"], inplace=True)
    htf.reset_index(inplace=True)
    htf["end_time"] = htf["timestamp"] + pd.to_timedelta(minutes, unit="m")
    htf["asia_key"] = htf["timestamp"].map(lambda ts: str(_asia_key(ts)))
    htf["is_london"] = htf["timestamp"].map(_is_london)
    return htf


def _entry_index_after(df: pd.DataFrame, ts: datetime) -> int | None:
    idx = df.index[df["timestamp"] >= ts]
    if len(idx) == 0:
        return None
    return int(idx[0])


def _exit_hit(direction: int, high: float, low: float, sl: float, tp: float) -> tuple[float, str] | None:
    if direction < 0:
        hit_sl = high >= sl
        hit_tp = low <= tp
        if hit_sl and hit_tp:
            return sl, "sl_same_bar"
        if hit_sl:
            return sl, "sl"
        if hit_tp:
            return tp, "tp_asia_low"
    else:
        hit_sl = low <= sl
        hit_tp = high >= tp
        if hit_sl and hit_tp:
            return sl, "sl_same_bar"
        if hit_sl:
            return sl, "sl"
        if hit_tp:
            return tp, "tp_asia_high"
    return None


def build_signals(
    df: pd.DataFrame,
    params: Params,
    ranges: dict[str, dict],
    htf_cache: dict[int, pd.DataFrame],
) -> list[dict]:
    htf = htf_cache[params.htf_minutes]
    signals = []
    used: set[tuple[str, int]] = set()
    sweep_buffer = params.sweep_buffer_ticks * TICK
    reclaim_buffer = params.reclaim_buffer_ticks * TICK
    sl_buffer = params.sl_buffer_ticks * TICK

    for _, bar in htf.iterrows():
        if not bool(bar["is_london"]):
            continue
        key = str(bar["asia_key"])
        ar = ranges.get(key)
        if not ar:
            continue
        asia_high = float(ar["high"])
        asia_low = float(ar["low"])
        asia_range = asia_high - asia_low
        atr = float(bar["atr14"]) if np.isfinite(bar["atr14"]) else float(ar["atr"])
        if atr <= 0:
            continue
        if asia_range < params.min_asia_range_atr * atr:
            continue

        if params.side_mode in {"short", "both"} and (key, -1) not in used:
            swept = float(bar["high"]) > asia_high + sweep_buffer
            reclaimed = float(bar["close"]) < asia_high - reclaim_buffer
            extension = float(bar["high"]) - asia_high
            if swept and reclaimed and extension <= params.max_sweep_atr * atr:
                entry_i = _entry_index_after(df, bar["end_time"])
                if entry_i is not None:
                    signals.append(
                        {
                            "direction": -1,
                            "entry_i": entry_i,
                            "confirm_time": bar["end_time"],
                            "asia_key": key,
                            "asia_high": asia_high,
                            "asia_low": asia_low,
                            "sweep_price": float(bar["high"]),
                            "tp": asia_low,
                            "sl": _round_to_tick(float(bar["high"]) + sl_buffer),
                        }
                    )
                    used.add((key, -1))

        if params.side_mode in {"long", "both"} and (key, 1) not in used:
            swept = float(bar["low"]) < asia_low - sweep_buffer
            reclaimed = float(bar["close"]) > asia_low + reclaim_buffer
            extension = asia_low - float(bar["low"])
            if swept and reclaimed and extension <= params.max_sweep_atr * atr:
                entry_i = _entry_index_after(df, bar["end_time"])
                if entry_i is not None:
                    signals.append(
                        {
                            "direction": 1,
                            "entry_i": entry_i,
                            "confirm_time": bar["end_time"],
                            "asia_key": key,
                            "asia_high": asia_high,
                            "asia_low": asia_low,
                            "sweep_price": float(bar["low"]),
                            "tp": asia_high,
                            "sl": _round_to_tick(float(bar["low"]) - sl_buffer),
                        }
                    )
                    used.add((key, 1))
    return sorted(signals, key=lambda s: s["entry_i"])


def backtest(
    df: pd.DataFrame,
    params: Params,
    ranges: dict[str, dict],
    htf_cache: dict[int, pd.DataFrame],
) -> list[Trade]:
    signals = build_signals(df, params, ranges, htf_cache)
    trades = []
    last_exit_i = -1
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    ts = df["timestamp"].to_list()

    for signal in signals:
        entry_i = int(signal["entry_i"])
        if entry_i <= last_exit_i or entry_i >= len(df) - 1:
            continue
        direction = int(signal["direction"])
        entry = _round_to_tick(opens[entry_i])
        tp = _round_to_tick(float(signal["tp"]))
        sl = _round_to_tick(float(signal["sl"]))
        if direction < 0 and (tp >= entry or sl <= entry):
            continue
        if direction > 0 and (tp <= entry or sl >= entry):
            continue

        exit_i = min(len(df) - 1, entry_i + params.max_hold_bars)
        exit_price = closes[exit_i]
        reason = "time"
        for i in range(entry_i, min(len(df), entry_i + params.max_hold_bars + 1)):
            hit = _exit_hit(direction, highs[i], lows[i], sl, tp)
            if hit is not None:
                exit_price, reason = hit
                exit_i = i
                break
        gross = (exit_price - entry) * direction * POINT_VALUE
        pnl = gross - ROUND_TURN_COST
        trades.append(
            Trade(
                param_id=params.param_id,
                direction="short" if direction < 0 else "long",
                entry_time=ts[entry_i],
                exit_time=ts[exit_i],
                entry=entry,
                exit=exit_price,
                sl=sl,
                tp=tp,
                pnl=pnl,
                gross_pnl=gross,
                exit_reason=reason,
                hold_bars=exit_i - entry_i,
                asia_key=str(signal["asia_key"]),
                asia_high=float(signal["asia_high"]),
                asia_low=float(signal["asia_low"]),
                sweep_price=float(signal["sweep_price"]),
                confirm_time=signal["confirm_time"],
            )
        )
        last_exit_i = exit_i
    return trades


def _max_dd(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def metrics(trades: list[Trade]) -> dict:
    values = [float(t.pnl) for t in sorted(trades, key=lambda t: t.exit_time)]
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return {"trades": 0, "pnl": 0.0, "max_dd": 0.0, "profit_factor": 0.0, "win_rate": 0.0, "expectancy": 0.0, "total_gain": 0.0, "total_loss": 0.0}
    wins = arr[arr > 0]
    losses = arr[arr < 0]
    gain = float(wins.sum())
    loss = float(losses.sum())
    return {
        "trades": int(len(arr)),
        "pnl": round(float(arr.sum()), 2),
        "max_dd": round(_max_dd(values), 2),
        "profit_factor": round(gain / abs(loss), 4) if loss < 0 else (999.0 if gain > 0 else 0.0),
        "win_rate": round(float((arr > 0).mean()), 4),
        "expectancy": round(float(arr.mean()), 3),
        "total_gain": round(gain, 2),
        "total_loss": round(loss, 2),
    }


def split_time(df: pd.DataFrame, frac: float) -> datetime:
    idx = max(1, min(len(df) - 1, int(len(df) * frac)))
    return df.iloc[idx]["timestamp"]


def wf_positive(trades: list[Trade]) -> bool:
    ordered = sorted(trades, key=lambda t: t.entry_time)
    if len(ordered) < 15:
        return False
    parts = np.array_split(np.asarray(ordered, dtype=object), 3)
    return all(sum(float(t.pnl) for t in part.tolist()) > 0 for part in parts)


def row_for(params: Params, trades: list[Trade], split_dt: datetime) -> dict:
    is_trades = [t for t in trades if t.entry_time < split_dt]
    oos_trades = [t for t in trades if t.entry_time >= split_dt]
    row = asdict(params)
    for prefix, data in (("all", metrics(trades)), ("is", metrics(is_trades)), ("oos", metrics(oos_trades))):
        for key, value in data.items():
            row[f"{prefix}_{key}"] = value
    row["wf_positive"] = wf_positive(trades)
    return row


def make_grid(full_grid: bool = False) -> list[Params]:
    sides = ["short", "both", "long"] if full_grid else ["short", "both"]
    sweeps = [0, 4, 8] if full_grid else [0, 4]
    sl_buffers = [4, 8, 12] if full_grid else [4, 8]
    max_sweeps = [1.5, 3.0, 999.0] if full_grid else [3.0, 999.0]
    holds = [24, 48, 72] if full_grid else [24, 48]
    grid = []
    idx = 1
    for side, htf, sweep, reclaim, sl_buffer, min_range, max_sweep, hold in product(
        sides,
        [15, 30, 60],
        sweeps,
        [0, 2],
        sl_buffers,
        [0.5, 1.0],
        max_sweeps,
        holds,
    ):
        grid.append(
            Params(
                param_id=f"LSA_{idx:04d}",
                side_mode=side,
                htf_minutes=htf,
                sweep_buffer_ticks=sweep,
                reclaim_buffer_ticks=reclaim,
                sl_buffer_ticks=sl_buffer,
                min_asia_range_atr=min_range,
                max_sweep_atr=max_sweep,
                max_hold_bars=hold,
            )
        )
        idx += 1
    return grid


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _trade_rows(trades: list[Trade]) -> list[dict]:
    rows = []
    for trade in sorted(trades, key=lambda t: t.entry_time):
        row = asdict(trade)
        row["entry_time"] = trade.entry_time.isoformat()
        row["exit_time"] = trade.exit_time.isoformat()
        row["confirm_time"] = trade.confirm_time.isoformat()
        rows.append(row)
    return rows


def _monthly(trades: list[Trade]) -> list[dict]:
    by_month: dict[str, list[Trade]] = {}
    for trade in trades:
        key = trade.entry_time.strftime("%Y-%m")
        by_month.setdefault(key, []).append(trade)
    return [{"month": key, **metrics(value)} for key, value in sorted(by_month.items())]


def _fmt(name: str, data: dict) -> str:
    return (
        f"{name}: trades={data['trades']} pnl={data['pnl']:.2f} "
        f"pf={data['profit_factor']:.2f} dd={data['max_dd']:.2f} "
        f"expect={data['expectancy']:.2f} win={data['win_rate']:.1%}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split-frac", type=float, default=0.70)
    parser.add_argument("--min-is-trades", type=int, default=12)
    parser.add_argument("--min-oos-trades", type=int, default=5)
    parser.add_argument("--min-is-pf", type=float, default=1.20)
    parser.add_argument("--min-oos-pf", type=float, default=1.10)
    parser.add_argument("--full-grid", action="store_true")
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = load_5m()
    ranges = asia_ranges(df)
    htf_cache = {minutes: aggregate_htf(df, minutes) for minutes in [15, 30, 60]}
    split_dt = split_time(df, args.split_frac)
    grid = make_grid(args.full_grid)
    print(
        f"MNQ bars={len(df)} asia_days={len(ranges)} span={df.iloc[0]['timestamp']} -> {df.iloc[-1]['timestamp']} "
        f"split={split_dt} variants={len(grid)}"
    )

    rows = []
    trades_by_param = {}
    for idx, params in enumerate(grid, start=1):
        trades = backtest(df, params, ranges, htf_cache)
        trades_by_param[params.param_id] = trades
        rows.append(row_for(params, trades, split_dt))
        if idx % 100 == 0 or idx == len(grid):
            print(f"tested {idx}/{len(grid)}")

    ranked = sorted(
        rows,
        key=lambda r: (
            min(float(r["is_profit_factor"]), float(r["oos_profit_factor"])),
            float(r["oos_pnl"]),
            -float(r["oos_max_dd"]),
        ),
        reverse=True,
    )
    validated = [
        r
        for r in ranked
        if int(r["is_trades"]) >= args.min_is_trades
        and int(r["oos_trades"]) >= args.min_oos_trades
        and float(r["is_profit_factor"]) >= args.min_is_pf
        and float(r["oos_profit_factor"]) >= args.min_oos_pf
        and float(r["is_pnl"]) > 0
        and float(r["oos_pnl"]) > 0
        and bool(r["wf_positive"])
    ]
    selected = validated[0] if validated else (ranked[0] if ranked else None)
    selected_trades = trades_by_param.get(selected["param_id"], []) if selected else []
    selected_is = [t for t in selected_trades if t.entry_time < split_dt]
    selected_oos = [t for t in selected_trades if t.entry_time >= split_dt]

    _write_csv(out_dir / "grid_results.csv", rows)
    _write_csv(out_dir / "ranked.csv", ranked)
    _write_csv(out_dir / "validated.csv", validated)
    _write_csv(out_dir / "best_trades.csv", _trade_rows(selected_trades))
    _write_csv(out_dir / "best_monthly.csv", _monthly(selected_trades))

    report = {
        "bars": len(df),
        "asia_days": len(ranges),
        "span": [df.iloc[0]["timestamp"].isoformat(), df.iloc[-1]["timestamp"].isoformat()],
        "split": split_dt.isoformat(),
        "variants": len(grid),
        "validated": len(validated),
        "selected": selected,
        "selected_all": metrics(selected_trades),
        "selected_is": metrics(selected_is),
        "selected_oos": metrics(selected_oos),
    }
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    lines = [
        "# MNQ London sweep -> HTF reversion -> Asia target",
        "",
        "Rules:",
        "- Asia range: 22:00-07:00 UTC.",
        "- London/EURO sweep window: 07:00-11:00 UTC.",
        "- Main short: sweep Asia high, completed HTF candle closes back under Asia high, target Asia low.",
        "- Symmetric long tested only for comparison.",
        "- Entry: next 5m open after completed HTF confirmation.",
        "",
        f"bars: {len(df)} {df.iloc[0]['timestamp']} -> {df.iloc[-1]['timestamp']}",
        f"asia_days: {len(ranges)}",
        f"split: {split_dt}",
        f"variants: {len(grid)}",
        f"validated: {len(validated)}",
    ]
    if selected:
        lines.extend(["", "Selected:", json.dumps(selected, indent=2, default=str), "", _fmt("ALL", report["selected_all"]), _fmt("IS", report["selected_is"]), _fmt("OOS", report["selected_oos"])])
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\noutputs={out_dir}")
    print(f"validated={len(validated)}")
    if selected:
        print(
            f"selected {selected['param_id']} side={selected['side_mode']} htf={selected['htf_minutes']} "
            f"sweep={selected['sweep_buffer_ticks']} reclaim={selected['reclaim_buffer_ticks']} "
            f"slbuf={selected['sl_buffer_ticks']} range_atr={selected['min_asia_range_atr']} "
            f"max_sweep_atr={selected['max_sweep_atr']} hold={selected['max_hold_bars']}"
        )
        print(_fmt("ALL", report["selected_all"]))
        print(_fmt("IS", report["selected_is"]))
        print(_fmt("OOS", report["selected_oos"]))


if __name__ == "__main__":
    main()
