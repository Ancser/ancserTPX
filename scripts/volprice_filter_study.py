"""1.0.8 研究(僅腳本):無 L2 之下,用量價行為過濾盤整/假突破(機構腳印)。

全部過濾器只用 1m OHLCV、且在「進場訊號當下」可計算(無未來函數):

  F1 quietVol  — 突破 3 根量 / 前 10 根量 ≤ 1.25:只要「安靜突破」。
                 (先前研究:爆量突破 >1.5x 勝率最低 = 追高散戶+機構出貨)
  F2 pumpVol   — 同比 ≥ 1.25:傳統「放量突破」說法(預期更差,對照用)。
  F3 body      — 突破當根實體佔比 ≥ 0.5:長影線 = 吸收/拒絕,實體 = 推動。
  F4 er60      — Kaufman 效率比(60m 淨位移/路徑總長)≥ 0.25:
                 只在「趨勢時段」進場,盤整時段效率比接近 0。
  F5 stop2loss — 粗暴法:當個交易日虧 2 單後,今天不再進場。
                 (直接治「來來回回騙我把 FT2 浪費掉」)
  F6 combo     — F1 + F4 + F5 疊加。
  F7 attempt2  — 非常規:同日同方向必須先死過 1 單才准進第 2 次
                 (機構先掃損再發動;第一次突破送死,第二次才是真的)。

兩種進場風格各跑一輪:
  A) CLAUDE #1(5m clock-bucket VA70 RR4 C3)
  B) 0.15.5 session 生長 zone(同風控,area=session)

Run:  PYTHONIOENCODING=utf-8 python -m scripts.volprice_filter_study
"""
from __future__ import annotations

import copy
import logging
import statistics
from collections import deque

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0
BRK_N, PRE_N = 3, 10          # 突破量窗 / 突破前量窗
ER_N = 60                     # 效率比回看(分鐘)


class FilterBacktest(BacktestEngine):
    """在 trend_follow.evaluate 之後套量價過濾器;不改進場/出場邏輯本身。"""

    def __init__(self, *args, f_volmax=None, f_volmin=None, f_body=None,
                 f_er=None, f_daily_stop=None, f_second_attempt=False, **kw):
        super().__init__(*args, **kw)
        self.f_volmax = f_volmax
        self.f_volmin = f_volmin
        self.f_body = f_body
        self.f_er = f_er
        self.f_daily_stop = f_daily_stop
        self.f_second_attempt = f_second_attempt

        self._vols = deque(maxlen=BRK_N + PRE_N)
        self._closes = deque(maxlen=ER_N + 1)
        self._cur = None
        self._daily_losses = {}
        self._dir_losses = {}

        _orig = self.trend_follow.evaluate

        def gated(candle, zones, mature):
            sig = _orig(candle, zones, mature)
            if sig is None:
                return None
            return sig if self._pass(candle, sig) else None

        self.trend_follow.evaluate = gated

    def _process_candle(self, candle):
        self._vols.append(float(candle.volume or 0))
        self._closes.append(float(candle.close))
        self._cur = candle
        super()._process_candle(candle)

    def _execute_exit(self, candle, exit_price, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None and (t.pnl or 0.0) < 0:
            d = _topstep_trade_date(candle.timestamp)
            dirv = t.direction.value if hasattr(t.direction, "value") else str(t.direction)
            self._daily_losses[d] = self._daily_losses.get(d, 0) + 1
            k = (d, dirv)
            self._dir_losses[k] = self._dir_losses.get(k, 0) + 1

    # ── filters ──
    def _vol_ratio(self):
        if len(self._vols) < BRK_N + PRE_N:
            return None
        vs = list(self._vols)
        pre = statistics.mean(vs[:PRE_N])
        brk = statistics.mean(vs[PRE_N:])
        return (brk / pre) if pre > 0 else None

    def _body_ratio(self, candle):
        rng = candle.high - candle.low
        return abs(candle.close - candle.open) / rng if rng > 0 else 0.0

    def _er(self):
        if len(self._closes) < ER_N + 1:
            return None
        cs = list(self._closes)
        net = abs(cs[-1] - cs[0])
        path = sum(abs(cs[i] - cs[i - 1]) for i in range(1, len(cs)))
        return (net / path) if path > 0 else 0.0

    def _pass(self, candle, sig) -> bool:
        if self.f_daily_stop is not None:
            d = _topstep_trade_date(candle.timestamp)
            if self._daily_losses.get(d, 0) >= self.f_daily_stop:
                return False
        if self.f_second_attempt:
            d = _topstep_trade_date(candle.timestamp)
            dirv = "BUY" if sig.direction == Direction.BUY else "SELL"
            if self._dir_losses.get((d, dirv), 0) < 1:
                return False
        if self.f_volmax is not None or self.f_volmin is not None:
            r = self._vol_ratio()
            if r is None:
                return False
            if self.f_volmax is not None and r > self.f_volmax:
                return False
            if self.f_volmin is not None and r < self.f_volmin:
                return False
        if self.f_body is not None and self._body_ratio(candle) < self.f_body:
            return False
        if self.f_er is not None:
            er = self._er()
            if er is None or er < self.f_er:
                return False
        return True


def _run(params, candles, **filters):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    result = FilterBacktest(config=config, strategy_params=params,
                            zone_timeline=None, record_equity=False,
                            **filters).run(candles)
    m = result.metrics
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def _row(tag, r):
    print(f"{tag:<24} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}", flush=True)


VARIANTS = [
    ("base 無過濾", {}),
    ("F1 quietVol<=1.25", {"f_volmax": 1.25}),
    ("F2 pumpVol>=1.25", {"f_volmin": 1.25}),
    ("F3 body>=0.5", {"f_body": 0.5}),
    ("F4 er60>=0.25", {"f_er": 0.25}),
    ("F5 stop2loss", {"f_daily_stop": 2}),
    ("F6 F1+F4+F5", {"f_volmax": 1.25, "f_er": 0.25, "f_daily_stop": 2}),
    ("F7 attempt2", {"f_second_attempt": True}),
]


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    base.value_area_pct = float(preset.get("value_area_pct", 0.70))

    header = (f"{'variant':<24} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")

    for style, area in (("A CLAUDE#1 5m clock", "5m"), ("B 0.15.5 session", "session")):
        p0 = copy.deepcopy(base)
        p0.area_timeframe = area
        p0.method = "single"
        p0.tf_combo = []
        print(f"\n== {style}(VA70 RR4 C3)==", flush=True)
        print(header, flush=True)
        print("-" * len(header), flush=True)
        for tag, filters in VARIANTS:
            r = _run(copy.deepcopy(p0), candles, **filters)
            _row(tag, r)


if __name__ == "__main__":
    main()
