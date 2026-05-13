# ============================================================
# 文件: backend/backtest/engine.py
# 功能: 回測引擎 — SessionZone + SessionTrendFollow / MACD / Reversion
# 主要職責:
#   1. 逐根 1m K 線餵入 → Zone 偵測 → 策略 evaluate
#   2. 掛限價單 / 市價單 → fill / SL / TP / Trail / Flatten 模擬
#   3. PnL 計算: gross = Δ * point_value * contracts；net = gross − commission − fees
#   4. 突破後 60 分鐘的價格路徑追蹤 (MFE/MAE in ticks, trail/SL/TP 順序)
# 版本變更 (v0.11):
#   - POINT_VALUE 由 contract 動態決定 (NQ=$20, MNQ=$2)
#   - contracts 從 strategy_params.contract_size 取得 (預設 3 × MNQ)
#   - commission/fees 由 BacktestConfig.commission_rt / fees_rt 取得
#     (routes 會根據 contract_id 帶入正確費率 — MNQ ≈ $1.20/口, NQ ≈ $3.80/口)
#   - 新增 post-breakout 60m 追蹤器: 把 MFE/MAE/破線順序 寫到 trade 物件
#   - trail_enabled=False 時 _check_trailing_sl 直接 short-circuit, 讓部位跑滿 SL/TP
# 關聯:
#   ← backend/api/routes.py (POST /backtest/run, /ml/run)
#   → backend/strategy/consolidation.py / trend_follow.py / reversion.py / macd_strategy.py
#   → backend/db/models.py (Trade, Metrics, get_point_value)
# ============================================================
"""
回測引擎 v3 — SessionTrendFollow Only

將歷史 1m K 線逐根餵入 → Session Zone 偵測 → SessionTrendFollow 評估 → 模擬成交 → 績效計算
"""

from __future__ import annotations
import uuid
import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from backend.db.models import (
    Candle, Trade, TradeSignal, BacktestConfig, BacktestResult,
    Metrics, ConsolidationZone, BreakoutAnalysis, StrategyParams,
    Direction, ExitReason, StrategyType, ZoneStatus,
    get_point_value, get_tick_size,
)
from backend.strategy.consolidation import SessionZoneDetector
from backend.strategy.trend_follow import SessionTrendFollow

logger = logging.getLogger(__name__)

# How long after entry we keep tracking price action for the post-breakout
# stats (MFE / MAE / trail-or-SL-then-TP path). 60 candles ≈ 1h on 1m bars.
POST_BREAKOUT_WINDOW_MIN = 60


_CT = ZoneInfo("America/Chicago")
_UTC_TZ = ZoneInfo("UTC")


def _topstep_trade_date(utc_dt: datetime) -> str:
    """TopStep trading date for a UTC timestamp. Day resets at CT 17:00 (CME new session)."""
    aware = utc_dt.replace(tzinfo=_UTC_TZ) if utc_dt.tzinfo is None else utc_dt
    ct_dt = aware.astimezone(_CT)
    if ct_dt.hour >= 17:
        return (ct_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    return ct_dt.strftime("%Y-%m-%d")


class BacktestEngine:
    """回測引擎 v3 — Session Zone + SessionTrendFollow"""

    # Default fallbacks. Real values set per-instance from contract_id below.
    POINT_VALUE = 20.0
    TICK_SIZE = 0.25
    TRAIL_TICK_STEP = 5
    # PT 12:45 = UTC 19:45
    FLATTEN_TIME_UTC = time(19, 45)
    PRE_FLATTEN_UTC = time(19, 30)

    def __init__(self, config: Optional[BacktestConfig] = None,
                 strategy_params: Optional[StrategyParams] = None,
                 zone_timeline: Optional[List[dict]] = None):
        self.config = config or BacktestConfig()
        self.strategy_params = strategy_params or StrategyParams()

        # Resolve contract specs once. NQ=$20, MNQ=$2; tick size 0.25 for both.
        _cid = getattr(self.strategy_params, "contract_id", "") or "CON.F.US.MNQ.M26"
        self.contract_id = _cid
        self.contract_size = max(1, int(getattr(self.strategy_params, "contract_size", 1) or 1))
        # Per-instance values shadow class defaults so multi-contract ML scans work.
        self.POINT_VALUE = get_point_value(_cid)
        self.TICK_SIZE = get_tick_size(_cid)

        # Session-based zone detector (skipped when zone_timeline is provided)
        self.detector = SessionZoneDetector(
            value_area_pct=self.config.value_area_pct,
            skip_stability_wait=getattr(self.strategy_params, "skip_zone_stability", True),
        )
        # Strategy selection
        _strat = (self.strategy_params.strategy or "trend").lower()
        if _strat == "macd":
            from backend.strategy.macd_strategy import MACDOnlyStrategy
            self.trend_follow = MACDOnlyStrategy(params=self.strategy_params)
        elif _strat == "reversion":
            from backend.strategy.reversion import SessionReversion
            self.trend_follow = SessionReversion(params=self.strategy_params)
        elif _strat == "trend_reversion":
            from backend.strategy.reversion import SessionTrendReversion
            self.trend_follow = SessionTrendReversion(params=self.strategy_params)
        else:
            self.trend_follow = SessionTrendFollow(params=self.strategy_params)

        # Pre-computed zone timeline (set once for ML optimizer — avoids re-running detection)
        self._zone_timeline: Optional[List[dict]] = zone_timeline
        self._zi: int = 0  # current index into zone_timeline

        # State
        self._capital = self.config.initial_capital
        self._open_position: Optional[Trade] = None
        self._pending_order: Optional[TradeSignal] = None
        self._pending_age: int = 0
        self._pending_max_age: int = self.trend_follow.PENDING_TIMEOUT_CANDLES
        self._trades: List[Trade] = []
        self._equity_curve: List[Tuple[datetime, float]] = []
        self._daily_pnl: Dict[str, float] = {}
        self._last_closed_trade: Optional[Trade] = None
        # Trailing SL state (forced ON — one-time trigger per position)
        self._trail_sl_triggered: bool = False
        # Max profit lock (resets at PT 22:00)
        self._max_profit_lock: int = getattr(self.strategy_params, 'max_profit_lock', 0) or 0
        self._profit_lock_pnl: float = 0.0
        self._profit_lock_ts_date: str = ""
        # Active post-breakout trackers (one per recently-entered trade).
        # We keep tracking even after the trade exits, since the user wants to
        # know whether price would have reached TP within 60m even if SL fired first.
        self._breakout_trackers: List[dict] = []

    @classmethod
    def _floor_ticks_to_step(cls, ticks: float) -> int:
        try:
            n = abs(float(ticks))
        except (TypeError, ValueError):
            return 0
        return int(n // cls.TRAIL_TICK_STEP) * cls.TRAIL_TICK_STEP

    def _resolved_trail_ticks(self) -> int:
        sl_ticks = abs(int(getattr(self.strategy_params, 'sl_ticks', 50) or 50))
        tp_ticks = abs(int(getattr(self.strategy_params, 'tp_ticks', 0) or 0))
        trail_ticks = int(getattr(self.strategy_params, 'trail_sl_ticks', 5) or 0)
        trigger_pct = getattr(self.strategy_params, 'trail_trigger_pct', 0.30)
        if trigger_pct > 1:
            trigger_pct = trigger_pct / 100.0
        if trigger_pct <= 0:
            return 0

        max_positive = max(0, self._floor_ticks_to_step(tp_ticks * trigger_pct) - self.TRAIL_TICK_STEP)
        return max(-sl_ticks, min(min(tp_ticks, max_positive), trail_ticks))

    def run(self, candles: List[Candle]) -> BacktestResult:
        """執行回測 (1m candles)"""
        logger.info(
            f"開始回測: {len(candles)} 根 K 線, "
            f"初始資金=${self._capital:,.0f}"
        )
        self._reset()

        # Ensure chronological order (API may return newest-first).
        # Skip when using zone_timeline — ml_run pre-sorts once so _zi indices stay aligned.
        if self._zone_timeline is None:
            candles = sorted(candles, key=lambda c: c.timestamp)

        # Live-edge guard: cancel pending + block new signals for the last N candles.
        # Prevents phantom 0/1-min trades when the backtest reaches real-time and
        # there is not enough future data for a trade to develop normally.
        _live_edge_guard = self._pending_max_age + 2
        total = len(candles)

        for i, candle in enumerate(candles):
            remaining = total - i - 1
            if not self._near_data_end and remaining < _live_edge_guard:
                self._near_data_end = True
                if self._pending_order:
                    logger.debug(
                        f"[LiveEdge] 距末尾 {remaining} 根K線，取消掛單，封鎖新信號"
                    )
                    self._cancel_pending_order()
            self._process_candle(candle)

        if self._open_position:
            self._force_exit(candles[-1], ExitReason.FLATTEN)

        # Close any remaining active zone at end of backtest (skip when using timeline)
        if candles and self._zone_timeline is None:
            self.detector.close_final_zone(candles[-1])

        # Flush any 60m post-breakout windows that didn't naturally close.
        self._finalize_breakout_trackers()

        from backend.backtest.metrics import MetricsCalculator
        calc = MetricsCalculator()
        metrics = calc.calculate_all(self._trades, self.config.initial_capital)

        # ML fast runs don't need zone data for chart rendering
        all_zones = [] if self._zone_timeline is not None else self.detector.get_all_zones()

        result = BacktestResult(
            config=self.config,
            trades=self._trades,
            zones=all_zones,
            metrics=metrics,
            equity_curve=self._equity_curve,
        )

        logger.info(
            f"回測完成: {metrics.total_trades} 筆交易, "
            f"勝率={metrics.win_rate:.1%}, PnL=${metrics.total_pnl:,.0f}"
        )
        return result

    def _reset(self):
        self._capital = self.config.initial_capital
        self._open_position = None
        self._pending_order = None
        self._pending_age = 0
        self._trades = []
        self._equity_curve = []
        self._daily_pnl = {}
        self._last_closed_trade = None
        self._trail_sl_triggered = False
        self._profit_lock_pnl = 0.0
        self._profit_lock_ts_date = ""
        self._breakout_trackers = []
        self._near_data_end = False   # live-edge guard flag
        self._zi = 0                  # zone timeline index
        if self._zone_timeline is None:
            self.detector.reset()
        self.trend_follow.reset()

    def _process_candle(self, candle: Candle):
        self._equity_curve.append((candle.timestamp, self._capital))

        # Advance any active 60m post-breakout trackers BEFORE we touch
        # position state — they keep tracking even after the trade exits.
        if self._breakout_trackers:
            self._update_breakout_trackers(candle)

        # ── Zone state: either live detector or pre-computed timeline ──
        if self._zone_timeline is not None:
            # Fast path: look up pre-computed state, skip expensive detector
            _zt = self._zone_timeline[self._zi] if self._zi < len(self._zone_timeline) else {}
            self._zi += 1
            _active_zone = _zt.get('active')
            _is_mature   = _zt.get('mature', False)
            _last_left   = _zt.get('last_left')
        else:
            # Normal path: run detector live
            self.detector.update(candle)

        # Daily loss limit
        date_str = candle.timestamp.strftime("%Y-%m-%d")
        daily = self._daily_pnl.get(date_str, 0)
        if daily <= -self.config.max_daily_loss:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            return

        # Max profit lock — block new trades when daily PnL ≥ threshold (resets PT 22:00)
        if self._max_profit_lock > 0:
            ts_date = _topstep_trade_date(candle.timestamp)
            if ts_date != self._profit_lock_ts_date:
                self._profit_lock_pnl = 0.0
                self._profit_lock_ts_date = ts_date
            if self._profit_lock_pnl >= self._max_profit_lock:
                if self._pending_order:
                    self._cancel_pending_order()
                if self._open_position:
                    self._check_exit(candle)
                    if self._open_position:
                        self._check_trailing_sl(candle)
                return

        # Flatten time (UTC 19:45 = PT 12:45)
        # Only flatten between 19:45-21:59 UTC (not after 22:00 = new session)
        candle_time = candle.timestamp.time()
        SESSION_START = time(22, 0)  # 22:00 UTC = new session

        in_flatten_window = (
            candle_time >= self.FLATTEN_TIME_UTC and candle_time < SESSION_START
        )
        if in_flatten_window:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            return  # no new trades during flatten, but detector already updated

        # Pre-flatten: cancel pending (UTC 19:30 = PT 12:30)
        in_pre_flatten = (
            candle_time >= self.PRE_FLATTEN_UTC and candle_time < SESSION_START
        )
        if in_pre_flatten and self._pending_order:
            logger.debug("收盤前取消掛單")
            self._cancel_pending_order()

        # Check SL/TP on open position
        if self._open_position:
            self._check_exit(candle)
            if self._open_position:
                # ── Trailing SL: trigger at configured TP%, then move SL from entry ──
                self._check_trailing_sl(candle)
                return  # still open, don't open new

        # ── Check if pending limit order fills on this candle ──
        if self._pending_order and not self._open_position:
            filled = self._check_pending_fill(candle)
            if filled:
                return
            # Check expiry
            self._pending_age += 1
            if self._pending_age > self._pending_max_age:
                logger.debug(f"掛單超時取消: {self._pending_order.reason}")
                self._cancel_pending_order()

        # ── Strategy evaluation ──
        if not self._open_position and not self._pending_order:
            if self._near_data_end:
                return   # no new entries near live edge

            if self._zone_timeline is not None:
                # Fast path: zones already looked up above
                eval_zone   = _active_zone
                eval_mature = _is_mature
                zone_source = "current"
                if not eval_mature and _last_left:
                    eval_zone   = _last_left
                    eval_mature = True
                    zone_source = "previous"
            else:
                # Normal path
                active_zone = self.detector.get_active_zone()
                is_mature   = self.detector.is_zone_mature
                eval_zone   = active_zone
                eval_mature = is_mature
                zone_source = "current"
                if not is_mature:
                    prev = self.detector.get_last_left_zone()
                    if prev:
                        eval_zone   = prev
                        eval_mature = True
                        zone_source = "previous"

            signal = self.trend_follow.evaluate(candle, eval_zone, eval_mature)
            if signal:
                signal.zone_source = zone_source
                if getattr(signal, 'order_type', 'limit') == 'market':
                    # Market order: execute immediately at candle close
                    self._execute_entry(signal, candle)
                    if self._open_position:
                        self._check_sl_only(candle)
                else:
                    self._place_pending_order(signal, candle)
                return

    def _check_exit(self, candle: Candle):
        """Check SL/TP exit with open-price heuristic for same-candle ambiguity.

        When a single candle hits BOTH SL and TP, we use the open price
        to infer which was hit first:
          - Open closer to SL → price likely moved away from SL first → TP hit first
          - Open closer to TP → price likely moved away from TP first → SL hit first
        When only one side is hit, there's no ambiguity.
        """
        pos = self._open_position
        if not pos:
            return

        if pos.direction == Direction.BUY:
            hit_sl = candle.low <= pos.sl_price
            hit_tp = candle.high >= pos.tp_price
            if hit_sl and hit_tp:
                # Ambiguous — use open to decide
                dist_sl = abs(candle.open - pos.sl_price)
                dist_tp = abs(candle.open - pos.tp_price)
                if dist_sl <= dist_tp:
                    # Open near SL → likely went up first → TP hit first
                    self._execute_exit(candle, pos.tp_price, ExitReason.TP)
                else:
                    # Open near TP → likely went down first → SL hit first
                    self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())
            elif hit_sl:
                self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())
            elif hit_tp:
                self._execute_exit(candle, pos.tp_price, ExitReason.TP)
        else:  # SELL
            hit_sl = candle.high >= pos.sl_price
            hit_tp = candle.low <= pos.tp_price
            if hit_sl and hit_tp:
                dist_sl = abs(candle.open - pos.sl_price)
                dist_tp = abs(candle.open - pos.tp_price)
                if dist_sl <= dist_tp:
                    self._execute_exit(candle, pos.tp_price, ExitReason.TP)
                else:
                    self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())
            elif hit_sl:
                self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())
            elif hit_tp:
                self._execute_exit(candle, pos.tp_price, ExitReason.TP)

    def _check_sl_only(self, candle: Candle):
        """Entry candle: only check SL, skip TP.

        Limit buy fills when price drops to entry — the candle's high
        may be entirely pre-fill, so TP check would be false positive.
        Only SL (further drop after fill) is valid on entry candle.
        """
        pos = self._open_position
        if not pos:
            return
        if pos.direction == Direction.BUY:
            if candle.low <= pos.sl_price:
                self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())
        else:  # SELL
            if candle.high >= pos.sl_price:
                self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())

    def _stop_exit_reason(self) -> ExitReason:
        return ExitReason.TRAIL_SL if self._trail_sl_triggered else ExitReason.SL

    def _cancel_pending_order(self):
        """Cancel a pending limit order and notify the strategy."""
        if self._pending_order:
            self.trend_follow.notify_order_cancelled()
        self._pending_order = None
        self._pending_age = 0

    def _place_pending_order(self, signal: TradeSignal, candle: Candle):
        """Place a limit order — will fill on a future candle when price touches."""
        self._pending_order = signal
        self._pending_age = 0
        logger.debug(
            f"掛單: {signal.strategy.value} {signal.direction.value} "
            f"limit @ {signal.entry_price:.2f} | SL={signal.sl_price:.2f} TP={signal.tp_price:.2f}"
        )

    def _check_pending_fill(self, candle: Candle) -> bool:
        """Check if the pending limit order fills on this candle."""
        order = self._pending_order
        if not order:
            return False

        filled = False
        if order.direction == Direction.BUY:
            if candle.low <= order.entry_price:
                filled = True
        else:
            if candle.high >= order.entry_price:
                filled = True

        if filled:
            self._execute_entry(order, candle)
            self._pending_order = None
            self._pending_age = 0
            # On the ENTRY candle, only check SL — never TP.
            # Reason: limit buy fills when price DROPS to entry.
            # The candle's high might be BEFORE the fill (pre-entry).
            # TP requires price to move in our favor AFTER entry,
            # which we can only confirm on the NEXT candle.
            if self._open_position:
                self._check_sl_only(candle)
            return True

        return False

    def _execute_entry(self, signal: TradeSignal, candle: Candle):
        slippage = self.config.slippage_ticks * self.TICK_SIZE
        if signal.direction == Direction.BUY:
            fill_price = signal.entry_price + slippage
        else:
            fill_price = signal.entry_price - slippage

        trade = Trade(
            trade_id=f"T{uuid.uuid4().hex[:8]}",
            strategy=signal.strategy,
            direction=signal.direction,
            entry_price=fill_price,
            entry_time=candle.timestamp,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
            zone_id=signal.zone_id,
            zone_source=getattr(signal, 'zone_source', None),
            contracts=self.contract_size,
            point_value=self.POINT_VALUE,
            contract_id=self.contract_id,
            vol_ratio=signal.vol_ratio,
            is_big_trend=signal.is_big_trend,
            breakout_range=signal.breakout_range,
            macd_hist=getattr(signal, 'macd_hist', None),
        )
        self._open_position = trade
        self._trail_sl_triggered = False

        # Spawn a 60m post-breakout tracker. We track price action for
        # POST_BREAKOUT_WINDOW_MIN minutes regardless of when (or whether)
        # the trade actually exits — the user wants to know how price
        # behaved within 1h after breakout, not just up to the exit.
        trail_ticks = self._resolved_trail_ticks()
        trail_pts = trail_ticks * self.TICK_SIZE
        if signal.direction == Direction.BUY:
            trail_lvl = fill_price + trail_pts
        else:
            trail_lvl = fill_price - trail_pts
        self._breakout_trackers.append({
            "trade": trade,
            "direction": signal.direction,
            "entry_price": fill_price,
            "sl_price": signal.sl_price,
            "tp_price": signal.tp_price,
            "trail_lvl": trail_lvl,        # entry ± trail_sl_ticks×tick (where trail-SL would sit)
            "deadline": candle.timestamp + timedelta(minutes=POST_BREAKOUT_WINDOW_MIN),
            "max_fav_ticks": 0.0,
            "max_adv_ticks": 0.0,
            "ever_hit_trail": False,
            "ever_hit_sl": False,
            "ever_hit_tp": False,
            "first_event": None,            # one of "trail" / "sl" / "tp" / None
        })

        logger.debug(
            f"入場: {trade.strategy.value} {trade.direction.value} "
            f"@ {fill_price:.2f} | SL={trade.sl_price:.2f} TP={trade.tp_price:.2f}"
        )

    def _update_breakout_trackers(self, candle: Candle):
        """Advance each active post-breakout tracker with this candle's range.

        Updates MFE/MAE and detects which level (trail / sl / tp) is crossed
        first. When a candle straddles multiple levels, the open-distance
        heuristic (open closer to high vs low) decides which extreme came
        first — same heuristic _check_exit uses for SL/TP ambiguity.
        """
        if not self._breakout_trackers:
            return

        keep: List[dict] = []
        for tr in self._breakout_trackers:
            # The entry candle itself is the "breakout" candle — start tracking
            # from the NEXT candle. Skip if candle.timestamp == entry_time.
            if candle.timestamp <= tr["trade"].entry_time:
                keep.append(tr)
                continue

            # Window expired → finalize and write back to the trade.
            if candle.timestamp >= tr["deadline"]:
                t = tr["trade"]
                t.post_breakout_max_favorable_ticks = round(tr["max_fav_ticks"], 2)
                t.post_breakout_max_adverse_ticks   = round(tr["max_adv_ticks"], 2)
                t.post_breakout_reached_tp          = bool(tr["ever_hit_tp"])
                t.post_breakout_broke_trail_first   = (tr["first_event"] == "trail")
                t.post_breakout_broke_sl_first      = (tr["first_event"] == "sl")
                continue  # do not re-add — tracker is done

            entry = tr["entry_price"]
            direction = tr["direction"]

            # MFE / MAE in ticks for this candle's range.
            if direction == Direction.BUY:
                fav = (candle.high - entry) / self.TICK_SIZE
                adv = (entry - candle.low) / self.TICK_SIZE
            else:
                fav = (entry - candle.low) / self.TICK_SIZE
                adv = (candle.high - entry) / self.TICK_SIZE
            if fav > tr["max_fav_ticks"]:
                tr["max_fav_ticks"] = fav
            if adv > tr["max_adv_ticks"]:
                tr["max_adv_ticks"] = adv

            # Detect level crossings during this candle.
            sl_p = tr["sl_price"]
            tp_p = tr["tp_price"]
            trail_p = tr["trail_lvl"]
            if direction == Direction.BUY:
                hit_sl = candle.low <= sl_p
                hit_tp = candle.high >= tp_p
                hit_trail = candle.low <= trail_p   # adverse retrace through trail-SL level
                # Heuristic: if open is closer to high, low likely came first (adverse first).
                open_to_high = candle.high - candle.open
                open_to_low  = candle.open - candle.low
                adverse_first = open_to_high <= open_to_low
            else:
                hit_sl = candle.high >= sl_p
                hit_tp = candle.low <= tp_p
                hit_trail = candle.high >= trail_p
                open_to_high = candle.high - candle.open
                open_to_low  = candle.open - candle.low
                adverse_first = open_to_low <= open_to_high

            if hit_sl:
                tr["ever_hit_sl"] = True
            if hit_tp:
                tr["ever_hit_tp"] = True
            if hit_trail:
                tr["ever_hit_trail"] = True

            if tr["first_event"] is None:
                # Order events within this candle. SL is more adverse than trail
                # (trail sits between entry and SL), so SL implies trail too.
                # If only one event fires, that's the first event. If multiple
                # fire on this candle, use adverse_first to decide whether
                # adverse (sl/trail) or favorable (tp) came first.
                events_adverse = []
                if hit_sl:
                    events_adverse.append("sl")
                elif hit_trail:
                    events_adverse.append("trail")
                events_fav = ["tp"] if hit_tp else []

                if events_adverse and events_fav:
                    tr["first_event"] = events_adverse[0] if adverse_first else events_fav[0]
                elif events_adverse:
                    tr["first_event"] = events_adverse[0]
                elif events_fav:
                    tr["first_event"] = events_fav[0]

            keep.append(tr)

        self._breakout_trackers = keep

    def _finalize_breakout_trackers(self):
        """End-of-backtest: flush any trackers whose 60m window did not close
        because the data ran out. Whatever stats we accumulated still go on
        the trade — partial windows are better than no signal at all."""
        for tr in self._breakout_trackers:
            t = tr["trade"]
            t.post_breakout_max_favorable_ticks = round(tr["max_fav_ticks"], 2)
            t.post_breakout_max_adverse_ticks   = round(tr["max_adv_ticks"], 2)
            t.post_breakout_reached_tp          = bool(tr["ever_hit_tp"])
            t.post_breakout_broke_trail_first   = (tr["first_event"] == "trail")
            t.post_breakout_broke_sl_first      = (tr["first_event"] == "sl")
        self._breakout_trackers = []

    def _check_trailing_sl(self, candle: Candle):
        """Trailing SL (opt-in via strategy_params.trail_enabled, default ON):
        if price moves enough to reach the configured fraction of TP, move SL
        to entry +/- trail_sl_ticks.
        trail_sl_ticks=5 (default) → new SL = entry ± 5 ticks locked profit.
        One-time trigger per position.

        Disabling lets the trade run all the way to TP or full SL — useful when
        post-breakout stats show many trades dipping back through the trail
        level before reaching TP (a high TP↶TRAIL count means trail is
        cutting off would-be winners).
        """
        if not getattr(self.strategy_params, 'trail_enabled', True):
            return
        if self._trail_sl_triggered:
            return
        pos = self._open_position
        if not pos:
            return
        mkt = candle.close
        if pos.direction == Direction.BUY:
            ticks_moved = (mkt - pos.entry_price) / self.TICK_SIZE
        else:
            ticks_moved = (pos.entry_price - mkt) / self.TICK_SIZE

        tp_ticks = abs(int(getattr(self.strategy_params, 'tp_ticks', 0) or 0))
        trigger_pct = getattr(self.strategy_params, 'trail_trigger_pct', 0.30)
        if trigger_pct > 1:
            trigger_pct = trigger_pct / 100.0
        if trigger_pct <= 0:
            return
        trigger_ticks = max(1.0, tp_ticks * trigger_pct)
        if ticks_moved >= trigger_ticks:
            self._trail_sl_triggered = True
            trail_ticks = self._resolved_trail_ticks()
            trail_pts = trail_ticks * self.TICK_SIZE
            if pos.direction == Direction.BUY:
                pos.sl_price = pos.entry_price + trail_pts
            else:
                pos.sl_price = pos.entry_price - trail_pts
            logger.debug(
                f"Trail SL: {ticks_moved:.1f} ticks moved → SL moved to {pos.sl_price:.2f} "
                f"({trail_ticks}t from entry, trigger={trigger_pct:.0%} TP)"
            )

    def _execute_exit(self, candle: Candle, exit_price: float, reason: ExitReason):
        pos = self._open_position
        if not pos:
            return

        slippage = self.config.slippage_ticks * self.TICK_SIZE
        if reason in (ExitReason.SL, ExitReason.TRAIL_SL):
            if pos.direction == Direction.BUY:
                exit_price -= slippage
            else:
                exit_price += slippage
        else:
            if pos.direction == Direction.BUY:
                exit_price -= slippage
            else:
                exit_price += slippage

        # Use the per-trade point_value (set on entry) so multi-contract
        # backtests with different specs (e.g. NQ vs MNQ) PnL correctly.
        pt_val = getattr(pos, "point_value", self.POINT_VALUE) or self.POINT_VALUE
        if pos.direction == Direction.BUY:
            gross_pnl = (exit_price - pos.entry_price) * pt_val * pos.contracts
        else:
            gross_pnl = (pos.entry_price - exit_price) * pt_val * pos.contracts

        # Deduct round-turn commission + exchange/regulatory fees per contract
        commission = self.config.commission_rt * pos.contracts
        fees = self.config.fees_rt * pos.contracts
        pnl = gross_pnl - commission - fees

        pos.exit_price = exit_price
        pos.exit_time = candle.timestamp
        pos.pnl = pnl                 # NET (gross − commission − fees)
        pos.commission = commission
        pos.fees = fees
        pos.exit_reason = reason

        self._capital += pnl
        date_str = candle.timestamp.strftime("%Y-%m-%d")
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0) + pnl
        if self._max_profit_lock > 0:
            ts_date = _topstep_trade_date(candle.timestamp)
            if ts_date != self._profit_lock_ts_date:
                self._profit_lock_pnl = 0.0
                self._profit_lock_ts_date = ts_date
            self._profit_lock_pnl += pnl

        self._trades.append(pos)
        self._last_closed_trade = pos
        self._open_position = None
        self._trail_sl_triggered = False

        # Notify strategy of trade close
        self.trend_follow.notify_trade_closed(reason.value)

        logger.debug(
            f"出場: {reason.value} @ {exit_price:.2f} | "
            f"PnL=${pnl:+.0f} | 資金=${self._capital:,.0f}"
        )

    def _force_exit(self, candle: Candle, reason: ExitReason):
        # If trail SL was already triggered (profit locked), classify the forced
        # exit as TRAIL_SL — the trail mechanism is what protected this trade,
        # the clock just happened to end the day before the stop was touched.
        # Without this, profitable trail-protected positions closed by 12:45 PT
        # auto-flatten end up in the 'other' bucket and disappear from
        # TP/SL/TRAIL counts and AVG $ stats.
        if reason == ExitReason.FLATTEN and self._trail_sl_triggered:
            reason = ExitReason.TRAIL_SL
        self._execute_exit(candle, candle.close, reason)

    @staticmethod
    def aggregate_1m_to_5m(candles_1m: List[Candle]) -> List[Candle]:
        if not candles_1m:
            return []
        buckets: Dict[datetime, List[Candle]] = {}
        for c in candles_1m:
            minute = c.timestamp.minute
            aligned_minute = (minute // 5) * 5
            bucket_time = c.timestamp.replace(
                minute=aligned_minute, second=0, microsecond=0
            )
            if bucket_time not in buckets:
                buckets[bucket_time] = []
            buckets[bucket_time].append(c)

        candles_5m = []
        for ts in sorted(buckets.keys()):
            group = buckets[ts]
            candles_5m.append(Candle(
                timestamp=ts,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
                symbol=group[0].symbol,
                interval="5m",
            ))
        return candles_5m
