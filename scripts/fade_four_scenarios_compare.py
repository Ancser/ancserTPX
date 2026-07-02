"""Research script: independent previous-day VA scenario comparison.

Runs each scenario alone so win rate and PnL are not distorted by another leg
using the same day's trade slot or occupying the single open position:
  - brkLong   : close crosses above previous VAH, market buy
  - brkShort  : close crosses below previous VAL, market sell
  - fadeLong  : inside VA, buy limit at previous VAL, TP previous POC
  - fadeShort : inside VA, sell limit at previous VAH, TP previous POC

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.fade_four_scenarios_compare
"""
from __future__ import annotations

import copy
import logging
from collections import defaultdict
from pathlib import Path
from typing import Optional

from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    Candle,
    Direction,
    TradeSignal,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS,
    FABLE_702_FADE_X1,
    _build_strategy_params,
)
from scripts.futureman_study import FuturemanBacktest, FuturemanStrategy, SL_TICKS, TICK


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "machinelearning" / "fade_four_scenarios_compare.txt"


class SingleScenario(FuturemanStrategy):
    def __init__(self, scenario: str, rr: float = 2.0):
        super().__init__(rr=rr, fades=True, breakouts=True)
        self.scenario = scenario

    def evaluate(self, candle: Candle, zones, is_mature) -> Optional[TradeSignal]:
        lv = self.levels
        prev_close = self._prev_close
        self._prev_close = candle.close
        if not lv:
            return None
        poc, vah, val = float(lv["poc"]), float(lv["vah"]), float(lv["val"])
        slp = SL_TICKS * TICK
        d = lv["date"]

        def used(play: str) -> bool:
            return f"{d}:{play}" in self._used

        if self.scenario == "brkLong":
            if prev_close is not None and prev_close <= vah < candle.close and not used("brkLong"):
                e = candle.close
                return self._mk(candle, "brkLong", Direction.BUY,
                                e, e - slp, e + slp * self.rr, "market")
        elif self.scenario == "brkShort":
            if prev_close is not None and prev_close >= val > candle.close and not used("brkShort"):
                e = candle.close
                return self._mk(candle, "brkShort", Direction.SELL,
                                e, e + slp, e - slp * self.rr, "market")
        elif self.scenario == "fadeLong":
            if val < candle.close < vah and not used("fadeLong") and (poc - val) > 8 * TICK:
                return self._mk(candle, "fadeLong", Direction.BUY,
                                val, val - slp, poc, "limit")
        elif self.scenario == "fadeShort":
            if val < candle.close < vah and not used("fadeShort") and (vah - poc) > 8 * TICK:
                return self._mk(candle, "fadeShort", Direction.SELL,
                                vah, vah + slp, poc, "limit")
        return None


class ScenarioBacktest(FuturemanBacktest):
    def __init__(self, *args, scenario: str, rr: float = 2.0, **kw):
        super().__init__(*args, rr=rr, fades=True, breakouts=True, **kw)
        self.trend_follow = SingleScenario(scenario, rr=rr)
        self.play_win = defaultdict(int)

    def _execute_exit(self, candle, exit_price, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None and (t.pnl or 0.0) > 0:
            self.play_win[self._cur_play] += 1


def _metrics(result, engine: ScenarioBacktest, play: str) -> dict:
    m = result.metrics
    return {
        "trades": int(m.total_trades),
        "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl),
        "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor),
        "expectancy": float(m.expectancy),
        "wins": int(engine.play_win.get(play, 0)),
    }


def _row(tag: str, r: dict) -> str:
    return (
        f"{tag:<10} {r['trades']:>5} {r['wins']:>4} {100*r['win_rate']:>6.1f}% "
        f"{r['pnl']:>+10.1f} {r['max_dd']:>8.1f} {r['pf']:>6.2f} "
        f"{r['expectancy']:>+8.2f}"
    )


def main() -> None:
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)

    preset = BUILTIN_PRESETS[FABLE_702_FADE_X1]
    cid = preset.get("contract_id")
    base = _build_strategy_params(preset, cid)
    base.strategy = "trend"
    base.tr_allowed_sessions = None
    base.one_trade_per_session_direction = False
    base.tr_one_trade_per_session = False
    base.full_tp_lock = 0
    base.tr_full_tp_lock = 0

    config = BacktestConfig(
        strategies=["trend"],
        initial_capital=50_000.0,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=0.80,
    )

    lines = [
        f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}",
        "",
        f"{'scenario':<10} {'n':>5} {'wins':>4} {'win%':>7} {'pnl':>10} "
        f"{'maxDD':>8} {'PF':>6} {'expect':>8}",
        "-" * 70,
    ]
    results = {}
    for scenario in ("brkLong", "brkShort", "fadeLong", "fadeShort"):
        p = copy.deepcopy(base)
        engine = ScenarioBacktest(
            config=config,
            strategy_params=p,
            zone_timeline=None,
            record_equity=False,
            scenario=scenario,
            rr=2.0,
        )
        result = engine.run(candles)
        r = _metrics(result, engine, scenario)
        results[scenario] = r
        lines.append(_row(scenario, r))

    fade_pnl = results["fadeLong"]["pnl"] + results["fadeShort"]["pnl"]
    brk_pnl = results["brkLong"]["pnl"] + results["brkShort"]["pnl"]
    fade_trades = results["fadeLong"]["trades"] + results["fadeShort"]["trades"]
    brk_trades = results["brkLong"]["trades"] + results["brkShort"]["trades"]
    lines.extend([
        "",
        f"fade total     trades={fade_trades} pnl={fade_pnl:+.1f}",
        f"breakout total trades={brk_trades} pnl={brk_pnl:+.1f}",
    ])

    text = "\n".join(lines) + "\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(text, flush=True)
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
