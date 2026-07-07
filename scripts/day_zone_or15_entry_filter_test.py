"""Research OR15 fake-break entry definitions for DAY ZONE.

This script is read-only with respect to live trading. It does not modify
presets, start/stop servers, or place orders.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
from datetime import timezone
from pathlib import Path
from typing import Iterable

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    Direction,
    StrategyParams,
    StrategyType,
    TradeSignal,
)
from backend.strategy.fade import OpeningRangeFade


DEFAULT_PRESET = "07.06 DAY ZONE #1 OR15 PF2.30 S0"


def _params_from_preset(name: str) -> StrategyParams:
    data = json.loads(Path("data/presets.json").read_text(encoding="utf-8"))
    raw = data.get("presets", {}).get(name)
    if not isinstance(raw, dict):
        raise SystemExit(f"Preset not found: {name}")
    fields = {f.name for f in dataclasses.fields(StrategyParams)}
    kwargs = {k: v for k, v in raw.items() if k in fields}
    kwargs["fade_entry_mode"] = "or15"
    kwargs["tr_exit_mode"] = "tp"
    return StrategyParams(**kwargs)


def _inside(price: float, low: float, high: float) -> bool:
    return low <= price <= high


class StrictInsideOr15(OpeningRangeFade):
    """Require open and close inside OR after a sweep outside."""

    def _candidate(self, candle, orh: float, orl: float):
        open_inside = _inside(float(candle.open), orl, orh)
        close_inside = _inside(float(candle.close), orl, orh)
        if not open_inside or not close_inside:
            return None
        short = float(candle.high) > orh
        long = float(candle.low) < orl
        if short == long:
            return None
        return (-1, Direction.SELL, "ORH") if short else (+1, Direction.BUY, "ORL")

    @staticmethod
    def _aware_utc(ts):
        return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)

    def _signal(self, candle, lv, orh: float, orl: float, dsign: int, direction, side: str, entry: float):
        vah, val = float(lv["vah"]), float(lv["val"])
        rng = max(self.TICK_SIZE, vah - val)
        sl_dist = max(self.MIN_STOP_TICKS * self.TICK_SIZE, self.SL_FRAC * rng)
        tp_dist = max(self.MIN_TARGET_TICKS * self.TICK_SIZE, self.TP_FRAC_RNG * rng)
        sl = entry - dsign * sl_dist
        tp = entry + dsign * tp_dist
        key = f"{lv['date']}:or{'Short' if dsign < 0 else 'Long'}"
        if key in self._used:
            return None
        self._used.add(key)
        self._last_key = key
        self._state = "confirmed"

        ts_utc = self._aware_utc(candle.timestamp)
        or_start = ts_utc.replace(hour=13, minute=30, second=0, microsecond=0)
        or_end = ts_utc.replace(hour=13, minute=45, second=0, microsecond=0)
        reason = (
            f"OR15 strict inside {side} | OR {orl:.2f}~{orh:.2f} "
            f"entry {entry:.2f} -> TP {tp:.2f} SL {sl:.2f} | prevVA={rng:.2f}"
        )
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            zone_id=f"OR15:{lv['date']}",
            reason=reason,
            timestamp=candle.timestamp,
            breakout_range=rng,
            order_type="market",
            meta={
                "strategy_family": "fade",
                "mode": "or15_strict_inside",
                "side": side,
                "signal_reason": reason,
                "trade_tf": "15m",
                "labels": [f"or15-strict:{'short' if dsign < 0 else 'long'}"],
                "or_range": {
                    "tf": "or15",
                    "zone_id": f"OR15:{ts_utc.date().isoformat()}",
                    "formed_at": or_start.isoformat(),
                    "left_at": or_end.isoformat(),
                    "or_high": orh,
                    "or_low": orl,
                    "vah_80": orh,
                    "val_80": orl,
                    "high_100": orh,
                    "low_100": orl,
                    "break_side": side,
                },
                "primary_zone": {
                    "tf": "1d",
                    "zone_id": f"OR15:{lv['date']}",
                    "poc": float(lv["poc"]),
                    "vah_80": vah,
                    "val_80": val,
                },
            },
        )

    def evaluate(self, candle, zones=None, is_mature: bool = True):
        self._track_or(candle)
        lv = self._levels
        if not lv or self._state == "in_trade":
            return None
        if self._utc_minutes(candle.timestamp) < self.OR_END_MIN:
            return None
        if self._or_high is None or self._or_low is None:
            return None
        orh, orl = float(self._or_high), float(self._or_low)
        candidate = self._candidate(candle, orh, orl)
        if candidate is None:
            return None
        dsign, direction, side = candidate
        return self._signal(candle, lv, orh, orl, dsign, direction, side, float(candle.close))


class LiveSafeNextOpenOr15(StrictInsideOr15):
    """Strict signal candle, then enter next bar open only if still inside OR."""

    def __init__(self, params=None):
        super().__init__(params=params)
        self._pending_fake = None

    def set_levels(self, levels):
        super().set_levels(levels)
        self._pending_fake = None

    def reset(self):
        super().reset()
        self._pending_fake = None

    def notify_order_cancelled(self):
        super().notify_order_cancelled()
        self._pending_fake = None

    def evaluate(self, candle, zones=None, is_mature: bool = True):
        self._track_or(candle)
        lv = self._levels
        if not lv or self._state == "in_trade":
            return None
        if self._utc_minutes(candle.timestamp) < self.OR_END_MIN:
            return None
        if self._or_high is None or self._or_low is None:
            return None
        orh, orl = float(self._or_high), float(self._or_low)

        if self._pending_fake is not None:
            pending = self._pending_fake
            self._pending_fake = None
            if _inside(float(candle.open), orl, orh):
                return self._signal(
                    candle,
                    lv,
                    orh,
                    orl,
                    pending["dsign"],
                    pending["direction"],
                    pending["side"],
                    float(candle.open),
                )

        candidate = self._candidate(candle, orh, orl)
        if candidate is None:
            return None
        dsign, direction, side = candidate
        self._pending_fake = {"dsign": dsign, "direction": direction, "side": side}
        return None


class Or15VariantBacktest(BacktestEngine):
    variant = "current"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.strategy_mode != "fade":
            return
        if self.variant == "strict":
            self.trend_follow = StrictInsideOr15(params=self.strategy_params)
        elif self.variant == "live_safe":
            self.trend_follow = LiveSafeNextOpenOr15(params=self.strategy_params)
        self._pending_max_age = self.trend_follow.PENDING_TIMEOUT_CANDLES


class StrictBacktest(Or15VariantBacktest):
    variant = "strict"


class LiveSafeBacktest(Or15VariantBacktest):
    variant = "live_safe"


def _config(params: StrategyParams) -> BacktestConfig:
    symbol = "MNQ" if ".MNQ." in str(params.contract_id).upper() else "NQ"
    return BacktestConfig(symbol=symbol, initial_capital=100_000.0)


def _run(engine_cls: type[BacktestEngine], params: StrategyParams, candles: list):
    engine = engine_cls(
        config=_config(params),
        strategy_params=copy.deepcopy(params),
        record_equity=False,
    )
    return engine.run(candles)


def _max_dd(trades: Iterable) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in sorted(trades, key=lambda t: t.exit_time or t.entry_time):
        equity += float(trade.pnl or 0.0)
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _stats(trades: list) -> dict:
    trades = [t for t in trades if t.pnl is not None]
    pnl = sum(float(t.pnl) for t in trades)
    gain = sum(float(t.pnl) for t in trades if float(t.pnl) > 0)
    loss = -sum(float(t.pnl) for t in trades if float(t.pnl) < 0)
    pf = gain / loss if loss > 0 else (999.0 if gain > 0 else 0.0)
    reasons = {"tp": 0, "sl": 0, "trail_sl": 0, "flatten": 0}
    for trade in trades:
        reason = str(trade.exit_reason.value if trade.exit_reason else "").lower()
        if reason in reasons:
            reasons[reason] += 1
    return {
        "N": len(trades),
        "PnL": pnl,
        "PF": pf,
        "DD": _max_dd(trades),
        "Expect": pnl / len(trades) if trades else 0.0,
        "TP": reasons["tp"],
        "SL": reasons["sl"],
        "TRAIL": reasons["trail_sl"],
        "FLAT": reasons["flatten"],
    }


def _month_key(trade) -> str:
    ts = trade.entry_time or trade.exit_time
    return ts.strftime("%Y-%m")


def _print_result(name: str, trades: list) -> None:
    print(f"\n{name}")
    print("period,N,PnL,PF,DD,Expect,TP,SL,TRAIL,FLAT")
    rows = [("ALL", _stats(trades))]
    for month in sorted({_month_key(t) for t in trades}):
        rows.append((month, _stats([t for t in trades if _month_key(t) == month])))
    for period, s in rows:
        print(
            f"{period},{s['N']},{s['PnL']:.0f},{s['PF']:.2f},"
            f"{s['DD']:.0f},{s['Expect']:.1f},"
            f"{s['TP']},{s['SL']},{s['TRAIL']},{s['FLAT']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default=DEFAULT_PRESET)
    args = parser.parse_args()

    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    params = _params_from_preset(args.preset)
    print(
        f"preset={args.preset}\n"
        f"candles={len(candles)} span={candles[0].timestamp} -> {candles[-1].timestamp}"
    )

    cases = [
        ("A current loose OR15: high/low outside + close back over boundary", BacktestEngine),
        ("B strict inside: open inside + sweep outside + close inside", StrictBacktest),
        ("C live-safe: strict signal, enter next open only if still inside", LiveSafeBacktest),
    ]
    for name, engine_cls in cases:
        result = _run(engine_cls, params, candles)
        _print_result(name, result.trades)


if __name__ == "__main__":
    main()
