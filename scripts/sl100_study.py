"""1.0.8 研究(僅腳本):SL 改用 zone 100% 極值 vs 現行最低量節點。

現行 SL = POC↔進場邊界之間的最低量節點(fallback sl_ticks)。
提案   = 進場 limit 照舊掛在 preset 的 70/80% VAH/VAL,但 SL 放到
         zone 全區間極值:多單 SL=low_100(區間底)、空單 SL=high_100(區間頂)。
         突破失敗的定義從「跌回節點」變成「跌穿整個區間」。
         SL 變寬 → 另試 RR3(TP 不至於太遠)。

每個現行 CLAUDE preset 跑三種:
  A base    — 現行邏輯,原 RR(對照組)
  B SL100   — 100% 極值 SL,原 RR
  C SL100r3 — 100% 極值 SL,RR=3

Run:  PYTHONIOENCODING=utf-8 python -m scripts.sl100_study
"""
from __future__ import annotations

import copy
import logging

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.strategy.trend_follow import SessionTrendFollow
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, CLAUDE_701_PRESET_2,
    CLAUDE_701_PRESET_3, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0

PRESETS = [
    ("C#1 單5m VA70 RR4", CLAUDE_701_PRESET_1),
    ("C#2 ov30m1h VA70 RR6", CLAUDE_701_PRESET_2),
    ("C#3 ov5m30m VA80 RR6", CLAUDE_701_PRESET_3),
]


class SL100TrendFollow(SessionTrendFollow):
    """SL = zone 100% 極值(low_100/high_100);entry/TP 結構與父類相同。"""

    def _generate_signal(self, candle, zone, direction):
        sig = super()._generate_signal(candle, zone, direction)
        lo = getattr(zone, "low_100", None)
        hi = getattr(zone, "high_100", None)
        if lo is None or hi is None:
            return sig  # 合成 zone 缺極值 → 保留原節點 SL
        entry = sig.entry_price
        if direction == "up":
            sl = min(lo, entry - self.MIN_STOP_TICKS * self.TICK_SIZE)
            dist = entry - sl
            tp = entry + dist * self.RR_RATIO
        else:
            sl = max(hi, entry + self.MIN_STOP_TICKS * self.TICK_SIZE)
            dist = sl - entry
            tp = entry - dist * self.RR_RATIO
        sig.sl_price = sl
        sig.tp_price = tp
        sig.reason = (
            f"TREND {direction.upper()} | SL@100%range {sl:.2f} "
            f"TP 1:{self.RR_RATIO} ({tp:.2f})"
        )
        return sig


class SL100Backtest(BacktestEngine):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.trend_follow = SL100TrendFollow(params=self.strategy_params)


def _run(engine_cls, params, va, candles):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=va,
    )
    result = engine_cls(config=config, strategy_params=params,
                        zone_timeline=None, record_equity=False).run(candles)
    m = result.metrics
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    header = (f"{'variant':<34} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")

    for label, key in PRESETS:
        preset = BUILTIN_PRESETS[key]
        cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
        va = float(preset.get("value_area_pct", 0.80))
        base = _build_strategy_params(preset, cid)
        base.value_area_pct = va
        orig_rr = int(base.rr_ratio)

        print(f"\n=== {label}  (VA={int(va*100)} origRR={orig_rr}) ===", flush=True)
        print(header, flush=True)
        print("-" * len(header), flush=True)

        for tag, cls, rr in (
            ("A base 節點SL", BacktestEngine, orig_rr),
            ("B SL100 極值SL", SL100Backtest, orig_rr),
            ("C SL100 極值SL RR3", SL100Backtest, 3),
        ):
            p = copy.deepcopy(base)
            p.rr_ratio = int(rr)
            r = _run(cls, p, va, candles)
            print(f"{tag:<34} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
                  f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
                  f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}", flush=True)


if __name__ == "__main__":
    main()
