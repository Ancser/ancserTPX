"""200-minute momentum + 5-minute reversion factor study.

Research-only script. It does not touch live engines, broker state, orders,
presets, or the running server.

Hypothesis:
- Use exactly 200 minutes of momentum as the directional filter
  (40 completed 5m bars).
- Enter only on a 5m pullback/reversion against that 200m direction.
- Signals are evaluated on completed 5m bars and entered at the next 5m open.
- Optional ATR-mixed exits compare fast ATR, slow ATR blend, fixed+ATR blend,
  and max(fixed, ATR) risk widths.
"""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "scratchpad" / "momentum200_reversion5m"

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
    mom_lookback_bars: int
    mom_threshold: float
    rev_span: int
    rev_threshold: float
    risk_mode: str
    sl_atr: float
    tp_atr: float
    sl_fixed: float
    tp_fixed: float
    max_hold_bars: int
    confirm_turn: bool
    mean_exit: bool
    session_set: str
    max_trades_per_day: int


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
    session: str
    trade_date: str
    mom_norm: float
    rev_z: float


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
        return ContractSpec("MNQ", point_value=2.0, tick_size=0.25, round_turn_cost=1.24)
    if sym == "MES":
        return ContractSpec("MES", point_value=5.0, tick_size=0.25, round_turn_cost=1.24)
    if sym == "ES":
        return ContractSpec("ES", point_value=50.0, tick_size=0.25, round_turn_cost=3.80)
    if sym in {"MGC", "MGC1!"}:
        return ContractSpec("MGC", point_value=10.0, tick_size=0.1, round_turn_cost=1.50)
    if sym in {"GC", "GC1!"}:
        return ContractSpec("GC", point_value=100.0, tick_size=0.1, round_turn_cost=3.80)
    if sym == "ZL":
        # Soybean Oil futures quote cents per pound. Contract size is 60,000 lb,
        # so a 1.00 price move is about $600 and the 0.01 tick is about $6.
        return ContractSpec("ZL", point_value=600.0, tick_size=0.01, round_turn_cost=3.80)
    return ContractSpec(sym, point_value=1.0, tick_size=0.01, round_turn_cost=0.0)


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


def _max_dd(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += float(value)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _metrics(trades: list[Trade]) -> dict:
    values = [float(t.pnl) for t in sorted(trades, key=lambda t: t.exit_time)]
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return {
            "trades": 0,
            "pnl": 0.0,
            "max_dd": 0.0,
            "profit_factor": 0.0,
            "win_rate": 0.0,
            "expectancy": 0.0,
            "total_gain": 0.0,
            "total_loss": 0.0,
        }
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


def _bootstrap_ci(values: list[float], seed: int = 2005, n: int = 2000) -> tuple[list[float], float]:
    arr = np.asarray(values, dtype=float)
    if len(arr) < 10:
        return [0.0, 0.0], 0.0
    rng = np.random.default_rng(seed)
    means = rng.choice(arr, size=(n, len(arr)), replace=True).mean(axis=1)
    return (
        [round(float(np.quantile(means, 0.025)), 3), round(float(np.quantile(means, 0.975)), 3)],
        round(float((means > 0).mean()), 4),
    )


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


def load_5m(symbol: str, csv_path: str = "", csv_interval: int = 5) -> pd.DataFrame:
    if csv_path:
        df = _read_csv_bars(Path(csv_path))
        if csv_interval > 5:
            raise SystemExit("Cannot downsample a larger CSV interval into 5m.")
    else:
        candles = sorted(candle_store.load(symbol.upper(), 1), key=lambda c: c.timestamp)
        if not candles:
            store_dir = ROOT / "data" / "store"
            available = sorted(p.name.split("_accumulated_")[0] for p in store_dir.glob("*_accumulated_1m.pkl"))
            raise SystemExit(f"No {symbol} candle store found. Available stores: {available}. Use --csv for external data.")
        rows = []
        for c in candles:
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

    if csv_path and csv_interval == 5:
        bars = df.resample("5min", label="left", closed="left").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
    else:
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


def add_base_indicators(df: pd.DataFrame, atr_len: int = 14, slow_atr_len: int = 40) -> pd.DataFrame:
    out = df.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = tr.rolling(atr_len, min_periods=max(5, atr_len // 2)).mean()
    out["atr_slow"] = tr.rolling(slow_atr_len, min_periods=max(10, slow_atr_len // 2)).mean()
    return out


def add_param_factors(df: pd.DataFrame, params: Params) -> pd.DataFrame:
    out = df.copy()
    close = out["close"]
    atr = out["atr"].replace(0, np.nan)
    lookback = params.mom_lookback_bars
    # Normalized 200m momentum. sqrt(lookback) keeps thresholds comparable
    # across nearby lookback tests, though default keeps exactly 40 bars.
    out["mom_norm"] = (close - close.shift(lookback)) / (atr * np.sqrt(lookback))
    mean = close.ewm(span=params.rev_span, adjust=False).mean()
    out["rev_mean"] = mean
    out["rev_z"] = (close - mean) / atr
    out["rev_turn"] = out["rev_z"].diff()
    return out


def _risk_widths(params: Params, atr_fast: float, atr_slow: float) -> tuple[float, float]:
    if not np.isfinite(atr_slow) or atr_slow <= 0:
        atr_slow = atr_fast
    atr_fast_sl = atr_fast * params.sl_atr
    atr_fast_tp = atr_fast * params.tp_atr
    atr_slow_sl = atr_slow * params.sl_atr
    atr_slow_tp = atr_slow * params.tp_atr
    if params.risk_mode == "atr":
        return atr_fast_sl, atr_fast_tp
    if params.risk_mode == "atr_blend":
        return (atr_fast_sl + atr_slow_sl) / 2.0, (atr_fast_tp + atr_slow_tp) / 2.0
    if params.risk_mode == "fixed_atr_blend":
        return (params.sl_fixed + atr_fast_sl) / 2.0, (params.tp_fixed + atr_fast_tp) / 2.0
    if params.risk_mode == "max_fixed_atr":
        return max(params.sl_fixed, atr_fast_sl), max(params.tp_fixed, atr_fast_tp)
    raise ValueError(f"unknown risk_mode: {params.risk_mode}")


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
    data = add_param_factors(df, params)
    allowed = _allowed_sessions(params.session_set)

    ts = data["timestamp"].to_list()
    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    atrs = data["atr"].to_numpy(float)
    atrs_slow = data["atr_slow"].to_numpy(float)
    mom = data["mom_norm"].to_numpy(float)
    rev = data["rev_z"].to_numpy(float)
    rev_turn = data["rev_turn"].to_numpy(float)
    rev_mean = data["rev_mean"].to_numpy(float)
    sessions = data["session"].to_list()
    trade_dates = data["trade_date"].to_list()

    trades: list[Trade] = []
    daily_counts: dict[str, int] = {}
    open_pos: dict | None = None
    min_i = max(params.mom_lookback_bars + 2, params.rev_span + 2, 20)

    for i in range(min_i, len(data) - 1):
        if open_pos is not None:
            direction = int(open_pos["direction"])
            hit = _exit_hit(direction, highs[i], lows[i], open_pos["sl"], open_pos["tp"])
            hold_bars = i - int(open_pos["entry_i"])
            if hit is None and params.mean_exit:
                if direction > 0 and closes[i] >= rev_mean[i]:
                    hit = (closes[i], "mean")
                elif direction < 0 and closes[i] <= rev_mean[i]:
                    hit = (closes[i], "mean")
            if hit is None and hold_bars >= params.max_hold_bars:
                hit = (closes[i], "time")
            if hit is not None:
                exit_price, reason = hit
                gross = (exit_price - open_pos["entry"]) * direction * spec.point_value * contracts
                pnl = gross - spec.round_turn_cost * contracts
                trades.append(
                    Trade(
                        param_id=params.param_id,
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
                        hold_bars=hold_bars,
                        session=open_pos["session"],
                        trade_date=open_pos["trade_date"],
                        mom_norm=open_pos["mom_norm"],
                        rev_z=open_pos["rev_z"],
                    )
                )
                open_pos = None
            continue

        if sessions[i] not in allowed:
            continue
        trade_date = trade_dates[i]
        if daily_counts.get(trade_date, 0) >= params.max_trades_per_day:
            continue
        if not np.isfinite(mom[i]) or not np.isfinite(rev[i]) or not np.isfinite(atrs[i]):
            continue

        direction = 0
        if mom[i] >= params.mom_threshold and rev[i] <= -params.rev_threshold:
            if not params.confirm_turn or rev_turn[i] > 0:
                direction = 1
        elif mom[i] <= -params.mom_threshold and rev[i] >= params.rev_threshold:
            if not params.confirm_turn or rev_turn[i] < 0:
                direction = -1
        if direction == 0:
            continue

        entry_i = i + 1
        entry = _round_to_tick(opens[entry_i], spec.tick_size)
        risk, reward = _risk_widths(params, atrs[i], atrs_slow[i])
        risk = max(spec.tick_size, risk)
        reward = max(spec.tick_size, reward)
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
            "mom_norm": float(mom[i]),
            "rev_z": float(rev[i]),
        }

    if open_pos is not None:
        direction = int(open_pos["direction"])
        exit_price = closes[-1]
        gross = (exit_price - open_pos["entry"]) * direction * spec.point_value * contracts
        trades.append(
            Trade(
                param_id=params.param_id,
                direction="long" if direction > 0 else "short",
                entry_time=open_pos["entry_time"],
                exit_time=ts[-1],
                entry=open_pos["entry"],
                exit=exit_price,
                sl=open_pos["sl"],
                tp=open_pos["tp"],
                pnl=gross - spec.round_turn_cost * contracts,
                gross_pnl=gross,
                exit_reason="final_flat",
                hold_bars=len(data) - 1 - int(open_pos["entry_i"]),
                session=open_pos["session"],
                trade_date=open_pos["trade_date"],
                mom_norm=open_pos["mom_norm"],
                rev_z=open_pos["rev_z"],
            )
        )
    return trades


def make_grid(
    fixed_base: float,
    risk_modes: list[str],
    full_grid: bool = False,
) -> list[Params]:
    mom_lookbacks = [40] if not full_grid else [32, 40, 48]
    mom_thresholds = [0.4, 0.7] if not full_grid else [0.3, 0.5, 0.7, 0.9]
    rev_spans = [12, 20]
    rev_thresholds = [0.8, 1.1] if not full_grid else [0.7, 0.9, 1.1, 1.3]
    risk_pairs = [(1.0, 0.75), (1.25, 1.0), (1.5, 1.25), (1.5, 1.5)]
    fixed_pairs = [(fixed_base, fixed_base)] if not full_grid else [(fixed_base, fixed_base), (fixed_base * 1.5, fixed_base * 1.5)]
    max_holds = [6, 12]
    confirm_turns = [False, True]
    mean_exits = [False, True]
    session_sets = ["ALL", "PRE_RTH", "RTH"]
    max_per_day = [1, 3]

    grid = []
    idx = 1
    for (
        lookback,
        mom_threshold,
        rev_span,
        rev_threshold,
        risk_mode,
        risk_pair,
        fixed_pair,
        max_hold,
        confirm_turn,
        mean_exit,
        session_set,
        max_trades,
    ) in product(
        mom_lookbacks,
        mom_thresholds,
        rev_spans,
        rev_thresholds,
        risk_modes,
        risk_pairs,
        fixed_pairs,
        max_holds,
        confirm_turns,
        mean_exits,
        session_sets,
        max_per_day,
    ):
        grid.append(
            Params(
                param_id=f"M200R5_{idx:04d}",
                mom_lookback_bars=lookback,
                mom_threshold=mom_threshold,
                rev_span=rev_span,
                rev_threshold=rev_threshold,
                risk_mode=risk_mode,
                sl_atr=risk_pair[0],
                tp_atr=risk_pair[1],
                sl_fixed=fixed_pair[0],
                tp_fixed=fixed_pair[1],
                max_hold_bars=max_hold,
                confirm_turn=confirm_turn,
                mean_exit=mean_exit,
                session_set=session_set,
                max_trades_per_day=max_trades,
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
    all_metrics = _metrics(trades)
    is_metrics = _metrics(is_trades)
    oos_metrics = _metrics(oos_trades)
    row = asdict(params)
    for prefix, data in (("all", all_metrics), ("is", is_metrics), ("oos", oos_metrics)):
        for key, value in data.items():
            row[f"{prefix}_{key}"] = value
    row["wf_positive"] = wf_positive(trades)
    return row


def wf_positive(trades: list[Trade]) -> bool:
    ordered = sorted(trades, key=lambda t: t.entry_time)
    if len(ordered) < 30:
        return False
    parts = np.array_split(np.asarray(ordered, dtype=object), 3)
    return all(sum(float(t.pnl) for t in part.tolist()) > 0 for part in parts)


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
    for t in sorted(trades, key=lambda x: x.entry_time):
        row = asdict(t)
        row["entry_time"] = t.entry_time.isoformat()
        row["exit_time"] = t.exit_time.isoformat()
        rows.append(row)
    return rows


def _monthly(trades: list[Trade]) -> list[dict]:
    by_month: dict[str, list[Trade]] = {}
    for trade in trades:
        key = trade.entry_time.strftime("%Y-%m")
        by_month.setdefault(key, []).append(trade)
    return [{"month": key, **_metrics(value)} for key, value in sorted(by_month.items())]


def _format_metrics(name: str, data: dict) -> str:
    return (
        f"{name}: trades={data['trades']} pnl={data['pnl']:.2f} "
        f"pf={data['profit_factor']:.2f} dd={data['max_dd']:.2f} "
        f"expect={data['expectancy']:.2f} win={data['win_rate']:.1%}"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="MNQ")
    parser.add_argument("--csv", default="")
    parser.add_argument("--csv-interval", type=int, default=5)
    parser.add_argument("--contracts", type=int, default=1)
    parser.add_argument("--split-frac", type=float, default=0.70)
    parser.add_argument("--full-grid", action="store_true")
    parser.add_argument(
        "--risk-modes",
        default="atr,atr_blend,fixed_atr_blend,max_fixed_atr",
        help="Comma list: atr,atr_blend,fixed_atr_blend,max_fixed_atr.",
    )
    parser.add_argument("--fixed-base", type=float, default=0.0, help="0 = median ATR.")
    parser.add_argument("--out", default=str(OUT_DIR))
    parser.add_argument("--min-is-trades", type=int, default=40)
    parser.add_argument("--min-oos-trades", type=int, default=20)
    parser.add_argument("--min-is-pf", type=float, default=1.20)
    parser.add_argument("--min-oos-pf", type=float, default=1.10)
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = _spec_for_symbol(args.symbol)
    df = add_base_indicators(load_5m(args.symbol, args.csv, args.csv_interval))
    median_atr = float(df["atr"].dropna().median())
    fixed_base = float(args.fixed_base) if args.fixed_base > 0 else median_atr
    risk_modes = [x.strip() for x in str(args.risk_modes).split(",") if x.strip()]
    split_dt = split_time(df, args.split_frac)
    grid = make_grid(fixed_base, risk_modes, args.full_grid)
    print(
        f"{spec.symbol} 5m bars={len(df)} span={df.iloc[0]['timestamp']} -> {df.iloc[-1]['timestamp']} "
        f"split={split_dt} variants={len(grid)} contracts={args.contracts} "
        f"risk_modes={risk_modes} fixed_base={fixed_base:.4f}"
    )

    rows = []
    trades_by_param: dict[str, list[Trade]] = {}
    for idx, params in enumerate(grid, start=1):
        trades = backtest(df, params, spec, contracts=max(1, args.contracts))
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
    ci, prob_pos = _bootstrap_ci([t.pnl for t in selected_trades])

    _write_csv(out_dir / "grid_results.csv", rows)
    _write_csv(out_dir / "ranked.csv", ranked)
    _write_csv(out_dir / "validated.csv", validated)
    _write_csv(out_dir / "best_trades.csv", _trade_rows(selected_trades))
    _write_csv(out_dir / "best_monthly.csv", _monthly(selected_trades))

    report = {
        "symbol": spec.symbol,
        "point_value": spec.point_value,
        "tick_size": spec.tick_size,
        "round_turn_cost": spec.round_turn_cost,
        "risk_modes": risk_modes,
        "fixed_base": fixed_base,
        "bars": int(len(df)),
        "span": [df.iloc[0]["timestamp"].isoformat(), df.iloc[-1]["timestamp"].isoformat()],
        "split": split_dt.isoformat(),
        "variants": len(grid),
        "validated": len(validated),
        "selected": selected,
        "selected_all": _metrics(selected_trades),
        "selected_is": _metrics(selected_is),
        "selected_oos": _metrics(selected_oos),
        "bootstrap_expectancy_ci": ci,
        "bootstrap_prob_expectancy_positive": prob_pos,
    }
    (out_dir / "latest.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    lines = [
        f"# {spec.symbol} 200m momentum + 5m reversion",
        "",
        "Research-only. Signal is completed 5m bar, entry is next 5m open.",
        "200m momentum is exactly 40 completed 5m bars in the default grid.",
        "ATR-mixed risk modes: atr, atr_blend, fixed_atr_blend, max_fixed_atr.",
        "",
        f"symbol: {spec.symbol}, point_value: {spec.point_value}, tick_size: {spec.tick_size}",
        f"bars: {len(df)} {df.iloc[0]['timestamp']} -> {df.iloc[-1]['timestamp']}",
        f"split: {split_dt}",
        f"variants: {len(grid)}",
        f"risk_modes: {risk_modes}",
        f"fixed_base: {fixed_base}",
        f"validated: {len(validated)}",
    ]
    if selected:
        lines.extend(
            [
                "",
                "Selected row:",
                json.dumps(selected, indent=2, default=str),
                "",
                _format_metrics("ALL", report["selected_all"]),
                _format_metrics("IS", report["selected_is"]),
                _format_metrics("OOS", report["selected_oos"]),
                f"bootstrap expectancy ci: {ci}, prob_positive={prob_pos}",
            ]
        )
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\noutputs={out_dir}")
    print(f"validated={len(validated)}")
    if selected:
        print(
            "selected "
            f"{selected['param_id']} mom_th={selected['mom_threshold']} rev_span={selected['rev_span']} "
            f"rev_th={selected['rev_threshold']} risk={selected['risk_mode']} "
            f"sl_atr={selected['sl_atr']} tp_atr={selected['tp_atr']} "
            f"sl_fixed={selected['sl_fixed']:.4f} tp_fixed={selected['tp_fixed']:.4f} "
            f"hold={selected['max_hold_bars']} turn={selected['confirm_turn']} "
            f"mean_exit={selected['mean_exit']} session={selected['session_set']} "
            f"max_day={selected['max_trades_per_day']}"
        )
        print(_format_metrics("ALL", report["selected_all"]))
        print(_format_metrics("IS", report["selected_is"]))
        print(_format_metrics("OOS", report["selected_oos"]))
        print(f"bootstrap_expectancy_ci={ci} prob_positive={prob_pos}")


if __name__ == "__main__":
    main()
