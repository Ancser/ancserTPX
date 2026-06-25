# ============================================================
# 文件: backend/backtest/ml_trend_backtest.py
# 狀態: v1.0.6 (ML Trend — Value Area mean reversion backtester)
# 執行邏輯:
#   1. 滾動 VP 計算 VAL/VAH/POC (或使用預計算 timeline)
#   2. 價格在 VAL 附近 → 做多; VAH 附近 → 做空
#   3. Market order: 下一根K線 OPEN 成交
#   4. TP = POC (區間50%), SL = 100% range 外 + buffer
#   5. Trail TP (50%/5%) 可選
# 關聯文件:
#   ← backend/strategy/ml_trend.py    (特徵抽取, RSI, 設定)
#   ← backend/strategy/exit_policy.py (trailing SL)
#   ← backend/backtest/metrics.py     (MetricsCalculator)
# ============================================================
"""Backtester for ML Trend: Value Area mean reversion with market orders."""

from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass
from datetime import timezone, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.db.models import (
    Candle, Trade, Direction, ExitReason, StrategyType, BacktestConfig,
    Metrics, get_point_value, get_tick_size,
)
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.strategy.ml_trend import (
    MLTrendConfig, MLTrendSignal,
    extract_features,
)
from backend.strategy.session_filter import is_allowed_session
from backend.backtest.metrics import MetricsCalculator
from backend.backtest.intrabar import resolve_same_bar_exit

logger = logging.getLogger(__name__)
_CT = ZoneInfo("America/Chicago")


@dataclass
class MLTrendBacktestConfig:
    """Run-level knobs (distinct from signal-level MLTrendConfig)."""
    trail_trigger_pct: float = 0.0    # 0 = trail OFF
    trail_lock_pct: float = 0.0       # locked SL as fraction of TP dist
    one_trade_per_session: bool = True
    allowed_sessions: Optional[tuple] = ("ASIA",)
    min_score: float = 0.0            # scorer gate


def _session_key(ts) -> str:
    """Topstep trade-date boundary: CT 17:00."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ct = ts.astimezone(_CT)
    if ct.hour >= 17:
        ct = ct + timedelta(days=1)
    return ct.strftime("%Y-%m-%d")


# ── VP timeline pre-computation ──

def precompute_vp_timeline(
    candles: List[Candle],
    lookback: int,
    tick_size: float = 0.25,
    recalc_interval: int = 5,
) -> List[Optional[dict]]:
    """Compute a rolling-window VP for every bar.

    ``recalc_interval`` controls how often the VP is recomputed (every N bars);
    between recalculations the previous result is reused. This is a ~5x speedup
    with negligible accuracy loss because the lookback window shifts by only 1 bar.
    """
    calc = VolumeProfileCalculator(tick_size=tick_size, value_area_pct=0.80)
    timeline: List[Optional[dict]] = [None] * len(candles)
    last_vp: Optional[dict] = None
    for i in range(lookback, len(candles)):
        if last_vp is not None and i % recalc_interval != 0:
            timeline[i] = last_vp
            continue
        window = candles[i - lookback:i]
        try:
            vp = calc.calculate(window)
            last_vp = {
                "val": vp.val, "vah": vp.vah, "poc": vp.poc,
                "low_100": vp.low_100, "high_100": vp.high_100,
                "total_volume": vp.total_volume,
            }
            timeline[i] = last_vp
        except (ValueError, ZeroDivisionError):
            timeline[i] = last_vp  # fallback to previous
    return timeline


# ── Backtester ──

class MLTrendBacktester:
    """Self-contained backtester for ML Trend mean-reversion strategy."""

    def __init__(
        self,
        signal_cfg: MLTrendConfig,
        run_cfg: Optional[MLTrendBacktestConfig] = None,
        contract_id: str = "CON.F.US.MNQ.M26",
        contract_size: int = 1,
        bt_config: Optional[BacktestConfig] = None,
        scorer=None,
    ):
        self.signal_cfg = signal_cfg
        self.run_cfg = run_cfg or MLTrendBacktestConfig()
        self.scorer = scorer
        self.contract_id = contract_id
        self.contract_size = max(1, int(contract_size or 1))
        self.bt = bt_config or BacktestConfig()
        self.POINT_VALUE = get_point_value(contract_id)
        self.TICK_SIZE = get_tick_size(contract_id)
        self.signal_cfg.tick_size = self.TICK_SIZE

        # Trail exit style (reuse confluence's ExitStyle)
        from backend.strategy.exit_policy import ConfluenceExitStyle
        self.style = ConfluenceExitStyle(
            trail_trigger_pct=float(self.run_cfg.trail_trigger_pct or 0.0),
            trail_lock_pct=float(self.run_cfg.trail_lock_pct or 0.0),
        )

        # State
        self._capital = self.bt.initial_capital
        self._trades: List[Trade] = []
        self._open: Optional[Trade] = None
        self._pending: Optional[MLTrendSignal] = None
        self._trail_triggered = False
        self._session_used: set = set()

    def run(
        self,
        candles: List[Candle],
        vp_timeline: Optional[List[Optional[dict]]] = None,
    ) -> "MLTrendBacktestResult":
        """Run backtest. Supply ``vp_timeline`` (from ``precompute_vp_timeline``)
        to skip the internal rolling VP computation."""
        candles = sorted(candles, key=lambda c: c.timestamp)
        total = len(candles)
        lookback = self.signal_cfg.lookback

        for i, candle in enumerate(candles):
            # 1) manage open position — check exit
            if self._open is not None:
                self._check_exit(candle)
                if self._open is not None:
                    continue  # still open

            # 2) fill pending market order
            if self._pending is not None:
                self._try_fill(candle)
                continue

            # 3) generate new signal (flat + idle)
            if i < lookback or i >= total - 2:
                continue
            if not is_allowed_session(candle.timestamp, self.run_cfg.allowed_sessions):
                continue
            if self.run_cfg.one_trade_per_session:
                sk = _session_key(candle.timestamp)
                if sk in self._session_used:
                    continue

            # get VP
            vp = vp_timeline[i] if vp_timeline is not None else None
            if vp is None and vp_timeline is None:
                # inline VP computation (slow path)
                from backend.strategy.volume_profile import VolumeProfileCalculator
                calc = VolumeProfileCalculator(tick_size=self.TICK_SIZE, value_area_pct=0.80)
                window = candles[max(0, i - lookback):i]
                if len(window) < 20:
                    continue
                try:
                    vpr = calc.calculate(window)
                    vp = {"val": vpr.val, "vah": vpr.vah, "poc": vpr.poc,
                          "low_100": vpr.low_100, "high_100": vpr.high_100}
                except (ValueError, ZeroDivisionError):
                    continue
            if vp is None:
                continue

            self._maybe_signal(candle, i, candles, vp)

        return self._finalize()

    # ── signal generation ──

    def _maybe_signal(self, candle: Candle, idx: int, candles: List[Candle], vp: dict):
        price = candle.close
        tick = self.TICK_SIZE
        band = self.signal_cfg.band_ticks * tick
        buf = self.signal_cfg.sl_buffer_ticks * tick
        cfg = self.signal_cfg
        val, vah, poc = vp["val"], vp["vah"], vp["poc"]
        low_100, high_100 = vp["low_100"], vp["high_100"]

        va_width = vah - val
        if va_width < tick * 4:  # VA too narrow
            return

        signal = None

        # SL reference: "va" = VA edge, "range" = 100% range edge
        use_va_sl = (cfg.sl_mode == "va")

        # ── LONG: price at or below VAL + band ──
        if price <= val + band:
            entry = price  # will fill at next bar's open
            sl = (val - buf) if use_va_sl else (low_100 - buf)
            tp = poc if cfg.tp_mode == "poc" else entry + abs(entry - sl) * cfg.rr
            signal = self._build_signal(
                candle, idx, candles, Direction.BUY, entry, sl, tp,
                val, vah, poc,
                f"LONG near VAL ({val:.2f}) SL={'VA' if use_va_sl else '100%'}",
            )

        # ── SHORT: price at or above VAH − band ──
        elif price >= vah - band:
            entry = price
            sl = (vah + buf) if use_va_sl else (high_100 + buf)
            tp = poc if cfg.tp_mode == "poc" else entry - abs(sl - entry) * cfg.rr
            signal = self._build_signal(
                candle, idx, candles, Direction.SELL, entry, sl, tp,
                val, vah, poc,
                f"SHORT near VAH ({vah:.2f}) SL={'VA' if use_va_sl else '100%'}",
            )

        if signal is None:
            return

        # ML gate
        if self.scorer is not None:
            score = self.scorer.score(signal.features)
            signal.score = score
            signal.prob = self.scorer.probability(signal.features)
            if score < self.run_cfg.min_score:
                return

        # session lock
        if self.run_cfg.one_trade_per_session:
            self._session_used.add(_session_key(candle.timestamp))

        self._pending = signal

    def _build_signal(self, candle, idx, candles, direction, entry, sl, tp,
                      val, vah, poc, reason) -> Optional[MLTrendSignal]:
        tick = self.TICK_SIZE
        cfg = self.signal_cfg
        risk_t = abs(entry - sl) / tick
        reward_t = abs(tp - entry) / tick
        if risk_t < cfg.min_risk_ticks or risk_t > cfg.max_risk_ticks or reward_t < 2:
            return None
        recent = candles[max(0, idx - 45):idx + 1]
        feats = extract_features(
            candle, val, vah, poc, recent, direction, entry, sl, tp, tick,
        )
        return MLTrendSignal(
            timestamp=candle.timestamp, direction=direction,
            entry_price=entry, sl_price=sl, tp_price=tp,
            features=feats, bar_index=idx, reason=reason,
        )

    # ── fill ──

    def _try_fill(self, candle: Candle):
        sig = self._pending
        market_entry = float(candle.open)

        # Keep the structural SL, but reject market fills that have already
        # crossed it.  Otherwise a next-bar market entry can be opened on the
        # wrong side of the planned stop and immediately "exit at SL" for a
        # fake profit / zero-duration trade.  Live bracket placement would not
        # preserve that as a real edge.
        sl = sig.sl_price
        if sig.direction == Direction.BUY and sl >= market_entry:
            self._pending = None
            return
        if sig.direction == Direction.SELL and sl <= market_entry:
            self._pending = None
            return

        # Recalculate TP from the actual market entry if rr mode.
        if self.signal_cfg.tp_mode == "poc":
            tp = sig.tp_price  # POC is fixed
        else:
            risk = abs(market_entry - sl)
            if sig.direction == Direction.BUY:
                tp = market_entry + risk * self.signal_cfg.rr
            else:
                tp = market_entry - risk * self.signal_cfg.rr

        risk = abs(market_entry - sl)
        reward = abs(tp - market_entry)
        risk_t = risk / self.TICK_SIZE
        reward_t = reward / self.TICK_SIZE
        if risk <= 0 or reward <= 0:
            self._pending = None
            return
        if risk_t < self.signal_cfg.min_risk_ticks or risk_t > self.signal_cfg.max_risk_ticks or reward_t < 2:
            self._pending = None
            return
        if sig.direction == Direction.BUY and tp <= market_entry:
            self._pending = None
            return
        if sig.direction == Direction.SELL and tp >= market_entry:
            self._pending = None
            return

        self._trail_triggered = False
        self._open = Trade(
            trade_id=f"MT{uuid.uuid4().hex[:8]}",
            strategy=StrategyType.TREND_FOLLOW,
            direction=sig.direction,
            entry_price=market_entry,
            entry_time=candle.timestamp,
            sl_price=sl, tp_price=tp,
            original_sl_price=sl, original_tp_price=tp,
            contracts=self.contract_size,
            point_value=self.POINT_VALUE,
            contract_id=self.contract_id,
            meta={
                "strategy_type": "ml_trend",
                "score": round(sig.score, 4),
                "prob": round(sig.prob, 4),
                "reason": sig.reason,
            },
        )
        self._pending = None

        # entry bar: only immediate SL can trigger
        self._check_sl_only(candle)

    # ── exits ──

    def _check_sl_only(self, candle: Candle):
        pos = self._open
        if not pos:
            return
        if pos.direction == Direction.BUY and candle.low <= pos.sl_price:
            self._exit(candle, pos.sl_price, ExitReason.SL)
        elif pos.direction == Direction.SELL and candle.high >= pos.sl_price:
            self._exit(candle, pos.sl_price, ExitReason.SL)

    def _check_exit(self, candle: Candle):
        pos = self._open
        if not pos:
            return
        if pos.direction == Direction.BUY:
            hit_sl = candle.low <= pos.sl_price
            hit_tp = candle.high >= pos.tp_price
        else:
            hit_sl = candle.high >= pos.sl_price
            hit_tp = candle.low <= pos.tp_price

        sl_reason = ExitReason.TRAIL_SL if self._trail_triggered else ExitReason.SL
        if hit_sl and hit_tp:
            if resolve_same_bar_exit(candle.open, pos.sl_price, pos.tp_price) == "sl":
                self._exit(candle, pos.sl_price, sl_reason)
            else:
                self._exit(candle, pos.tp_price, ExitReason.TP)
        elif hit_sl:
            self._exit(candle, pos.sl_price, sl_reason)
        elif hit_tp:
            self._exit(candle, pos.tp_price, ExitReason.TP)
        elif self.style.trail_enabled:
            from backend.strategy.exit_policy import maybe_trail_sl
            new_sl, self._trail_triggered = maybe_trail_sl(
                pos.direction, pos.entry_price, pos.tp_price, pos.sl_price,
                self._trail_triggered, candle.close, self.style,
            )
            pos.sl_price = new_sl

    def _exit(self, candle: Candle, exit_price: float, reason: ExitReason):
        pos = self._open
        pt = pos.point_value or self.POINT_VALUE
        if pos.direction == Direction.BUY:
            gross = (exit_price - pos.entry_price) * pt * pos.contracts
        else:
            gross = (pos.entry_price - exit_price) * pt * pos.contracts
        commission = self.bt.commission_rt * pos.contracts
        fees = self.bt.fees_rt * pos.contracts
        pos.pnl = gross - commission - fees
        pos.commission = commission
        pos.fees = fees
        pos.exit_price = exit_price
        pos.exit_time = candle.timestamp
        pos.exit_reason = reason
        self._capital += pos.pnl
        self._trades.append(pos)
        self._open = None

    def _finalize(self) -> "MLTrendBacktestResult":
        metrics = MetricsCalculator().calculate_all(self._trades, self.bt.initial_capital)
        return MLTrendBacktestResult(
            metrics=metrics, trades=self._trades, final_capital=self._capital,
        )


@dataclass
class MLTrendBacktestResult:
    metrics: Metrics
    trades: List[Trade]
    final_capital: float
