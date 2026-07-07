"""Compare DAY ZONE OR15 fixed TP vs ladder-style exits.

This is a research-only script. It does not touch live engines, broker state,
orders, or persisted presets.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
from pathlib import Path
from typing import Iterable

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import BacktestConfig, Direction, StrategyParams


DEFAULT_PRESET = "07.06 DAY ZONE #1 OR15 PF2.30 S0"


def _params_from_preset(name: str, overrides: dict | None = None) -> StrategyParams:
    data = json.loads(Path("data/presets.json").read_text(encoding="utf-8"))
    raw = data.get("presets", {}).get(name)
    if not isinstance(raw, dict):
        raise SystemExit(f"Preset not found: {name}")
    fields = {f.name for f in dataclasses.fields(StrategyParams)}
    kwargs = {k: v for k, v in raw.items() if k in fields}
    kwargs.update(overrides or {})
    return StrategyParams(**kwargs)


class FadeTpLadderBacktest(BacktestEngine):
    """DAY ZONE keeps original TP, but also ratchets SL after +2R."""

    def _execute_entry(self, signal, candle):  # noqa: ANN001
        super()._execute_entry(signal, candle)
        if self.strategy_mode == "fade" and self._open_position:
            self._ladder_risk = abs(
                self._open_position.entry_price - self._open_position.sl_price
            )
            self._ladder_max_r = 0.0

    def _check_trailing_sl(self, candle):  # noqa: ANN001
        if self.strategy_mode == "fade":
            self._check_ladder_sl(candle)
            return
        super()._check_trailing_sl(candle)


class FadeNoTpLadderBacktest(BacktestEngine):
    """DAY ZONE removes fixed TP and exits only by ladder SL/flatten."""

    def _execute_entry(self, signal, candle):  # noqa: ANN001
        super()._execute_entry(signal, candle)
        if self.strategy_mode == "fade" and self._open_position:
            pos = self._open_position
            self._ladder_risk = abs(pos.entry_price - pos.sl_price)
            self._ladder_max_r = 0.0
            far = 1_000_000.0
            pos.tp_price = (
                pos.entry_price + far
                if pos.direction == Direction.BUY
                else pos.entry_price - far
            )

    def _check_trailing_sl(self, candle):  # noqa: ANN001
        if self.strategy_mode == "fade":
            self._check_ladder_sl(candle)
            return
        super()._check_trailing_sl(candle)


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
    reasons = {
        "tp": 0,
        "sl": 0,
        "trail_sl": 0,
        "flatten": 0,
    }
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


def _month_key(trade) -> str:  # noqa: ANN001
    ts = trade.entry_time or trade.exit_time
    return ts.strftime("%Y-%m")


def _print_result(name: str, trades: list) -> None:
    print(f"\n{name}")
    print("period,N,PnL,PF,DD,Expect,TP,SL,TRAIL,FLAT")
    rows = [("ALL", _stats(trades))]
    months = sorted({_month_key(t) for t in trades})
    for month in months:
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
    print(
        f"candles={len(candles)} span={candles[0].timestamp}"
        f" -> {candles[-1].timestamp}"
    )

    cases = [
        (
            "A base DAY #1 fixed TP",
            BacktestEngine,
            _params_from_preset(args.preset, {"tr_exit_mode": "tp"}),
        ),
        (
            "B current code DAY #1 tr_exit_mode=ladder",
            BacktestEngine,
            _params_from_preset(args.preset, {"tr_exit_mode": "ladder"}),
        ),
        (
            "C simulated DAY ladder-only / no fixed TP",
            FadeNoTpLadderBacktest,
            _params_from_preset(args.preset, {"tr_exit_mode": "ladder"}),
        ),
        (
            "D simulated DAY fixed TP + ladder protection",
            FadeTpLadderBacktest,
            _params_from_preset(args.preset, {"tr_exit_mode": "tp"}),
        ),
    ]

    results = []
    for name, engine_cls, params in cases:
        result = _run(engine_cls, params, candles)
        results.append((name, result.trades))
        _print_result(name, result.trades)

    a = results[0][1]
    b = results[1][1]
    same = len(a) == len(b) and all(
        (
            x.entry_time,
            x.entry_price,
            x.exit_time,
            x.exit_price,
            round(float(x.pnl or 0.0), 6),
            str(x.exit_reason),
        )
        == (
            y.entry_time,
            y.entry_price,
            y.exit_time,
            y.exit_price,
            round(float(y.pnl or 0.0), 6),
            str(y.exit_reason),
        )
        for x, y in zip(a, b)
    )
    print(f"\nA_equals_B_current_ladder_no_effect={same}")


if __name__ == "__main__":
    main()
