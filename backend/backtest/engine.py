# ============================================================

# 文件: backend/backtest/engine.py
# 狀態: v1.0.6
# 功能 / Features:
#   - Completed 1m candle backtest engine for the trend strategy.
#   - Simulates limit/market entry, SL, TP, trail SL, pending timeout, and close-window flatten.
#   - Uses dynamic NQ/MNQ point value, contract size, commission, and fees.
#   - Full TP lock blocks new entries after N full TP exits until the next Topstep session.
#   - Value Area is locked to 80%, matching live mode.
# ============================================================

from __future__ import annotations
import uuid
import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from backend.db.models import (
    Candle, Trade, TradeSignal, BacktestConfig, BacktestResult,
    Metrics, ConsolidationZone, BreakoutAnalysis, StrategyParams,
    Direction, ExitReason, StrategyType, ZoneStatus,
    FACTOR_PIPELINE_STRATEGIES, ZONELESS_STRATEGIES, ZONELESS_ZONE_RENDER,
    get_point_value, get_tick_size,
)
from backend.strategy.consolidation import SessionZoneDetector, build_zone_detector
from backend.strategy.session_filter import (
    DEFAULT_ALLOWED_SESSIONS, MARKET_PHASE_FLATTEN,
    MARKET_PHASE_PRE_FLATTEN, allowed_sessions_label, is_allowed_session,
    market_close_phase,
)
from backend.strategy.sigma import RollingSigmaFade
from backend.strategy.factor import FactorSignalStrategy
from backend.strategy.fade import PrevDayFade, OpeningRangeFade  # 1.0.8 FADE / 1.0.9 OR15 假突破
from backend.strategy.volume_profile import VolumeProfileCalculator  # 1.0.8: fade 前日 VP
from backend.backtest.intrabar import resolve_same_bar_exit

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
    """回測引擎 v3 — Session Zone + 策略插槽(fade / sigma / pmo / factor)"""

    # Default fallbacks. Real values set per-instance from contract_id below.
    POINT_VALUE = 20.0
    TICK_SIZE = 0.25
    TRAIL_TICK_STEP = 5
    CLOSE_WINDOW_ENABLED = True

    def __init__(self, config: Optional[BacktestConfig] = None,
                 strategy_params: Optional[StrategyParams] = None,
                 zone_timeline: Optional[List[dict]] = None,
                 record_equity: bool = True,
                 pi_replay_rows: Optional[List[dict]] = None):
        # record_equity=False skips the per-candle equity curve. Machine-learning
        # grid runs (186 combos × up to 32 parallel workers) don't use the equity
        # curve — metrics come from trades — and on full-range data (hundreds of
        # thousands of 1m bars) the per-candle list is the dominant RAM hog.
        self._record_equity = record_equity
        self.config = config or BacktestConfig()
        self.strategy_params = strategy_params or StrategyParams()
        # Run-scoped Live PI audit overlay.  It is intentionally passed only
        # by the explicit PI Backtest route and never written to history.
        self.pi_replay_rows = list(pi_replay_rows or [])

        # Resolve contract specs once. NQ=$20, MNQ=$2; tick size 0.25 for both.
        _cid = getattr(self.strategy_params, "contract_id", "") or "CON.F.US.MNQ.M26"
        self.contract_id = _cid
        self.contract_size = max(1, int(getattr(self.strategy_params, "contract_size", 1) or 1))
        # Per-instance values shadow class defaults so machine-learning contract scans stay isolated.
        self.POINT_VALUE = get_point_value(_cid)
        self.TICK_SIZE = get_tick_size(_cid)

        # v1.0.6: value-area width + area timeframe are selectable per run.
        _va_pct = float(getattr(self.strategy_params, "value_area_pct", 0.80) or 0.80)
        self.config.value_area_pct = _va_pct
        _area_tf = getattr(self.strategy_params, "area_timeframe", "5m") or "5m"
        _method = str(getattr(self.strategy_params, "method", "single") or "single").lower()
        _tf_combo = list(getattr(self.strategy_params, "tf_combo", None) or [])
        _overlap_combo = _tf_combo if _method == "overlap" and len(_tf_combo) >= 2 else None

        # Clock-bucket zone detector — keeps the recent 10 reference zones.
        # (skipped when a pre-computed zone_timeline is provided)
        self.detector = build_zone_detector(
            area_timeframe=_area_tf,
            value_area_pct=_va_pct,
            tick_size=self.TICK_SIZE,
            max_recent=10,
            tf_combo=_overlap_combo,
            overlap_trade_tf=getattr(self.strategy_params, "tr_overlap_trade_tf", "merged"),
        )
        # 1.0.8: strategy_mode "trend"(現行)或 "fade"(前日 VA 回歸)。
        # 屬性名沿用 trend_follow,兩策略介面相容,其餘管線不變。
        _strat = str(getattr(self.strategy_params, "strategy", "") or "").lower()
        self.strategy_mode = _strat if _strat in ("fade", "sigma", "factor", "momentum", "betafib", "pi") else "factor"
        if self.strategy_mode == "fade":
            # 1.0.9: fade_entry_mode="or15" → 15m 開盤區間假突破(雙向);其餘走前日 VA fade
            if str(getattr(self.strategy_params, "fade_entry_mode", "") or "").lower() == "or15":
                self.trend_follow = OpeningRangeFade(params=self.strategy_params)
            else:
                self.trend_follow = PrevDayFade(params=self.strategy_params)
        elif self.strategy_mode == "sigma":
            self.trend_follow = RollingSigmaFade(params=self.strategy_params)
        elif self.strategy_mode == "factor":
            self.trend_follow = FactorSignalStrategy(params=self.strategy_params)
        # 1.0.10: PI —— 外部 Discord 訊號驅動。進場時機來自推播,
        # 出場/風控/下單路徑與其他策略完全共用。
        elif self.strategy_mode == "pi":
            from backend.strategy.pi_signal import PiSignalStrategy
            self.trend_follow = PiSignalStrategy(
                params=self.strategy_params,
                replay_rows=self.pi_replay_rows,
            )
        # 1.0.9: INTRAMOM —— 研究驗證通過的外部策略(見
        # docs/1.0.9_RESEARCH_FINDINGS.md)。實作在 research_lab.py,
        # 介面與 fade/factor 相同,直接插進同一個策略插槽。
        elif self.strategy_mode == "momentum":
            from backend.strategy.research_lab import MomentumContinuation
            self.trend_follow = MomentumContinuation(params=self.strategy_params)
        # 1.0.9: SESSFIB —— RTH 推動腿的 fib 回撤位掛單,夜盤觸價後市價進場。
        # ⚠ 尚未通過 G5(MNQ 過 G0–G4、MES 死在走查)—— 觀察用,勿直接實盤。
        elif self.strategy_mode == "betafib":
            from backend.strategy.research_lab import BetaFibRetrace
            self.trend_follow = BetaFibRetrace(params=self.strategy_params)
        # 1.0.9: TREND(SessionTrendFollow)已移除 —— 288 個變體 0 通過
        # MC+WF+PF>2,每筆邊際最佳 +9.6t 低於實測 14t 往返滑價。
        # 舊 preset 若仍帶 strategy="trend",一律落到 FACTOR。
        # 詳見 docs/1.0.9_DELETE_LIST.md。
        else:
            self.trend_follow = FactorSignalStrategy(params=self.strategy_params)
        # 1.0.8: fade 模式 — 前日 VP 水位計算狀態
        self._fade_vp = VolumeProfileCalculator(self.TICK_SIZE, float(self.config.value_area_pct))
        self._fade_day: Optional[str] = None
        self._fade_day_candles: List[Candle] = []
        self._fade_level_zones: List[ConsolidationZone] = []
        self._fade_active_level_zone: Optional[ConsolidationZone] = None
        # 1.0.8: 出場模式("tp" 固定 TP | "ladder" 無 TP 階梯滾動)
        self._tr_exit_mode = (
            "ladder"
            if str(getattr(self.strategy_params, "tr_exit_mode", "tp") or "tp").lower() == "ladder"
            else "tp"
        )
        self.LADDER_TRIGGER_R = 2.0   # 浮盈達 2R 啟動(SL→entry)
        self.LADDER_GAP_R = 2.0       # 之後每 +1R 跟 1R,恆落後峰值整數 2R
        self._ladder_risk: float = 0.0
        self._ladder_max_r: float = 0.0
        # 1.0.8: 日虧斷路器 — 當日虧損單數達 N 停新單(0=OFF)
        self._tr_daily_loss_stop = max(0, int(getattr(self.strategy_params, "tr_daily_loss_stop", 0) or 0))
        self._daily_loss_count: int = 0
        # 1.0.9: FULL WIN LOCK — 當日贏 N 單停新單(0=OFF)
        self._tr_daily_win_stop = max(0, int(getattr(self.strategy_params, "tr_daily_win_stop", 0) or 0))
        self._daily_win_count: int = 0
        # 1.0.9: PDPT —— 當日已實現獲利上限(美元),達標後停開新單
        self._tr_daily_profit_stop = max(0.0, float(
            getattr(self.strategy_params, "tr_daily_profit_stop", 0) or 0))
        self._daily_profit_td: float = 0.0
        self._loss_count_date: Optional[str] = None
        # 1.0.9: prevRV regime gate — 前一日 RV 落在近 N 日最高三分位 → 今日不進場
        # 1.0.10 BUG FIX:原本只有 strategy_mode == "factor" 才算出非零值,
        # 但下方 _process_candle 的時間出場閘門檢查的是 FACTOR_PIPELINE_STRATEGIES
        # (含 momentum / betafib)—— 閘門看起來支援它們,值卻永遠是 0。
        # 實測:MOMENTUM/BETAFIB 在 12/24/48 根四種設定下回傳**完全相同**的結果,
        # 就是這個靜默無效造成的;研究時會誤判成「時間出場對這兩族沒影響」。
        # 預設 factor_max_hold_bars=0,所以修正後生產行為不變。
        if self.strategy_mode in FACTOR_PIPELINE_STRATEGIES:
            # 兩族的「一根」定義不同:factor 用 factor_timeframe_minutes,
            # research_lab(momentum / betafib)用 research_tf_minutes。
            _tf = (int(getattr(self.strategy_params, "factor_timeframe_minutes", 5) or 5)
                   if self.strategy_mode == "factor"
                   else int(getattr(self.strategy_params, "research_tf_minutes", 5) or 5))
            self._pmo_max_hold_minutes = (
                max(0, int(getattr(self.strategy_params, "factor_max_hold_bars", 0) or 0))
                * max(1, _tf)
            )
        else:
            self._pmo_max_hold_minutes = 0

        # Pre-computed zone timeline (set once for machine learning grid runs)
        self._zone_timeline: Optional[List[dict]] = zone_timeline
        self._zi: int = 0  # current index into zone_timeline

        # State
        self._capital = self.config.initial_capital
        self._open_position: Optional[Trade] = None
        self._pending_order: Optional[TradeSignal] = None
        self._pending_age: int = 0
        self._pending_max_age: int = self.trend_follow.PENDING_TIMEOUT_CANDLES
        self._pending_lock_key: Optional[tuple[str, str]] = None
        self._trades: List[Trade] = []
        self._equity_curve: List[Tuple[datetime, float]] = []
        self._daily_pnl: Dict[str, float] = {}
        self._last_closed_trade: Optional[Trade] = None
        # Trailing SL state (forced ON — one-time trigger per position)
        self._trail_sl_triggered: bool = False
        # Full TP lock: stop new entries after N full TP exits in the same Topstep session.
        self._full_tp_lock: int = max(
            int(getattr(self.strategy_params, "full_tp_lock", 0) or 0),
            int(getattr(self.strategy_params, "tr_full_tp_lock", 0) or 0),
            int(getattr(self.strategy_params, "cd_full_tp_lock", 0) or 0),
        )
        self._full_tp_count: int = 0
        self._full_tp_counts: Dict[str, int] = {"tr": 0}
        self._full_tp_ts_date: str = ""
        self._one_trade_per_session_direction: bool = bool(
            getattr(self.strategy_params, "one_trade_per_session_direction", True)
        )
        self._tr_one_trade_per_session: bool = bool(
            getattr(self.strategy_params, "tr_one_trade_per_session", True)
        )
        self._tr_allowed_sessions = (
            getattr(self.strategy_params, "tr_allowed_sessions", DEFAULT_ALLOWED_SESSIONS)
            or None
        )
        self._session_direction_used: set[tuple[str, str]] = set()
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

    @staticmethod
    def _strategy_group(strategy) -> str:
        return "tr"

    def _strategy_param(self, strategy, suffix: str, fallback):
        key = self._strategy_group(strategy)
        prefixed = getattr(self.strategy_params, f"{key}_{suffix}", None)
        if prefixed is not None:
            return prefixed
        return getattr(self.strategy_params, suffix, fallback)

    def _strategy_trail_enabled(self, strategy) -> bool:
        return bool(self._strategy_param(strategy, "trail_enabled", True))

    def _strategy_trigger_pct(self, strategy) -> float:
        trigger_pct = self._strategy_param(strategy, "trail_trigger_pct", 0.30)
        if trigger_pct is None:
            trigger_pct = 0.30
        if trigger_pct > 1:
            trigger_pct = trigger_pct / 100.0
        return trigger_pct

    def _full_tp_lock_for_strategy(self, strategy) -> int:
        try:
            lock = int(self._strategy_param(strategy, "full_tp_lock", 0) or 0)
        except (TypeError, ValueError):
            lock = 0
        return max(0, min(3, lock))

    def _trend_session_allowed(self, ts: datetime) -> bool:
        return is_allowed_session(ts, self._tr_allowed_sessions)

    def _reset_trend_session_state(self) -> None:
        if hasattr(self.trend_follow, "reset_breakout_confirmation"):
            self.trend_follow.reset_breakout_confirmation()
        else:
            self.trend_follow.reset()

    def _reset_full_tp_counts_for_session(self, utc_dt: datetime) -> None:
        ts_date = _topstep_trade_date(utc_dt)
        if ts_date == self._full_tp_ts_date:
            return
        self._full_tp_ts_date = ts_date
        self._full_tp_counts = {"tr": 0}
        self._full_tp_count = 0

    def _signal_full_tp_locked(self, signal: TradeSignal, candle: Candle) -> bool:
        self._reset_full_tp_counts_for_session(candle.timestamp)
        lock = self._full_tp_lock_for_strategy(signal.strategy)
        if lock <= 0:
            return False
        key = self._strategy_group(signal.strategy)
        return self._full_tp_counts.get(key, 0) >= lock

    def _resolved_trail_ticks(self, strategy=None) -> int:
        sl_ticks = abs(int(self._strategy_param(strategy, 'sl_ticks', 50) or 50))
        tp_ticks = abs(int(self._strategy_param(strategy, 'tp_ticks', 0) or 0))
        trail_ticks = int(self._strategy_param(strategy, 'trail_sl_ticks', 5) or 0)
        trigger_pct = self._strategy_trigger_pct(strategy)
        if trigger_pct <= 0:
            return 0

        max_positive = max(0, self._floor_ticks_to_step(tp_ticks * trigger_pct) - self.TRAIL_TICK_STEP)
        return max(0, min(min(tp_ticks, max_positive), trail_ticks))

    def run(self, candles: List[Candle], progress_cb=None) -> BacktestResult:
        """執行回測 (1m candles)

        progress_cb(current, total, detail) — optional, fired on date change so
        a caller (routes.py) can report progress to the UI while run() executes
        inside a worker thread. Kept lightweight; never raises into the loop.
        """
        logger.info(
            f"Backtest started: {len(candles)} candles, "
            f"initial capital=${self._capital:,.0f}"
        )
        self._reset()

        # Ensure chronological order (API may return newest-first).
        # Skip when using zone_timeline; machine-learning grid runs pre-sort once so _zi indices stay aligned.
        if self._zone_timeline is None:
            candles = sorted(candles, key=lambda c: c.timestamp)

        # Live-edge guard: cancel pending + block new signals for the last N candles.
        # Prevents phantom 0/1-min trades when the backtest reaches real-time and
        # there is not enough future data for a trade to develop normally.
        _live_edge_guard = self._pending_max_age + 2
        total = len(candles)

        # Date progress only for a normal single backtest. The ML grid sweep runs
        # with a precomputed zone timeline (hundreds of runs) — stay silent there.
        _log_progress = self._zone_timeline is None and total > 0
        _prev_date = None

        for i, candle in enumerate(candles):
            remaining = total - i - 1
            if not self._near_data_end and remaining < _live_edge_guard:
                self._near_data_end = True
                if self._pending_order:
                    logger.debug(
                        f"[LiveEdge] {remaining} candles remain; cancelling the pending order "
                        "and blocking new signals"
                    )
                    self._cancel_pending_order()
            if _log_progress:
                _d = candle.timestamp.strftime("%Y-%m-%d")
                if _d != _prev_date:
                    _prev_date = _d
                    logger.info(
                        f"[Backtest] {_d} | progress {i + 1}/{total} ({(i + 1) * 100 // total}%) "
                        f"| trades {len(self._trades)}"
                    )
                    if progress_cb is not None:
                        try:
                            progress_cb(i + 1, total, f"{_d} | trades {len(self._trades)}")
                        except Exception:
                            pass
            self._process_candle(candle)

        if self._open_position:
            self._force_exit(candles[-1], ExitReason.FLATTEN)

        # Close any remaining active zone at end of backtest (skip timeline/sigma).
        if candles and self._zone_timeline is None and self.strategy_mode not in ZONELESS_STRATEGIES:
            self.detector.close_final_zone(candles[-1])
        if candles and self.strategy_mode == "fade":
            self._close_fade_level_zone(candles[-1].timestamp)

        # Flush any 60m post-breakout windows that didn't naturally close.
        self._finalize_breakout_trackers()

        from backend.backtest.metrics import MetricsCalculator
        calc = MetricsCalculator()
        metrics = calc.calculate_all(self._trades, self.config.initial_capital)

        # Timeline/sigma modes do not render detector zones. Fade renders the
        # previous-day VAH/VAL levels as day-wide chart reference lines.
        if self.strategy_mode == "fade":
            all_zones = self._fade_level_zones
        elif self._zone_timeline is not None or self.strategy_mode in ZONELESS_ZONE_RENDER:
            all_zones = []
        else:
            all_zones = self.detector.get_all_zones()

        result = BacktestResult(
            config=self.config,
            trades=self._trades,
            zones=all_zones,
            metrics=metrics,
            equity_curve=self._equity_curve,
        )

        logger.info(
            f"Backtest complete: {metrics.total_trades} trades, "
            f"win rate={metrics.win_rate:.1%}, PnL=${metrics.total_pnl:,.0f}"
        )
        return result

    def _reset(self):
        self._capital = self.config.initial_capital
        self._open_position = None
        self._pending_order = None
        self._pending_age = 0
        self._pending_lock_key = None
        self._trades = []
        self._equity_curve = []
        self._daily_pnl = {}
        self._last_closed_trade = None
        self._trail_sl_triggered = False
        self._full_tp_count = 0
        self._full_tp_counts = {"tr": 0}
        self._full_tp_ts_date = ""
        self._session_direction_used = set()
        self._breakout_trackers = []
        self._near_data_end = False   # live-edge guard flag
        self._zi = 0                  # zone timeline index
        self._fade_day = None
        self._fade_day_candles = []
        self._fade_level_zones = []
        self._fade_active_level_zone = None
        if self._zone_timeline is None:
            self.detector.reset()
        self.trend_follow.reset()

    def _close_fade_level_zone(self, end_ts: datetime) -> None:
        zone = self._fade_active_level_zone
        if zone is None or zone.left_at is not None:
            return
        zone.left_at = end_ts
        zone.status = ZoneStatus.LEFT
        try:
            zone.duration_minutes = max(0, int((end_ts - zone.formed_at).total_seconds() // 60))
        except Exception:
            zone.duration_minutes = 0

    def _add_fade_level_zone(self, trade_date: str, start_ts: datetime, vp) -> None:
        zone = ConsolidationZone(
            zone_id=f"FDLVL:{trade_date}",
            formed_at=start_ts,
            left_at=None,
            poc=vp.poc,
            vah_80=vp.vah,
            val_80=vp.val,
            high_100=vp.high_100,
            low_100=vp.low_100,
            total_volume=vp.total_volume,
            duration_minutes=0,
            num_candles=0,
            status=ZoneStatus.ACTIVE,
            exit_direction=None,
            mature=True,
            candles=[],
            timeframe="fade",
            profile={},
            va_bands={},
        )
        self._fade_level_zones.append(zone)
        self._fade_active_level_zone = zone

    def _process_candle(self, candle: Candle):
        if self._record_equity:
            self._equity_curve.append((candle.timestamp, self._capital))

        # 1.0.8: 交易日 rollover — 日虧斷路器計數重置 + fade 前日 VP 水位計算
        _ts_date = _topstep_trade_date(candle.timestamp)
        if _ts_date != self._loss_count_date:
            self._loss_count_date = _ts_date
            self._daily_loss_count = 0
            self._daily_win_count = 0   # 1.0.9: FULL WIN LOCK 換日重置
            self._daily_profit_td = 0.0  # 1.0.9: PDPT 換日重置
        if self.strategy_mode == "fade":
            if _ts_date != self._fade_day:
                self._close_fade_level_zone(candle.timestamp)
                if self._fade_day_candles:
                    try:
                        vp = self._fade_vp.calculate(self._fade_day_candles)
                        self.trend_follow.set_levels({
                            "date": _ts_date,
                            "poc": vp.poc, "vah": vp.vah, "val": vp.val,
                        })
                        self._add_fade_level_zone(_ts_date, candle.timestamp, vp)
                    except ValueError:
                        pass  # 前日 K 線不足以算 VP → 沿用舊水位或無水位
                self._fade_day = _ts_date
                self._fade_day_candles = []
            self._fade_day_candles.append(candle)

        # Advance any active 60m post-breakout trackers BEFORE we touch
        # position state — they keep tracking even after the trade exits.
        if self._breakout_trackers:
            self._update_breakout_trackers(candle)

        # ── Zone state: either live detector or pre-computed timeline ──
        _recent_zones = []
        if self._zone_timeline is not None:
            # Fast path: look up pre-computed state, skip expensive detector
            _zt = self._zone_timeline[self._zi] if self._zi < len(self._zone_timeline) else {}
            self._zi += 1
            _active_zone = _zt.get('active')
            _is_mature   = _zt.get('mature', False)
            _recent_zones = _zt.get('recent') or ([_active_zone] if _active_zone else [])
        elif self.strategy_mode not in ZONELESS_STRATEGIES:
            # Normal path: run detector live
            self.detector.update(candle)

        if self.strategy_mode == "sigma":
            if self._trend_session_allowed(candle.timestamp):
                self.trend_follow.observe(candle, [], True)
            elif not self._open_position and not self._pending_order:
                self._reset_trend_session_state()
        elif self.strategy_mode in FACTOR_PIPELINE_STRATEGIES and (self._open_position or self._pending_order):
            self.trend_follow.observe(candle, [], True)

        # Daily loss limit
        date_str = candle.timestamp.strftime("%Y-%m-%d")
        daily = self._daily_pnl.get(date_str, 0)
        if daily <= -self.config.max_daily_loss:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            return

        # Full TP lock counts reset on the Topstep session boundary.
        self._reset_full_tp_counts_for_session(candle.timestamp)

        close_phase = (
            market_close_phase(candle.timestamp)
            if self.CLOSE_WINDOW_ENABLED
            else None
        )
        in_flatten_window = close_phase == MARKET_PHASE_FLATTEN
        if (
            in_flatten_window
            and self.strategy_mode in FACTOR_PIPELINE_STRATEGIES
            and not self._open_position
            and not self._pending_order
        ):
            # Keep completed-bar factor indicators warm while orders are blocked.
            self.trend_follow.observe(candle, [], True)
        if in_flatten_window:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            return  # no new trades during flatten, but detector already updated

        # Cancel pending orders from 15:30 ET; DST is resolved by ZoneInfo.
        in_pre_flatten = close_phase == MARKET_PHASE_PRE_FLATTEN
        if in_pre_flatten and self._pending_order:
            logger.debug("Cancelling pending order before session close")
            self._cancel_pending_order()

        # Check SL/TP on open position
        if self._open_position:
            self._check_exit(candle)
            if self._open_position:
                # 1.0.10: PI 策略的持倉上限**依方向不同** —— 實測多單抱越久越好
                # (240m PF 2.80)、空單抱越久越差(240m PF 0.79)。空單用 60m
                # 時間出場的 PF 是純 SL/TP 的 2.28 vs 1.89(總額幾乎相同,
                # 差在時間出場會把一部分虧損單提早砍掉)。
                _hold = self._pmo_max_hold_minutes
                if self.strategy_mode == "pi" and self._open_position.direction == Direction.SELL:
                    _hold = max(0, int(getattr(
                        self.strategy_params, "pi_short_hold_min", 0) or 0))
                if _hold > 0 and self.strategy_mode in FACTOR_PIPELINE_STRATEGIES:
                    held = (candle.timestamp - self._open_position.entry_time).total_seconds() / 60.0
                    if held >= _hold:
                        self._force_exit(candle, ExitReason.FLATTEN)
                        return
                # ── Trailing SL: trigger at configured TP%, then move SL from entry ──
                self._check_trailing_sl(candle)
                return  # still open, don't open new

        if (
            self._pending_order
            and not self._open_position
            and not self._trend_session_allowed(candle.timestamp)
        ):
            logger.debug(
                "Trend session filter %s: cancel pending outside allowed segment",
                allowed_sessions_label(self._tr_allowed_sessions),
            )
            self._cancel_pending_order(release_lock=True)
            self._reset_trend_session_state()
            return

        # ── Check if pending limit order fills on this candle ──
        if self._pending_order and not self._open_position:
            filled = self._check_pending_fill(candle)
            if filled:
                return
            # Unfilled pending entry is not a real trade attempt.  Match live:
            # cancel, release the zone/direction lock, then re-evaluate next.
            self._cancel_pending_order(release_lock=True)

        # ── Strategy evaluation ──
        if not self._open_position and not self._pending_order:
            if self._near_data_end:
                return   # no new entries near live edge
            if not self._trend_session_allowed(candle.timestamp):
                self._reset_trend_session_state()
                return

            if self._zone_timeline is not None:
                # Fast path: zones already looked up above
                eval_zones  = _recent_zones
                eval_mature = _is_mature
                zone_source = "current"
            elif self.strategy_mode == "sigma":
                eval_zones = []
                eval_mature = True
                zone_source = "rolling_sigma"
            elif self.strategy_mode == "factor":
                eval_zones = []
                eval_mature = True
                zone_source = "factor"
            elif self.strategy_mode == "fade":
                eval_zones = []
                eval_mature = True
                zone_source = "prev_day_va"
            else:
                # Normal path — evaluate breakout vs the recent 10 reference zones
                eval_zones  = self.detector.get_recent_zones()
                eval_mature = self.detector.is_zone_mature
                zone_source = "current"

            signal = self.trend_follow.evaluate(candle, eval_zones, eval_mature)
            if signal:
                signal.zone_source = zone_source
                # 1.0.8: 日虧斷路器 — 當日虧損單數達上限,今天不再開新單
                if (self._tr_daily_loss_stop
                        and self._daily_loss_count >= self._tr_daily_loss_stop):
                    self.trend_follow.notify_order_cancelled()
                    return
                # 1.0.9: FULL WIN LOCK — 當日贏單數達上限,落袋停手
                if (self._tr_daily_win_stop
                        and self._daily_win_count >= self._tr_daily_win_stop):
                    self.trend_follow.notify_order_cancelled()
                    return
                # 1.0.9: PDPT —— 當日獲利達標,停開新單(既有部位照常由 SL/TP 結束)
                if (self._tr_daily_profit_stop
                        and self._daily_profit_td >= self._tr_daily_profit_stop):
                    self.trend_follow.notify_order_cancelled()
                    return
                if self._signal_full_tp_locked(signal, candle):
                    self.trend_follow.notify_order_cancelled()
                    return
                if self._session_direction_is_used(signal, candle):
                    self.trend_follow.notify_order_cancelled()
                    return
                self._mark_session_direction_used(signal, candle)
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

        The level nearest to the open is treated as first. Exact ties resolve
        conservatively to SL. The ML labeler and confluence backtester use the
        same shared rule.
        """
        pos = self._open_position
        if not pos:
            return

        if pos.direction == Direction.BUY:
            hit_sl = candle.low <= pos.sl_price
            hit_tp = candle.high >= pos.tp_price
            if hit_sl and hit_tp:
                if resolve_same_bar_exit(candle.open, pos.sl_price, pos.tp_price) == "sl":
                    self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())
                else:
                    self._execute_exit(candle, pos.tp_price, ExitReason.TP)
            elif hit_sl:
                self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())
            elif hit_tp:
                self._execute_exit(candle, pos.tp_price, ExitReason.TP)
        else:  # SELL
            hit_sl = candle.high >= pos.sl_price
            hit_tp = candle.low <= pos.tp_price
            if hit_sl and hit_tp:
                if resolve_same_bar_exit(candle.open, pos.sl_price, pos.tp_price) == "sl":
                    self._execute_exit(candle, pos.sl_price, self._stop_exit_reason())
                else:
                    self._execute_exit(candle, pos.tp_price, ExitReason.TP)
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

    def _cancel_pending_order(self, *, release_lock: bool = True):
        """Cancel a pending limit order and notify the strategy.

        Session/breakout locks represent *filled* opportunities.  A limit order
        that never touched is only a working order, so releasing the lock keeps
        backtest parity with live's Topstep-confirmed pending cancellation.
        """
        if self._pending_order:
            self.trend_follow.notify_order_cancelled()
        if release_lock and self._pending_lock_key:
            zone_id, direction = self._pending_lock_key
            self._session_direction_used.discard(self._pending_lock_key)
            if hasattr(self.trend_follow, "unlock_breakout"):
                self.trend_follow.unlock_breakout(zone_id, direction)
        self._pending_order = None
        self._pending_age = 0
        self._pending_lock_key = None

    @staticmethod
    def _signal_direction_key(signal: TradeSignal) -> str:
        return "up" if signal.direction == Direction.BUY else "down"

    def _signal_session_key(self, signal: TradeSignal, candle: Candle) -> str:
        return str(signal.zone_id or _topstep_trade_date(candle.timestamp))

    def _session_direction_key(self, signal: TradeSignal, candle: Candle) -> tuple[str, str]:
        return (self._signal_session_key(signal, candle), self._signal_direction_key(signal))

    def _session_limit_flag(self, signal: TradeSignal) -> bool:
        return self._tr_one_trade_per_session

    def _session_direction_is_used(self, signal: TradeSignal, candle: Candle) -> bool:
        if not self._session_limit_flag(signal):
            return False
        return self._session_direction_key(signal, candle) in self._session_direction_used

    def _mark_session_direction_used(self, signal: TradeSignal, candle: Candle) -> None:
        if not self._session_limit_flag(signal):
            return
        key = self._session_direction_key(signal, candle)
        self._session_direction_used.add(key)
        if hasattr(self.trend_follow, "mark_breakout_used"):
            self.trend_follow.mark_breakout_used(key[0], key[1])

    def _place_pending_order(self, signal: TradeSignal, candle: Candle):
        """Place a limit order — will fill on a future candle when price touches."""
        self._pending_order = signal
        self._pending_age = 0
        self._pending_lock_key = (
            self._session_direction_key(signal, candle)
            if self._session_limit_flag(signal)
            else None
        )
        logger.debug(
            f"Pending order: {signal.strategy.value} {signal.direction.value} "
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
            # Filled orders consume the session/direction lock.
            self._pending_lock_key = None
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
        fill_price = signal.entry_price
        meta = dict(getattr(signal, "meta", None) or {})
        if getattr(signal, "reason", None):
            meta.setdefault("signal_reason", signal.reason)

        trade = Trade(
            trade_id=f"T{uuid.uuid4().hex[:8]}",
            strategy=signal.strategy,
            direction=signal.direction,
            entry_price=fill_price,
            entry_time=candle.timestamp,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
            original_sl_price=signal.sl_price,
            original_tp_price=signal.tp_price,
            zone_id=signal.zone_id,
            zone_source=getattr(signal, 'zone_source', None),
            contracts=self.contract_size,
            point_value=self.POINT_VALUE,
            contract_id=self.contract_id,
            vol_ratio=signal.vol_ratio,
            is_big_trend=signal.is_big_trend,
            breakout_range=signal.breakout_range,
            meta=meta,
        )
        self._open_position = trade
        self._trail_sl_triggered = False

        # 1.0.8/1.0.10: ladder exit for TREND-compatible market-entry strategies.
        # DAY ZONE keeps its own target definition.
        if self._tr_exit_mode == "ladder" and self.strategy_mode in ("trend", "factor"):
            self._ladder_risk = abs(trade.entry_price - trade.sl_price)
            self._ladder_max_r = 0.0
            far = 1_000_000.0
            trade.tp_price = (
                trade.entry_price + far
                if trade.direction == Direction.BUY
                else trade.entry_price - far
            )

        # Spawn a 60m post-breakout tracker. We track price action for
        # POST_BREAKOUT_WINDOW_MIN minutes regardless of when (or whether)
        # the trade actually exits — the user wants to know how price
        # behaved within 1h after breakout, not just up to the exit.
        trail_ticks = self._resolved_trail_ticks(signal.strategy)
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
            f"Entry: {trade.strategy.value} {trade.direction.value} "
            f"@ {fill_price:.2f} | SL={trade.sl_price:.2f} TP={trade.tp_price:.2f}"
        )

    def _update_breakout_trackers(self, candle: Candle):
        """Advance each active post-breakout tracker with this candle's range.

        Updates MFE/MAE and detects which level (trail / sl / tp) is crossed
        first. When a candle straddles adverse and favorable levels, the shared
        nearest-to-open rule decides which level came first.
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
            else:
                hit_sl = candle.high >= sl_p
                hit_tp = candle.low <= tp_p
                hit_trail = candle.high >= trail_p

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
                    adverse_price = sl_p if hit_sl else trail_p
                    adverse_first = (
                        resolve_same_bar_exit(candle.open, adverse_price, tp_p) == "sl"
                    )
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

        1.0.8: tr_exit_mode="ladder" 時改走 _check_ladder_sl(多段棘輪,非一次性)。

        Disabling lets the trade run all the way to TP or full SL — useful when
        post-breakout stats show many trades dipping back through the trail
        level before reaching TP (a high TP↶TRAIL count means trail is
        cutting off would-be winners).
        """
        # 1.0.8/1.0.10: ladder exit mode for TREND/FACTOR.
        if self._tr_exit_mode == "ladder" and self.strategy_mode in ("trend", "factor"):
            self._check_ladder_sl(candle)
            return
        if self._trail_sl_triggered:
            return
        pos = self._open_position
        if not pos:
            return
        if not self._strategy_trail_enabled(pos.strategy):
            return
        mkt = candle.close
        if pos.direction == Direction.BUY:
            ticks_moved = (mkt - pos.entry_price) / self.TICK_SIZE
        else:
            ticks_moved = (pos.entry_price - mkt) / self.TICK_SIZE

        # v1.0.6: TP is RR-based, so derive the trail trigger from the position's
        # actual TP distance instead of the removed fixed tp_ticks param.
        tp_ticks = abs(pos.tp_price - pos.entry_price) / self.TICK_SIZE
        trigger_pct = self._strategy_trigger_pct(pos.strategy)
        if trigger_pct <= 0:
            return
        trigger_ticks = max(1.0, tp_ticks * trigger_pct)
        if ticks_moved >= trigger_ticks:
            self._trail_sl_triggered = True
            trail_ticks = self._resolved_trail_ticks(pos.strategy)
            trail_pts = trail_ticks * self.TICK_SIZE
            if pos.direction == Direction.BUY:
                pos.sl_price = pos.entry_price + trail_pts
            else:
                pos.sl_price = pos.entry_price - trail_pts
            logger.debug(
                f"Trail SL: {ticks_moved:.1f} ticks moved → SL moved to {pos.sl_price:.2f} "
                f"({trail_ticks}t from entry, trigger={trigger_pct:.0%} TP)"
            )

    def _check_ladder_sl(self, candle: Candle):
        """1.0.8: 無 TP 階梯滾動出場(回測驗證 +8044 vs 固定TP +7181)。

        浮盈(收盤計)首達 +2R → SL 移到 entry(保本);之後每多 +1R,
        SL 跟進 +1R — 恆落後最高浮盈整數 R 約 2R。只上不下(棘輪)。
        """
        pos = self._open_position
        if not pos or self._ladder_risk <= 0:
            return
        mkt = candle.close
        if pos.direction == Direction.BUY:
            fav = mkt - pos.entry_price
        else:
            fav = pos.entry_price - mkt
        r = fav / self._ladder_risk
        if r > self._ladder_max_r:
            self._ladder_max_r = r
        if self._ladder_max_r < self.LADDER_TRIGGER_R:
            return
        lock_r = math.floor(self._ladder_max_r) - self.LADDER_GAP_R  # 2R→0(entry), 3R→+1R…
        tick = self.TICK_SIZE
        if pos.direction == Direction.BUY:
            new_sl = round((pos.entry_price + lock_r * self._ladder_risk) / tick) * tick
            if new_sl > pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True
                logger.debug(f"Ladder SL: peak {self._ladder_max_r:.2f}R → SL {new_sl:.2f} (+{lock_r:g}R)")
        else:
            new_sl = round((pos.entry_price - lock_r * self._ladder_risk) / tick) * tick
            if new_sl < pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True
                logger.debug(f"Ladder SL: peak {self._ladder_max_r:.2f}R → SL {new_sl:.2f} (+{lock_r:g}R)")

    def _execute_exit(self, candle: Candle, exit_price: float, reason: ExitReason):
        pos = self._open_position
        if not pos:
            return

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
        if reason == ExitReason.TP:
            self._reset_full_tp_counts_for_session(candle.timestamp)
            lock = self._full_tp_lock_for_strategy(pos.strategy)
            if lock > 0:
                key = self._strategy_group(pos.strategy)
                self._full_tp_counts[key] = self._full_tp_counts.get(key, 0) + 1
                self._full_tp_count = sum(self._full_tp_counts.values())

        self._trades.append(pos)
        self._last_closed_trade = pos
        self._open_position = None
        self._trail_sl_triggered = False
        # 1.0.8: 日虧斷路器計數(任何原因的虧損出場都算一單虧)
        if pnl < 0:
            self._daily_loss_count += 1
        elif pnl > 0:
            self._daily_win_count += 1   # 1.0.9: FULL WIN LOCK 計數
        self._daily_profit_td += pnl     # 1.0.9: PDPT 累計(含虧損,才是真實日損益)
        self._ladder_risk = 0.0
        self._ladder_max_r = 0.0

        # Notify strategy of trade close
        self.trend_follow.notify_trade_closed(reason.value)

        logger.debug(
            f"Exit: {reason.value} @ {exit_price:.2f} | "
            f"PnL=${pnl:+.0f} | capital=${self._capital:,.0f}"
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


