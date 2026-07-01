"""1.0.8 研究(僅腳本,不動實際 preset):原始 trend 用 70% vs 80% 價值區間,
所有 preset × 降低 RR 掃描。

value_area_pct 控制 zone 的 vah/val 邊界寬度:70% = 更窄的 VA,更早觸發突破、
進場更靠近 POC。RR 只改 tp = entry ± sl_dist*RR(SL 節點不變)。

Preset:
  #1 = 06.30 單 5m RR6 C2
  #2 = 06.26 單 5m RR4 C3
  #3 = 06.30 overlap 5m+30m(小TF)RR6 C3
  #4 = 06.30 overlap 30m+1h(小TF)RR7 C4

Run:  PYTHONIOENCODING=utf-8 python -m scripts.va_rr_study
"""
from __future__ import annotations

import copy
import logging

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, CODEX_630_PRESET_1, CODEX_626_PRESET_2,
    CODEX_630_PRESET_3, CODEX_630_PRESET_4, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0
VA_LEVELS = (0.80, 0.70)
RR_SWEEP = (1, 2, 3, 4, 6)

PRESETS = [
    ("#1 single5m", CODEX_630_PRESET_1),
    ("#2 single5m", CODEX_626_PRESET_2),
    ("#3 ov5m+30m", CODEX_630_PRESET_3),
    ("#4 ov30m+1h", CODEX_630_PRESET_4),
]


def _run(params, va, candles):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=va,
    )
    result = BacktestEngine(config=config, strategy_params=params,
                            zone_timeline=None, record_equity=False).run(candles)
    m = result.metrics
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def main():
    # Silence per-breakout / progress spam so output streams cleanly to file.
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    header = (f"{'preset':<13} {'VA':>4} {'RR':>3} {'trades':>6} {'win%':>7} "
              f"{'pnl':>11} {'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")

    for label, key in PRESETS:
        preset = BUILTIN_PRESETS[key]
        cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
        base = _build_strategy_params(preset, cid)
        orig_rr = int(base.rr_ratio)
        rr_list = sorted(set(RR_SWEEP) | {orig_rr})
        print(f"\n=== {label}  (orig RR={orig_rr}) ===", flush=True)
        print(header, flush=True)
        print("-" * len(header), flush=True)
        for va in VA_LEVELS:
            for rr in rr_list:
                p = copy.deepcopy(base)
                p.rr_ratio = int(rr)
                p.value_area_pct = va
                r = _run(p, va, candles)
                star = " *orig" if rr == orig_rr else ""
                print(f"{label:<13} {int(va*100):>4} {rr:>3} {r['trades']:>6} "
                      f"{100*r['win_rate']:>6.1f}% {r['pnl']:>+11.1f} "
                      f"{r['max_dd']:>9.1f} {r['pf']:>6.2f} {r['calmar']:>7.2f} "
                      f"{r['expectancy']:>+9.2f}{star}", flush=True)


if __name__ == "__main__":
    main()
