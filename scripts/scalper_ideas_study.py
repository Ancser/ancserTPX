"""1.0.8 研究(僅腳本):pro/機構手法可測部分 — 出場工程 + 時段 + POC 掛單。

以 ladder(2R觸發/1R步進/gap2R,+8145)為新基準,單變量測:

  P1(分析)   — ladder 交易按「進場小時(UTC)」和「整點距離」拆解 pnl/勝率。
                回答:整點交易?哪些時段是糧倉?
  S1 gap1.5R   — 階梯回吐緩衝收窄(更早鎖利,vs 2R)。
  S2 gap2.5R   — 緩衝放寬(給更多空間)。
  S3 sdTrail   — 標準差自適應:啟動後 SL = 峰值價 − 2×σ(30m),
                波動大自動放寬、盤整自動收緊(取代固定 R 階梯)。
  S4 pocEntry  — VP 最大成交峰(POC)掛單:突破後 limit 掛更深的 POC
                (回踩更深 = 更好價格,SL 換 VAL↔POC 間最低量節點)。
  S5 hourEarly — 只在 ASIA 頭 4 小時(22-01 UTC)進場。
  S6 roundHour — 只在整點 ±5 分鐘進場(演算法資金流窗口)。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.scalper_ideas_study
"""
from __future__ import annotations

import copy
import logging
import statistics
from collections import defaultdict, deque

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.strategy.trend_follow import SessionTrendFollow
from backend.terminal_live import (
    BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params,
)
from scripts.rolling_exit_study import RollingExitBacktest

INITIAL_CAPITAL = 50_000.0
TICK = 0.25


class LadderVar(RollingExitBacktest):
    """ladder + gap 參數 / SD trail / 時段 gate。"""

    def __init__(self, *args, gap_r=2.0, sd_trail=False,
                 allow_hours=None, round_minute=None, **kw):
        super().__init__(*args, roll_mode="ladder", **kw)
        self.GAP_R = float(gap_r)
        self.sd_trail = sd_trail
        self.allow_hours = set(allow_hours) if allow_hours else None
        self.round_minute = round_minute
        self._closes30 = deque(maxlen=30)

        _orig = self.trend_follow.evaluate

        def gated(candle, zones, mature):
            sig = _orig(candle, zones, mature)
            if sig is None:
                return None
            if self.allow_hours is not None and candle.timestamp.hour not in self.allow_hours:
                return None
            if self.round_minute is not None:
                m = candle.timestamp.minute
                if not (m >= 60 - self.round_minute or m <= self.round_minute):
                    return None
            return sig

        self.trend_follow.evaluate = gated

    def _process_candle(self, candle):
        self._closes30.append(float(candle.close))
        super()._process_candle(candle)

    def _check_trailing_sl(self, candle):
        if not self.sd_trail:
            return super()._check_trailing_sl(candle)
        pos = self._open_position
        if not pos or self._initial_risk <= 0:
            return
        mkt = candle.close
        fav = (mkt - pos.entry_price) if pos.direction == Direction.BUY else (pos.entry_price - mkt)
        r = fav / self._initial_risk
        if r > self._max_r:
            self._max_r = r
        if self._max_r < self.TRIGGER_R or len(self._closes30) < 30:
            return
        sd = statistics.pstdev(self._closes30)   # 30m 收盤價標準差(點)
        gap_pts = max(2.0 * sd, 4 * TICK)
        if pos.direction == Direction.BUY:
            peak_px = pos.entry_price + self._max_r * self._initial_risk
            new_sl = round(max(pos.entry_price, peak_px - gap_pts) / TICK) * TICK
            if new_sl > pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True
        else:
            peak_px = pos.entry_price - self._max_r * self._initial_risk
            new_sl = round(min(pos.entry_price, peak_px + gap_pts) / TICK) * TICK
            if new_sl < pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True


class POCEntryTrend(SessionTrendFollow):
    """突破確認後,limit 掛在 POC(更深回踩);SL = VAL↔POC(多)/POC↔VAH(空)最低量節點。"""

    PENDING_TIMEOUT_CANDLES = 10   # POC 較深,給 10 根等回踩

    def _generate_signal(self, candle, zone, direction):
        sig = super()._generate_signal(candle, zone, direction)
        fallback_pts = self.SL_TICKS * self.TICK_SIZE
        poc = float(zone.poc)
        if direction == "up":
            entry = poc
            node = zone.lowest_volume_price_between(zone.val_80, zone.poc)
            sl = (entry - fallback_pts) if (node is None or node >= entry) else node
            sl = min(sl, entry - self.MIN_STOP_TICKS * self.TICK_SIZE)
            dist = entry - sl
            tp = entry + dist * self.RR_RATIO
        else:
            entry = poc
            node = zone.lowest_volume_price_between(zone.poc, zone.vah_80)
            # 空單 SL 在上方:取 POC↔VAH 節點
            sl = (entry + fallback_pts) if (node is None or node <= entry) else node
            sl = max(sl, entry + self.MIN_STOP_TICKS * self.TICK_SIZE)
            dist = sl - entry
            tp = entry - dist * self.RR_RATIO
        sig.entry_price = entry
        sig.sl_price = sl
        sig.tp_price = tp
        sig.reason = f"POC-PULLBACK {direction} entry={entry:.2f}"
        return sig


class POCLadder(LadderVar):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        # 換策略(S4 不用時段 gate,直接覆蓋)
        self.trend_follow = POCEntryTrend(params=self.strategy_params)
        self._pending_max_age = self.trend_follow.PENDING_TIMEOUT_CANDLES


def _run(engine_cls, params, candles, **kw):
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    eng = engine_cls(config=config, strategy_params=params,
                     zone_timeline=None, record_equity=False, **kw)
    result = eng.run(candles)
    m = result.metrics
    return result, {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def _row(tag, r):
    print(f"{tag:<20} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}", flush=True)


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    base.value_area_pct = float(preset.get("value_area_pct", 0.70))

    header = (f"{'variant':<20} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    # 基準 ladder 2R(重跑取 trades 供 P1 分析)
    res0, r0 = _run(LadderVar, copy.deepcopy(base), candles, gap_r=2.0)
    _row("L0 ladder gap2R", r0)

    for tag, kw in (
        ("S1 gap1.5R", {"gap_r": 1.5}),
        ("S2 gap2.5R", {"gap_r": 2.5}),
        ("S3 sdTrail 2sigma", {"sd_trail": True}),
        ("S5 hour22-01", {"allow_hours": (22, 23, 0, 1)}),
        ("S6 round±5min", {"round_minute": 5}),
    ):
        _, r = _run(LadderVar, copy.deepcopy(base), candles, **kw)
        _row(tag, r)

    _, r = _run(POCLadder, copy.deepcopy(base), candles, gap_r=2.0)
    _row("S4 pocEntry+ladder", r)

    # P1: ladder 交易按進場小時 / 整點距離拆解
    print("\n== P1a. ladder 按進場小時(UTC)==", flush=True)
    hour = defaultdict(lambda: [0, 0, 0.0])
    for t in res0.trades:
        h = t.entry_time.hour
        hour[h][0] += 1
        hour[h][1] += 1 if (t.pnl or 0) > 0 else 0
        hour[h][2] += t.pnl or 0.0
    for h in sorted(hour, key=lambda x: ((x - 22) % 24)):
        n, w, p = hour[h]
        print(f"  {h:02d}:00  n={n:>4}  win%={100*w/n:>5.1f}  pnl={p:>+9.1f}", flush=True)

    print("\n== P1b. ladder 按「距整點分鐘」==", flush=True)
    mins = defaultdict(lambda: [0, 0.0])
    for t in res0.trades:
        m = t.entry_time.minute
        d = min(m, 60 - m)          # 距最近整點
        b = "0-5" if d <= 5 else ("6-15" if d <= 15 else "16-30")
        mins[b][0] += 1
        mins[b][1] += t.pnl or 0.0
    for b in ("0-5", "6-15", "16-30"):
        n, p = mins[b]
        if n:
            print(f"  距整點 {b:<6} n={n:>4}  pnl={p:>+9.1f}  每筆={p/n:>+7.2f}", flush=True)


if __name__ == "__main__":
    main()
