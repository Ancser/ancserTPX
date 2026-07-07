"""Research-only replica of the screenshot MGC 5m strategy.

This script does not touch live engines, broker state, orders, presets, or the
running server. It implements a testable hypothesis from the screenshots:

- 5m futures data, originally MGC.
- Trend/regime filter from ATR stair-step bands (SuperTrend-like).
- Pullback entries at fib-style levels, matching labels such as S-0.382/S-0.5.
- Compare ATR-regime SL vs ATR-regime TP, matching the visible optimizer notes.
- Chronological IS/OOS validation and CSV outputs similar to the screenshot.

The private Pine code cannot be recovered from screenshots. Treat this as a
replica hypothesis that can be falsified with MGC data.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from backend.data import candle_store
from backend.db.models import Candle


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "scratchpad" / "eddie_replicate"


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    point_value: float
    tick_size: float
    round_turn_cost: float


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Params:
    atr_len: int
    st_mult: float
    swing_len: int
    fib: float
    entry_mode: str
    side: str
    regime_mode: str
    sl_base: float
    tp_base: float
    atr_regime_lookback: int
    atr_regime_pct: float
    atr_high_mult: float
    atr_low_mult: float
    max_hold_bars: int


@dataclass
class Trade:
    param_id: str
    side: str
    label: str
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


def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_dt(raw: str) -> datetime:
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
    ):
        try:
            return _as_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {raw!r}")


def _spec_for_symbol(symbol: str) -> ContractSpec:
    sym = symbol.upper()
    if sym in {"MGC", "MGC1!"}:
        return ContractSpec("MGC", point_value=10.0, tick_size=0.1, round_turn_cost=1.50)
    if sym in {"GC", "GC1!"}:
        return ContractSpec("GC", point_value=100.0, tick_size=0.1, round_turn_cost=3.80)
    if sym in {"ZL", "ZL=F"}:
        # Soybean Oil futures quote cents per pound. Contract size is 60,000 lb,
        # so a 1.00 price move is about $600 and the 0.01 tick is about $6.
        return ContractSpec("ZL", point_value=600.0, tick_size=0.01, round_turn_cost=3.80)
    if sym == "MNQ":
        return ContractSpec("MNQ", point_value=2.0, tick_size=0.25, round_turn_cost=1.24)
    if sym == "NQ":
        return ContractSpec("NQ", point_value=20.0, tick_size=0.25, round_turn_cost=3.80)
    if sym == "MES":
        return ContractSpec("MES", point_value=5.0, tick_size=0.25, round_turn_cost=1.24)
    if sym == "ES":
        return ContractSpec("ES", point_value=50.0, tick_size=0.25, round_turn_cost=3.80)
    return ContractSpec(sym, point_value=1.0, tick_size=0.01, round_turn_cost=0.0)


def _round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(price / tick) * tick


def _floor_time(ts: datetime, minutes: int) -> datetime:
    ts = _as_utc(ts)
    minute = (ts.minute // minutes) * minutes
    return ts.replace(minute=minute, second=0, microsecond=0)


def _from_candles(candles: Iterable[Candle]) -> list[Bar]:
    return [
        Bar(
            timestamp=_as_utc(c.timestamp),
            open=float(c.open),
            high=float(c.high),
            low=float(c.low),
            close=float(c.close),
            volume=float(c.volume or 0),
        )
        for c in candles
    ]


def _aggregate_bars(bars: list[Bar], minutes: int) -> list[Bar]:
    if minutes <= 1:
        return sorted(bars, key=lambda b: b.timestamp)
    out: list[Bar] = []
    bucket_ts: datetime | None = None
    bucket: list[Bar] = []
    for bar in sorted(bars, key=lambda b: b.timestamp):
        ts = _floor_time(bar.timestamp, minutes)
        if bucket_ts is None:
            bucket_ts = ts
        if ts != bucket_ts:
            out.append(_collapse_bucket(bucket_ts, bucket))
            bucket_ts = ts
            bucket = []
        bucket.append(bar)
    if bucket_ts is not None and bucket:
        out.append(_collapse_bucket(bucket_ts, bucket))
    return out


def _collapse_bucket(ts: datetime, bucket: list[Bar]) -> Bar:
    return Bar(
        timestamp=ts,
        open=bucket[0].open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
        volume=sum(b.volume for b in bucket),
    )


def _load_csv(path: Path) -> list[Bar]:
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
        bars = []
        for row in reader:
            bars.append(
                Bar(
                    timestamp=_parse_dt(row[ts_key]),
                    open=float(row[open_key]),
                    high=float(row[high_key]),
                    low=float(row[low_key]),
                    close=float(row[close_key]),
                    volume=float(row[volume_key]) if volume_key else 0.0,
                )
            )
    return sorted(bars, key=lambda b: b.timestamp)


def load_bars(args: argparse.Namespace) -> tuple[list[Bar], str]:
    if args.csv:
        source = Path(args.csv)
        bars = _load_csv(source)
        if args.csv_interval and args.csv_interval != args.interval:
            if args.csv_interval > args.interval:
                raise SystemExit("Cannot downsample CSV interval to a smaller interval.")
            bars = _aggregate_bars(bars, args.interval)
        return bars, str(source)

    candles = candle_store.load(args.symbol.upper(), 1)
    if not candles:
        store_dir = ROOT / "data" / "store"
        available = sorted(p.name.split("_accumulated_")[0] for p in store_dir.glob("*_accumulated_1m.pkl"))
        raise SystemExit(
            f"No candle store for {args.symbol}. Available stores: {available}. "
            "Use --csv for MGC data such as mgc_5m_2024_2026_merged.csv."
        )
    bars = _aggregate_bars(_from_candles(candles), args.interval)
    return bars, f"data/store/{args.symbol.upper()}_accumulated_1m.pkl"


def _slice_dates(
    bars: list[Bar],
    start: str | None,
    end: str | None,
) -> list[Bar]:
    start_dt = _parse_dt(start) if start else None
    end_dt = _parse_dt(end) if end else None
    out = []
    for bar in bars:
        if start_dt and bar.timestamp < start_dt:
            continue
        if end_dt and bar.timestamp > end_dt:
            continue
        out.append(bar)
    return out


def _atr(bars: list[Bar], length: int) -> list[float | None]:
    values: list[float | None] = [None] * len(bars)
    trs: list[float] = []
    prev_close: float | None = None
    for i, bar in enumerate(bars):
        if prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        trs.append(tr)
        prev_close = bar.close
        if i + 1 >= length:
            values[i] = sum(trs[i + 1 - length : i + 1]) / length
    return values


def _supertrend_dir(bars: list[Bar], atr: list[float | None], mult: float) -> tuple[list[int], list[float | None]]:
    n = len(bars)
    direction = [0] * n
    trail: list[float | None] = [None] * n
    final_upper: list[float | None] = [None] * n
    final_lower: list[float | None] = [None] * n

    for i, bar in enumerate(bars):
        atr_i = atr[i]
        if atr_i is None:
            continue
        hl2 = (bar.high + bar.low) / 2.0
        basic_upper = hl2 + mult * atr_i
        basic_lower = hl2 - mult * atr_i
        if i == 0 or final_upper[i - 1] is None or final_lower[i - 1] is None:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            direction[i] = 1
            trail[i] = final_lower[i]
            continue

        prev_upper = final_upper[i - 1]
        prev_lower = final_lower[i - 1]
        prev_close = bars[i - 1].close
        final_upper[i] = basic_upper if (basic_upper < prev_upper or prev_close > prev_upper) else prev_upper
        final_lower[i] = basic_lower if (basic_lower > prev_lower or prev_close < prev_lower) else prev_lower

        if bar.close > prev_upper:
            direction[i] = 1
        elif bar.close < prev_lower:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1] or 1
        trail[i] = final_lower[i] if direction[i] == 1 else final_upper[i]
    return direction, trail


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * pct))
    idx = max(0, min(idx, len(ordered) - 1))
    return ordered[idx]


def _atr_multiplier(
    atr: list[float | None],
    i: int,
    lookback: int,
    pct: float,
    high_mult: float,
    low_mult: float,
) -> float:
    start = max(0, i - lookback)
    window = [x for x in atr[start:i] if x is not None and x > 0]
    if len(window) < max(20, min(lookback, 20)):
        return 1.0
    threshold = _percentile(window, pct)
    current = atr[i]
    if current is None:
        return 1.0
    return high_mult if current >= threshold else low_mult


def _label(side: str, fib: float) -> str:
    prefix = "S" if side == "short" else "L"
    return f"{prefix}-{fib:.3f}".rstrip("0").rstrip(".")


def _exit_label(side: str, fib: float) -> str:
    prefix = "XS" if side == "short" else "XL"
    return f"{prefix}-{fib:.3f}".rstrip("0").rstrip(".")


def _entry_signal(
    bars: list[Bar],
    direction: list[int],
    i: int,
    params: Params,
) -> tuple[str, float] | None:
    prev_start = i - params.swing_len
    if prev_start < 1:
        return None
    prev = bars[prev_start:i]
    swing_high = max(b.high for b in prev)
    swing_low = min(b.low for b in prev)
    width = swing_high - swing_low
    if width <= 0:
        return None

    bar = bars[i]
    prev_close = bars[i - 1].close
    sides = ["short", "long"] if params.side == "both" else [params.side]

    for side in sides:
        if side == "short":
            if direction[i - 1] != -1:
                continue
            level = swing_low + params.fib * width
            touched = bar.high >= level
            rejected = bar.close <= level
            crossed = prev_close > level and bar.close <= level
            if params.entry_mode == "pullback" and touched and rejected:
                return side, level
            if params.entry_mode == "cross_back" and touched and crossed:
                return side, level
        else:
            if direction[i - 1] != 1:
                continue
            level = swing_high - params.fib * width
            touched = bar.low <= level
            rejected = bar.close >= level
            crossed = prev_close < level and bar.close >= level
            if params.entry_mode == "pullback" and touched and rejected:
                return side, level
            if params.entry_mode == "cross_back" and touched and crossed:
                return side, level
    return None


def _hit_exit(side: str, bar: Bar, sl: float, tp: float) -> tuple[float, str] | None:
    if side == "short":
        hit_sl = bar.high >= sl
        hit_tp = bar.low <= tp
        if hit_sl and hit_tp:
            return sl, "sl_same_bar"
        if hit_sl:
            return sl, "sl"
        if hit_tp:
            return tp, "tp"
    else:
        hit_sl = bar.low <= sl
        hit_tp = bar.high >= tp
        if hit_sl and hit_tp:
            return sl, "sl_same_bar"
        if hit_sl:
            return sl, "sl"
        if hit_tp:
            return tp, "tp"
    return None


def run_backtest(
    bars: list[Bar],
    params: Params,
    spec: ContractSpec,
    param_id: str,
) -> list[Trade]:
    if len(bars) < max(params.atr_len, params.swing_len) + 5:
        return []
    atr = _atr(bars, params.atr_len)
    direction, _trail = _supertrend_dir(bars, atr, params.st_mult)
    trades: list[Trade] = []
    open_pos: dict | None = None
    start_i = max(params.atr_len, params.swing_len, params.atr_regime_lookback // 2)

    for i in range(start_i, len(bars)):
        bar = bars[i]
        if open_pos is not None:
            exit_hit = _hit_exit(open_pos["side"], bar, open_pos["sl"], open_pos["tp"])
            hold_bars = i - open_pos["entry_i"]
            if exit_hit is None and hold_bars >= params.max_hold_bars:
                exit_hit = (bar.close, "time")
            if exit_hit is not None:
                exit_price, exit_reason = exit_hit
                side_mult = -1.0 if open_pos["side"] == "short" else 1.0
                gross = (exit_price - open_pos["entry"]) * side_mult * spec.point_value
                pnl = gross - spec.round_turn_cost
                trades.append(
                    Trade(
                        param_id=param_id,
                        side=open_pos["side"],
                        label=open_pos["label"],
                        entry_time=open_pos["entry_time"],
                        exit_time=bar.timestamp,
                        entry=open_pos["entry"],
                        exit=exit_price,
                        sl=open_pos["sl"],
                        tp=open_pos["tp"],
                        pnl=pnl,
                        gross_pnl=gross,
                        exit_reason=exit_reason,
                        hold_bars=hold_bars,
                    )
                )
                open_pos = None
            continue

        signal = _entry_signal(bars, direction, i, params)
        if signal is None:
            continue
        side, _level = signal
        entry = _round_to_tick(bar.close, spec.tick_size)
        regime_mult = _atr_multiplier(
            atr,
            i,
            params.atr_regime_lookback,
            params.atr_regime_pct,
            params.atr_high_mult,
            params.atr_low_mult,
        )
        sl_points = params.sl_base
        tp_points = params.tp_base
        if params.regime_mode == "atr_sl_fixed_tp":
            sl_points *= regime_mult
        elif params.regime_mode == "fixed_sl_atr_tp":
            tp_points *= regime_mult

        if side == "short":
            sl = _round_to_tick(entry + sl_points, spec.tick_size)
            tp = _round_to_tick(entry - tp_points, spec.tick_size)
        else:
            sl = _round_to_tick(entry - sl_points, spec.tick_size)
            tp = _round_to_tick(entry + tp_points, spec.tick_size)
        if sl == entry or tp == entry:
            continue

        open_pos = {
            "side": side,
            "label": _label(side, params.fib),
            "entry_i": i,
            "entry_time": bar.timestamp,
            "entry": entry,
            "sl": sl,
            "tp": tp,
        }

    if open_pos is not None:
        last = bars[-1]
        side_mult = -1.0 if open_pos["side"] == "short" else 1.0
        gross = (last.close - open_pos["entry"]) * side_mult * spec.point_value
        trades.append(
            Trade(
                param_id=param_id,
                side=open_pos["side"],
                label=open_pos["label"],
                entry_time=open_pos["entry_time"],
                exit_time=last.timestamp,
                entry=open_pos["entry"],
                exit=last.close,
                sl=open_pos["sl"],
                tp=open_pos["tp"],
                pnl=gross - spec.round_turn_cost,
                gross_pnl=gross,
                exit_reason="final_flat",
                hold_bars=len(bars) - 1 - open_pos["entry_i"],
            )
        )
    return trades


def metrics(trades: list[Trade]) -> dict[str, float]:
    pnl = [t.pnl for t in trades]
    total = sum(pnl)
    gains = sum(x for x in pnl if x > 0)
    losses = -sum(x for x in pnl if x < 0)
    wins = sum(1 for x in pnl if x > 0)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in sorted(trades, key=lambda t: t.exit_time):
        equity += trade.pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": float(len(trades)),
        "pnl": total,
        "gross_gain": gains,
        "gross_loss": losses,
        "pf": gains / losses if losses > 0 else (999.0 if gains > 0 else 0.0),
        "max_dd": max_dd,
        "expect": total / len(trades) if trades else 0.0,
        "win_rate": wins / len(trades) if trades else 0.0,
        "avg_hold": statistics.mean([t.hold_bars for t in trades]) if trades else 0.0,
    }


def param_grid(args: argparse.Namespace) -> list[Params]:
    atr_lens = [10, 14] if not args.full_grid else [10, 14, 20]
    st_mults = [1.5, 2.0] if not args.full_grid else [1.5, 2.0, 2.5, 3.0]
    swing_lens = [12, 24] if not args.full_grid else [12, 18, 24, 36]
    fibs = [0.382, 0.5]
    entry_modes = ["pullback"] if not args.full_grid else ["pullback", "cross_back"]
    sl_bases = [8.0, 10.0, 12.0]
    tp_bases = [16.0, 20.0, 24.0]
    regime_modes = ["atr_sl_fixed_tp", "fixed_sl_atr_tp"]
    high_mults = [1.25, 1.5]
    params = []
    for atr_len in atr_lens:
        for st_mult in st_mults:
            for swing_len in swing_lens:
                for fib in fibs:
                    for entry_mode in entry_modes:
                        for sl_base in sl_bases:
                            for tp_base in tp_bases:
                                for regime_mode in regime_modes:
                                    for high_mult in high_mults:
                                        params.append(
                                            Params(
                                                atr_len=atr_len,
                                                st_mult=st_mult,
                                                swing_len=swing_len,
                                                fib=fib,
                                                entry_mode=entry_mode,
                                                side=args.side,
                                                regime_mode=regime_mode,
                                                sl_base=sl_base,
                                                tp_base=tp_base,
                                                atr_regime_lookback=args.atr_regime_lookback,
                                                atr_regime_pct=args.atr_regime_pct,
                                                atr_high_mult=high_mult,
                                                atr_low_mult=1.0,
                                                max_hold_bars=args.max_hold_bars,
                                            )
                                        )
    return params


def _split_is_oos(
    bars: list[Bar],
    split_frac: float,
    is_end: str | None,
) -> tuple[list[Bar], list[Bar], datetime]:
    if is_end:
        split_dt = _parse_dt(is_end)
        return [b for b in bars if b.timestamp <= split_dt], [b for b in bars if b.timestamp > split_dt], split_dt
    idx = max(1, min(len(bars) - 1, int(len(bars) * split_frac)))
    return bars[:idx], bars[idx:], bars[idx].timestamp


def _row(param_id: str, params: Params, is_metrics: dict, oos_metrics: dict) -> dict:
    row = {"param_id": param_id}
    row.update(asdict(params))
    for prefix, data in (("is", is_metrics), ("oos", oos_metrics)):
        for key, value in data.items():
            row[f"{prefix}_{key}"] = value
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
        for row in rows:
            writer.writerow(row)


def _trade_rows(trades: list[Trade], params: Params) -> list[dict]:
    rows = []
    for t in trades:
        row = asdict(t)
        row["entry_time"] = t.entry_time.isoformat()
        row["exit_time"] = t.exit_time.isoformat()
        row["exit_label"] = _exit_label(t.side, params.fib)
        rows.append(row)
    return rows


def _fmt_metrics(prefix: str, data: dict[str, float]) -> str:
    return (
        f"{prefix}: trades={data['trades']:.0f} pnl={data['pnl']:.0f} "
        f"pf={data['pf']:.2f} dd={data['max_dd']:.0f} "
        f"expect={data['expect']:.1f} win={data['win_rate']:.1%}"
    )


def _write_readme(
    path: Path,
    args: argparse.Namespace,
    source: str,
    spec: ContractSpec,
    bars: list[Bar],
    is_bars: list[Bar],
    oos_bars: list[Bar],
    split_dt: datetime,
    best: dict | None,
) -> None:
    lines = [
        "# Eddie screenshot replica research",
        "",
        "This is a research-only approximation, not the original private Pine.",
        "",
        "Visible clues translated into rules:",
        "- MGC 5m style data.",
        "- ATR stair-step regime filter, implemented as SuperTrend-like bands.",
        "- Fib-style entries labelled S-0.382/S-0.5 or mirrored long labels.",
        "- Optimizer compares fixed TP with ATR-regime SL against fixed SL with ATR-regime TP.",
        "- Chronological IS/OOS validation.",
        "",
        f"source: {source}",
        f"symbol: {spec.symbol}",
        f"bars: {len(bars)} {bars[0].timestamp.isoformat()} -> {bars[-1].timestamp.isoformat()}",
        f"IS: {len(is_bars)} bars through {split_dt.isoformat()}",
        f"OOS: {len(oos_bars)} bars after {split_dt.isoformat()}",
        "",
        "Output files:",
        "- grid_results.csv: all tested variants.",
        "- is_ranked_all.csv: all variants sorted by IS PF.",
        "- validated_top.csv: variants passing IS/OOS filters.",
        "- best_trades.csv: trade list for the best validated variant, or best ranked fallback.",
        "",
        "Run examples:",
        "python scripts/eddie_mgc_strategy_research.py --symbol MNQ",
        "python scripts/eddie_mgc_strategy_research.py --symbol MGC --csv data/mgc_5m_2024_2026_merged.csv --csv-interval 5",
    ]
    if best:
        lines.extend(
            [
                "",
                "Best selected row:",
                ", ".join(f"{k}={v}" for k, v in best.items() if k in {
                    "param_id",
                    "atr_len",
                    "st_mult",
                    "swing_len",
                    "fib",
                    "entry_mode",
                    "regime_mode",
                    "sl_base",
                    "tp_base",
                    "atr_high_mult",
                    "is_trades",
                    "is_pnl",
                    "is_pf",
                    "is_max_dd",
                    "oos_trades",
                    "oos_pnl",
                    "oos_pf",
                    "oos_max_dd",
                }),
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="MNQ", help="Store symbol or CSV symbol label.")
    parser.add_argument("--csv", default="", help="Optional OHLCV CSV, e.g. mgc_5m_2024_2026_merged.csv.")
    parser.add_argument("--csv-interval", type=int, default=5, help="CSV bar interval in minutes.")
    parser.add_argument("--interval", type=int, default=5, help="Research interval in minutes.")
    parser.add_argument("--side", choices=["short", "long", "both"], default="short")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--is-end", default="", help="Optional IS end timestamp/date. Default uses split fraction.")
    parser.add_argument("--split-frac", type=float, default=0.70)
    parser.add_argument("--max-hold-bars", type=int, default=24)
    parser.add_argument("--atr-regime-lookback", type=int, default=96)
    parser.add_argument("--atr-regime-pct", type=float, default=0.70)
    parser.add_argument("--min-is-trades", type=int, default=30)
    parser.add_argument("--min-oos-trades", type=int, default=15)
    parser.add_argument("--min-is-pf", type=float, default=1.20)
    parser.add_argument("--min-oos-pf", type=float, default=1.10)
    parser.add_argument("--full-grid", action="store_true")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    bars, source = load_bars(args)
    bars = _slice_dates(bars, args.start or None, args.end or None)
    if len(bars) < 500:
        raise SystemExit(f"Not enough bars after filtering: {len(bars)}")

    spec = _spec_for_symbol(args.symbol)
    is_bars, oos_bars, split_dt = _split_is_oos(bars, args.split_frac, args.is_end or None)
    if len(is_bars) < 200 or len(oos_bars) < 100:
        raise SystemExit(f"Bad IS/OOS split: IS={len(is_bars)} OOS={len(oos_bars)}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    grid = param_grid(args)
    print(
        f"source={source} symbol={spec.symbol} bars={len(bars)} "
        f"span={bars[0].timestamp} -> {bars[-1].timestamp}"
    )
    print(f"IS={len(is_bars)} through {split_dt}; OOS={len(oos_bars)}; variants={len(grid)}")

    rows: list[dict] = []
    best_trades: list[Trade] = []
    best_params: Params | None = None
    for idx, params in enumerate(grid, start=1):
        param_id = f"R{idx:04d}"
        is_trades = run_backtest(is_bars, params, spec, param_id)
        oos_trades = run_backtest(oos_bars, params, spec, param_id)
        is_m = metrics(is_trades)
        oos_m = metrics(oos_trades)
        rows.append(_row(param_id, params, is_m, oos_m))
        if idx % 100 == 0 or idx == len(grid):
            print(f"tested {idx}/{len(grid)}")

    ranked = sorted(rows, key=lambda r: (float(r["is_pf"]), float(r["is_pnl"]), -float(r["is_max_dd"])), reverse=True)
    validated = [
        r
        for r in ranked
        if float(r["is_trades"]) >= args.min_is_trades
        and float(r["oos_trades"]) >= args.min_oos_trades
        and float(r["is_pf"]) >= args.min_is_pf
        and float(r["oos_pf"]) >= args.min_oos_pf
        and float(r["is_pnl"]) > 0
        and float(r["oos_pnl"]) > 0
    ]
    validated = sorted(
        validated,
        key=lambda r: (
            min(float(r["is_pf"]), float(r["oos_pf"])),
            float(r["oos_pnl"]),
            -float(r["oos_max_dd"]),
        ),
        reverse=True,
    )
    selected = validated[0] if validated else (ranked[0] if ranked else None)
    if selected:
        selected_params = Params(
            atr_len=int(selected["atr_len"]),
            st_mult=float(selected["st_mult"]),
            swing_len=int(selected["swing_len"]),
            fib=float(selected["fib"]),
            entry_mode=str(selected["entry_mode"]),
            side=str(selected["side"]),
            regime_mode=str(selected["regime_mode"]),
            sl_base=float(selected["sl_base"]),
            tp_base=float(selected["tp_base"]),
            atr_regime_lookback=int(selected["atr_regime_lookback"]),
            atr_regime_pct=float(selected["atr_regime_pct"]),
            atr_high_mult=float(selected["atr_high_mult"]),
            atr_low_mult=float(selected["atr_low_mult"]),
            max_hold_bars=int(selected["max_hold_bars"]),
        )
        best_params = selected_params
        best_trades = run_backtest(oos_bars, selected_params, spec, str(selected["param_id"]))

    _write_csv(out_dir / "grid_results.csv", rows)
    _write_csv(out_dir / "is_ranked_all.csv", ranked)
    _write_csv(out_dir / "validated_top.csv", validated)
    _write_csv(out_dir / "best_trades.csv", _trade_rows(best_trades, best_params) if best_params else [])
    _write_readme(
        out_dir / "README_optimizer.md",
        args,
        source,
        spec,
        bars,
        is_bars,
        oos_bars,
        split_dt,
        selected,
    )

    print(f"\noutputs={out_dir}")
    print(f"validated={len(validated)}")
    if selected:
        print("selected:")
        print(
            f"{selected['param_id']} "
            f"atr={selected['atr_len']} st={selected['st_mult']} swing={selected['swing_len']} "
            f"fib={selected['fib']} mode={selected['entry_mode']} regime={selected['regime_mode']} "
            f"sl={selected['sl_base']} tp={selected['tp_base']} atr_mult={selected['atr_high_mult']}"
        )
        print(
            _fmt_metrics(
                "IS",
                {
                    "trades": float(selected["is_trades"]),
                    "pnl": float(selected["is_pnl"]),
                    "pf": float(selected["is_pf"]),
                    "max_dd": float(selected["is_max_dd"]),
                    "expect": float(selected["is_expect"]),
                    "win_rate": float(selected["is_win_rate"]),
                },
            )
        )
        print(
            _fmt_metrics(
                "OOS",
                {
                    "trades": float(selected["oos_trades"]),
                    "pnl": float(selected["oos_pnl"]),
                    "pf": float(selected["oos_pf"]),
                    "max_dd": float(selected["oos_max_dd"]),
                    "expect": float(selected["oos_expect"]),
                    "win_rate": float(selected["oos_win_rate"]),
                },
            )
        )
    else:
        print("selected=None")


if __name__ == "__main__":
    main()
