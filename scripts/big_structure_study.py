"""1.0.8 研究(僅腳本):大結構 zone + 更嚴格確認 —「少交易、少假突破」假設。

使用者假設:
  1. 5m zone 假突破太頻繁,實盤常被 hunt;更大的結構(4h / 8h / 整個 session)
     突破次數少、參與者看得到的 SL 群也少 → 是否更好?
  2. 0.15.0 版用的「entire session breakout」(SessionZoneDetector,一個 session
     一個 zone,邊界隨 session 發展)是不是比 5m clock-bucket 好?
  3. confirmation 蠟燭 3 → 5 → 10,更嚴格是否更好?
  4. less trades is better? 目標:1 MNQ,2.5 個月 maxDD ~700 內 PnL > 7k。

基準參數 = CLAUDE #1(單TF VA70 RR4 C3 SL80 Trail50L10 SesON ASIA)。
Phase 1: zone ∈ {5m, 4h, 8h, session} × confirm ∈ {3, 5, 10},RR=4。
Phase 2: 大結構 SL 較寬 → zone ∈ {4h, 8h, session} × RR ∈ {2, 3},confirm=5。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.big_structure_study
"""
from __future__ import annotations

import copy
import logging

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.strategy import consolidation as consol
from backend.strategy.consolidation import SessionZoneDetector
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0

# 研究用:臨時加 8h clock bucket(僅本 process,不動源碼)
consol.AREA_TIMEFRAME_MINUTES.setdefault("8h", 480)


class SessionZoneAdapter(SessionZoneDetector):
    """補 get_recent_zones() 讓 0.15.0 式 session zone 能插進現行回測引擎。"""

    def get_recent_zones(self):
        z = self.get_active_zone()
        return [z] if z is not None else []


class SessionZoneBacktest(BacktestEngine):
    """把 clock-bucket detector 換成整個-session zone(其餘引擎行為不變)。"""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.detector = SessionZoneAdapter(
            value_area_pct=float(self.config.value_area_pct),
            tick_size=self.TICK_SIZE,
        )


def _run(engine_cls, params, candles):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
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


def _row(tag, r):
    hit = " <= 目標" if (r["max_dd"] <= 750 and r["pnl"] >= 7000) else ""
    print(f"{tag:<30} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}{hit}", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]   # 單5m VA70 RR4 C3
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    base.value_area_pct = float(preset.get("value_area_pct", 0.70))

    header = (f"{'variant':<30} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")

    zones = [
        ("5m", BacktestEngine, "5m"),
        ("4h", BacktestEngine, "4h"),
        ("8h", BacktestEngine, "8h"),
        ("session", SessionZoneBacktest, "5m"),  # area_tf 無所謂,detector 被換掉
    ]

    print("\n== Phase 1: zone 結構 × confirm bars(VA70 RR4)==", flush=True)
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for zlabel, cls, area_tf in zones:
        for c in (3, 5, 10):
            p = copy.deepcopy(base)
            p.area_timeframe = area_tf
            p.method = "single"
            p.tf_combo = []
            p.breakout_confirm_bars = c
            r = _run(cls, p, candles)
            _row(f"{zlabel} C{c}", r)

    print("\n== Phase 2: 大結構 × 低 RR(confirm=5,VA70)==", flush=True)
    print(header, flush=True)
    print("-" * len(header), flush=True)
    for zlabel, cls, area_tf in zones[1:]:
        for rr in (2, 3):
            p = copy.deepcopy(base)
            p.area_timeframe = area_tf
            p.method = "single"
            p.tf_combo = []
            p.breakout_confirm_bars = 5
            p.rr_ratio = rr
            r = _run(cls, p, candles)
            _row(f"{zlabel} C5 RR{rr}", r)


if __name__ == "__main__":
    main()
