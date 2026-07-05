"""1.0.9 P2 研究(僅腳本):傳統指標過濾工廠 — RSI/KDJ/EMA/BB/ATR × ladder 基準。

使用者 C-1 提案的修正版:全部指標便宜地測一遍,但**只認 P1 接受標準**:
  wf 三段各正 + 樣本 ≥80 + 期望 >0(平原留給參數化掃描)。
先驗提醒:1.0.8 的 8 個量價過濾全滅;本輪目的是「測完歸檔」,不是信仰。

門(進場當下可算,無未來函數;方向條件式):
  base       — 無過濾(對照,FABLE#2 = VA70 ladder C3 S0)
  rsi_align  — BUY 需 RSI14≥55;SELL 需 ≤45(動量同向)
  rsi_notex  — BUY 需 RSI14≤70;SELL 需 ≥30(不追過熱)
  ema_align  — BUY 需 EMA20>EMA50;SELL 反之(趨勢同向)
  kdj_align  — BUY 需 %K>%D(14,3);SELL 反之
  bb_block   — |20期 z-score|>2.2 → 不進(不接極端延伸)
  atr_mid    — ATR14 z-score(vs 近3日)∈[-1,1] 才進(只做中等波動)

Run:  PYTHONIOENCODING=utf-8 python -m scripts.indicator_filter_study
"""
from __future__ import annotations

import copy
import logging
import statistics
from collections import defaultdict, deque
from datetime import date as _date

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.backtest.sweep import build_trend_zone_timeline
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, FABLE_702_PRESET_2, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0


class IndicatorGateBacktest(BacktestEngine):
    def __init__(self, *args, gate: str = "base", **kw):
        super().__init__(*args, **kw)
        self.gate = gate
        self._closes = deque(maxlen=260)
        self._trs = deque(maxlen=15)        # true ranges (ATR14)
        self._atrs = deque(maxlen=1380 * 3) # 近3日 ATR 分佈
        self._prev_close = None

        _orig = self.trend_follow.evaluate

        def gated(candle, zones, mature):
            sig = _orig(candle, zones, mature)
            if sig is None:
                return None
            return sig if self._pass(sig) else None

        self.trend_follow.evaluate = gated

    # ── indicators(更新於 evaluate 前:_process_candle 先入列)──
    def _process_candle(self, candle):
        c = float(candle.close)
        if self._prev_close is not None:
            tr = max(candle.high - candle.low,
                     abs(candle.high - self._prev_close),
                     abs(candle.low - self._prev_close))
            self._trs.append(tr)
            if len(self._trs) >= 14:
                self._atrs.append(statistics.mean(list(self._trs)[-14:]))
        self._prev_close = c
        self._closes.append(c)
        super()._process_candle(candle)

    def _rsi(self, n=14):
        cs = list(self._closes)
        if len(cs) < n + 1:
            return None
        gains = losses = 0.0
        for i in range(-n, 0):
            d = cs[i] - cs[i - 1]
            if d >= 0:
                gains += d
            else:
                losses -= d
        if losses == 0:
            return 100.0
        rs = gains / losses
        return 100 - 100 / (1 + rs)

    def _ema(self, n):
        cs = list(self._closes)
        if len(cs) < n:
            return None
        k = 2 / (n + 1)
        e = cs[-n]
        for v in cs[-n + 1:]:
            e = v * k + e * (1 - k)
        return e

    def _stoch_kd(self, n=14, d=3):
        cs = list(self._closes)
        if len(cs) < n + d:
            return None, None
        ks = []
        for off in range(d):
            win = cs[len(cs) - n - off: len(cs) - off]
            lo, hi = min(win), max(win)
            ks.append(50.0 if hi == lo else (win[-1] - lo) / (hi - lo) * 100)
        return ks[0], statistics.mean(ks)

    def _bb_z(self, n=20):
        cs = list(self._closes)
        if len(cs) < n:
            return None
        win = cs[-n:]
        sd = statistics.pstdev(win)
        return 0.0 if sd == 0 else (cs[-1] - statistics.mean(win)) / sd

    def _atr_z(self):
        if len(self._trs) < 14 or len(self._atrs) < 200:
            return None
        cur = statistics.mean(list(self._trs)[-14:])
        mu = statistics.mean(self._atrs)
        sd = statistics.pstdev(self._atrs)
        return 0.0 if sd == 0 else (cur - mu) / sd

    def _pass(self, sig) -> bool:
        g = self.gate
        buy = sig.direction == Direction.BUY
        if g == "base":
            return True
        if g == "rsi_align":
            r = self._rsi()
            return r is not None and ((buy and r >= 55) or (not buy and r <= 45))
        if g == "rsi_notex":
            r = self._rsi()
            return r is not None and ((buy and r <= 70) or (not buy and r >= 30))
        if g == "ema_align":
            e20, e50 = self._ema(20), self._ema(50)
            if e20 is None or e50 is None:
                return False
            return (e20 > e50) if buy else (e20 < e50)
        if g == "kdj_align":
            k, d = self._stoch_kd()
            if k is None:
                return False
            return (k > d) if buy else (k < d)
        if g == "bb_block":
            z = self._bb_z()
            return z is not None and abs(z) <= 2.2
        if g == "atr_mid":
            z = self._atr_z()
            return z is not None and -1.0 <= z <= 1.0
        return True


def _seg_pnls(trades):
    day = defaultdict(float)
    for t in trades:
        day[_topstep_trade_date(t.entry_time)] += t.pnl or 0.0
    if not day:
        return [0.0, 0.0, 0.0]
    keys = sorted(day)
    d0 = _date.fromisoformat(keys[0])
    span = max(1, (_date.fromisoformat(keys[-1]) - d0).days + 1)
    segs = [0.0, 0.0, 0.0]
    for k, v in day.items():
        segs[min(2, int((_date.fromisoformat(k) - d0).days * 3 / span))] += v
    return segs


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[FABLE_702_PRESET_2]        # VA70 ladder C3 S0
    base = _build_strategy_params(preset, preset.get("contract_id"))
    timeline = build_trend_zone_timeline(candles, "5m", float(base.value_area_pct))
    print("timeline built", flush=True)

    cfg = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(base.contract_id),
        commission_rt=get_commission_rt(base.contract_id),
        fees_rt=get_fees_rt(base.contract_id),
        value_area_pct=float(base.value_area_pct),
    )

    header = (f"{'gate':<12} {'trades':>6} {'win%':>6} {'pnl':>9} {'maxDD':>7} "
              f"{'PF':>5} {'expect':>7} {'seg1/2/3':>22} {'WF':>3} {'ACC':>4}")
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    for gate in ("base", "rsi_align", "rsi_notex", "ema_align",
                 "kdj_align", "bb_block", "atr_mid"):
        p = copy.deepcopy(base)
        res = IndicatorGateBacktest(config=cfg, strategy_params=p,
                                    zone_timeline=timeline, record_equity=False,
                                    gate=gate).run(candles)
        m = res.metrics
        segs = _seg_pnls(res.trades)
        wf = all(x > 0 for x in segs)
        acc = wf and m.total_trades >= 80 and m.expectancy > 0
        print(f"{gate:<12} {m.total_trades:>6} {100*m.win_rate:>5.1f}% "
              f"{m.total_pnl:>+9.0f} {m.max_drawdown:>7.0f} {m.profit_factor:>5.2f} "
              f"{m.expectancy:>+7.1f} "
              f"{'/'.join(f'{x:+.0f}' for x in segs):>22} "
              f"{'✓' if wf else '✗':>3} {'★' if acc else '—':>4}", flush=True)


if __name__ == "__main__":
    main()
