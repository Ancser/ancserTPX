"""Port icefishball Pine indicators into research strategies.

Research-only. It does not touch live engines, broker state, orders, presets,
or the running server.

Tested signal ports:
- EMAPMO normal and early signals from the attached Pine script.
- KDJMA R signals from the pasted Pine script.

Strategy assumption because both Pine scripts are indicators, not strategies:
- Evaluate signals on completed 5m bars.
- Enter at the next 5m open.
- Red/top labels are short; green/bottom labels are long.
- Exits are ATR-based SL/TP, optional opposite-signal exit, and max hold.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store

OUT_ROOT = ROOT / "scratchpad" / "icefishball_pine_strategy"
NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    point_value: float
    tick_size: float
    round_turn_cost: float


@dataclass(frozen=True)
class Params:
    param_id: str
    signal_name: str
    side_mode: str
    session_set: str
    sl_atr: float
    tp_atr: float
    max_hold_bars: int
    exit_on_opposite: bool
    max_trades_per_day: int


@dataclass
class Trade:
    param_id: str
    signal_name: str
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
    session: str
    trade_date: str


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_dt(raw: str) -> datetime:
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
        try:
            return _utc(datetime.strptime(text, fmt))
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {raw!r}")


def _spec_for_symbol(symbol: str) -> ContractSpec:
    sym = symbol.upper().replace("=F", "")
    if sym == "MNQ":
        return ContractSpec("MNQ", 2.0, 0.25, 1.24)
    if sym == "MES":
        return ContractSpec("MES", 5.0, 0.25, 1.24)
    if sym == "ES":
        return ContractSpec("ES", 50.0, 0.25, 3.80)
    if sym == "MGC":
        return ContractSpec("MGC", 10.0, 0.1, 1.50)
    if sym == "GC":
        return ContractSpec("GC", 100.0, 0.1, 3.80)
    if sym == "ZL":
        return ContractSpec("ZL", 600.0, 0.01, 3.80)
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


def _allowed_sessions(name: str) -> set[str]:
    if name == "ALL":
        return {"ASIA", "EURO", "PRE", "RTH", "AH"}
    if name == "PRE_RTH":
        return {"PRE", "RTH"}
    if name == "RTH":
        return {"RTH"}
    if name == "EURO_PRE_RTH":
        return {"EURO", "PRE", "RTH"}
    raise ValueError(f"unknown session_set: {name}")


def _round_to_tick(price: float, tick_size: float) -> float:
    return round(price / tick_size) * tick_size


def _read_csv_bars(path: Path) -> pd.DataFrame:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise SystemExit(f"CSV has no header: {path}")
        keymap = {name.lower().strip(): name for name in reader.fieldnames}

        def key(*names: str) -> str:
            for name in names:
                if name in keymap:
                    return keymap[name]
            raise SystemExit(f"CSV missing required column, tried: {', '.join(names)}")

        ts_key = key("timestamp", "time", "datetime", "date")
        open_key = key("open", "o")
        high_key = key("high", "h")
        low_key = key("low", "l")
        close_key = key("close", "c")
        volume_key = keymap.get("volume") or keymap.get("vol")
        rows = []
        for row in reader:
            vals = [row[open_key], row[high_key], row[low_key], row[close_key]]
            if any(v in ("", None) for v in vals):
                continue
            rows.append(
                {
                    "timestamp": _parse_dt(row[ts_key]),
                    "open": float(row[open_key]),
                    "high": float(row[high_key]),
                    "low": float(row[low_key]),
                    "close": float(row[close_key]),
                    "volume": float(row[volume_key]) if volume_key and row.get(volume_key) not in ("", None) else 0.0,
                }
            )
    if not rows:
        raise SystemExit(f"CSV has no usable bars: {path}")
    return pd.DataFrame(rows).set_index("timestamp").sort_index()


def load_5m(symbol: str, csv_path: str = "") -> pd.DataFrame:
    if csv_path:
        df = _read_csv_bars(Path(csv_path))
    else:
        candles = sorted(candle_store.load(symbol.upper(), 1), key=lambda c: c.timestamp)
        if not candles:
            raise SystemExit(f"No local candle store for {symbol}; pass --csv.")
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
    bars["session"] = bars["timestamp"].map(_session_for)
    bars["trade_date"] = bars["timestamp"].map(lambda ts: str(_topstep_trade_date(ts)))
    ny = bars["timestamp"].map(lambda x: x.astimezone(NY))
    bars["ny_minute"] = ny.map(lambda x: x.hour * 60 + x.minute)
    return bars


def _rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def _bcwsma(values: pd.Series, length: int, multiplier: int) -> pd.Series:
    out = []
    prev = 0.0
    for raw in values.fillna(0.0).to_numpy(float):
        prev = (multiplier * raw + (length - multiplier) * prev) / length
        out.append(prev)
    return pd.Series(out, index=values.index)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
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
    out["atr14"] = tr.rolling(14, min_periods=7).mean()

    # KDJMA port: ilong=9, isig=3.
    low9 = out["low"].rolling(9, min_periods=9).min()
    high9 = out["high"].rolling(9, min_periods=9).max()
    rsv = 100.0 * (close - low9) / (high9 - low9).replace(0, np.nan)
    k = _bcwsma(rsv, 3, 1)
    d = _bcwsma(k, 3, 1)
    out["kdj_k"] = k
    out["kdj_d"] = d
    out["kdj_j"] = 3 * k - 2 * d

    delta = close.diff()
    up = _rma(delta.clip(lower=0), 14)
    down = _rma((-delta.clip(upper=0)), 14)
    rs = up / down.replace(0, np.nan)
    out["rsi14"] = np.where(down == 0, 100.0, np.where(up == 0, 0.0, 100.0 - (100.0 / (1.0 + rs))))

    out["kdjma_short"] = (
        (out["kdj_j"] > 80)
        & (out["kdj_j"] < out["kdj_j"].shift(1))
        & (close > close.shift(1))
        & (out["rsi14"] > 60)
    )
    out["kdjma_long"] = (
        (out["kdj_j"] < 20)
        & (out["kdj_j"] > out["kdj_j"].shift(1))
        & (close < close.shift(1))
        & (out["rsi14"] < 40)
    )

    # EMAPMO port: firstLength=100, secondLength=50, signalLength=10.
    roc = 100.0 * (close - close.shift(1)) / close.shift(1).replace(0, np.nan)
    pmo = (10.0 * roc.ewm(span=100, adjust=False).mean()).ewm(span=50, adjust=False).mean()
    signal = pmo.ewm(span=10, adjust=False).mean()
    out["pmo"] = pmo
    out["pmo_signal"] = signal
    p = pmo - signal
    q = signal - pmo
    updiv = 0.06
    downdiv = -0.10
    crossunder = (pmo < signal) & (pmo.shift(1) >= signal.shift(1))
    crossover = (pmo > signal) & (pmo.shift(1) <= signal.shift(1))
    out["emapmo_normal_short"] = (pmo > updiv) & crossunder
    out["emapmo_normal_long"] = (pmo < downdiv) & crossover
    out["emapmo_early_short"] = (signal > updiv) & (p < p.shift(1)) & (pmo > signal) & (p.shift(1) < p.shift(2))
    out["emapmo_early_long"] = (signal < downdiv) & (q < q.shift(1)) & (pmo < signal) & (q.shift(1) < q.shift(2))
    return out


def _exit_hit(direction: int, high: float, low: float, sl: float, tp: float) -> tuple[float, str] | None:
    if direction > 0:
        hit_sl = low <= sl
        hit_tp = high >= tp
        if hit_sl and hit_tp:
            return sl, "sl_same_bar"
        if hit_sl:
            return sl, "sl"
        if hit_tp:
            return tp, "tp"
    else:
        hit_sl = high >= sl
        hit_tp = low <= tp
        if hit_sl and hit_tp:
            return sl, "sl_same_bar"
        if hit_sl:
            return sl, "sl"
        if hit_tp:
            return tp, "tp"
    return None


def backtest(df: pd.DataFrame, params: Params, spec: ContractSpec, contracts: int = 1) -> list[Trade]:
    allowed = _allowed_sessions(params.session_set)
    long_col = f"{params.signal_name}_long"
    short_col = f"{params.signal_name}_short"

    ts = df["timestamp"].to_list()
    opens = df["open"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    closes = df["close"].to_numpy(float)
    atr = df["atr14"].to_numpy(float)
    sessions = df["session"].to_list()
    trade_dates = df["trade_date"].to_list()
    long_sig = df[long_col].fillna(False).to_numpy(bool)
    short_sig = df[short_col].fillna(False).to_numpy(bool)

    trades: list[Trade] = []
    open_pos: dict | None = None
    daily_counts: dict[str, int] = {}
    for i in range(150, len(df) - 1):
        if open_pos is not None:
            direction = int(open_pos["direction"])
            hit = _exit_hit(direction, highs[i], lows[i], open_pos["sl"], open_pos["tp"])
            hold = i - int(open_pos["entry_i"])
            if hit is None and params.exit_on_opposite:
                if direction > 0 and short_sig[i]:
                    hit = (closes[i], "opposite")
                elif direction < 0 and long_sig[i]:
                    hit = (closes[i], "opposite")
            if hit is None and hold >= params.max_hold_bars:
                hit = (closes[i], "time")
            if hit is not None:
                exit_price, reason = hit
                gross = (exit_price - open_pos["entry"]) * direction * spec.point_value * contracts
                pnl = gross - spec.round_turn_cost * contracts
                trades.append(
                    Trade(
                        param_id=params.param_id,
                        signal_name=params.signal_name,
                        direction="long" if direction > 0 else "short",
                        entry_time=open_pos["entry_time"],
                        exit_time=ts[i],
                        entry=open_pos["entry"],
                        exit=exit_price,
                        sl=open_pos["sl"],
                        tp=open_pos["tp"],
                        pnl=pnl,
                        gross_pnl=gross,
                        exit_reason=reason,
                        hold_bars=hold,
                        session=open_pos["session"],
                        trade_date=open_pos["trade_date"],
                    )
                )
                open_pos = None
            continue

        if sessions[i] not in allowed:
            continue
        trade_date = trade_dates[i]
        if daily_counts.get(trade_date, 0) >= params.max_trades_per_day:
            continue
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue

        direction = 0
        if long_sig[i] and params.side_mode in {"both", "long"}:
            direction = 1
        elif short_sig[i] and params.side_mode in {"both", "short"}:
            direction = -1
        if direction == 0:
            continue

        entry_i = i + 1
        entry = _round_to_tick(opens[entry_i], spec.tick_size)
        risk = max(spec.tick_size, atr[i] * params.sl_atr)
        reward = max(spec.tick_size, atr[i] * params.tp_atr)
        if direction > 0:
            sl = _round_to_tick(entry - risk, spec.tick_size)
            tp = _round_to_tick(entry + reward, spec.tick_size)
        else:
            sl = _round_to_tick(entry + risk, spec.tick_size)
            tp = _round_to_tick(entry - reward, spec.tick_size)
        if entry == sl or entry == tp:
            continue

        daily_counts[trade_date] = daily_counts.get(trade_date, 0) + 1
        open_pos = {
            "direction": direction,
            "entry_i": entry_i,
            "entry_time": ts[entry_i],
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "session": sessions[i],
            "trade_date": trade_date,
        }
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


def wf_positive(trades: list[Trade]) -> bool:
    ordered = sorted(trades, key=lambda t: t.entry_time)
    if len(ordered) < 30:
        return False
    parts = np.array_split(np.asarray(ordered, dtype=object), 3)
    return all(sum(float(t.pnl) for t in part.tolist()) > 0 for part in parts)


def make_grid() -> list[Params]:
    signal_names = ["emapmo_normal", "emapmo_early", "kdjma"]
    side_modes = ["both", "long", "short"]
    session_sets = ["ALL", "PRE_RTH", "RTH"]
    risk_pairs = [(1.0, 0.75), (1.0, 1.0), (1.5, 1.0), (1.5, 1.5)]
    max_holds = [6, 12, 24]
    opposite = [False, True]
    max_per_day = [1, 3]
    grid = []
    idx = 1
    for signal_name, side_mode, session_set, risk_pair, hold, exit_opp, max_day in product(
        signal_names, side_modes, session_sets, risk_pairs, max_holds, opposite, max_per_day
    ):
        grid.append(
            Params(
                param_id=f"IFB_{idx:04d}",
                signal_name=signal_name,
                side_mode=side_mode,
                session_set=session_set,
                sl_atr=risk_pair[0],
                tp_atr=risk_pair[1],
                max_hold_bars=hold,
                exit_on_opposite=exit_opp,
                max_trades_per_day=max_day,
            )
        )
        idx += 1
    return grid


def split_time(df: pd.DataFrame, frac: float) -> datetime:
    idx = max(1, min(len(df) - 1, int(len(df) * frac)))
    return df.iloc[idx]["timestamp"]


def row_for(params: Params, trades: list[Trade], split_dt: datetime) -> dict:
    is_trades = [t for t in trades if t.entry_time < split_dt]
    oos_trades = [t for t in trades if t.entry_time >= split_dt]
    row = asdict(params)
    for prefix, data in (("all", metrics(trades)), ("is", metrics(is_trades)), ("oos", metrics(oos_trades))):
        for key, value in data.items():
            row[f"{prefix}_{key}"] = value
    row["wf_positive"] = wf_positive(trades)
    return row


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
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
    parser.add_argument("--symbol", default="MNQ")
    parser.add_argument("--csv", default="")
    parser.add_argument("--contracts", type=int, default=1)
    parser.add_argument("--split-frac", type=float, default=0.70)
    parser.add_argument("--min-is-trades", type=int, default=30)
    parser.add_argument("--min-oos-trades", type=int, default=15)
    parser.add_argument("--min-is-pf", type=float, default=1.20)
    parser.add_argument("--min-oos-pf", type=float, default=1.10)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    spec = _spec_for_symbol(args.symbol)
    out_dir = Path(args.out) if args.out else OUT_ROOT / spec.symbol.lower()
    out_dir.mkdir(parents=True, exist_ok=True)
    df = add_indicators(load_5m(args.symbol, args.csv))
    split_dt = split_time(df, args.split_frac)
    grid = make_grid()
    print(
        f"{spec.symbol} bars={len(df)} span={df.iloc[0]['timestamp']} -> {df.iloc[-1]['timestamp']} "
        f"split={split_dt} variants={len(grid)}"
    )

    rows = []
    trades_by_param = {}
    for idx, params in enumerate(grid, start=1):
        trades = backtest(df, params, spec, max(1, args.contracts))
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
        "symbol": spec.symbol,
        "bars": len(df),
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
        f"# Icefishball Pine strategy test - {spec.symbol}",
        "",
        "Indicator signals converted to strategy assumptions:",
        "- EMAPMO green/bottom = long, red/top = short.",
        "- KDJMA green R = long, red R = short.",
        "- Signal on completed 5m bar, entry at next 5m open.",
        "- Exits use ATR SL/TP grid plus optional opposite-signal exit.",
        "",
        f"bars: {len(df)} {df.iloc[0]['timestamp']} -> {df.iloc[-1]['timestamp']}",
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
            f"selected {selected['param_id']} {selected['signal_name']} side={selected['side_mode']} "
            f"session={selected['session_set']} sl={selected['sl_atr']} tp={selected['tp_atr']} "
            f"hold={selected['max_hold_bars']} opposite={selected['exit_on_opposite']}"
        )
        print(_fmt("ALL", report["selected_all"]))
        print(_fmt("IS", report["selected_is"]))
        print(_fmt("OOS", report["selected_oos"]))


if __name__ == "__main__":
    main()
