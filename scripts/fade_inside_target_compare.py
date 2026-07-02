"""Research script: compare previous-day inside FADE targets.

Compares consolidation-only FADE legs:
  - long  : buy limit at previous VAL
  - short : sell limit at previous VAH
Targets:
  - poc : previous-day POC
  - mid : midpoint of previous-day VAH/VAL

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.fade_inside_target_compare
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
from scripts.futureman_study import FuturemanBacktest, FuturemanStrategy, TICK


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "machinelearning" / "fade_inside_target_compare.txt"


class InsideFadeTarget(FuturemanStrategy):
    def __init__(self, *, side: str, target: str, sl_ticks: int = 80):
        super().__init__(rr=2.0, fades=True, breakouts=False)
        self.side = side
        self.target = target
        self.sl_ticks = int(sl_ticks)

    def _tp(self, poc: float, vah: float, val: float) -> float:
        if self.target == "mid":
            return (vah + val) / 2.0
        return poc

    def evaluate(self, candle: Candle, zones, is_mature) -> Optional[TradeSignal]:
        lv = self.levels
        self._prev_close = candle.close
        if not lv:
            return None
        poc, vah, val = float(lv["poc"]), float(lv["vah"]), float(lv["val"])
        d = lv["date"]
        slp = self.sl_ticks * TICK
        tp = self._tp(poc, vah, val)

        def used(play: str) -> bool:
            return f"{d}:{play}" in self._used

        if not (val < candle.close < vah):
            return None

        allow_short = self.side in ("both", "short")
        allow_long = self.side in ("both", "long")

        if allow_short and not used("fadeShort") and (vah - tp) > 8 * TICK:
            return self._mk(candle, "fadeShort", Direction.SELL,
                            vah, vah + slp, tp, "limit")
        if allow_long and not used("fadeLong") and (tp - val) > 8 * TICK:
            return self._mk(candle, "fadeLong", Direction.BUY,
                            val, val - slp, tp, "limit")
        return None


class TargetBacktest(FuturemanBacktest):
    def __init__(self, *args, fade_kw=None, **kw):
        super().__init__(*args, fades=True, breakouts=False, **kw)
        self.trend_follow = InsideFadeTarget(**(fade_kw or {}))
        self.play_win = defaultdict(int)

    def _execute_exit(self, candle, exit_price, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None and (t.pnl or 0.0) > 0:
            self.play_win[self._cur_play] += 1


def _metrics(result, engine: TargetBacktest) -> dict:
    m = result.metrics
    return {
        "trades": int(m.total_trades),
        "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl),
        "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor),
        "expectancy": float(m.expectancy),
        "fade_long_n": int(engine.play_n.get("fadeLong", 0)),
        "fade_long_w": int(engine.play_win.get("fadeLong", 0)),
        "fade_long_pnl": float(engine.play_pnl.get("fadeLong", 0.0)),
        "fade_short_n": int(engine.play_n.get("fadeShort", 0)),
        "fade_short_w": int(engine.play_win.get("fadeShort", 0)),
        "fade_short_pnl": float(engine.play_pnl.get("fadeShort", 0.0)),
    }


def _format_row(tag: str, r: dict) -> str:
    return (
        f"{tag:<18} {r['trades']:>5} {100*r['win_rate']:>6.1f}% "
        f"{r['pnl']:>+10.1f} {r['max_dd']:>8.1f} {r['pf']:>6.2f} "
        f"{r['expectancy']:>+8.2f} "
        f"L {r['fade_long_n']:>2}/{r['fade_long_w']:>2} {r['fade_long_pnl']:>+8.1f} "
        f"S {r['fade_short_n']:>2}/{r['fade_short_w']:>2} {r['fade_short_pnl']:>+8.1f}"
    )


def main() -> None:
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)

    preset = BUILTIN_PRESETS[FABLE_702_FADE_X1]
    cid = preset.get("contract_id")
    base = _build_strategy_params(preset, cid)
    # This script injects previous-day levels through FuturemanBacktest. Keep
    # the base engine on the generic trend path to avoid duplicate fade wiring.
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
        f"{'variant':<18} {'n':>5} {'win%':>7} {'pnl':>10} {'maxDD':>8} "
        f"{'PF':>6} {'expect':>8} {'legs':>30}",
        "-" * 105,
    ]

    results = {}
    for side in ("long", "short", "both"):
        for target in ("poc", "mid"):
            tag = f"{side}-{target}"
            p = copy.deepcopy(base)
            engine = TargetBacktest(
                config=config,
                strategy_params=p,
                zone_timeline=None,
                record_equity=False,
                fade_kw={"side": side, "target": target, "sl_ticks": 80},
            )
            result = engine.run(candles)
            r = _metrics(result, engine)
            results[tag] = r
            lines.append(_format_row(tag, r))

    best_pf = max(results.items(), key=lambda kv: (kv[1]["pf"], kv[1]["pnl"]))
    best_wr = max(results.items(), key=lambda kv: (kv[1]["win_rate"], kv[1]["pf"]))
    best_score = max(
        results.items(),
        key=lambda kv: (kv[1]["pnl"] / max(kv[1]["max_dd"], 100.0), kv[1]["pf"]),
    )
    lines.extend([
        "",
        f"best_pf    {best_pf[0]} PF={best_pf[1]['pf']:.2f} pnl={best_pf[1]['pnl']:+.1f}",
        f"best_win   {best_wr[0]} win={100*best_wr[1]['win_rate']:.1f}% PF={best_wr[1]['pf']:.2f}",
        f"best_score {best_score[0]} pnl/DD={best_score[1]['pnl']/max(best_score[1]['max_dd'], 100.0):.2f} "
        f"pnl={best_score[1]['pnl']:+.1f} maxDD={best_score[1]['max_dd']:.1f}",
    ])

    text = "\n".join(lines) + "\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(text, flush=True)
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
