"""1.0.8 研究:失敗突破 → 反手區間收斂(fade)策略,對照現行 trend。

使用者提案:在現行 trend 之上,**當突破失敗**(K 線曾 open+close 突破 VAH/VAL,
下一根又收回 80% 價值區間內)時,不再等下一次突破,而是「反過來做」——
把它當成區間震盪:
  - 收回後掛 SELL LIMIT @ VAH,目標 = 區間 50%(VA 中點),當作突破被拒。
  - 收回後掛 BUY  LIMIT @ VAL,目標 = 區間 50%(VA 中點)。
只要價格還在 80 區間內就持續掛;真正持續突破 or 離場則解除。

SL 三種測法(--=更保守):
  extreme : SL = 區間極值 high_100 / low_100(真的創新高才算突破成立)
  rr1     : SL 與 TP 對稱(RR1),SL = VAH+(VAH-mid) / VAL-(mid-VAL)
  ticks   : SL = VAH + N tick 固定緩衝

對照:C = 現行 #3 trend(overlap smallest, RR6)。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.fade_strategy_study
"""
from __future__ import annotations

import copy
from typing import List, Optional, Set, Tuple

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig, Candle, ConsolidationZone, Direction, StrategyParams,
    StrategyType, TradeSignal, _extract_symbol, get_commission_rt, get_fees_rt,
)
from backend.terminal_live import (
    BUILTIN_PRESETS, CODEX_630_PRESET_3, _build_strategy_params,
)

INITIAL_CAPITAL = 50_000.0
POINT_VALUE = 20.0


class FadeStrategy:
    """反手區間收斂:失敗突破後,在 VA 邊界掛反向 limit,目標區間中點。

    Interface-compatible with SessionTrendFollow so it can be dropped into
    BacktestEngine.trend_follow.
    """

    TICK_SIZE = 0.25
    MIN_STOP_TICKS = 4
    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, params: Optional[StrategyParams] = None,
                 sl_mode: str = "extreme", sl_ticks: int = 40,
                 target: str = "mid"):
        p = params or StrategyParams()
        self.area_timeframe = str(getattr(p, "area_timeframe", "5m") or "5m")
        self.method = str(getattr(p, "method", "single") or "single").lower()
        self.tf_combo = [str(t) for t in (getattr(p, "tf_combo", None) or []) if t]
        self.sl_mode = sl_mode
        self.sl_ticks = sl_ticks
        self.target = target  # "mid" (VA midpoint) or "poc"

        self._state = "idle"          # idle | armed | confirmed | in_trade
        self._fade_dir: Optional[str] = None   # "down"=sell@VAH, "up"=buy@VAL
        self._prev_breakout: Optional[str] = None  # last sustained breakout dir
        self._recent: List[Candle] = []
        self._traded: Set[Tuple[str, str]] = set()

    # --- interface stubs the engine may call ---
    def reset(self):
        self._state = "idle"
        self._fade_dir = None
        self._prev_breakout = None
        self._recent = []

    def reset_state_only(self):
        self.reset()

    def reset_breakout_confirmation(self):
        self._state = "idle"
        self._fade_dir = None
        self._prev_breakout = None

    def set_traded_breakouts(self, keys):
        out: Set[Tuple[str, str]] = set()
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

    def notify_trade_closed(self, exit_reason: str):
        self._state = "idle"
        self._fade_dir = None
        self._prev_breakout = None

    def notify_order_cancelled(self):
        if self._state == "confirmed":
            self._state = "armed"

    def warmup(self, candle: Candle):
        self._remember(candle)

    def observe(self, candle, zones, is_mature):
        self._remember(candle)
        self._advance(candle, self._norm(zones), is_mature)

    def get_phase_label(self) -> str:
        return "等待失敗突破"

    @property
    def raw_state(self) -> str:
        return self._state

    # --- helpers ---
    @staticmethod
    def _norm(zones) -> List[ConsolidationZone]:
        if zones is None:
            return []
        if isinstance(zones, ConsolidationZone):
            return [zones]
        return [z for z in zones if z is not None]

    def _remember(self, candle: Candle):
        if self._recent:
            gap = (candle.timestamp - self._recent[-1].timestamp).total_seconds() / 60
            if gap > 60:
                self._recent = []
        self._recent.append(candle)
        if len(self._recent) > 20:
            self._recent = self._recent[-20:]

    @staticmethod
    def _classify(candle: Candle, z: ConsolidationZone):
        up = candle.open > z.vah_80 and candle.close > z.vah_80
        down = candle.open < z.val_80 and candle.close < z.val_80
        inside = (z.val_80 <= candle.open <= z.vah_80
                  and z.val_80 <= candle.close <= z.vah_80)
        return up, down, inside

    def _pick_zone(self, zone_list):
        # widest / strongest reference zone (same spirit as trend picking).
        return max(zone_list, key=lambda z: (z.vah_80 - z.val_80))

    def _advance(self, candle, zone_list, is_mature):
        if not zone_list or not is_mature or self._state == "in_trade":
            return
        z = self._pick_zone(zone_list)
        up, down, inside = self._classify(candle, z)
        if up:
            self._prev_breakout = "up"
            self._state = "idle"   # real breakout underway → trend territory, no fade
            self._fade_dir = None
        elif down:
            self._prev_breakout = "down"
            self._state = "idle"
            self._fade_dir = None
        elif inside:
            # returned inside → if we had a poke/breakout, arm the opposite fade
            if self._prev_breakout == "up":
                self._fade_dir = "down"   # sell @ VAH
                self._state = "armed"
            elif self._prev_breakout == "down":
                self._fade_dir = "up"     # buy @ VAL
                self._state = "armed"
            self._prev_breakout = None

    def evaluate(self, candle, zones, is_mature) -> Optional[TradeSignal]:
        self._remember(candle)
        zone_list = self._norm(zones)
        if not zone_list or not is_mature or self._state == "in_trade":
            return None
        self._advance(candle, zone_list, is_mature)
        if self._state != "armed" or not self._fade_dir:
            return None
        z = self._pick_zone(zone_list)
        # only fade while price is inside the 80 range
        _, _, inside = self._classify(candle, z)
        if not inside:
            return None
        bk = (str(z.zone_id), self._fade_dir)
        if bk in self._traded:
            return None
        self._state = "confirmed"
        return self._signal(candle, z, self._fade_dir)

    def _target_price(self, z: ConsolidationZone) -> float:
        if self.target == "poc" and getattr(z, "poc", None) is not None:
            return float(z.poc)
        return (z.vah_80 + z.val_80) / 2.0

    def _sl(self, z: ConsolidationZone, direction: str, entry: float, tp: float) -> float:
        buf = self.sl_ticks * self.TICK_SIZE
        if direction == "down":  # sell @ VAH
            if self.sl_mode == "extreme":
                sl = max(z.high_100, entry + self.MIN_STOP_TICKS * self.TICK_SIZE)
            elif self.sl_mode == "rr1":
                sl = entry + (entry - tp)
            else:  # ticks
                sl = entry + buf
            return max(sl, entry + self.MIN_STOP_TICKS * self.TICK_SIZE)
        else:  # buy @ VAL
            if self.sl_mode == "extreme":
                sl = min(z.low_100, entry - self.MIN_STOP_TICKS * self.TICK_SIZE)
            elif self.sl_mode == "rr1":
                sl = entry - (tp - entry)
            else:
                sl = entry - buf
            return min(sl, entry - self.MIN_STOP_TICKS * self.TICK_SIZE)

    def _signal(self, candle, z, direction) -> TradeSignal:
        if direction == "down":
            entry = z.vah_80
            tp = self._target_price(z)
            sl = self._sl(z, direction, entry, tp)
            trade_dir = Direction.SELL
        else:
            entry = z.val_80
            tp = self._target_price(z)
            sl = self._sl(z, direction, entry, tp)
            trade_dir = Direction.BUY
        meta = {
            "strategy_family": "trend", "mode": self.method,
            "side": "VAH" if direction == "down" else "VAL",
            "trade_tf": str(getattr(z, "timeframe", "") or self.area_timeframe),
            "primary_zone": {"tf": str(getattr(z, "timeframe", "") or self.area_timeframe),
                             "zone_id": getattr(z, "zone_id", "") or ""},
        }
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW, direction=trade_dir,
            entry_price=entry, sl_price=sl, tp_price=tp, zone_id=z.zone_id,
            reason=f"FADE {'sell@VAH' if direction=='down' else 'buy@VAL'} -> mid {tp:.2f}",
            timestamp=candle.timestamp,
            breakout_range=abs(z.vah_80 - z.val_80), meta=meta,
        )


class FadeBacktest(BacktestEngine):
    def __init__(self, *args, sl_mode="extreme", sl_ticks=40, target="mid", **kw):
        super().__init__(*args, **kw)
        self.trend_follow = FadeStrategy(
            params=self.strategy_params, sl_mode=sl_mode,
            sl_ticks=sl_ticks, target=target,
        )


def _run(engine_cls, params, config, candles, **kw):
    result = engine_cls(config=config, strategy_params=params,
                        zone_timeline=None, record_equity=False, **kw).run(candles)
    m = result.metrics
    return {
        "trades": int(m.total_trades), "win_rate": float(m.win_rate),
        "pnl": float(m.total_pnl), "max_dd": float(m.max_drawdown),
        "pf": float(m.profit_factor), "calmar": float(m.calmar_ratio),
        "expectancy": float(m.expectancy),
    }


def _fmt(label, r):
    print(f"{label:<28} {r['trades']:>6} {100*r['win_rate']:>6.1f}% "
          f"{r['pnl']:>+11.1f} {r['max_dd']:>9.1f} {r['pf']:>6.2f} "
          f"{r['calmar']:>7.2f} {r['expectancy']:>+9.2f}", flush=True)


def main():
    candles = candle_store.load("MNQ", 1)
    if not candles:
        raise SystemExit("No MNQ 1m candles.")
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CODEX_630_PRESET_3]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)  # overlap 5m+30m

    def cfg(params):
        return BacktestConfig(
            strategies=["trend"], initial_capital=INITIAL_CAPITAL,
            symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
            fees_rt=get_fees_rt(cid),
            value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
        )

    header = (f"{'variant':<28} {'trades':>6} {'win%':>7} {'pnl':>11} "
              f"{'maxDD':>9} {'PF':>6} {'Calmar':>7} {'expect':>9}")
    print("\n" + header, flush=True)
    print("-" * len(header), flush=True)

    # C baseline: current #3 trend
    c = _run(BacktestEngine, base, cfg(base), candles)
    _fmt("C #3 trend (overlap RR6)", c)

    # Fade variants on same overlap zones (smallest-TF border)
    for sl_mode, sl_ticks, tag in (
        ("extreme", 0, "F extreme(high/low_100)"),
        ("rr1", 0, "F rr1 (SL=TP symmetric)"),
        ("ticks", 20, "F fixed 20tick SL"),
        ("ticks", 40, "F fixed 40tick SL"),
    ):
        r = _run(FadeBacktest, base, cfg(base), candles,
                 sl_mode=sl_mode, sl_ticks=sl_ticks, target="mid")
        _fmt(tag, r)

    # POC target on best SL modes
    r = _run(FadeBacktest, base, cfg(base), candles,
             sl_mode="rr1", sl_ticks=0, target="poc")
    _fmt("F rr1 target=POC", r)


if __name__ == "__main__":
    main()
