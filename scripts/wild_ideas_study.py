"""1.0.8 研究(僅腳本):狂野實驗批次 — 非常規粗暴反盤整手段。

全部在 CLAUDE #1 基準(5m clock VA70 RR4 C3 SL-node Trail50L10 ASIA)上改一件事:

  W0 base          — 對照。
  W1 sweepReclaim2 — 【換進場】不做突破!偵測「影線掃過 VAH/VAL ≥4tick 但收回
                     區間內」= 流動性被掃 → 立刻市價反向進場。SL=掃損極值+4t,
                     TP=RR2。機構掃損後的真動方向。
  W1b sweepReclaim1— 同上,TP=RR1(快進快出)。
  W2 openDisp40t   — 【日級 gate】ASIA 開盤 60 分鐘淨位移 <40tick → 今天整天
                     不交易(開盤沒能量=盤整日)。前 60 分鐘一律不進。
  W3 adrBudget80   — 【日級 gate】當日已走 range ≥ 0.8×ADR5(5日均振幅)→
                     剩餘時間停手(油箱見底,後面大概率來回掃)。
  W4 deltaDir      — 【方向 gate】60m delta 代理(收漲量−收跌量)符號必須與
                     進場方向一致。
  W5 prevRVcap     — 【日級 gate】前一交易日 RV 落在近 20 日最高三分位 →
                     今天不交易(實盤在高波動日虧最兇)。
  W6 sweep2+stop2  — W1 + 當日虧 2 單斷路器。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.wild_ideas_study
"""
from __future__ import annotations

import copy
import logging
import math
import statistics
from collections import deque
from datetime import timedelta
from typing import List, Optional, Set, Tuple

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Candle, ConsolidationZone, Direction, StrategyType,
    TradeSignal, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0
TICK = 0.25


class SweepReclaim:
    """掃損反打:影線刺穿 VAH/VAL 又收回 → 市價反向。與 SessionTrendFollow 同介面。"""

    TICK_SIZE = TICK
    MIN_STOP_TICKS = 4
    PENDING_TIMEOUT_CANDLES = 1
    MIN_PIERCE_TICKS = 4     # 影線至少刺穿 4 tick 才算「掃」
    SL_BUF_TICKS = 4

    def __init__(self, params=None, rr: float = 2.0):
        self.rr = float(rr)
        self.area_timeframe = str(getattr(params, "area_timeframe", "5m") or "5m")
        self._traded: Set[Tuple[str, str]] = set()
        self._state = "idle"

    # -- interface stubs --
    def reset(self): self._state = "idle"
    def reset_state_only(self): self.reset()
    def reset_breakout_confirmation(self): self.reset()
    def warmup(self, candle): pass
    def observe(self, candle, zones, is_mature): pass
    def notify_trade_closed(self, exit_reason): self._state = "idle"
    def notify_order_cancelled(self): self._state = "idle"
    def get_phase_label(self): return "等待掃損"
    @property
    def raw_state(self): return self._state

    def set_traded_breakouts(self, keys):
        out = set()
        for item in keys or []:
            try:
                zid, d = item[:2]
            except (TypeError, ValueError, IndexError):
                continue
            if zid and d:
                out.add((str(zid), str(d)))
        self._traded = out

    def mark_breakout_used(self, zone_id, direction):
        if zone_id and direction:
            self._traded.add((str(zone_id), str(direction)))

    def unlock_breakout(self, zone_id, direction):
        self._traded.discard((str(zone_id), str(direction)))

    @staticmethod
    def _norm(zones):
        if zones is None:
            return []
        if isinstance(zones, ConsolidationZone):
            return [zones]
        return [z for z in zones if z is not None]

    def evaluate(self, candle: Candle, zones, is_mature) -> Optional[TradeSignal]:
        zone_list = self._norm(zones)
        if not zone_list or not is_mature or self._state == "in_trade":
            return None
        pierce = self.MIN_PIERCE_TICKS * TICK
        buf = self.SL_BUF_TICKS * TICK
        for z in zone_list:
            # 上掃:影線破 VAH 又收回 → SELL
            if (candle.high >= z.vah_80 + pierce
                    and candle.open <= z.vah_80 and candle.close <= z.vah_80):
                key = (str(z.zone_id), "up")
                if key in self._traded:
                    continue
                entry = candle.close
                sl = candle.high + buf
                dist = max(sl - entry, self.MIN_STOP_TICKS * TICK)
                sl = entry + dist
                tp = entry - dist * self.rr
                return self._sig(candle, z, Direction.SELL, entry, sl, tp, "up")
            # 下掃:影線破 VAL 又收回 → BUY
            if (candle.low <= z.val_80 - pierce
                    and candle.open >= z.val_80 and candle.close >= z.val_80):
                key = (str(z.zone_id), "down")
                if key in self._traded:
                    continue
                entry = candle.close
                sl = candle.low - buf
                dist = max(entry - sl, self.MIN_STOP_TICKS * TICK)
                sl = entry - dist
                tp = entry + dist * self.rr
                return self._sig(candle, z, Direction.BUY, entry, sl, tp, "down")
        return None

    def _sig(self, candle, z, direction, entry, sl, tp, sweep_dir):
        self._state = "confirmed"
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp,
            zone_id=z.zone_id,
            reason=f"SWEEP-RECLAIM {sweep_dir} fade -> {tp:.2f} (RR{self.rr:g})",
            timestamp=candle.timestamp,
            breakout_range=abs(z.vah_80 - z.val_80),
            order_type="market",
            meta={"strategy_family": "trend", "mode": "sweep",
                  "side": "VAH" if sweep_dir == "up" else "VAL",
                  "trade_tf": str(getattr(z, "timeframe", "") or self.area_timeframe)},
        )


class WildBacktest(BacktestEngine):
    def __init__(self, *args, entry_mode="trend", sweep_rr=2.0,
                 open_disp_ticks=None, adr_budget_pct=None, delta_dir=False,
                 prev_rv_cap=False, daily_stop=None, **kw):
        super().__init__(*args, **kw)
        self.open_disp_ticks = open_disp_ticks
        self.adr_budget_pct = adr_budget_pct
        self.delta_dir = delta_dir
        self.prev_rv_cap = prev_rv_cap
        self.daily_stop = daily_stop

        if entry_mode == "sweep":
            self.trend_follow = SweepReclaim(params=self.strategy_params, rr=sweep_rr)

        # per-day state
        self._d = None                  # current trade date
        self._d_open_px = None
        self._d_open_ts = None
        self._d_disp60: Optional[float] = None
        self._d_hi = -math.inf
        self._d_lo = math.inf
        self._day_ranges: List[float] = []   # completed days
        self._d_closes: List[float] = []
        self._day_rvs: List[float] = []      # completed days
        self._prev_rv: Optional[float] = None
        self._delta = deque(maxlen=60)
        self._daily_losses = {}

        _orig = self.trend_follow.evaluate

        def gated(candle, zones, mature):
            sig = _orig(candle, zones, mature)
            if sig is None:
                return None
            return sig if self._gates_pass(candle, sig) else None

        self.trend_follow.evaluate = gated

    def _roll_day(self, d):
        if self._d is not None:
            if math.isfinite(self._d_hi) and math.isfinite(self._d_lo):
                self._day_ranges.append(self._d_hi - self._d_lo)
                self._day_ranges = self._day_ranges[-10:]
            rets = [math.log(self._d_closes[i] / self._d_closes[i - 1])
                    for i in range(1, len(self._d_closes))
                    if self._d_closes[i - 1] > 0]
            rv = statistics.pstdev(rets) if len(rets) > 1 else 0.0
            self._day_rvs.append(rv)
            self._day_rvs = self._day_rvs[-20:]
            self._prev_rv = rv
        self._d = d
        self._d_open_px = None
        self._d_open_ts = None
        self._d_disp60 = None
        self._d_hi, self._d_lo = -math.inf, math.inf
        self._d_closes = []

    def _process_candle(self, candle):
        d = _topstep_trade_date(candle.timestamp)
        if d != self._d:
            self._roll_day(d)
        if self._d_open_px is None:
            self._d_open_px = candle.open
            self._d_open_ts = candle.timestamp
        if (self._d_disp60 is None and self._d_open_ts is not None
                and candle.timestamp >= self._d_open_ts + timedelta(minutes=60)):
            self._d_disp60 = abs(candle.close - self._d_open_px)
        self._d_hi = max(self._d_hi, candle.high)
        self._d_lo = min(self._d_lo, candle.low)
        self._d_closes.append(candle.close)
        v = float(candle.volume or 0)
        self._delta.append(v if candle.close >= candle.open else -v)
        super()._process_candle(candle)

    def _execute_exit(self, candle, exit_price, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None and (t.pnl or 0.0) < 0:
            d = _topstep_trade_date(candle.timestamp)
            self._daily_losses[d] = self._daily_losses.get(d, 0) + 1

    def _gates_pass(self, candle, sig) -> bool:
        if self.daily_stop is not None:
            if self._daily_losses.get(self._d, 0) >= self.daily_stop:
                return False
        if self.open_disp_ticks is not None:
            if self._d_disp60 is None:       # 開盤未滿 60 分鐘 → 不進
                return False
            if self._d_disp60 < self.open_disp_ticks * TICK:
                return False
        if self.adr_budget_pct is not None and len(self._day_ranges) >= 5:
            adr = statistics.mean(self._day_ranges[-5:])
            if math.isfinite(self._d_hi) and (self._d_hi - self._d_lo) >= self.adr_budget_pct * adr:
                return False
        if self.delta_dir:
            s = sum(self._delta)
            if sig.direction == Direction.BUY and s <= 0:
                return False
            if sig.direction == Direction.SELL and s >= 0:
                return False
        if self.prev_rv_cap and len(self._day_rvs) >= 9 and self._prev_rv is not None:
            cut = sorted(self._day_rvs)[len(self._day_rvs) * 2 // 3]
            if self._prev_rv >= cut:
                return False
        return True


def _run(params, candles, **kw):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    result = WildBacktest(config=config, strategy_params=params,
                          zone_timeline=None, record_equity=False, **kw).run(candles)
    m = result.metrics
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def _row(tag, r):
    print(f"{tag:<22} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}", flush=True)


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

    header = (f"{'variant':<22} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    runs = [
        ("W0 base", {}),
        ("W1 sweepReclaim RR2", {"entry_mode": "sweep", "sweep_rr": 2.0}),
        ("W1b sweepReclaim RR1", {"entry_mode": "sweep", "sweep_rr": 1.0}),
        ("W2 openDisp40t", {"open_disp_ticks": 40}),
        ("W3 adrBudget80", {"adr_budget_pct": 0.8}),
        ("W4 deltaDir60m", {"delta_dir": True}),
        ("W5 prevRVcap", {"prev_rv_cap": True}),
        ("W6 sweep2+stop2", {"entry_mode": "sweep", "sweep_rr": 2.0, "daily_stop": 2}),
    ]
    for tag, kw in runs:
        r = _run(copy.deepcopy(base), candles, **kw)
        _row(tag, r)


if __name__ == "__main__":
    main()
