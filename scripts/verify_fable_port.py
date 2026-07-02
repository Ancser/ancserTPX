"""1.0.8 驗證:引擎原生 ladder/breaker/fade 與研究腳本結果對賬。

期望(研究腳本,同數據):
  ladder+stop4 : +5039.6 / DD 498.3 / 260 筆
  ladder 無斷路 : +8263.7 / DD 915.4 / 541 筆
  fadeA4 只多   : +1254.6 / DD 247.4 / 31 筆

Run:  PYTHONIOENCODING=utf-8 python -m scripts.verify_fable_port
"""
from __future__ import annotations

import copy
import logging

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)


def _run(params, candles):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    m = BacktestEngine(config=config, strategy_params=params,
                       zone_timeline=None, record_equity=False).run(candles).metrics
    return m


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")

    # 1) ladder + stop4(trend)
    p = _build_strategy_params(preset, cid)
    p.value_area_pct = 0.70
    p.tr_exit_mode = "ladder"
    p.tr_daily_loss_stop = 4
    m = _run(p, candles)
    print(f"ladder+stop4 : {m.total_trades}筆 pnl={m.total_pnl:+.1f} DD={m.max_drawdown:.1f}  (期望 260 / +5039.6 / 498.3)", flush=True)

    # 2) ladder 無斷路
    p2 = _build_strategy_params(preset, cid)
    p2.value_area_pct = 0.70
    p2.tr_exit_mode = "ladder"
    m = _run(p2, candles)
    print(f"ladder 無斷路: {m.total_trades}筆 pnl={m.total_pnl:+.1f} DD={m.max_drawdown:.1f}  (期望 541 / +8263.7 / 915.4)", flush=True)

    # 3) fade
    p3 = _build_strategy_params(preset, cid)
    p3.strategy = "fade"
    p3.value_area_pct = 0.80
    p3.tr_allowed_sessions = None
    p3.one_trade_per_session_direction = False
    p3.tr_one_trade_per_session = False
    p3.full_tp_lock = 0
    p3.tr_full_tp_lock = 0
    m = _run(p3, candles)
    print(f"fade 只多    : {m.total_trades}筆 pnl={m.total_pnl:+.1f} DD={m.max_drawdown:.1f}  (期望 31 / +1254.6 / 247.4)", flush=True)


if __name__ == "__main__":
    main()
