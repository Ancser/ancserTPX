"""1.0.8 研究(僅腳本):FUTUREMAN — 前一整日 VP 80% 價值區 playbook。

規則(使用者設計):
  每個 Topstep 交易日開盤,用「前一整個交易日」全部 1m K 線算 VP →
  POC / VAH(80%) / VAL(80%),當日四張常駐單:
    fadeLong  : buy  limit @ VAL(內側)  TP=POC          — VAL 上面做到 POC
    fadeShort : sell limit @ VAH(內側)  TP=POC          — VAH 下面做到 POC
    brkShort  : 收盤跌穿 VAL → 市價追空   TP=RR×SL(80t)  — VAL 下面掛單做空
    brkLong   : 收盤升穿 VAH → 市價追多   TP=RR×SL(80t)  — VAH 以上掛單做多
  每張單每天最多成交一次;SL 一律 80 tick(20pt);全時段(不限 ASIA),
  19:45 flatten 照舊。天然序列:碰 VAH 先 fade,fade 被打掉才輪到破位追。

變體:
  F1 RR2 + trail50   F2 RR3 + trail50   F3 ladder(破位腿無TP滾動,fade 腿保留 POC)
  F4 只 fade 腿 + trail50   F5 只破位腿 RR2 + trail50
每輪報 fade / breakout 分腿 pnl。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.futureman_study
"""
from __future__ import annotations

import copy
import logging
import math
from collections import defaultdict
from typing import Optional

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Candle, Direction, StrategyType, TradeSignal,
    _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0
TICK = 0.25
SL_TICKS = 80          # 兩類腿統一 20pt SL
FAR_TP_PTS = 1_000_000.0


class FuturemanStrategy:
    """前日 VA playbook。levels 由引擎每日餵入。"""

    TICK_SIZE = TICK
    MIN_STOP_TICKS = 4
    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, rr: float = 2.0, fades=True, breakouts=True):
        self.rr = float(rr)
        self.fades = fades
        self.breakouts = breakouts
        self.levels = None            # {"date","poc","vah","val"}
        self._prev_close: Optional[float] = None
        # 每張單每天一次:發信號即上鎖;掛單過期未成交解鎖;成交後鎖到日終。
        # (engine 的 session-limit 已關,鎖由策略自管)
        self._used = set()            # f"{date}:{play}"
        self._last_key: Optional[str] = None
        self._state = "idle"

    # interface stubs
    def reset(self): self._state = "idle"
    def reset_state_only(self): self.reset()
    def reset_breakout_confirmation(self): self.reset()
    def warmup(self, candle): pass
    def observe(self, candle, zones, is_mature): self._prev_close = candle.close
    def notify_trade_closed(self, exit_reason):
        self._state = "idle"
        self._last_key = None         # 已成交並平倉 → 今天這張單用掉了
    def notify_order_cancelled(self):
        self._state = "idle"
        if self._last_key:            # 掛單過期沒成交 → 解鎖,下一根重掛
            self._used.discard(self._last_key)
            self._last_key = None
    def get_phase_label(self): return "FUTUREMAN"
    @property
    def raw_state(self): return self._state

    def set_traded_breakouts(self, keys): pass
    def mark_breakout_used(self, zone_id, direction): pass
    def unlock_breakout(self, zone_id, direction): pass

    def _mk(self, candle, play, direction, entry, sl, tp, order_type):
        self._state = "confirmed"
        lv = self.levels
        key = f"{lv['date']}:{play}"
        self._used.add(key)
        self._last_key = key
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW, direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp,
            zone_id=f"FM:{lv['date']}:{play}",
            reason=f"FUTUREMAN {play} @ {entry:.2f}",
            timestamp=candle.timestamp,
            breakout_range=abs(lv["vah"] - lv["val"]),
            order_type=order_type,
            meta={"strategy_family": "trend", "mode": "futureman", "play": play},
        )

    def evaluate(self, candle: Candle, zones, is_mature) -> Optional[TradeSignal]:
        lv = self.levels
        prev_close = self._prev_close
        self._prev_close = candle.close
        if not lv:
            return None
        poc, vah, val = lv["poc"], lv["vah"], lv["val"]
        slp = SL_TICKS * TICK
        d = lv["date"]

        def used(play):
            return f"{d}:{play}" in self._used

        # ── 破位腿(收盤穿越,市價)──
        if self.breakouts and prev_close is not None:
            if (prev_close <= vah < candle.close) and not used("brkLong"):
                e = candle.close
                return self._mk(candle, "brkLong", Direction.BUY,
                                e, e - slp, e + slp * self.rr, "market")
            if (prev_close >= val > candle.close) and not used("brkShort"):
                e = candle.close
                return self._mk(candle, "brkShort", Direction.SELL,
                                e, e + slp, e - slp * self.rr, "market")

        # ── fade 腿(內側 limit,TP=POC;距 POC 太近不做)──
        if self.fades:
            if val < candle.close < vah:
                if not used("fadeShort") and (vah - poc) > 8 * TICK:
                    return self._mk(candle, "fadeShort", Direction.SELL,
                                    vah, vah + slp, poc, "limit")
                if not used("fadeLong") and (poc - val) > 8 * TICK:
                    return self._mk(candle, "fadeLong", Direction.BUY,
                                    val, val - slp, poc, "limit")
        return None


class FuturemanBacktest(BacktestEngine):
    def __init__(self, *args, rr=2.0, fades=True, breakouts=True,
                 ladder=False, **kw):
        super().__init__(*args, **kw)
        self.trend_follow = FuturemanStrategy(rr=rr, fades=fades, breakouts=breakouts)
        self.ladder = ladder
        self._vp = VolumeProfileCalculator(TICK, float(self.config.value_area_pct))
        self._day = None
        self._day_candles = []
        self._initial_risk = 0.0
        self._max_r = 0.0
        self._cur_play = ""
        self.play_pnl = defaultdict(float)
        self.play_n = defaultdict(int)

    def _process_candle(self, candle):
        d = _topstep_trade_date(candle.timestamp)
        if d != self._day:
            if self._day_candles:
                try:
                    vp = self._vp.calculate(self._day_candles)
                    self.trend_follow.levels = {
                        "date": str(d), "poc": vp.poc, "vah": vp.vah, "val": vp.val,
                    }
                except ValueError:
                    pass
            self._day = d
            self._day_candles = []
        self._day_candles.append(candle)
        super()._process_candle(candle)

    def _execute_entry(self, signal, candle):
        super()._execute_entry(signal, candle)
        pos = self._open_position
        if not pos:
            return
        self._cur_play = str((signal.meta or {}).get("play", "?"))
        self._initial_risk = abs(pos.entry_price - pos.sl_price)
        self._max_r = 0.0
        if self.ladder and self._cur_play.startswith("brk"):
            if pos.direction == Direction.BUY:
                pos.tp_price = pos.entry_price + FAR_TP_PTS
            else:
                pos.tp_price = pos.entry_price - FAR_TP_PTS

    def _execute_exit(self, candle, exit_price, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None:
            self.play_pnl[self._cur_play] += t.pnl or 0.0
            self.play_n[self._cur_play] += 1

    def _check_trailing_sl(self, candle):
        if not (self.ladder and self._cur_play.startswith("brk")):
            return super()._check_trailing_sl(candle)
        pos = self._open_position
        if not pos or self._initial_risk <= 0:
            return
        mkt = candle.close
        fav = (mkt - pos.entry_price) if pos.direction == Direction.BUY else (pos.entry_price - mkt)
        r = fav / self._initial_risk
        if r > self._max_r:
            self._max_r = r
        if self._max_r < 2.0:
            return
        lock_r = math.floor(self._max_r) - 2.0
        if pos.direction == Direction.BUY:
            new_sl = round((pos.entry_price + lock_r * self._initial_risk) / TICK) * TICK
            if new_sl > pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True
        else:
            new_sl = round((pos.entry_price - lock_r * self._initial_risk) / TICK) * TICK
            if new_sl < pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True


def _run(params, candles, **kw):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=0.80,
    )
    eng = FuturemanBacktest(config=config, strategy_params=params,
                            zone_timeline=None, record_equity=False, **kw)
    result = eng.run(candles)
    m = result.metrics
    legs = "  ".join(
        f"{k}:{eng.play_n[k]}筆{eng.play_pnl[k]:+.0f}"
        for k in sorted(eng.play_pnl)
    )
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy), "legs": legs,
    }


def _row(tag, r):
    print(f"{tag:<20} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}", flush=True)
    print(f"{'':<20} 分腿: {r['legs']}", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    # futureman:全時段、無 session 鎖(playbook 每天每張單只成交一次,自限)
    base.tr_allowed_sessions = None
    base.one_trade_per_session_direction = False
    base.tr_one_trade_per_session = False
    base.full_tp_lock = 0
    base.tr_full_tp_lock = 0

    header = (f"{'variant':<20} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    for tag, kw in (
        ("F1 RR2 trail50", {"rr": 2.0}),
        ("F2 RR3 trail50", {"rr": 3.0}),
        ("F3 ladder破位腿", {"rr": 2.0, "ladder": True}),
        ("F4 只fade腿", {"rr": 2.0, "breakouts": False}),
        ("F5 只破位腿 RR2", {"rr": 2.0, "fades": False}),
    ):
        r = _run(copy.deepcopy(base), candles, **kw)
        _row(tag, r)


if __name__ == "__main__":
    main()
