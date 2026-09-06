"""Replay Astra PI events with the current PI BEST exit rules.

Research-only script.  It deliberately does not change the production PI
strategy.  The default report uses the same $7 round-turn cost as the older
PI research scripts so the result is comparable with their PF figures.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import candle_store  # noqa: E402
from backend.strategy.session_filter import market_close_phase, MARKET_PHASE_FLATTEN  # noqa: E402

DATASET = Path(r"F:\ancserData\astra_2026\astra_event_dataset.csv")
OUT_ROOT = Path(r"F:\ancserData\astra_2026")
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}
RT_COST = {"MNQ": 7.0, "MES": 7.0}

PI_LONG = {"青π", "深蓝圈"}
PI_SHORT = {"粉π"}


def utc(v) -> datetime:
    t = pd.Timestamp(v).to_pydatetime()
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _load_bars(events: pd.DataFrame) -> dict[str, list]:
    out = {}
    for sym, group in events.groupby("future"):
        snap = candle_store.load_snapshot(str(sym), 1, use_cache=False)
        if not snap.bars:
            out[str(sym)] = []
            continue
        start = utc(group["entry_ts"].min()) - timedelta(minutes=5)
        # The PI long side has no time limit and is flattened at 15:45 ET;
        # one full day after the last signal is sufficient for this replay.
        end = utc(group["entry_ts"].max()) + timedelta(days=1)
        bars = candle_store.select_range(snap, start=start, end=end)
        out[str(sym)] = sorted(bars, key=lambda b: utc(b.timestamp))
    return out


def _at_or_after(bars: list, ts: datetime) -> int | None:
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if utc(bars[mid].timestamp) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(bars) else None


def _trade_date(ts: datetime) -> str:
    # Same CME/Topstep reset used by _ResearchBase (17:00 Chicago).
    from zoneinfo import ZoneInfo
    ct = ts.astimezone(ZoneInfo("America/Chicago"))
    if ct.hour >= 17:
        ct = ct + timedelta(days=1)
    return ct.strftime("%Y-%m-%d")


def _simulate(bars: list, i0: int, direction: int, width: float,
              sl_k: float, rr: float, hold_min: int) -> tuple[float, str, datetime]:
    """Return points, exit reason, exit timestamp.

    This follows scripts/pi_hypothesis_tests.py: enter at the event-matched
    candle close, inspect subsequent candles, flatten in the 15:45 ET window.
    """
    entry = float(bars[i0].close)
    sl = entry - direction * sl_k * width if sl_k > 0 else None
    tp = entry + direction * sl_k * rr * width if sl_k > 0 and rr > 0 else None
    entry_ts = utc(bars[i0].timestamp)
    deadline = entry_ts + timedelta(minutes=hold_min) if hold_min > 0 else None
    for j in range(i0 + 1, min(i0 + 6000, len(bars))):
        b = bars[j]
        ts = utc(b.timestamp)
        if sl is not None and ((float(b.low) <= sl) if direction > 0 else (float(b.high) >= sl)):
            return direction * (sl - entry), "SL", ts
        if tp is not None and ((float(b.high) >= tp) if direction > 0 else (float(b.low) <= tp)):
            return direction * (tp - entry), "TP", ts
        if deadline is not None and ts >= deadline:
            return direction * (float(b.close) - entry), "TIME", ts
        if market_close_phase(ts) == MARKET_PHASE_FLATTEN:
            return direction * (float(b.close) - entry), "FLAT", ts
    j = min(i0 + 5999, len(bars) - 1)
    return direction * (float(bars[j].close) - entry), "EOD", utc(bars[j].timestamp)


def _stats(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    vals = [float(r["usd"]) for r in rows]
    gross_win = sum(x for x in vals if x > 0)
    gross_loss = -sum(x for x in vals if x < 0)
    eq = peak = max_dd = 0.0
    for x in vals:
        eq += x
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    daily = defaultdict(float)
    for r in rows:
        daily[r["date"]] += float(r["usd"])
    return {
        "n": len(rows),
        "pnl": sum(vals),
        "pf": gross_win / gross_loss if gross_loss else float("inf"),
        "win_rate": sum(x > 0 for x in vals) / len(vals),
        "max_dd": max_dd,
        "worst_day": min(daily.values()) if daily else 0.0,
        "avg_trade": sum(vals) / len(vals),
        "gross_points": sum(float(r["points"]) for r in rows),
    }


def _fmt(s: dict | None) -> str:
    if not s:
        return "n=0"
    pf = "inf" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    return (f"n={s['n']} PnL=${s['pnl']:,.0f} PF={pf} "
            f"勝率={s['win_rate'] * 100:.1f}% MaxDD=${s['max_dd']:,.0f} "
            f"最差日=${s['worst_day']:,.0f} 平均=${s['avg_trade']:,.0f}")


def run(events: pd.DataFrame, bars: dict[str, list], *, mode: str,
        include_short: bool, all_symbols: bool) -> list[dict]:
    rows = events.copy()
    if not all_symbols:
        rows = rows[rows["future"] == "MNQ"]
    allowed = PI_LONG | (PI_SHORT if include_short else set())
    rows = rows[rows["kind"].isin(allowed)].sort_values("entry_ts")
    out: list[dict] = []
    open_until: dict[str, datetime] = {}
    day_count: defaultdict[str, int] = defaultdict(int)
    for rec in rows.to_dict("records"):
        sym = str(rec["future"])
        ts = utc(rec["entry_ts"])
        # Production PI is one-position-at-a-time per instrument. Signals
        # arriving while a position is open are not queued as new trades.
        if sym in open_until and ts < open_until[sym]:
            continue
        d = 1 if int(rec["direction"]) > 0 else -1
        if d < 0 and not include_short:
            continue
        td = _trade_date(ts)
        if day_count[f"{sym}:{td}"] >= 3:  # factor_max_trades_per_day
            continue
        width = float(rec["atr_blend"])
        if not width > 0:
            continue
        b = bars.get(sym) or []
        i0 = _at_or_after(b, ts)
        if i0 is None or i0 + 1 >= len(b):
            continue
        if mode == "long_best" or d > 0:
            sl_k, rr, hold = 4.0, 3.0, 0
        else:
            sl_k, rr, hold = 2.5, 0.0, 60
        points, why, exit_ts = _simulate(b, i0, d, width, sl_k, rr, hold)
        usd = points * POINT_VALUE[sym] - RT_COST[sym]
        out.append({
            "ts": ts.isoformat(), "date": ts.date().isoformat(), "future": sym,
            "kind": rec["kind"], "direction": d, "stars": int(rec.get("stars") or 0),
            "entry": float(b[i0].close), "exit_ts": exit_ts.isoformat(),
            "points": points, "usd": usd, "reason": why,
            "source": rec.get("source", ""), "message_id": rec.get("message_id", ""),
        })
        day_count[f"{sym}:{td}"] += 1
        open_until[sym] = exit_ts
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(DATASET))
    ap.add_argument("--all-symbols", action="store_true")
    ap.add_argument("--json-out", default=str(OUT_ROOT / "astra_pi_best_backtest.json"))
    args = ap.parse_args()
    frame = pd.read_csv(args.dataset, parse_dates=["ts", "entry_ts"])
    frame = frame[frame["atr_blend"].notna()].copy()
    bars = _load_bars(frame if args.all_symbols else frame[frame["future"] == "MNQ"])
    result = {}
    for name, include_short in (("current_pi_best_long_only", False),
                                ("pi_best_with_short", True)):
        trades = run(frame, bars, mode="long_best", include_short=include_short,
                     all_symbols=args.all_symbols)
        result[name] = {"stats": _stats(trades), "trades": trades}
    payload = {
        "dataset": str(Path(args.dataset)), "events": int(len(frame)),
        "all_symbols": bool(args.all_symbols),
        "rules": {
            "long": "青π/深藍圈; SL=4.0×completed ATR blend; TP=3R; no time exit",
            "short": "粉π only; SL=2.5×completed ATR blend; 60m time exit",
            "max_trades_per_instrument_trade_date": 3,
            "round_turn_cost_usd": RT_COST,
            "one_position_per_instrument": True,
        },
        "results": result,
    }
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Astra events={len(frame)} symbols={sorted(frame.future.unique())}")
    for name, value in result.items():
        print(f"{name}: {_fmt(value['stats'])}")
        by_side = {}
        for side, label in ((1, "long"), (-1, "short")):
            by_side[label] = _stats([t for t in value["trades"] if t["direction"] == side])
            print(f"  {label}: {_fmt(by_side[label])}")
        for stars in (0, 1, 2, 3):
            s = _stats([t for t in value["trades"] if t["stars"] == stars])
            if s:
                print(f"  stars={stars}: {_fmt(s)}")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
