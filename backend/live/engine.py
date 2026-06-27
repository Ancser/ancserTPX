# ============================================================

# 文件: backend/live/engine.py
# 狀態: v1.0.6
# 功能 / Features:
#   - Live trading engine for the trend strategy.
#   - Polls TopstepX 1m bars and drops the newest bar so decisions use only the
#     previous completed minute, matching backtest timing.
#   - Uses current mature 80% session zone only; no previous-zone fallback.
#   - Syncs Auto OCO SL/TP, supports trail SL, close-window flatten, and full TP lock.
# ============================================================

from __future__ import annotations
import asyncio
import json
import logging
import math
import os
import time as time_mod
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import httpx

from backend.db.models import (
    Candle, TradeSignal, OrderRequest, OrderResponse,
    ConsolidationZone, Direction, StrategyType, ZoneStatus, BarUnit,
    StrategyParams, get_point_value, get_tick_size,
)
from backend.strategy.consolidation import SessionZoneDetector, build_zone_detector
from backend.strategy.session_filter import (
    DEFAULT_ALLOWED_SESSIONS, allowed_sessions_label, is_allowed_session,
)
from backend.strategy.trend_follow import SessionTrendFollow
from backend.broker.topstepx import TopstepXClient, order_error_meaning

logger = logging.getLogger(__name__)

ENGINE_VERSION = "1.0.6"
# Default fallbacks for legacy paths — actual values come from contract on init.
POINT_VALUE = 20.0
TICK_SIZE = 0.25

_CT = ZoneInfo("America/Chicago")
_UTC_TZ = ZoneInfo("UTC")


def _conf_ev_floor(val) -> Optional[float]:
    """Normalise the configured EV gate floor. Blank / None / non-numeric →
    None (legacy win-prob gate). A number (incl. 0.0) → EV-priority gate at
    that floor (0.0 = every positive-EV setup)."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


class LiveTradingEngine:
    """即時交易引擎 — Session 模式 (1m K 線, 晚盤 overnight zone)"""

    # 加州 12:45 PT = 15:45 ET = 14:45 CT = 19:45 UTC
    TRAIL_TICK_STEP = 5
    MIN_STOP_BRACKET_TICKS = 4
    MIN_TP_BRACKET_TICKS = 1
    AUTO_OCO_FAILSAFE_SECONDS = 5 * 60
    AUTO_OCO_RETRY_SECONDS = 15.0
    AUTO_OCO_SETTINGS_URL = "https://topstepx.com/settings?tab=risk-settings"
    FLATTEN_TIME_UTC = time(19, 45)     # UTC 19:45 = PT 12:45 flatten
    PRE_FLATTEN_UTC = time(19, 30)      # UTC 19:30 = PT 12:30 cancel pending

    def __init__(
        self,
        client: TopstepXClient,
        account_id: int,
        contract_id: str,
        # Strategy params (simplified — SessionTrendFollow only)
        value_area_pct: float = 0.80,
        contract_size: int = 1,
        # Configurable strategy params
        strategy_params: Optional[StrategyParams] = None,
        # Legacy params (ignored, kept for API compatibility)
        strategies: List[str] = None,
        sl_dollars: float = 300.0,
        tp_dollars: float = 900.0,
        trend_tp_mode: str = "multiplier",
        trend_tp_multiplier: float = 4.0,
        min_candles_for_zone: int = 6,
        poc_drift_threshold: float = 3.0,
    ):
        self.client = client
        self.account_id = account_id
        self.contract_id = contract_id
        # Strategy label set after strategy object is created (below)
        self.strategies: List[str] = []
        self.strategy_params = strategy_params or StrategyParams()
        # Contract sizing — prefer the strategy_params value, fall back to ctor arg.
        self.contract_size = max(
            1,
            int(getattr(self.strategy_params, "contract_size", contract_size) or contract_size or 1),
        )
        # Per-contract market specs (NQ=$20/pt, MNQ=$2/pt; both 0.25 tick).
        self.point_value = get_point_value(contract_id)
        self.tick_size = get_tick_size(contract_id)

        # v1.0.6: value-area width + area timeframe + method (single/overlap) are selectable.
        value_area_pct = float(getattr(self.strategy_params, "value_area_pct", value_area_pct) or value_area_pct)
        area_timeframe = getattr(self.strategy_params, "area_timeframe", "5m") or "5m"
        method = (getattr(self.strategy_params, "method", "single") or "single").lower()
        tf_combo = list(getattr(self.strategy_params, "tf_combo", None) or [])

        # Clock-bucket zone detector — single timeframe, or multi-timeframe
        # OVERLAP (identical to the backtest/ML overlap sweep) when method=overlap
        # with 2+ timeframes. Keeps the recent 10 reference zones.
        overlap_combo = tf_combo if (method == "overlap" and len(tf_combo) >= 2) else None
        self.detector = build_zone_detector(
            area_timeframe=area_timeframe,
            value_area_pct=value_area_pct,
            tick_size=self.tick_size,
            max_recent=10,
            tf_combo=overlap_combo,
        )
        # Strategy mode: "trend" (default, 1.0.6) or "confluence" (v1.0.6 ML).
        # The trend object is ALWAYS built so legacy state/helpers keep working;
        # confluence only adds a parallel evaluator and diverges at signal time.
        self.strategy_mode = (getattr(self.strategy_params, "strategy", "trend") or "trend").lower()
        if self.strategy_mode not in ("confluence", "ml_consolidation_v2"):
            self.strategy_mode = "trend"
        self.trend_follow = SessionTrendFollow(params=self.strategy_params)
        if self.strategy_mode == "confluence":
            try:
                conf_wait = int(getattr(self.strategy_params, "conf_wait_minutes", 1) or 1)
            except (TypeError, ValueError):
                conf_wait = 1
            # Confluence uses the same pending-order state machine as the
            # trend engine.  Keep the shipped default at 1m, but make live
            # honor the ML/confluence parameter when presets/UI change it.
            self.trend_follow.PENDING_TIMEOUT_CANDLES = max(1, conf_wait)

        # v1.0.6: explainable multi-timeframe confluence evaluator (shadow or live).
        self.confluence = None
        self._conf_shadow = bool(getattr(self.strategy_params, "conf_shadow", False))
        self._conf_allowed_sessions = (
            getattr(self.strategy_params, "conf_allowed_sessions", DEFAULT_ALLOWED_SESSIONS)
            or None
        )
        self._tr_allowed_sessions = (
            getattr(self.strategy_params, "tr_allowed_sessions", DEFAULT_ALLOWED_SESSIONS)
            or None
        )
        self._last_session_block_log: Optional[str] = None
        self._conf_signals_log: List[Dict] = []
        # explain payload (weights x features) of the confluence signal that is
        # currently pending/active — carried through fill so each closed trade can
        # persist a durable, replayable "why" alongside its outcome.
        self._pending_conf_payload: Optional[Dict] = None
        self._active_conf_payload: Optional[Dict] = None
        if self.strategy_mode == "confluence":
            from backend.live.confluence_live import ConfluenceLiveEvaluator
            self.confluence = ConfluenceLiveEvaluator(
                contract_id=contract_id,
                band_ticks=float(getattr(self.strategy_params, "conf_band_ticks", 4.0)),
                min_distinct_tf=int(getattr(self.strategy_params, "conf_min_distinct_tf", 2)),
                rr=float(getattr(self.strategy_params, "conf_rr", 1.0)),
                base_minutes=int(getattr(self.strategy_params, "conf_base_minutes", 1)),
                min_prob=float(getattr(self.strategy_params, "conf_min_prob", 0.65)),
                ev_floor=_conf_ev_floor(getattr(self.strategy_params, "conf_ev_floor", None)),
                rr_grid=None,
                use_scorer=bool(getattr(self.strategy_params, "conf_use_scorer", True)),
                enable_breakout=bool(getattr(self.strategy_params, "conf_enable_breakout", False)),
                max_risk_ticks=getattr(self.strategy_params, "conf_max_risk_ticks", None),
            )
        # ML Consolidation V2: VA mean reversion strategy
        self.mlc2_evaluator = None
        self._mlc2_shadow = bool(getattr(self.strategy_params, "mlc2_shadow", False))
        self._mlc2_signals_log: List[Dict] = []
        self._pending_mlc2_payload: Optional[Dict] = None
        self._active_mlc2_payload: Optional[Dict] = None
        if self.strategy_mode == "ml_consolidation_v2":
            from backend.live.ml_consolidation_v2_live import MLConsolidationV2LiveEvaluator
            _mlc2_sessions = (
                getattr(self.strategy_params, "mlc2_allowed_sessions", None)
                or ["ASIA", "EURO"]
            )
            self.mlc2_evaluator = MLConsolidationV2LiveEvaluator(
                contract_id=contract_id,
                lookback=int(getattr(self.strategy_params, "mlc2_lookback", 30) or 30),
                band_ticks=float(getattr(self.strategy_params, "mlc2_band_ticks", 2.0) or 2.0),
                sl_buffer_ticks=float(getattr(self.strategy_params, "mlc2_sl_buffer_ticks", 4.0) or 4.0),
                sl_mode=str(getattr(self.strategy_params, "mlc2_sl_mode", "va") or "va"),
                tp_mode=str(getattr(self.strategy_params, "mlc2_tp_mode", "rr") or "rr"),
                rr=float(getattr(self.strategy_params, "mlc2_rr", 4.0) or 4.0),
                max_risk_ticks=float(getattr(self.strategy_params, "mlc2_max_risk_ticks", 40.0) or 40.0),
                min_score=float(getattr(self.strategy_params, "mlc2_min_score", 0.0) or 0.0),
                allowed_sessions=tuple(_mlc2_sessions),
            )
        self.strategies = [self.strategy_mode]

        # Live state
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._pending_order_id: Optional[int] = None
        self._pending_signal: Optional[TradeSignal] = None
        self._pending_age: int = 0
        self._pending_created_at: Optional[datetime] = None
        self._open_position: Optional[Dict] = None
        self._fill_price: Optional[float] = None
        self._sl_order_id: Optional[int] = None
        self._tp_order_id: Optional[int] = None
        self._active_signal: Optional[TradeSignal] = None  # preserved after fill for SL/TP
        self._position_just_closed: bool = False  # skip strategy eval on same tick as close
        self._position_age: int = 0              # candles since position opened (for display)
        self._trail_sl_triggered: bool = False    # trailing SL: one-time trigger per position
        self._protection_synced: bool = False     # Auto OCO child orders moved to strategy prices
        self._auto_oco_fail_safe_triggered: bool = False
        self._last_auto_oco_retry_ts: float = 0.0
        self._entry_time: Optional[datetime] = None  # when current position opened (UTC)
        self._force_exit_reason: Optional[str] = None  # set by flatten_now / emergency close
        self._daily_pnl: float = 0.0
        self._today: str = ""
        self._full_tp_lock: int = max(
            int(getattr(self.strategy_params, "full_tp_lock", 0) or 0),
            int(getattr(self.strategy_params, "tr_full_tp_lock", 0) or 0),
            int(getattr(self.strategy_params, "cd_full_tp_lock", 0) or 0),
        )
        self._full_tp_count: int = 0
        self._full_tp_counts: Dict[str, int] = {"tr": 0}
        self._tp_locked: bool = False
        self._one_trade_per_session_direction: bool = bool(
            getattr(self.strategy_params, "one_trade_per_session_direction", True)
        )
        self._tr_one_trade_per_session: bool = bool(
            getattr(self.strategy_params, "tr_one_trade_per_session", True)
        )
        self._session_direction_locks: set[tuple[str, str]] = set()
        self._capital: float = 0.0
        self._candles_processed: int = 0
        # Rolling 1m candle history (warm-up + live) so the chart's multi-timeframe
        # zone filter can recompute all-TF zones from the freshest data.
        self._all_candles: List[Candle] = []
        # Per-timeframe breakout state for the PHASE display:
        #   { tf: {"dir": "up"|"down"|None, "count": int} }
        self._tf_breakout: Dict[str, Dict] = {}
        self._last_market_price: Optional[float] = None
        self._last_candle_time: Optional[str] = None
        self._last_account_refresh: float = 0.0
        # Network resilience
        self._disconnected: bool = False
        self._consecutive_errors: int = 0
        self._last_safety_check: float = 0.0   # position size check (5 min)
        self._position_open_ts: float = 0.0     # when position first detected (grace period)
        self._trades: List[Dict] = []
        self._log: List[str] = []
        self._last_status_log_minute: int = -1  # track minute for periodic status log
        self._zone_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "live_zones.json"
        )
        self._exits_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "live_exits.json"
        )
        self._breakout_locks_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "live_breakout_locks.json"
        )
        # Durable per-trade ledger: every closed position is appended here with
        # its full explainable confluence payload (weights x features, prob,
        # score, scorer version) + all params + outcome. Capped at 10k rows.
        self._trades_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "trades.json"
        )
        # daily_capital.json removed — PnL now read directly from API

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

    def _reset_full_tp_counts(self) -> None:
        self._full_tp_counts = {"tr": 0}
        self._full_tp_count = 0
        self._tp_locked = False

    def _signal_full_tp_locked(self, signal: TradeSignal) -> bool:
        lock = self._full_tp_lock_for_strategy(signal.strategy)
        if lock <= 0:
            return False
        key = self._strategy_group(signal.strategy)
        return self._full_tp_counts.get(key, 0) >= lock

    def _any_full_tp_locked(self) -> bool:
        lock = self._full_tp_lock_for_strategy(StrategyType.TREND_FOLLOW)
        return lock > 0 and self._full_tp_counts.get("tr", 0) >= lock

    def _resolved_trail_ticks(self, strategy=None) -> int:
        sl_ticks = abs(int(self._strategy_param(strategy, 'sl_ticks', 50) or 50))
        tp_ticks = abs(int(self._strategy_param(strategy, 'tp_ticks', 0) or 0))
        trail_ticks = int(self._strategy_param(strategy, 'trail_sl_ticks', 5) or 0)
        trigger_pct = self._strategy_trigger_pct(strategy)
        if trigger_pct <= 0:
            return 0

        max_positive = max(0, self._floor_ticks_to_step(tp_ticks * trigger_pct) - self.TRAIL_TICK_STEP)
        return max(0, min(min(tp_ticks, max_positive), trail_ticks))

    def _confluence_exit_style(self):
        """ML/confluence trail knobs use the ML panel's conf_* fields.

        Trend trailing is expressed as fixed ticks; confluence trailing is
        expressed as percentages of the actual entry→TP distance, matching the
        confluence backtester exactly.
        """
        from backend.strategy.exit_policy import ConfluenceExitStyle

        return ConfluenceExitStyle(
            trail_trigger_pct=float(getattr(self.strategy_params, "conf_trail_trigger_pct", 0.0) or 0.0),
            trail_lock_pct=float(getattr(self.strategy_params, "conf_trail_lock_pct", 0.0) or 0.0),
            full_tp_lock=int(getattr(self.strategy_params, "conf_full_tp_lock", 0) or 0),
            session_limit=bool(getattr(self.strategy_params, "conf_session_limit", True)),
        )

    def _confluence_session_allowed(self, ts: datetime) -> bool:
        return is_allowed_session(ts, self._conf_allowed_sessions)

    def _confluence_session_label(self) -> str:
        return allowed_sessions_label(self._conf_allowed_sessions)

    def _trend_session_allowed(self, ts: datetime) -> bool:
        return is_allowed_session(ts, self._tr_allowed_sessions)

    def _trend_session_label(self) -> str:
        return allowed_sessions_label(self._tr_allowed_sessions)

    @staticmethod
    def _order_id(order: Dict[str, Any]) -> Optional[int]:
        oid = order.get("id", order.get("orderId"))
        try:
            return int(oid) if oid is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _order_float(order: Dict[str, Any], *keys: str) -> Optional[float]:
        for key in keys:
            value = order.get(key)
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _order_int(order: Dict[str, Any], *keys: str) -> Optional[int]:
        for key in keys:
            value = order.get(key)
            if value is None or value == "":
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return None

    @classmethod
    def _order_type(cls, order: Dict[str, Any]) -> Optional[int]:
        raw = order.get("type", order.get("orderType", order.get("order_type")))
        if raw is None:
            return None
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text.isdigit():
                return int(text)
            if "trail" in text and "stop" in text:
                return 5
            if "stop" in text:
                return 4
            if "limit" in text:
                return 1
            if "market" in text:
                return 2
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _order_side_api(cls, order: Dict[str, Any]) -> Optional[int]:
        raw = order.get("side", order.get("orderSide", order.get("order_side")))
        if raw is None:
            return None
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in ("0", "buy", "bid", "long"):
                return 0
            if text in ("1", "sell", "ask", "short"):
                return 1
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _order_contract_matches(self, order: Dict[str, Any]) -> bool:
        contract = order.get("contractId") or order.get("contract_id") or order.get("contractID")
        return contract == self.contract_id

    @staticmethod
    def _exit_api_side(signal: TradeSignal) -> int:
        return 1 if signal.direction == Direction.BUY else 0

    def _select_auto_oco_orders(
        self,
        open_orders: List[Dict[str, Any]],
        signal: TradeSignal,
    ) -> tuple[Optional[int], Optional[int]]:
        close_side = self._exit_api_side(signal)
        sl_candidates = []
        tp_candidates = []

        for order in open_orders:
            if not self._order_contract_matches(order):
                continue
            oid = self._order_id(order)
            if not oid:
                continue

            side = self._order_side_api(order)
            if side is not None and side != close_side:
                continue

            size = self._order_int(order, "size", "quantity", "qty", "remainingSize", "openSize")
            if size is not None and abs(size) != self.contract_size:
                continue

            order_type = self._order_type(order)
            stop_price = self._order_float(order, "stopPrice", "stop_price")
            limit_price = self._order_float(order, "limitPrice", "limit_price", "price")

            # Auto OCO should use Stop Market for SL; the bot controls trailing by modifying it.
            if order_type == 4 or (order_type is None and stop_price is not None):
                sl_candidates.append((oid, stop_price))
            elif order_type == 1 or (order_type is None and limit_price is not None):
                tp_candidates.append((oid, limit_price))

        def pick(candidates, target_price: float) -> Optional[int]:
            if not candidates:
                return None
            oid, _ = min(
                candidates,
                key=lambda item: abs((item[1] if item[1] is not None else target_price) - target_price),
            )
            return oid

        return pick(sl_candidates, signal.sl_price), pick(tp_candidates, signal.tp_price)

    async def _scan_auto_oco_order_ids(self, signal: TradeSignal) -> tuple[Optional[int], Optional[int]]:
        try:
            open_orders = await self.client.get_open_orders(self.account_id)
        except Exception as e:
            self._log_event(f"[AUTO OCO] 掃描 SL/TP 失敗: {e}", "error")
            return self._sl_order_id, self._tp_order_id

        sl_id, tp_id = self._select_auto_oco_orders(open_orders, signal)
        if sl_id:
            self._sl_order_id = sl_id
        if tp_id:
            self._tp_order_id = tp_id
        return self._sl_order_id, self._tp_order_id

    async def _sync_auto_oco_protection(self, signal: TradeSignal, wait_seconds: float = 4.0) -> bool:
        """Wait for Auto OCO SL/TP child orders and move them to strategy prices."""
        if not signal or not self._open_position:
            self._log_event("[AUTO OCO] 無 signal 或無持倉，跳過保護單同步", "error")
            return False

        deadline = time_mod.monotonic() + max(0.0, wait_seconds)
        waiting_logged = False

        while True:
            sl_id, tp_id = await self._scan_auto_oco_order_ids(signal)
            missing = []
            if not sl_id:
                missing.append("SL")
            if not tp_id:
                missing.append("TP")
            if not missing:
                break

            if time_mod.monotonic() >= deadline:
                self._protection_synced = False
                self._log_event(
                    f"[AUTO OCO] 等不到 {'+'.join(missing)} 子單；請確認 TopstepX 已啟用 Auto OCO preset",
                    "error",
                )
                return False

            if not waiting_logged:
                self._log_event(f"[AUTO OCO] 等待 TopstepX 生成 {'+'.join(missing)} 子單...")
                waiting_logged = True
            await asyncio.sleep(0.25)

        sl_price = self._round_to_tick(signal.sl_price)
        tp_price = self._round_to_tick(signal.tp_price)
        ok = True

        try:
            sl_resp = await self.client.modify_order(
                self.account_id,
                self._sl_order_id,
                size=self.contract_size,
                stop_price=sl_price,
            )
            if sl_resp.success:
                self._log_event(f"[AUTO OCO] SL #{self._sl_order_id} -> {sl_price:.2f}")
                signal.sl_price = sl_price
            else:
                ok = False
                self._log_event(f"[AUTO OCO] SL 修改失敗: {sl_resp.error_message}", "error")
        except Exception as e:
            ok = False
            self._log_event(f"[AUTO OCO] SL 修改異常: {e}", "error")

        try:
            tp_resp = await self.client.modify_order(
                self.account_id,
                self._tp_order_id,
                size=self.contract_size,
                limit_price=tp_price,
            )
            if tp_resp.success:
                self._log_event(f"[AUTO OCO] TP #{self._tp_order_id} -> {tp_price:.2f}")
                signal.tp_price = tp_price
            else:
                ok = False
                self._log_event(f"[AUTO OCO] TP 修改失敗: {tp_resp.error_message}", "error")
        except Exception as e:
            ok = False
            self._log_event(f"[AUTO OCO] TP 修改異常: {e}", "error")

        self._protection_synced = ok
        return ok

    async def _sweep_contract_open_orders(self, label: str) -> int:
        try:
            open_orders = await self.client.get_open_orders(self.account_id)
        except Exception as e:
            self._log_event(f"{label} residual order scan failed: {e}", "error")
            return 0

        cancel_tasks = []
        for od in open_orders:
            if not self._order_contract_matches(od):
                continue
            oid = self._order_id(od)
            if oid:
                cancel_tasks.append(self._cancel_with_retry(oid, f"SWEEP ({label})"))
        if cancel_tasks:
            self._log_event(f"{label} sweep found {len(cancel_tasks)} open order(s) -> cancel")
            await asyncio.gather(*cancel_tasks, return_exceptions=True)
        return len(cancel_tasks)

    @property
    def is_running(self) -> bool:
        return self._running

    def _save_zones(self):
        """Persist current zones to disk so they survive restart."""
        try:
            zones = self.detector.get_all_zones()
            active = self.detector.get_active_zone()
            data = {
                "saved_at": datetime.utcnow().isoformat(),
                "active_zone_id": active.zone_id if active else None,
                "zones": [],
            }
            for z in zones[-20:]:
                data["zones"].append({
                    "zone_id": z.zone_id,
                    "poc": z.poc,
                    "vah_80": z.vah_80,
                    "val_80": z.val_80,
                    "high_100": z.high_100,
                    "low_100": z.low_100,
                    "total_volume": z.total_volume,
                    "duration_minutes": z.duration_minutes,
                    "num_candles": z.num_candles,
                    "status": z.status.value,
                    "formed_at": z.formed_at.isoformat() if z.formed_at else None,
                    "left_at": z.left_at.isoformat() if z.left_at else None,
                    "exit_direction": z.exit_direction,
                    "timeframe": getattr(z, "timeframe", "5m"),
                    "profile": {str(k): v for k, v in (getattr(z, "profile", {}) or {}).items()},
                })
            os.makedirs(os.path.dirname(self._zone_file), exist_ok=True)
            with open(self._zone_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save zones: {e}")

    def _load_zones(self) -> bool:
        """Load persisted zones from disk. Returns True if loaded."""
        try:
            if not os.path.exists(self._zone_file):
                return False
            with open(self._zone_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Check freshness — only use if saved within last 6 hours
            saved_at = datetime.fromisoformat(data["saved_at"])
            age_hours = (datetime.utcnow() - saved_at).total_seconds() / 3600
            if age_hours > 6:
                self._log_event(f"Zone 快照過期 ({age_hours:.1f}h) — 重新偵測")
                return False

            # Overlap detector synthesizes merged zones live from its per-tf
            # sub-detectors (rebuilt during warm-up); it has no flat
            # _completed_zones list to restore into, so skip the snapshot.
            if not hasattr(self.detector, "_completed_zones"):
                self._log_event("Overlap 模式 — 略過 zone 快照 (即時由各 TF 重建)")
                return False

            active_id = data.get("active_zone_id")
            loaded = 0
            for zd in data.get("zones", []):
                # Clock-bucket zones use a "B" prefix; skip anything older.
                zid = zd.get("zone_id", "")
                if not zid.startswith("B"):
                    logger.info(f"跳過舊版 zone: {zid}")
                    continue
                profile = {}
                for k, v in (zd.get("profile") or {}).items():
                    try:
                        profile[float(k)] = int(v)
                    except (TypeError, ValueError):
                        continue
                zone = ConsolidationZone(
                    zone_id=zd["zone_id"],
                    formed_at=datetime.fromisoformat(zd["formed_at"]) if zd.get("formed_at") else None,
                    left_at=datetime.fromisoformat(zd["left_at"]) if zd.get("left_at") else None,
                    poc=zd["poc"],
                    vah_80=zd["vah_80"],
                    val_80=zd["val_80"],
                    high_100=zd["high_100"],
                    low_100=zd["low_100"],
                    total_volume=zd["total_volume"],
                    duration_minutes=zd["duration_minutes"],
                    num_candles=zd["num_candles"],
                    status=ZoneStatus(zd["status"]),
                    exit_direction=zd.get("exit_direction"),
                    candles=[],  # candles not persisted (too large)
                    timeframe=zd.get("timeframe", "5m"),
                    profile=profile,
                )
                # Restore as a completed reference zone (LEFT). The forming bucket
                # rebuilds itself from incoming live candles.
                self.detector._completed_zones.append(zone)
                try:
                    self.detector._zone_counter = max(
                        self.detector._zone_counter,
                        int(zid.lstrip("B")) if zid.startswith("B") else 0,
                    )
                except ValueError:
                    pass
                loaded += 1

            if loaded > 0:
                self._log_event(
                    f"載入 {loaded} 個 zone 快照 (存檔 {age_hours:.1f}h 前) | "
                    f"活躍={active_id or 'None'}"
                )
                return True
            return False
        except Exception as e:
            logger.warning(f"Failed to load zones: {e}")
            return False

    def _persist_exit_record(
        self,
        exit_reason: str,
        entry_time: Optional[datetime],
        exit_time: datetime,
        entry_price: Optional[float],
        exit_price: Optional[float],
        topstep_pnl: Optional[float],
        sl_price: Optional[float],
        tp_price: Optional[float],
        direction: Optional[str],
        trail_triggered: bool,
        zone_id: Optional[str] = None,
        conf_payload: Optional[Dict] = None,
        original_sl_price: Optional[float] = None,
        original_tp_price: Optional[float] = None,
    ):
        """Append a single exit record to data/live_exits.json so trade-history
        can map fills (which only carry pnl) to true exit reason buckets
        (TP / SL / TRAIL_SL / FLATTEN / MANUAL).

        Best-effort: file errors are logged but never raised — losing one row
        is preferable to crashing the engine on a disk hiccup.
        """
        try:
            existing: List[dict] = []
            if os.path.exists(self._exits_file):
                try:
                    with open(self._exits_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        if isinstance(loaded, list):
                            existing = loaded
                except Exception:
                    existing = []

            existing.append({
                "account_id": self.account_id,
                "contract_id": self.contract_id,
                "exit_reason": exit_reason,
                "exit_time": exit_time.isoformat() if exit_time else None,
                "entry_time": entry_time.isoformat() if entry_time else None,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "topstep_pnl": topstep_pnl,
                "sl_price": sl_price,
                "tp_price": tp_price,
                "original_sl_price": original_sl_price or sl_price,
                "original_tp_price": original_tp_price or tp_price,
                "direction": direction,
                "zone_id": zone_id,
                "mode": conf_payload.get("mode") if conf_payload else None,
                "side": conf_payload.get("side") if conf_payload else None,
                "largest_tf": conf_payload.get("largest_tf") if conf_payload else None,
                "wall_id": conf_payload.get("wall_id") if conf_payload else None,
                "labels": conf_payload.get("labels") if conf_payload else [],
                "primary_zone": conf_payload.get("primary_zone") if conf_payload else None,
                "trail_triggered": trail_triggered,
                "size": self.contract_size,
            })
            # Keep file bounded — last 5k rows is far more than any practice run will produce.
            if len(existing) > 5000:
                existing = existing[-5000:]

            os.makedirs(os.path.dirname(self._exits_file), exist_ok=True)
            with open(self._exits_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to persist exit record: {e}")

    def _persist_trade_record(
        self,
        exit_reason: str,
        entry_time: Optional[datetime],
        exit_time: datetime,
        entry_price: Optional[float],
        signal: Optional[TradeSignal],
        conf_payload: Optional[Dict],
        trail_triggered: bool,
        status: str = "closed",
        exit_price: Optional[float] = None,
        topstep_pnl: Optional[float] = None,
    ):
        """Append one fully-explainable order record to data/trades.json.

        `status` captures the order's final disposition:
          - "closed"    : filled then exited (won/exit_price from TP/SL)
          - "cancelled" : placed as a LIMIT but never filled (price never touched),
                          cancelled by timeout / pre-flatten / manual / shutdown.
                          won = null, no exit_price — but the FULL "why" (weights x
                          features) is still recorded, so unfilled signals are
                          auditable too (為何下單、為何沒成交).

        Each row carries the confluence weights/feature contributions, the scorer
        version, all signal/config params, and the outcome — permanently replayable
        and auditable (可解釋 + 可複刻), surviving restarts. Best-effort, capped at
        10k rows. Trend-only orders store null confluence weights.
        """
        try:
            filled = status == "closed"
            if filled and topstep_pnl is not None:
                won = topstep_pnl > 0
            else:
                won = (exit_reason == "tp") if filled else None
            # Prefer the actual Topstep closing fill price.  Only fall back to
            # the intended bracket when the broker fill is not yet available.
            if filled and exit_price is None and signal is not None:
                exit_price = signal.tp_price if exit_reason == "tp" else signal.sl_price

            scorer = getattr(self.confluence, "scorer", None) if self.confluence else None
            cfg = getattr(self.confluence, "cfg", None) if self.confluence else None

            record = {
                "exit_time": exit_time.isoformat() if exit_time else None,
                "entry_time": entry_time.isoformat() if entry_time else None,
                "account_id": self.account_id,
                "contract_id": self.contract_id,
                "strategy": self.strategy_mode,
                "status": status,
                "direction": signal.direction.value if signal else None,
                "size": self.contract_size,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "topstep_pnl": topstep_pnl,
                "sl_price": signal.sl_price if signal else None,
                "tp_price": signal.tp_price if signal else None,
                "original_sl_price": (
                    getattr(signal, "original_sl_price", signal.sl_price)
                    if signal else None
                ),
                "original_tp_price": (
                    getattr(signal, "original_tp_price", signal.tp_price)
                    if signal else None
                ),
                "exit_reason": exit_reason,
                "won": won,
                "trail_triggered": trail_triggered,
                "shadow": bool(self._conf_shadow),
                # ── explainable confluence payload (None for trend trades) ──
                "confluence": None,
            }

            if conf_payload:
                record["confluence"] = {
                    "mode": conf_payload.get("mode"),
                    "side": conf_payload.get("side"),
                    "prob": conf_payload.get("prob"),
                    "score": conf_payload.get("score"),
                    "cluster_weight": conf_payload.get("weight"),
                    "tfs": conf_payload.get("tfs"),
                    "largest_tf": conf_payload.get("largest_tf"),
                    "wall_id": conf_payload.get("wall_id"),
                    "labels": conf_payload.get("labels"),
                    "primary_zone": conf_payload.get("primary_zone"),
                    "reason": conf_payload.get("reason"),
                    # full per-feature breakdown: (name, value, weight, contribution)
                    "contributions": [
                        {"feature": n, "value": v, "weight": w, "contribution": c}
                        for (n, v, w, c) in conf_payload.get("explain", [])
                    ],
                }
            if scorer is not None:
                record["scorer"] = {
                    "source": self.confluence.scorer_source,
                    "bias": scorer.bias,
                    "weights": dict(scorer.weights),
                    "trained_at": scorer.meta.get("trained_at"),
                    "train_auc": scorer.meta.get("train_auc"),
                    "n_samples": scorer.meta.get("n_samples"),
                }
            if cfg is not None:
                record["config"] = {
                    "band_ticks": cfg.band_ticks,
                    "min_distinct_tf": cfg.min_distinct_tf,
                    "rr": cfg.rr,
                    "base_minutes": self.confluence.base_minutes,
                    "min_score": self.confluence.min_score,
                    "ev_floor": getattr(cfg, "ev_floor", None),
                    "gate": ("ev" if getattr(cfg, "ev_floor", None) is not None else "prob"),
                    "rr_grid": (list(cfg.rr_grid) if getattr(cfg, "rr_grid", None) else None),
                }

            existing: List[dict] = []
            if os.path.exists(self._trades_file):
                try:
                    with open(self._trades_file, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                        if isinstance(loaded, list):
                            existing = loaded
                except Exception:
                    existing = []

            existing.append(record)
            if len(existing) > 10000:
                existing = existing[-10000:]

            os.makedirs(os.path.dirname(self._trades_file), exist_ok=True)
            with open(self._trades_file, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"Failed to persist trade record: {e}")

    def _read_json_list_or_dict(self, path: str):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _lock_date_for_ts(self, ts_str: Optional[str]) -> Optional[str]:
        if not ts_str:
            return None
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_UTC_TZ)
            ct = ts.astimezone(_CT)
            if ct.hour >= 17:
                return (ct + timedelta(days=1)).strftime("%Y-%m-%d")
            return ct.strftime("%Y-%m-%d")
        except Exception:
            return None

    @staticmethod
    def _breakout_direction_from_trade_direction(direction: Optional[str]) -> Optional[str]:
        d = str(direction or "").lower()
        if d in ("buy", "long", "up"):
            return "up"
        if d in ("sell", "short", "down"):
            return "down"
        return None

    def _candidate_lock_zones(self) -> List[ConsolidationZone]:
        zones: List[ConsolidationZone] = []
        active = self.detector.get_active_zone()
        for z in (active,):
            if z and all(existing.zone_id != z.zone_id for existing in zones):
                zones.append(z)
        return zones

    def _infer_lock_from_exit(self, row: Dict, zones: List[ConsolidationZone]) -> Optional[tuple[str, str]]:
        direction = self._breakout_direction_from_trade_direction(row.get("direction"))
        if not direction:
            return None
        if row.get("zone_id"):
            return str(row.get("zone_id")), direction
        try:
            entry = float(row.get("entry_price"))
        except (TypeError, ValueError):
            return None
        tolerance = max(self.tick_size * 8, 2.0)
        for z in zones:
            target = z.vah_80 if direction == "up" else z.val_80
            if abs(entry - target) <= tolerance:
                return z.zone_id, direction
        return None

    def _load_breakout_locks(self) -> set[tuple[str, str]]:
        today = self._get_topstep_trade_date()
        keys: set[tuple[str, str]] = set()

        data = self._read_json_list_or_dict(self._breakout_locks_file)
        records = data.get("locks", []) if isinstance(data, dict) else []
        for row in records:
            if not isinstance(row, dict):
                continue
            if row.get("trade_date") != today:
                continue
            if row.get("account_id") != self.account_id:
                continue
            if row.get("contract_id") != self.contract_id:
                continue
            zid = row.get("zone_id")
            direction = row.get("direction")
            if zid and direction:
                keys.add((str(zid), str(direction)))

        zones = self._candidate_lock_zones()
        exits = self._read_json_list_or_dict(self._exits_file)
        for row in (exits if isinstance(exits, list) else []):
            if not isinstance(row, dict):
                continue
            if row.get("account_id") != self.account_id:
                continue
            if row.get("contract_id") != self.contract_id:
                continue
            if self._lock_date_for_ts(row.get("entry_time") or row.get("exit_time")) != today:
                continue
            inferred = self._infer_lock_from_exit(row, zones)
            if inferred:
                keys.add(inferred)

        if hasattr(self.trend_follow, "set_traded_breakouts"):
            self.trend_follow.set_traded_breakouts(keys)
        self._session_direction_locks = set(keys)
        return keys

    def _completed_exit_lock_keys(self) -> set[tuple[str, str]]:
        today = self._get_topstep_trade_date()
        zones = self._candidate_lock_zones()
        exits = self._read_json_list_or_dict(self._exits_file)
        keys: set[tuple[str, str]] = set()
        for row in (exits if isinstance(exits, list) else []):
            if not isinstance(row, dict):
                continue
            if row.get("account_id") != self.account_id:
                continue
            if row.get("contract_id") != self.contract_id:
                continue
            if self._lock_date_for_ts(row.get("entry_time") or row.get("exit_time")) != today:
                continue
            inferred = self._infer_lock_from_exit(row, zones)
            if inferred:
                keys.add(inferred)
        return keys

    async def _prune_stale_pending_breakout_locks(self) -> None:
        """Remove startup locks left behind by cancelled/disappeared pending entries.

        A completed trade is still locked via live_exits.json. An active position
        or any open order on this contract also keeps the lock. Only flat/no-open
        order startup locks with no exit record are considered stale.
        """
        if self._open_position:
            return
        data = self._read_json_list_or_dict(self._breakout_locks_file)
        if not isinstance(data, dict):
            return
        records = data.get("locks")
        if not isinstance(records, list):
            return

        try:
            open_orders = await self.client.get_open_orders(self.account_id)
        except Exception as e:
            self._log_event(f"無法檢查 open orders，保留 breakout 鎖: {e}", "error")
            return
        if any(self._order_contract_matches(od) for od in (open_orders or [])):
            return

        today = self._get_topstep_trade_date()
        completed = self._completed_exit_lock_keys()
        kept = []
        removed = 0
        for row in records:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            if row.get("trade_date") != today:
                kept.append(row)
                continue
            if row.get("account_id") != self.account_id:
                kept.append(row)
                continue
            if row.get("contract_id") != self.contract_id:
                kept.append(row)
                continue

            key = (str(row.get("zone_id")), row.get("direction"))
            if key in completed:
                kept.append(row)
                continue
            removed += 1

        if not removed:
            return

        data["locks"] = kept
        data["saved_at"] = datetime.utcnow().isoformat()
        try:
            os.makedirs(os.path.dirname(self._breakout_locks_file), exist_ok=True)
            with open(self._breakout_locks_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self._log_event(f"清除 {removed} 個 stale pending breakout 鎖")
        except Exception as e:
            logger.warning(f"Failed to prune stale breakout locks: {e}")

    def _persist_breakout_lock(self, signal: TradeSignal):
        direction = self._breakout_direction_from_trade_direction(signal.direction.value)
        if not signal.zone_id or not direction:
            return
        today = self._get_topstep_trade_date()
        data = self._read_json_list_or_dict(self._breakout_locks_file)
        if not isinstance(data, dict):
            data = {"locks": []}
        records = data.get("locks")
        if not isinstance(records, list):
            records = []
            data["locks"] = records

        key = (today, self.account_id, self.contract_id, str(signal.zone_id), direction)
        for row in records:
            if not isinstance(row, dict):
                continue
            row_key = (
                row.get("trade_date"),
                row.get("account_id"),
                row.get("contract_id"),
                str(row.get("zone_id")),
                row.get("direction"),
            )
            if row_key == key:
                return

        records.append({
            "trade_date": today,
            "account_id": self.account_id,
            "contract_id": self.contract_id,
            "zone_id": str(signal.zone_id),
            "direction": direction,
            "entry_price": signal.entry_price,
            "order_id": self._pending_order_id,
            "status": "pending_entry",
            "created_at": datetime.utcnow().isoformat(),
        })
        data["saved_at"] = datetime.utcnow().isoformat()
        data["locks"] = records[-1000:]
        try:
            os.makedirs(os.path.dirname(self._breakout_locks_file), exist_ok=True)
            with open(self._breakout_locks_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to persist breakout lock: {e}")

    def _session_direction_is_locked(self, signal: TradeSignal) -> bool:
        limit = self._tr_one_trade_per_session
        if not limit:
            return False
        direction = self._breakout_direction_from_trade_direction(signal.direction.value)
        if not signal.zone_id or not direction:
            return False
        return (str(signal.zone_id), direction) in self._session_direction_locks

    def _mark_session_direction_locked(self, signal: TradeSignal):
        direction = self._breakout_direction_from_trade_direction(signal.direction.value)
        if signal.zone_id and direction:
            self._session_direction_locks.add((str(signal.zone_id), direction))

    def _unlock_signal_breakout(self, signal: TradeSignal):
        direction = self._breakout_direction_from_trade_direction(signal.direction.value)
        if hasattr(self.trend_follow, "unlock_breakout") and signal.zone_id and direction:
            self.trend_follow.unlock_breakout(signal.zone_id, direction)

    def _release_breakout_lock(self, signal: TradeSignal):
        """Release a cleanly-cancelled pending-entry lock so the same breakout
        can be refreshed immediately on the latest candle."""
        direction = self._breakout_direction_from_trade_direction(signal.direction.value)
        if not signal.zone_id or not direction:
            return

        key = (str(signal.zone_id), direction)
        self._session_direction_locks.discard(key)
        if hasattr(self.trend_follow, "unlock_breakout"):
            self.trend_follow.unlock_breakout(signal.zone_id, direction)

        today = self._get_topstep_trade_date()
        data = self._read_json_list_or_dict(self._breakout_locks_file)
        if not isinstance(data, dict):
            return
        records = data.get("locks")
        if not isinstance(records, list):
            return

        kept = []
        removed = 0
        for row in records:
            if not isinstance(row, dict):
                kept.append(row)
                continue
            row_key = (
                row.get("trade_date"),
                row.get("account_id"),
                row.get("contract_id"),
                str(row.get("zone_id")),
                row.get("direction"),
            )
            if row_key == (today, self.account_id, self.contract_id, str(signal.zone_id), direction):
                removed += 1
                continue
            kept.append(row)

        if not removed:
            return

        data["locks"] = kept
        data["saved_at"] = datetime.utcnow().isoformat()
        try:
            os.makedirs(os.path.dirname(self._breakout_locks_file), exist_ok=True)
            with open(self._breakout_locks_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to release breakout lock: {e}")

    async def _calc_pnl_from_trades(self, *, emit_log: bool = True) -> float:
        """Fallback: sum today's realized PnL from trade history.

        Uses the same field names as routes.py _parse_fill:
          - PnL: profitAndLoss / ProfitAndLoss / pnl
          - Timestamp: creationTimestamp / CreationTimestamp / timestamp
        Only sums closing fills (profitAndLoss != 0).
        """
        try:
            trades = await self.client.get_trade_history(self.account_id, days=2)
            if not trades:
                return 0.0
            today = self._get_topstep_trade_date()
            total_pnl = 0.0
            count = 0
            for t in trades:
                # PnL field (same order as routes.py)
                pnl_raw = t.get("profitAndLoss")
                if pnl_raw is None:
                    pnl_raw = t.get("ProfitAndLoss")
                if pnl_raw is None:
                    pnl_raw = t.get("pnl")
                if not pnl_raw or float(pnl_raw) == 0:
                    continue  # opening fill, no PnL

                # Timestamp field
                ts_str = (
                    t.get("creationTimestamp")
                    or t.get("CreationTimestamp")
                    or t.get("timestamp")
                    or t.get("fillTime")
                    or ""
                )
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    ct = ts.astimezone(_CT)
                    trade_date = (ct + timedelta(days=1)).strftime("%Y-%m-%d") if ct.hour >= 17 else ct.strftime("%Y-%m-%d")
                    if trade_date == today:
                        total_pnl += float(pnl_raw)
                        count += 1
                except (ValueError, TypeError):
                    continue
            if emit_log and count > 0:
                self._log_event(f"[PNL] 從 trade history 計算: ${total_pnl:,.0f} ({count} closes)")
            return total_pnl
        except Exception as e:
            logger.warning(f"Failed to calc PnL from trades: {e}")
            return 0.0

    @staticmethod
    def _first_present(row: Dict[str, Any], *keys: str):
        for key in keys:
            if key in row and row.get(key) is not None:
                return row.get(key)
        return None

    @staticmethod
    def _coerce_float(value) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(str(value).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_utc_dt(value) -> Optional[datetime]:
        if not value:
            return None
        try:
            if isinstance(value, datetime):
                dt = value
            else:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return dt.replace(tzinfo=_UTC_TZ)
            return dt.astimezone(_UTC_TZ)
        except (TypeError, ValueError):
            return None

    def _topstep_fill_time(self, row: Dict[str, Any]) -> Optional[datetime]:
        return self._coerce_utc_dt(self._first_present(
            row,
            "creationTimestamp",
            "CreationTimestamp",
            "createdAt",
            "timestamp",
            "fillTime",
        ))

    def _topstep_fill_price(self, row: Dict[str, Any]) -> Optional[float]:
        price = self._coerce_float(self._first_present(
            row,
            "price",
            "Price",
            "fillPrice",
            "averagePrice",
        ))
        if price is None:
            return None
        return self._round_to_tick(price)

    def _topstep_fill_pnl(self, row: Dict[str, Any]) -> Optional[float]:
        return self._coerce_float(self._first_present(
            row,
            "profitAndLoss",
            "ProfitAndLoss",
            "pnl",
        ))

    def _topstep_trade_contract_matches(self, row: Dict[str, Any]) -> bool:
        contract = self._first_present(row, "contractId", "ContractId", "contract_id")
        return not contract or str(contract) == str(self.contract_id)

    async def _latest_topstep_closing_fill(
        self,
        *,
        entry_time: Optional[datetime],
        attempts: int = 3,
        delay_seconds: float = 0.4,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent Topstep close fill after this entry.

        Topstep is the source of truth for whether the position actually closed
        and at what price.  We only use local/market-price heuristics when the
        broker trade-history endpoint has not published the close yet.
        """
        entry_dt = self._coerce_utc_dt(entry_time)
        min_time = entry_dt - timedelta(seconds=10) if entry_dt else None

        for attempt in range(max(1, attempts)):
            try:
                rows = await self.client.get_trade_history(self.account_id, days=2)
            except Exception as e:
                self._log_event(f"[TOPSTEP EXIT] trade history 查詢失敗: {e}", "error")
                rows = []

            candidates: List[tuple[datetime, Dict[str, Any]]] = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                if not self._topstep_trade_contract_matches(row):
                    continue
                pnl = self._topstep_fill_pnl(row)
                if pnl is None or abs(pnl) < 1e-9:
                    continue  # opening fills normally have no realized PnL
                ts = self._topstep_fill_time(row)
                if min_time and ts and ts < min_time:
                    continue
                candidates.append((ts or datetime.min.replace(tzinfo=_UTC_TZ), row))

            if candidates:
                candidates.sort(key=lambda item: item[0])
                return candidates[-1][1]

            if attempt < max(1, attempts) - 1 and delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

        return None

    def _exit_reason_from_topstep_fill(
        self,
        close_fill: Optional[Dict[str, Any]],
        signal: Optional[TradeSignal],
        forced: Optional[str],
    ) -> tuple[str, Optional[float], Optional[float], Optional[datetime]]:
        """Classify close using Topstep fill price first, then pnl fallback."""
        if close_fill:
            exit_price = self._topstep_fill_price(close_fill)
            topstep_pnl = self._topstep_fill_pnl(close_fill)
            exit_time = self._topstep_fill_time(close_fill)
        else:
            exit_price = None
            topstep_pnl = None
            exit_time = None

        if forced:
            return forced, exit_price, topstep_pnl, exit_time

        if signal and exit_price is not None:
            tp_p = signal.tp_price
            sl_p = signal.sl_price
            tick_tol = max(self.tick_size * 1.5, 0.01)
            tp_dist = abs(exit_price - tp_p)
            sl_dist = abs(exit_price - sl_p)
            if tp_dist <= tick_tol and tp_dist <= sl_dist:
                return "tp", exit_price, topstep_pnl, exit_time
            if sl_dist <= tick_tol and sl_dist <= tp_dist:
                return ("trail_sl" if self._trail_sl_triggered else "sl"), exit_price, topstep_pnl, exit_time
            if tp_dist + tick_tol < sl_dist:
                return "tp", exit_price, topstep_pnl, exit_time
            if sl_dist + tick_tol < tp_dist:
                return ("trail_sl" if self._trail_sl_triggered else "sl"), exit_price, topstep_pnl, exit_time

        if topstep_pnl is not None:
            if topstep_pnl < 0:
                return ("trail_sl" if self._trail_sl_triggered else "sl"), exit_price, topstep_pnl, exit_time
            if topstep_pnl > 0:
                # With no usable exit price, positive PnL could be TP or a trailed
                # stop.  Treat TP as the safer default; price-based logic above
                # handles normal trail-SL cases.
                return "tp", exit_price, topstep_pnl, exit_time

        # Last-resort compatibility path: no Topstep close fill available yet.
        if signal and self._last_market_price:
            sl_p = signal.sl_price
            tp_p = signal.tp_price
            mkt = self._last_market_price
            if abs(mkt - sl_p) < abs(mkt - tp_p):
                return ("trail_sl" if self._trail_sl_triggered else "sl"), exit_price, topstep_pnl, exit_time
            return "tp", exit_price, topstep_pnl, exit_time

        return ("trail_sl" if self._trail_sl_triggered else "tp"), exit_price, topstep_pnl, exit_time

    async def _refresh_account_snapshot(
        self,
        reason: str = "account",
        *,
        emit_log: bool = False,
        attempts: int = 1,
    ) -> bool:
        """Refresh balance and daily PnL from account snapshot, with trade history fallback."""
        def first_present(row: Dict, *keys: str):
            for key in keys:
                if key in row and row.get(key) is not None:
                    return row.get(key)
            return None

        def as_float(value, default: float = 0.0) -> float:
            if value is None:
                return default
            try:
                return float(str(value).replace(",", "").replace("$", ""))
            except (TypeError, ValueError):
                return default

        last_error: Optional[Exception] = None
        tries = max(1, attempts)
        for attempt in range(tries):
            try:
                accounts = await self.client.get_accounts()
                account = next((acc for acc in accounts if acc.get("id") == self.account_id), None)
                if not account:
                    raise ValueError(f"account {self.account_id} not found")

                balance = first_present(account, "balance", "Balance")
                if balance is not None:
                    self._capital = as_float(balance, self._capital)

                daily = first_present(account, "dailyPnl", "dailyPnL", "pnl", "PnL")
                open_pnl = first_present(account, "openPnl", "openPnL", "unrealizedPnl", "unrealizedPnL")
                closed_pnl = first_present(account, "closedPnl", "closedPnL", "realizedPnl", "realizedPnL")
                source = "account"
                if daily is not None:
                    self._daily_pnl = as_float(daily)
                    source = "account dailyPnl"
                elif open_pnl is not None or closed_pnl is not None:
                    self._daily_pnl = as_float(open_pnl) + as_float(closed_pnl)
                else:
                    self._daily_pnl = await self._calc_pnl_from_trades(emit_log=emit_log)
                    source = "trade history"

                self._last_account_refresh = time_mod.time()
                if emit_log:
                    self._log_event(
                        f"[PNL] {reason}: daily=${self._daily_pnl:,.0f} | "
                        f"balance=${self._capital:,.0f} ({source})"
                    )
                return True
            except Exception as e:
                last_error = e
                if attempt < tries - 1:
                    await asyncio.sleep(0.5)

        if emit_log and last_error:
            self._log_event(f"[PNL] {reason} 更新失敗: {last_error}", "error")
        return False

    def _get_topstep_trade_date(self) -> str:
        """Current TopStep trade date (CT 17:00 boundary)."""
        now = datetime.utcnow().replace(tzinfo=_UTC_TZ)
        ct = now.astimezone(_CT)
        if ct.hour >= 17:
            return (ct + timedelta(days=1)).strftime("%Y-%m-%d")
        return ct.strftime("%Y-%m-%d")

    def get_status(self) -> Dict:
        """Return current engine state for frontend."""
        # Use active_signal (after fill) or pending_signal (before fill)
        sig = self._pending_signal or self._active_signal
        sig_payload = (
            self._pending_conf_payload
            or self._pending_mlc2_payload
            or self._active_conf_payload
            or {}
        )
        return {
            "engine_version": ENGINE_VERSION,
            "running": self._running,
            "account_id": self.account_id,
            "contract_id": self.contract_id,
            "position": self._open_position,
            "pending_order_id": self._pending_order_id,
            "pending_signal": {
                "direction": sig.direction.value,
                "entry_price": sig.entry_price,
                "sl_price": sig.sl_price,
                "tp_price": sig.tp_price,
                "original_sl_price": getattr(sig, "original_sl_price", sig.sl_price),
                "original_tp_price": getattr(sig, "original_tp_price", sig.tp_price),
                "mode": sig_payload.get("mode"),
                "side": sig_payload.get("side"),
                "largest_tf": sig_payload.get("largest_tf"),
                "wall_id": sig_payload.get("wall_id"),
                "labels": sig_payload.get("labels") or [],
                "primary_zone": sig_payload.get("primary_zone"),
                "strategy": sig.strategy.value,
                "order_type": getattr(sig, "order_type", "limit"),
            } if sig else None,
            "pending_age": self._pending_age,
            "pending_timeout": self.trend_follow.PENDING_TIMEOUT_CANDLES,
            "sl_order_id": self._sl_order_id,
            "tp_order_id": self._tp_order_id,
            "protection_synced": self._protection_synced,
            "auto_oco_fail_safe_triggered": self._auto_oco_fail_safe_triggered,
            "auto_oco_settings_url": self.AUTO_OCO_SETTINGS_URL,
            "trail_sl_triggered": self._trail_sl_triggered,
            "trail_trigger_pct": (
                float(getattr(self.strategy_params, "conf_trail_trigger_pct", 0.0) or 0.0)
                if self.strategy_mode == "confluence"
                else (
                    float(getattr(self.strategy_params, "mlc2_trail_trigger_pct", 0.0) or 0.0)
                    if self.strategy_mode == "ml_consolidation_v2"
                    else self._strategy_trigger_pct(StrategyType.TREND_FOLLOW)
                )
            ),
            "trail_lock_pct": (
                float(getattr(self.strategy_params, "conf_trail_lock_pct", 0.0) or 0.0)
                if self.strategy_mode == "confluence"
                else (
                    float(getattr(self.strategy_params, "mlc2_trail_lock_pct", 0.0) or 0.0)
                    if self.strategy_mode == "ml_consolidation_v2"
                    else None
                )
            ),
            "daily_pnl": self._daily_pnl,
            "tp_locked": self._any_full_tp_locked(),
            "full_tp_lock": self._full_tp_lock,
            "full_tp_count": self._full_tp_count,
            "full_tp_locks": {
                "trend": self._full_tp_lock_for_strategy(StrategyType.TREND_FOLLOW),
            },
            "full_tp_counts": dict(self._full_tp_counts),
            "strategy_mode": self.strategy_mode,
            "active_mode": getattr(self.trend_follow, 'active_mode', self.strategy_mode),
            "trend_allowed_sessions": self._trend_session_label(),
            "strategies": self.strategies,
            "disconnected": self._disconnected,
            "capital": self._capital,
            "candles_processed": self._candles_processed,
            "last_market_price": self._last_market_price,
            "fill_price": self._fill_price,
            "zones": self._get_zone_summary(),
            "phase": self._get_phase() if self._running else "引擎已停止",
            "trades": self._trades[-10:],
            "log": self._log[-20:],
            # v1.0.6: explainable confluence telemetry (None unless in that mode)
            "confluence_mode": self.strategy_mode == "confluence",
            "confluence_shadow": self._conf_shadow if self.strategy_mode == "confluence" else None,
            "confluence_scorer": (self.confluence.scorer_source if self.confluence else None),
            "confluence_allowed_sessions": self._confluence_session_label() if self.confluence else "ALL",
            "active_allowed_sessions": (
                self._confluence_session_label() if self.strategy_mode == "confluence"
                else (
                    allowed_sessions_label(getattr(self.strategy_params, "mlc2_allowed_sessions", None) or ["ASIA", "EURO"])
                    if self.strategy_mode == "ml_consolidation_v2"
                    else self._trend_session_label()
                )
            ),
            "confluence_signals": self._conf_signals_log[-20:] if self.confluence else [],
            # full explainable level universe (per-TF/recency zones + weight + distance)
            "confluence_universe": (
                self.confluence.level_universe(self._last_market_price)
                if self.confluence else []
            ),
            # best scorer candidate IGNORING the admission gate, so the chart can
            # draw what the model is weighing in real time (faded when not admitted)
            "confluence_candidate": (
                self.confluence.top_candidate() if self.confluence else None
            ),
            "mlc2_mode": self.strategy_mode == "ml_consolidation_v2",
            "mlc2_shadow": self._mlc2_shadow if self.strategy_mode == "ml_consolidation_v2" else None,
            "mlc2_signals": self._mlc2_signals_log[-20:] if self.mlc2_evaluator else [],
        }

    def _get_zone_phase(self) -> str:
        """Zone status: 發展中/穩定/無"""
        active = self.detector.get_active_zone()
        is_mature = self.detector.is_zone_mature
        if active and is_mature:
            return "穩定"
        if active:
            age_min = active.duration_minutes
            hours = age_min // 60
            mins = age_min % 60
            return f"發展({hours}h{mins:02d}m)"
        return "無"

    def _get_trade_zone_phase(self) -> str:
        """Current-zone gate. v1.0.6 never falls back to a previous/left zone."""
        active = self.detector.get_active_zone()
        if active and self.detector.is_zone_mature:
            return "當前成熟區間"
        if active:
            return "等待成熟"
        return "無"

    def _get_order_phase(self) -> str:
        """Order status: delegate to strategy's get_phase_label() when possible."""
        if self._open_position:
            age = self._position_age
            hours = age // 60
            mins = age % 60
            return f"持倉中({hours}h{mins:02d}m)" if age > 0 else "持倉中"
        if self._pending_order_id:
            age = self._pending_age
            timeout = self.trend_follow.PENDING_TIMEOUT_CANDLES
            if timeout >= 999:
                return f"市價單中({age}s)"
            return f"掛單中({age}/{timeout})"

        # Delegate to strategy's own label if it has one
        if hasattr(self.trend_follow, 'get_phase_label'):
            return self.trend_follow.get_phase_label()

        trend_state = self.trend_follow.raw_state
        if trend_state == "watching":
            count = getattr(self.trend_follow, '_consecutive_outside', 0)
            total = getattr(self.trend_follow, 'BREAKOUT_CONFIRM_CANDLES', 5)
            return f"確認突破中({count}/{total})"
        if trend_state == "confirmed":
            return "入場準備"
        return "等待突破"

    def _get_order_short(self) -> str:
        """Order/position state only (no breakout text): 無 / 掛單中 / 持倉中."""
        if self._open_position:
            age = self._position_age
            hours = age // 60
            mins = age % 60
            return f"持倉中({hours}h{mins:02d}m)" if age > 0 else "持倉中"
        if self._pending_order_id:
            age = self._pending_age
            timeout = self.trend_follow.PENDING_TIMEOUT_CANDLES
            if timeout >= 999:
                return f"市價單中({age}s)"
            return f"掛單中({age}/{timeout})"
        return "無"

    def _tf_detectors(self):
        """Return [(timeframe, ClockBucketZoneDetector), ...] for the active detector.

        Overlap mode exposes one sub-detector per timeframe; single mode has one.
        """
        d = self.detector
        sub = getattr(d, "_detectors", None)
        tfs = getattr(d, "tfs", None)
        if sub and tfs:
            return list(zip(tfs, sub))
        return [(getattr(d, "area_timeframe", "5m"), d)]

    def _update_tf_breakout(self, candle: Candle):
        """Track consecutive breakout candles per timeframe (open+close outside VA)."""
        for tf, det in self._tf_detectors():
            z = det.get_active_zone()
            st = self._tf_breakout.setdefault(tf, {"dir": None, "count": 0})
            if z is None:
                st["dir"], st["count"] = None, 0
                continue
            up = candle.open > z.vah_80 and candle.close > z.vah_80
            down = candle.open < z.val_80 and candle.close < z.val_80
            if up:
                st["count"] = st["count"] + 1 if st["dir"] == "up" else 1
                st["dir"] = "up"
            elif down:
                st["count"] = st["count"] + 1 if st["dir"] == "down" else 1
                st["dir"] = "down"
            else:
                st["dir"], st["count"] = None, 0

    def _tf_breakout_summary(self):
        tfs = [tf for tf, _ in self._tf_detectors()]
        breaking = 0
        dirs = []
        counts = []
        for tf in tfs:
            st = self._tf_breakout.get(tf, {"dir": None, "count": 0})
            c = int(st.get("count", 0) or 0)
            d = st.get("dir")
            if c > 0 and d in ("up", "down"):
                breaking += 1
                dirs.append(d)
                counts.append(c)

        all_breaking = bool(tfs) and breaking == len(tfs) and len(set(dirs)) == 1
        total_count = min(counts) if all_breaking and counts else 0
        direction = dirs[0] if all_breaking else None
        return tfs, breaking, total_count, direction, all_breaking

    def _reset_breakout_confirmation(self):
        if hasattr(self.trend_follow, "reset_breakout_confirmation"):
            self.trend_follow.reset_breakout_confirmation()
        else:
            self.trend_follow.reset()

    def _all_tf_breakout_required(self) -> bool:
        return len(self._tf_detectors()) > 1

    def _strategy_breakout_observable(self) -> bool:
        if not self._all_tf_breakout_required():
            return True
        return self._tf_breakout_summary()[4]

    def _get_confluence_phase(self) -> str:
        """ML (confluence) status line for the top-bar — every parameter in
        Chinese. The trend breakout phase (縂突破…) is meaningless here, so ML
        mode shows its own state: scorer, prob gate, how many timeframes have a
        zone vs the requirement, band/RR, position state, and the latest signal.
        """
        c = self.confluence
        if c is None:
            return "ML 未初始化"
        try:
            have = len(c.zones_by_tf())
        except Exception:
            have = 0
        total = len(c.timeframes)
        need = c.cfg.min_distinct_tf
        ev_floor = getattr(c.cfg, "ev_floor", None)
        if ev_floor is not None:
            # EV-priority gate active: breakeven prob = 1/(1+RR).
            be = 1.0 / (1.0 + c.cfg.rr)
            thr_txt = f"EV門檻≥{ev_floor:+.2f}(盈虧平衡勝率{be * 100:.0f}%)"
        elif c.min_score and c.min_score != 0.0:
            thr = 1.0 / (1.0 + math.exp(-c.min_score))
            thr_txt = f"勝率門檻≥{thr * 100:.0f}%"
        else:
            thr_txt = "勝率門檻 無"
        rr_grid = getattr(c.cfg, "rr_grid", None)
        rr_txt = (f"RR {'/'.join(f'{r:g}' for r in rr_grid)} (EV挑選)"
                  if rr_grid else f"RR{c.cfg.rr:g}")
        parts = [
            "ML匯流",
            f"評分器 {c.scorer_source}",
            thr_txt,
            f"匯流區間 {have}/{total}(需≥{need})",
            f"帶寬{c.cfg.band_ticks:g}刻度 {rr_txt}",
        ]
        order = self._get_order_short()
        if order != "無":
            parts.append(order)
        if self._conf_signals_log:
            last = self._conf_signals_log[-1]
            mode_cn = "逆勢" if last.get("mode") == "reversion" else "順勢"
            dir_cn = "做多" if str(last.get("direction", "")).lower() in ("buy", "long") else "做空"
            prob = last.get("prob")
            tail = f"最近 {mode_cn}{dir_cn}"
            if prob is not None:
                tail += f" 勝率{prob * 100:.0f}%"
            parts.append(tail)
        return " ｜ ".join(parts)

    def _get_phase(self) -> str:
        """Per-timeframe breakout phase for the top-bar PHASE display, e.g.
        '5m:等待突破  15m:突破中↑(3/7分)  縂突破區間(1/2) 縂突破時長(3/7分)'.

        ML (confluence) mode uses its own status line instead (no breakout phase).
        """
        if self.strategy_mode == "confluence":
            return self._get_confluence_phase()
        confirm = max(1, int(getattr(self.trend_follow, "CONFIRM_BARS", 1) or 1))
        tfs, breaking, total_count, _, _ = self._tf_breakout_summary()
        parts = []
        for tf in tfs:
            st = self._tf_breakout.get(tf, {"dir": None, "count": 0})
            c = int(st.get("count", 0) or 0)
            if c > 0:
                arrow = "↑" if st.get("dir") == "up" else "↓"
                parts.append(f"{tf}:突破中{arrow}({c}/{confirm}分)")
            else:
                parts.append(f"{tf}:等待突破")
        tail = f"縂突破區間({breaking}/{len(tfs)}) 縂突破時長({total_count}/{confirm}分)"
        return "  ".join(parts) + "  " + tail

    def _get_zone_summary(self) -> List[Dict]:
        zones = self.detector.get_all_zones()
        result = []
        for z in zones[-10:]:
            result.append({
                "zone_id": z.zone_id,
                "poc": z.poc,
                "vah_80": z.vah_80,
                "val_80": z.val_80,
                "high_100": z.high_100,
                "low_100": z.low_100,
                "status": z.status.value,
                "formed_at": z.formed_at.isoformat() if z.formed_at else None,
                "left_at": z.left_at.isoformat() if z.left_at else None,
                "exit_direction": z.exit_direction,
                "num_candles": z.num_candles,
                "mature": self.detector.is_zone_mature if z == self.detector.get_active_zone() else False,
                "va_curve": getattr(z, 'va_curve', None) or None,
            })
        return result

    def _log_event(self, msg: str, level: str = "info"):
        self._log.append(str(msg))
        if len(self._log) > 100:
            self._log = self._log[-50:]
        if level == "error":
            logger.error(msg)
        else:
            logger.info(msg)

    async def start(self, historical_candles: List[Candle]):
        """Start the live engine. Feed historical candles first for zone state."""
        if self._running:
            return

        self._running = True
        self._today = datetime.utcnow().strftime("%Y-%m-%d")
        self._daily_pnl = 0.0
        self._trades = []
        self._log = []
        self._reset_full_tp_counts()
        self._auto_oco_fail_safe_triggered = False
        self._last_auto_oco_retry_ts = 0.0
        self._last_account_refresh = 0.0

        # Log candle date range
        if historical_candles:
            first_ts = historical_candles[0].timestamp.strftime("%Y-%m-%d %H:%M")
            last_ts = historical_candles[-1].timestamp.strftime("%Y-%m-%d %H:%M")
            self._log_event(
                f"載入 {len(historical_candles)} 根歷史K線 | "
                f"範圍: {first_ts} ~ {last_ts}"
            )
        else:
            self._log_event("無歷史K線! warm-up 跳過", "error")

        # Warm up: feed historical candles to the zone detector and rebuild
        # breakout confirmation state without placing historical orders.
        # MUST sort chronologically — API returns newest-first
        historical_candles = sorted(historical_candles, key=lambda c: c.timestamp)
        can_observe_strategy = hasattr(self.trend_follow, "observe")
        for c in historical_candles:
            self.detector.update(c)
            self._update_tf_breakout(c)
            if can_observe_strategy:
                if self._strategy_breakout_observable() and self._trend_session_allowed(c.timestamp):
                    self.trend_follow.observe(
                        c,
                        self.detector.get_recent_zones(),
                        self.detector.is_zone_mature,
                    )
                else:
                    self._reset_breakout_confirmation()
            elif hasattr(self.trend_follow, 'warmup'):
                self.trend_follow.warmup(c)

        # v1.0.6: warm up confluence detectors on the SAME history so the first
        # live bar already has full multi-timeframe zone context (live==backtest).
        if self.confluence is not None:
            self.confluence.warmup(historical_candles)
            self._log_event(
                f"[confluence] warm-up {len(historical_candles)} bars | "
                f"TFs={self.confluence.timeframes} | scorer={self.confluence.scorer_source} | "
                f"{'SHADOW (log-only)' if self._conf_shadow else 'LIVE (places orders)'}"
            )

        if self.mlc2_evaluator is not None:
            self.mlc2_evaluator.warmup(historical_candles)
            self._log_event(
                f"[ml_consol_v2] warm-up {len(historical_candles)} bars | "
                f"LB={self.mlc2_evaluator.lookback} Band={self.mlc2_evaluator.band_ticks} "
                f"RR={self.mlc2_evaluator.rr} | "
                f"{'SHADOW (log-only)' if self._mlc2_shadow else 'LIVE (places orders)'}"
            )

        self._candles_processed = len(historical_candles)
        self._all_candles = list(historical_candles)
        self._last_candle_time = historical_candles[-1].timestamp.isoformat() if historical_candles else None

        # Legacy fallback: old strategies only warmed recent-candle buffers, so
        # clear any accidental state. SessionTrendFollow.observe() intentionally
        # keeps the restored breakout count/armed state.
        if not can_observe_strategy and hasattr(self.trend_follow, 'reset_state_only'):
            self.trend_follow.reset_state_only()
        elif not can_observe_strategy:
            self.trend_follow.reset()

        confirm_count = int(getattr(self.trend_follow, "_confirm_count", 0) or 0)
        if confirm_count > 0:
            confirm_total = max(1, int(getattr(self.trend_follow, "CONFIRM_BARS", 1) or 1))
            direction = getattr(self.trend_follow, "_breakout_direction", None) or "?"
            armed = "YES" if getattr(self.trend_follow, "_armed", False) else "NO"
            self._log_event(
                f"恢復突破確認狀態: dir={direction} count={confirm_count}/{confirm_total} armed={armed}"
            )

        active = self.detector.get_active_zone()
        is_mature = self.detector.is_zone_mature
        if active:
            self._log_event(
                f"Warm-up 完成 | session zone {active.zone_id} | "
                f"bars={active.num_candles} | mature={'YES' if is_mature else 'NO'} | "
                f"POC={active.poc:.2f} VAH={active.vah_80:.2f} VAL={active.val_80:.2f}"
            )
        else:
            self._log_event("Warm-up 完成 | 尚無 session zone")

        # Get initial account balance + today's PnL from account snapshot
        try:
            positions = await self.client.get_positions(self.account_id)
            self._open_position = positions[0] if positions else None
            self._today = self._get_topstep_trade_date()
            await self._refresh_account_snapshot("帳戶初始化", emit_log=True, attempts=2)
        except Exception as e:
            self._log_event(f"取得帳戶資訊失敗: {e}", "error")

        await self._prune_stale_pending_breakout_locks()
        locked = self._load_breakout_locks()
        if locked:
            labels = ", ".join(f"{zid}:{direction}" for zid, direction in sorted(locked))
            self._log_event(f"載入 breakout 鎖: {labels}")

        active = self.detector.get_active_zone()
        all_z = self.detector.get_all_zones()
        active_info = "None"
        if active:
            active_info = (
                f"{active.zone_id} POC={active.poc:.2f} "
                f"H100={active.high_100:.2f} L100={active.low_100:.2f} "
                f"bars={active.num_candles}"
            )
        self._log_event(
            f"引擎啟動 [{ENGINE_VERSION}] | 帳戶={self.account_id} | "
            f"區間={len(all_z)} | 活躍={active_info} | "
            f"策略={self.strategies}"
        )

        # Start main loop
        self._task = asyncio.create_task(self._main_loop())

    async def stop(self):
        """Stop the engine. Cancel pending orders. Save zones to disk."""
        self._save_zones()  # persist zones before shutdown
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._pending_order_id:
            try:
                success = await self.client.cancel_order(self.account_id, self._pending_order_id)
                if success:
                    self._log_event(f"取消掛單 #{self._pending_order_id}")
                    if self._pending_signal:
                        self._release_breakout_lock(self._pending_signal)
                else:
                    self._log_event(f"取消掛單 #{self._pending_order_id} 失敗，保留 session-direction 鎖", "error")
            except Exception as e:
                self._log_event(f"取消掛單失敗: {e}", "error")
            if self._pending_signal:
                self._persist_trade_record(
                    exit_reason="cancelled", entry_time=self._entry_time,
                    exit_time=datetime.utcnow(), entry_price=None,
                    signal=self._pending_signal,
                    conf_payload=self._pending_conf_payload or self._pending_mlc2_payload,
                    trail_triggered=False, status="cancelled",
                )
            self._pending_order_id = None
            self._pending_signal = None
            self._pending_conf_payload = None
            self._pending_mlc2_payload = None
            self._pending_created_at = None

        self._log_event("引擎已停止")

    async def cancel_pending_now(self):
        """Cancel pending order from UI. Returns True if cancelled."""
        if not self._pending_order_id:
            self._log_event("無掛單可取消")
            return False
        await self._cancel_pending(release_breakout_lock=True)
        return True

    async def _emergency_market_close(self, side: int, reason: str):
        """Place a market order to close position when SL/TP placement fails.
        Called when Stop order is rejected (price already past SL level).
        """
        # Treat reject-on-SL as an SL exit (price already past SL level).
        # If trail had triggered, the heuristic in _sync_position promotes to trail_sl.
        if reason in ("SL_REJECTED", "SL_EXCEPTION"):
            self._force_exit_reason = "trail_sl" if self._trail_sl_triggered else "sl"
        elif reason == "DOUBLE_FILL":
            self._force_exit_reason = "flatten"

        if reason in ("MARKET_FILL_INVALID", "MARKET_FILL_RISK"):
            self._force_exit_reason = "flatten"

        self._log_event(f"[{reason}] 緊急 Market 平倉 side={'SELL' if side == 2 else 'BUY'}")
        try:
            mkt_order = OrderRequest(
                account_id=self.account_id,
                contract_id=self.contract_id,
                order_type=2,  # Market
                side=side,
                size=self.contract_size,
            )
            resp = await self.client.place_order(mkt_order)
            if resp.success:
                self._log_event(f"Market 平倉成功 #{resp.order_id}")
            else:
                self._log_event(
                    f"Market 平倉失敗: {resp.error_message} → 需手動平倉!",
                    "error"
                )
        except Exception as e:
            self._log_event(f"Market 平倉異常: {e} → 需手動平倉!", "error")

    async def flatten_now(self):
        """Emergency flatten all positions AND cancel any working SL/TP/entry orders.

        TopstepX's flatten/closeContract only nets the position — it does NOT
        cancel working SL/TP orders. If we don't cancel them here, the SL
        stop-market and TP limit stay live on the book and can open a new,
        unintended reverse position when price later touches them.
        """
        # Tag the resulting close so _sync_position records it correctly.
        # If trail SL was already triggered, _sync_position will reclassify
        # this flatten as TRAIL_SL (the position was already in profit-protect mode).
        if self._open_position is not None or self._fill_price is not None:
            self._force_exit_reason = "flatten"

        # ── Snapshot the order IDs we need to cancel BEFORE we null them ──
        sl_id = self._sl_order_id
        tp_id = self._tp_order_id
        pending_id = self._pending_order_id

        # ── Cancel working SL / TP / pending-entry orders first ──
        # Run cancels concurrently — they're independent broker calls.
        cancel_tasks = []
        if sl_id:
            cancel_tasks.append(self._cancel_with_retry(sl_id, "SL (flatten)"))
        if tp_id:
            cancel_tasks.append(self._cancel_with_retry(tp_id, "TP (flatten)"))
        if pending_id:
            cancel_tasks.append(self._cancel_with_retry(pending_id, "ENTRY (flatten)"))

        if cancel_tasks:
            try:
                await asyncio.gather(*cancel_tasks, return_exceptions=True)
            except Exception as e:
                # gather with return_exceptions=True shouldn't throw, but be defensive
                self._log_event(f"取消 working orders 異常: {e}", "error")

        # ── Net out any open position ──
        try:
            results = await self.client.flatten_all(self.account_id)
            self._log_event(f"緊急平倉完成: {len(results)} orders")
        except Exception as e:
            self._log_event(f"緊急平倉失敗: {e}", "error")

        # ── Final sweep: query broker for ANY remaining open orders on our
        # contract and force-cancel them. Catches orders we lost track of
        # (e.g., after a restart) or whose cancel call dropped silently.
        try:
            open_orders = await self.client.get_open_orders(self.account_id)
            sweep_tasks = []
            for od in open_orders:
                if od.get("contractId") != self.contract_id:
                    continue
                oid = od.get("id") or od.get("orderId")
                if oid:
                    sweep_tasks.append(self._cancel_with_retry(oid, "SWEEP (flatten)"))
            if sweep_tasks:
                self._log_event(f"flatten 後掃出 {len(sweep_tasks)} 張殘留 working order → 取消")
                await asyncio.gather(*sweep_tasks, return_exceptions=True)
        except Exception as e:
            self._log_event(f"flatten 殘留掃描失敗: {e}", "error")

        await self._refresh_account_snapshot("flatten 後更新", emit_log=True, attempts=3)

        # ── Clear local references regardless of broker result ──
        self._open_position = None
        self._sl_order_id = None
        self._tp_order_id = None
        self._pending_order_id = None
        self._pending_signal = None
        self._pending_age = 0
        self._pending_created_at = None
        self._active_signal = None
        self._fill_price = None
        self._trail_sl_triggered = False
        self._protection_synced = False
        self._position_open_ts = 0.0
        self._last_auto_oco_retry_ts = 0.0

    def _auto_oco_missing_timed_out(self) -> bool:
        """True when an engine-filled position stayed without SL/TP past the grace period."""
        if not self._open_position:
            return False
        if self._sl_order_id and self._tp_order_id:
            return False
        if not self._position_open_ts:
            return False
        return (time_mod.time() - self._position_open_ts) >= self.AUTO_OCO_FAILSAFE_SECONDS

    async def _flatten_and_pause_missing_auto_oco(self):
        """Flatten and stop the engine when Auto OCO protection never appears."""
        if self._auto_oco_fail_safe_triggered:
            return
        self._auto_oco_fail_safe_triggered = True

        missing = []
        if not self._sl_order_id:
            missing.append("SL")
        if not self._tp_order_id:
            missing.append("TP")
        missing_text = "+".join(missing) if missing else "SL/TP"
        elapsed = time_mod.time() - self._position_open_ts if self._position_open_ts else 0.0

        self._log_event(
            f"[AUTO OCO] 入場成交後 {elapsed / 60:.1f} 分鐘仍沒有 {missing_text}。"
            f"可能沒有設置 Auto OCO，立即平倉並暫停運行。設定連結: {self.AUTO_OCO_SETTINGS_URL}",
            "error",
        )

        try:
            await self.flatten_now()
        finally:
            self._running = False
            self._save_zones()
            self._log_event(
                f"[AUTO OCO] 沒有設置 Auto OCO，已經暫停運行。請到 {self.AUTO_OCO_SETTINGS_URL}",
                "error",
            )

    # ── Auto OCO fail-safe ─────────────────────────────────

    async def _monitor_auto_oco_protection(self) -> bool:
        """Retry Auto OCO sync every loop and fail-safe flatten when protection is missing."""
        if not self._open_position:
            return False

        if self._auto_oco_missing_timed_out():
            if self._active_signal:
                await self._sync_auto_oco_protection(self._active_signal, wait_seconds=2.0)
            if self._auto_oco_missing_timed_out():
                await self._flatten_and_pause_missing_auto_oco()
                return True

        if self._active_signal:
            if not self._sl_order_id or not self._tp_order_id or not self._protection_synced:
                now_ts = time_mod.time()
                if now_ts - self._last_auto_oco_retry_ts < self.AUTO_OCO_RETRY_SECONDS:
                    return False
                self._last_auto_oco_retry_ts = now_ts
                missing = []
                if not self._sl_order_id:
                    missing.append("SL")
                if not self._tp_order_id:
                    missing.append("TP")
                if self._sl_order_id and self._tp_order_id and not self._protection_synced:
                    missing.append("PRICE")
                self._log_event(
                    f"持倉中 Auto OCO {'+'.join(missing)} 未同步 -> 重新掃描/修改",
                    "error",
                )
                synced = await self._sync_auto_oco_protection(self._active_signal, wait_seconds=2.0)
                if not synced and self._auto_oco_missing_timed_out():
                    await self._flatten_and_pause_missing_auto_oco()
                    return True
        elif not self._sl_order_id or not self._tp_order_id:
            self._log_event(
                "持倉中無 SL/TP 且無 signal（重啟後或手動入場）-> 需手動設定 SL/TP",
                "error",
            )

        return False

    # ── Main Loop ──────────────────────────────────────────

    async def _main_loop(self):
        """Main trading loop — runs every 5 seconds."""
        interval = 5
        self._log_event(f"主循環啟動 — 每{interval}秒輪詢")

        while self._running:
            try:
                await self._tick()
                # Reconnected after disconnect
                if self._disconnected:
                    self._disconnected = False
                    self._consecutive_errors = 0
                    self._log_event("網路恢復 — 恢復交易", "info")
            except Exception as e:
                self._consecutive_errors += 1
                if not self._disconnected:
                    self._disconnected = True
                    self._log_event(f"網路斷線: {e} — 暫停新單", "error")
                elif self._consecutive_errors % 12 == 0:
                    self._log_event(
                        f"仍然斷線 ({self._consecutive_errors} 次失敗): {e}",
                        "error"
                    )

            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _tick(self):
        """One iteration of the trading loop (1m candles — 30s bars stale on TopstepX)."""
        now = datetime.utcnow()

        # Reset daily counters at CT 17:00 (CME new session = TopStep day boundary)
        aware_now = now.replace(tzinfo=_UTC_TZ)
        ct_now = aware_now.astimezone(_CT)
        ts_date = (ct_now + timedelta(days=1)).strftime("%Y-%m-%d") if ct_now.hour >= 17 else ct_now.strftime("%Y-%m-%d")
        if ts_date != self._today:
            self._today = ts_date
            # API's closedPnl/openPnl reset automatically at CME day boundary
            self._daily_pnl = 0.0
            self._reset_full_tp_counts()
            self._log_event(
                f"新交易日 — PnL 重置 (CT 17:00)"
            )

        # Check position status from API (ALWAYS, even without new candle)
        await self._sync_position()

        if time_mod.time() - self._last_account_refresh >= 30:
            await self._refresh_account_snapshot("status", emit_log=False)

        if self._open_position:
            stopped = await self._monitor_auto_oco_protection()
            if stopped:
                return

        # Skip strategy evaluation on the same tick a position was just closed
        if self._position_just_closed:
            self._position_just_closed = False
            return

        # ── Disconnect guard — block new entries but keep monitoring ──
        if self._disconnected:
            return

        # Get recent 1m candles and replay any bars missed during a disconnect.
        candles = await self._fetch_latest_candles()
        if not candles:
            return

        # _fetch_latest_candles returns closed bars only. Older missed bars are
        # replayed to repair detector/strategy state; the newest closed bar is
        # the only bar allowed to trigger a fresh order.
        last_dt = None
        if self._last_candle_time:
            try:
                last_dt = datetime.fromisoformat(self._last_candle_time.replace("Z", "+00:00"))
            except Exception:
                last_dt = None

        new_candles = [c for c in candles if last_dt is None or c.timestamp > last_dt]
        if not new_candles:
            return

        if len(new_candles) > 1:
            self._log_event(f"補回 {len(new_candles) - 1} 根斷線期間漏掉的 1m K 線")

        for missed in new_candles[:-1]:
            self._ingest_catchup_candle(missed)

        candle = new_candles[-1]
        if self._last_market_price is None:
            self._last_market_price = candle.close

        candle_ts = candle.timestamp.isoformat()
        self._last_candle_time = candle_ts
        self._candles_processed += 1

        # Auto-save zones every 5 new candles (~5 minutes for 1m bars)
        if self._candles_processed % 5 == 0:
            self._save_zones()

        # ── ALWAYS update zone detector first (even during flatten/limits) ──
        self.detector.update(candle)
        if self.confluence is not None:
            self.confluence.update(candle)
        if self.mlc2_evaluator is not None:
            self.mlc2_evaluator.update(candle)
        self._append_history(candle)
        self._update_tf_breakout(candle)

        # ── Periodic status log every minute ──
        current_minute = now.minute
        if current_minute != self._last_status_log_minute:
            self._last_status_log_minute = current_minute
            self._log_event(
                f"{self._get_phase()} | 訂單: {self._get_order_short()}"
            )

        # Use UTC directly for time checks
        utc_time = now.time()

        # ── Flatten time (PT 12:45 = UTC 19:45) ──
        # Only flatten between 19:45-21:59 UTC (22:00+ is new session)
        from datetime import time as _time
        session_start = _time(22, 0)
        if utc_time >= self.FLATTEN_TIME_UTC and utc_time < session_start:
            if self._open_position:
                self._log_event("PT 12:45 收盤平倉")
                await self.flatten_now()
            if self._pending_order_id:
                await self._cancel_pending(release_breakout_lock=True)
            return  # no new trades during flatten, but detector already updated

        # ── Pre-flatten: cancel pending (PT 12:30 = UTC 19:30) ──
        if utc_time >= self.PRE_FLATTEN_UTC and utc_time < session_start and self._pending_order_id:
            self._log_event("PT 12:30 收盤前取消掛單")
            await self._cancel_pending(release_breakout_lock=True)

        # ── Check if pending order filled ──
        if (
            self.strategy_mode == "confluence"
            and self._pending_order_id
            and not self._confluence_session_allowed(candle.timestamp)
        ):
            self._log_event(
                f"Session filter {self._confluence_session_label()}: cancel pending outside allowed segment"
            )
            await self._cancel_pending(release_breakout_lock=True)
            return

        if (
            self.strategy_mode == "trend"
            and self._pending_order_id
            and not self._trend_session_allowed(candle.timestamp)
        ):
            self._log_event(
                f"Session filter {self._trend_session_label()}: cancel pending outside allowed segment"
            )
            await self._cancel_pending(release_breakout_lock=True)
            self._reset_breakout_confirmation()
            return

        if self._pending_order_id and not self._open_position:
            filled = await self._check_pending_fill()
            if filled:
                self._pending_order_id = None
                self._pending_signal = None
                self._pending_age = 0
                self._pending_created_at = None
                return
            self._pending_age += 1
            timeout = self.trend_follow.PENDING_TIMEOUT_CANDLES
            if self._pending_age >= timeout:
                self._log_event(f"掛單超時 {timeout} 分鐘取消")
                await self._cancel_pending(release_breakout_lock=True)

        # Auto OCO protection is monitored before the candle gate; trailing still needs price.
        if self._open_position:
            self._position_age += 1   # track for display only
            if self._last_market_price:
                await self._check_trailing_sl_live()
            return

        # ── Safety: cancel orphaned SL/TP if FLAT ──
        if not self._open_position and not self._pending_order_id:
            if self._sl_order_id or self._tp_order_id:
                self._log_event(
                    f"FLAT 但有殘留單 SL=#{self._sl_order_id} TP=#{self._tp_order_id} → 清除",
                    "error"
                )
                for oid, label in [
                    (self._sl_order_id, "SL"),
                    (self._tp_order_id, "TP"),
                ]:
                    if oid:
                        try:
                            await self._cancel_with_retry(oid, label)
                        except Exception as e:
                            self._log_event(f"清除 {label} #{oid} 失敗: {e}", "error")
                self._sl_order_id = None
                self._tp_order_id = None
                self._active_signal = None
                self._protection_synced = False

        if self._pending_order_id:
            return

        # ── v1.0.6: explainable confluence path (separate from the trend rule) ──
        if self.strategy_mode == "confluence" and self.confluence is not None:
            await self._evaluate_confluence(candle)
            return

        # ── ML Consolidation V2: VA mean reversion ──
        if self.strategy_mode == "ml_consolidation_v2" and self.mlc2_evaluator is not None:
            await self._evaluate_ml_consolidation_v2(candle)
            return

        # ── Strategy evaluation ──
        if not self._trend_session_allowed(candle.timestamp):
            label = self._trend_session_label()
            key = f"trend:{label}"
            if self._last_session_block_log != key:
                self._log_event(f"Session filter {label}: skip new Trend entries outside allowed segment")
                self._last_session_block_log = key
            self._reset_breakout_confirmation()
            return

        # Evaluate breakout vs the recent 10 reference zones (v1.0.6).
        if not self._strategy_breakout_observable():
            self._reset_breakout_confirmation()
            return

        eval_zones = self.detector.get_recent_zones()
        eval_mature = self.detector.is_zone_mature

        # Strategy evaluation
        strat = self.trend_follow
        # Safety: if strategy thinks it's confirmed but no order exists, reset
        if strat.raw_state == "confirmed" and not self._pending_order_id:
            self._log_event(
                f"Strategy stuck in 'confirmed' but no pending order → reset"
            )
            strat.reset()

        signal = self.trend_follow.evaluate(candle, eval_zones, eval_mature)

        if signal and not self._pending_order_id:
            if self._signal_full_tp_locked(signal):
                lock = self._full_tp_lock_for_strategy(signal.strategy)
                count = self._full_tp_counts.get(self._strategy_group(signal.strategy), 0)
                self._tp_locked = True
                self._log_event(
                    f"Full TP lock: {signal.strategy.value} {count}/{lock} TP — "
                    f"暫停該策略新單至下一個 Topstep session"
                )
                self._unlock_signal_breakout(signal)
                strat.notify_order_cancelled()
                return
            if self._session_direction_is_locked(signal):
                direction = self._breakout_direction_from_trade_direction(signal.direction.value)
                self._log_event(
                    f"Session-direction lock: zone={signal.zone_id} dir={direction} 已交易/已嘗試，跳過"
                )
                self._unlock_signal_breakout(signal)
                strat.notify_order_cancelled()
                return
            if getattr(signal, 'order_type', 'limit') == 'market':
                placed = await self._place_market_entry(signal)
            else:
                placed = await self._place_order(signal)
            if not placed:
                self._unlock_signal_breakout(signal)
                strat.notify_order_cancelled()
            return

    async def _evaluate_confluence(self, candle: Candle):
        """v1.0.6: evaluate the multi-timeframe confluence ML signal for this bar.

        SHADOW (default): logs the explainable signal (entry/SL/TP/prob + the
        top weighted feature contributions) and records it — places NO orders,
        so we can verify live == backtest with zero risk. When conf_shadow is
        False it places a one-shot MARKET order through the same entry path as
        the confluence backtest. TopstepX still creates working SL/TP child
        orders after fill; those are protection orders, not a limit entry.
        """
        if self._open_position or self._pending_order_id:
            return
        if not self._confluence_session_allowed(candle.timestamp):
            label = self._confluence_session_label()
            if self._last_session_block_log != label:
                self._log_event(f"Session filter {label}: skip new ML entries outside allowed segment")
                self._last_session_block_log = label
            return
        payload = self.confluence.explain(candle)
        if not payload:
            return
        signal = self.confluence.evaluate(candle)
        if signal is None:
            return

        # one-shot per zone(=largest TF) + direction, like the backtest session lock
        if self._session_direction_is_locked(signal):
            return

        # ── Chinese decision basis (判斷依據) — clearly states which zones (per
        # timeframe) formed the confluence, each zone's weight (各自的權重) and the
        # total weight (縂權重), plus the scorer's top feature contributions. ──
        _FEATURE_CN = {
            "mode_is_reversion": "逆勢偏好", "mean_band_pct": "帶寬位置",
            "side_is_vah": "VAH側", "n_distinct_tf": "TF數量",
            "largest_tf_rank": "最大TF階", "n_levels": "層級數",
            "total_weight": "匯流強度", "cluster_width_ticks": "簇寬",
            "dist_to_price_ticks": "距現價", "risk_ticks": "風險刻度",
            "rel_dist_to_price": "相對距離(R)", "rr": "盈虧比",
        }
        mode_cn = "逆勢" if payload["mode"] == "reversion" else "順勢"
        dir_cn = "做多" if signal.direction == Direction.BUY else "做空"
        side_cn = "VAH壓力" if payload["side"] == "VAH" else "VAL支撐"
        tfw = payload.get("tf_weights") or []
        tfw_str = " ".join(f"{d['tf']}(權{d['weight']:g})" for d in tfw) \
            or "/".join(payload["tfs"])
        top = payload.get("explain", [])[:3]
        contribs = "、".join(
            f"{_FEATURE_CN.get(n, n)}{c:+.2f}" for (n, _v, _w, c) in top
        )
        basis = (
            f"判斷依據：{mode_cn}{dir_cn}（{side_cn}）"
            f"｜匯流{len(payload['tfs'])}個區間 {tfw_str} 縂權重{payload['weight']:g}"
            f"｜進場{payload['entry']} 止損{payload['sl']} 止盈{payload['tp']}"
            f"｜勝率{payload['prob'] * 100:.0f}% 評分{payload['score']:+.2f} EV{payload.get('ev', 0.0):+.2f}"
            f"｜關鍵權重：{contribs}"
            f"｜評分器 {self.confluence.scorer_source}"
        )
        self._log_event(("【SHADOW】" if self._conf_shadow else "") + basis)
        # Stash the human-readable basis on the payload so the API/banner reuse it.
        payload["basis"] = basis
        payload["shadow"] = bool(self._conf_shadow)
        self._conf_signals_log.append(payload)
        if len(self._conf_signals_log) > 200:
            self._conf_signals_log = self._conf_signals_log[-100:]

        if self._conf_shadow:
            return  # log-only: prove live==backtest before risking real orders

        if getattr(signal, "order_type", "limit") == "market":
            placed = await self._place_market_entry(signal)
        else:
            placed = await self._place_order(signal)
        if placed:
            # carry the explainable payload through to the trade ledger on exit
            self._pending_conf_payload = payload
            self._mark_session_direction_locked(signal)
            self._log_event(
                f"{str(getattr(signal, 'order_type', 'limit')).upper()} ORDER on confluence: "
                f"{signal.direction.value} {payload['side']} "
                f"@ {payload['entry']} (prob={payload['prob']:.2f}, score={payload['score']:+.2f})"
            )

    def get_confluence_signals(self) -> List[Dict]:
        """Recent explainable confluence signals (for the live chart / API)."""
        return list(self._conf_signals_log)

    async def _evaluate_ml_consolidation_v2(self, candle: Candle):
        """ML Consolidation V2: VA mean reversion with market orders.

        LONG near VAL, SHORT near VAH, targeting POC or fixed RR.
        """
        if self._open_position or self._pending_order_id:
            return

        payload = self.mlc2_evaluator.explain(candle)
        if not payload:
            return

        signal = self.mlc2_evaluator.evaluate(candle)
        if signal is None:
            return

        if self._session_direction_is_locked(signal):
            return

        dir_cn = "做多" if signal.direction == Direction.BUY else "做空"
        basis = (
            f"ML-Consol-V2: {dir_cn} "
            f"@ {payload['entry']:.2f} SL={payload['sl']:.2f} TP={payload['tp']:.2f}"
            f"｜VAL={payload.get('val', 0):.2f} VAH={payload.get('vah', 0):.2f} "
            f"POC={payload.get('poc', 0):.2f}"
            f"｜prob={payload.get('prob', 0.5):.0%} score={payload.get('score', 0):+.2f}"
        )
        self._log_event(("【SHADOW】" if self._mlc2_shadow else "") + basis)
        payload["basis"] = basis
        self._mlc2_signals_log.append(payload)
        if len(self._mlc2_signals_log) > 200:
            self._mlc2_signals_log = self._mlc2_signals_log[-100:]

        if self._mlc2_shadow:
            return

        placed = await self._place_market_entry(signal)
        if placed:
            self._pending_mlc2_payload = payload
            self._mark_session_direction_locked(signal)
            self._log_event(
                f"MARKET ORDER on ML-Consol-V2: {signal.direction.value} "
                f"@ {payload['entry']:.2f}"
            )

    def get_ml_consolidation_v2_signals(self) -> List[Dict]:
        """Recent ML Consolidation V2 signals (for the live chart / API)."""
        return list(self._mlc2_signals_log)

    # ── Order Management ──────────────────────────────────

    @staticmethod
    def _round_to_tick(price: float) -> float:
        """Round price to nearest NQ tick (0.25)."""
        return round(round(price / TICK_SIZE) * TICK_SIZE, 2)

    def _normalize_entry_protection(self, signal: TradeSignal) -> List[str]:
        """Keep entry brackets valid for TopstepX before sending an order."""
        fixes: List[str] = []
        entry = signal.entry_price

        sl_ticks = int(round((signal.sl_price - entry) / self.tick_size))
        tp_ticks = int(round((signal.tp_price - entry) / self.tick_size))

        if signal.direction == Direction.BUY:
            fixed_sl_ticks = min(sl_ticks, -self.MIN_STOP_BRACKET_TICKS)
            fixed_tp_ticks = max(tp_ticks, self.MIN_TP_BRACKET_TICKS)
        else:
            fixed_sl_ticks = max(sl_ticks, self.MIN_STOP_BRACKET_TICKS)
            fixed_tp_ticks = min(tp_ticks, -self.MIN_TP_BRACKET_TICKS)

        if fixed_sl_ticks != sl_ticks:
            old = signal.sl_price
            signal.sl_price = self._round_to_tick(entry + fixed_sl_ticks * self.tick_size)
            fixes.append(f"SL {old:.2f}->{signal.sl_price:.2f} ({sl_ticks}t->{fixed_sl_ticks}t)")
        if fixed_tp_ticks != tp_ticks:
            old = signal.tp_price
            signal.tp_price = self._round_to_tick(entry + fixed_tp_ticks * self.tick_size)
            fixes.append(f"TP {old:.2f}->{signal.tp_price:.2f} ({tp_ticks}t->{fixed_tp_ticks}t)")

        return fixes

    def _entry_brackets_for_signal(self, signal: TradeSignal) -> tuple[Dict[str, int], Dict[str, int]]:
        """Build ProjectX bracket payload using signed offsets from the entry price."""
        sl_ticks = int(round((signal.sl_price - signal.entry_price) / self.tick_size))
        tp_ticks = int(round((signal.tp_price - signal.entry_price) / self.tick_size))
        return (
            {"ticks": sl_ticks, "type": 4},  # Stop Market
            {"ticks": tp_ticks, "type": 1},  # Limit
        )

    def _market_risk_limit_ticks(self, signal: TradeSignal) -> Optional[float]:
        """Runtime max-risk cap for market-entry strategies."""
        zone_source = str(getattr(signal, "zone_source", "") or "").lower()
        strategy = str(getattr(getattr(signal, "strategy", None), "value", "") or "").lower()

        if self.strategy_mode == "ml_consolidation_v2" or zone_source == "ml_consolidation_v2" or strategy == "ml_consolidation_v2":
            cap = getattr(self.strategy_params, "mlc2_max_risk_ticks", None)
        elif self.strategy_mode == "confluence" or zone_source == "confluence" or strategy == "confluence":
            cap = getattr(self.strategy_params, "conf_max_risk_ticks", None)
        else:
            cap = None

        try:
            cap_f = float(cap) if cap not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            cap_f = None
        return cap_f if cap_f and cap_f > 0 else None

    def _planned_rr_for_signal(self, signal: TradeSignal) -> float:
        """Reward:risk from the original planned entry/SL/TP before market fill slippage."""
        entry = float(getattr(signal, "original_entry_price", signal.entry_price))
        sl = float(getattr(signal, "original_sl_price", signal.sl_price))
        tp = float(getattr(signal, "original_tp_price", signal.tp_price))
        risk = abs(entry - sl)
        reward = abs(tp - entry)
        if risk <= 0:
            return 1.0
        rr = reward / risk
        return max(0.1, min(rr, 10.0))

    def _validate_market_signal_geometry(
        self,
        signal: TradeSignal,
        entry_price: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Validate SL geometry and max-risk against an intended or actual market fill."""
        if entry_price is None:
            entry_price = signal.entry_price
        try:
            entry = self._round_to_tick(float(entry_price))
            sl = self._round_to_tick(float(signal.sl_price))
        except (TypeError, ValueError):
            return False, "invalid price"

        if signal.direction == Direction.BUY and sl >= entry:
            return False, f"BUY SL wrong side: entry={entry:.2f} SL={sl:.2f}"
        if signal.direction == Direction.SELL and sl <= entry:
            return False, f"SELL SL wrong side: entry={entry:.2f} SL={sl:.2f}"

        risk_ticks = abs(entry - sl) / self.tick_size
        if risk_ticks < 5:
            return False, f"risk too small: {risk_ticks:.0f}t < 5t"

        max_risk = self._market_risk_limit_ticks(signal)
        if max_risk and risk_ticks > max_risk:
            return False, f"risk too wide: {risk_ticks:.0f}t > max {max_risk:.0f}t"

        return True, ""

    def _reprice_market_signal_to_fill(self, signal: TradeSignal, fill_price: float) -> tuple[bool, str]:
        """Rebuild market-entry TP from actual fill while preserving the structural SL."""
        ok, reason = self._validate_market_signal_geometry(signal, fill_price)
        if not ok:
            return False, reason

        old_entry = float(signal.entry_price)
        old_tp = float(signal.tp_price)
        rr = self._planned_rr_for_signal(signal)
        entry = self._round_to_tick(float(fill_price))
        sl = self._round_to_tick(float(signal.sl_price))
        risk = abs(entry - sl)

        if signal.direction == Direction.BUY:
            tp = entry + risk * rr
        else:
            tp = entry - risk * rr

        signal.entry_price = entry
        signal.tp_price = self._round_to_tick(tp)
        return (
            True,
            f"entry {old_entry:.2f}->{signal.entry_price:.2f} | "
            f"TP {old_tp:.2f}->{signal.tp_price:.2f} | RR={rr:.2f}",
        )

    async def _place_order(self, signal: TradeSignal) -> bool:
        """Place a limit order on the exchange.

        Safety checks:
        1. Entry price vs market price — block if too far (instant fill risk)
        2. No market price reference — block entirely

        Returns True if order was placed, False if blocked.
        """
        # Round all prices to valid tick size (0.25)
        signal.entry_price = self._round_to_tick(signal.entry_price)
        signal.sl_price = self._round_to_tick(signal.sl_price)
        signal.tp_price = self._round_to_tick(signal.tp_price)
        protection_fixes = self._normalize_entry_protection(signal)
        if protection_fixes:
            self._log_event("[BRACKET FIX] " + " | ".join(protection_fixes), "warn")
        signal.original_sl_price = signal.sl_price
        signal.original_tp_price = signal.tp_price

        side = 1 if signal.direction == Direction.BUY else 2
        dir_label = "買" if signal.direction == Direction.BUY else "賣"

        # ── Safety: validate entry price vs current market ──
        PRICE_SAFETY_MARGIN = 50.0  # points
        if self._last_market_price:
            mkt = self._last_market_price
            if signal.direction == Direction.SELL and signal.entry_price < mkt - PRICE_SAFETY_MARGIN:
                self._log_event(
                    f"[SAFETY BLOCK] SELL LIMIT @ {signal.entry_price:.2f} 遠低於市價 {mkt:.2f} "
                    f"(差 {mkt - signal.entry_price:.1f} pts) → 攔截",
                    "error"
                )
                return False
            if signal.direction == Direction.BUY and signal.entry_price > mkt + PRICE_SAFETY_MARGIN:
                self._log_event(
                    f"[SAFETY BLOCK] BUY LIMIT @ {signal.entry_price:.2f} 遠高於市價 {mkt:.2f} "
                    f"(差 {signal.entry_price - mkt:.1f} pts) → 攔截",
                    "error"
                )
                return False
            self._log_event(
                f"[SAFETY OK] {dir_label} LIMIT @ {signal.entry_price:.2f} | 市價={mkt:.2f} | "
                f"差距={abs(signal.entry_price - mkt):.1f} pts"
            )
        else:
            self._log_event(
                f"[SAFETY BLOCK] 無市價參考, 拒絕下單! entry={signal.entry_price:.2f}",
                "error"
            )
            return False

        if signal.zone_id:
            self._log_event(
                f"[ZONE] signal 使用 zone_id={signal.zone_id} | 策略={signal.strategy.value}"
            )

        stop_loss_bracket, take_profit_bracket = self._entry_brackets_for_signal(signal)
        order = OrderRequest(
            account_id=self.account_id,
            contract_id=self.contract_id,
            order_type=1,  # Limit
            side=side,
            size=self.contract_size,
            limit_price=signal.entry_price,
            stop_loss_bracket=stop_loss_bracket,
            take_profit_bracket=take_profit_bracket,
        )

        try:
            resp = await self.client.place_order(order)
            if resp.success:
                self._pending_order_id = resp.order_id
                self._pending_signal = signal
                self._pending_age = 0
                self._pending_created_at = datetime.utcnow()
                self._persist_breakout_lock(signal)
                self._mark_session_direction_locked(signal)
                self._log_event(
                    f"掛單成功 #{resp.order_id} | {dir_label} LIMIT @ {signal.entry_price:.2f} | "
                    f"SL={signal.sl_price:.2f} TP={signal.tp_price:.2f} | "
                    f"bracket SL={stop_loss_bracket['ticks']}t TP={take_profit_bracket['ticks']}t | "
                    f"策略={signal.strategy.value}"
                )
                return True
            else:
                self._log_event(
                    f"掛單失敗: code={resp.error_code} ({order_error_meaning(resp.error_code)}) "
                    f"msg={resp.error_message} "
                    f"| entry={signal.entry_price:.2f} side={'BUY' if side == 1 else 'SELL'} "
                    f"(api_side={0 if side == 1 else 1}) | 訂單遭 gateway 拒絕, 不會出現在 TopstepX",
                    "error"
                )
                return False
        except Exception as e:
            self._log_event(f"下單異常: {e}", "error")
            return False

    async def _place_market_entry(self, signal: TradeSignal) -> bool:
        """Place a market order with attached SL/TP brackets."""
        signal.entry_price = self._round_to_tick(signal.entry_price)
        signal.sl_price = self._round_to_tick(signal.sl_price)
        signal.tp_price = self._round_to_tick(signal.tp_price)
        signal.original_entry_price = signal.entry_price
        signal.original_sl_price = signal.sl_price
        signal.original_tp_price = signal.tp_price
        ok, reason = self._validate_market_signal_geometry(signal, signal.entry_price)
        if not ok:
            self._log_event(f"[MARKET BLOCK] {reason}", "error")
            return False
        if self._last_market_price:
            ok, reason = self._validate_market_signal_geometry(signal, self._last_market_price)
            if not ok:
                self._log_event(
                    f"[MARKET BLOCK] current market={self._last_market_price:.2f} | {reason}",
                    "error",
                )
                return False
        protection_fixes = self._normalize_entry_protection(signal)
        if protection_fixes:
            self._log_event("[BRACKET FIX] " + " | ".join(protection_fixes), "warn")
        ok, reason = self._validate_market_signal_geometry(signal, signal.entry_price)
        if not ok:
            self._log_event(f"[MARKET BLOCK] after bracket normalize: {reason}", "error")
            return False

        side = 1 if signal.direction == Direction.BUY else 2
        dir_label = "買" if signal.direction == Direction.BUY else "賣"

        stop_loss_bracket, take_profit_bracket = self._entry_brackets_for_signal(signal)
        order = OrderRequest(
            account_id=self.account_id,
            contract_id=self.contract_id,
            order_type=2,   # Market
            side=side,
            size=self.contract_size,
            stop_loss_bracket=stop_loss_bracket,
            take_profit_bracket=take_profit_bracket,
        )

        try:
            resp = await self.client.place_order(order)
            if resp.success:
                # Treat market order like a pending order — _sync_position will detect fill
                self._pending_order_id = resp.order_id
                self._pending_signal = signal
                self._pending_age = 0
                self._pending_created_at = datetime.utcnow()
                self._persist_breakout_lock(signal)
                self._mark_session_direction_locked(signal)
                self._log_event(
                    f"市價單 #{resp.order_id} | {dir_label} MKT @ ~{signal.entry_price:.2f} | "
                    f"SL={signal.sl_price:.2f} TP={signal.tp_price:.2f} | "
                    f"bracket SL={stop_loss_bracket['ticks']}t TP={take_profit_bracket['ticks']}t"
                )
                return True
            else:
                self._log_event(
                    f"市價單失敗: code={resp.error_code} ({order_error_meaning(resp.error_code)}) "
                    f"msg={resp.error_message} | 訂單遭 gateway 拒絕, 不會出現在 TopstepX",
                    "error"
                )
                return False
        except Exception as e:
            self._log_event(f"市價單異常: {e}", "error")
            return False

    async def _check_trailing_sl_live(self):
        """Live trailing SL: trigger at a configured fraction of TP, once."""
        if self._trail_sl_triggered or not self._active_signal or not self._fill_price:
            return
        sig = self._active_signal
        if self.strategy_mode == "confluence":
            style = self._confluence_exit_style()
            if not style.trail_enabled:
                return
            mkt = self._last_market_price
            if mkt is None:
                return
            from backend.strategy.exit_policy import maybe_trail_sl

            entry = float(self._fill_price or sig.entry_price)
            new_sl, triggered = maybe_trail_sl(
                sig.direction,
                entry,
                sig.tp_price,
                sig.sl_price,
                self._trail_sl_triggered,
                float(mkt),
                style,
            )
            if not triggered:
                return

            self._trail_sl_triggered = True
            new_sl = self._round_to_tick(new_sl)
            tp_dist = abs(sig.tp_price - entry)
            self._log_event(
                f"[TRAIL SL] ML {style.trail_trigger_pct:.0%} TP -> SL {new_sl:.2f} "
                f"(entry={entry:.2f}, lock={style.trail_lock_pct:.0%} TP, mkt={float(mkt):.2f})"
            )

            if not self._sl_order_id or not self._protection_synced:
                synced = await self._sync_auto_oco_protection(sig, wait_seconds=2.0)
                if not synced or not self._sl_order_id:
                    self._log_event("[TRAIL SL] 找不到可修改的 Auto OCO SL，保留原保護單並等待下次重試", "error")
                    self._trail_sl_triggered = False
                    return

            try:
                resp = await self.client.modify_order(
                    self.account_id,
                    self._sl_order_id,
                    size=self.contract_size,
                    stop_price=new_sl,
                )
                if resp.success:
                    self._log_event(f"[TRAIL SL] SL #{self._sl_order_id} -> {new_sl:.2f}")
                    sig.sl_price = new_sl
                    if tp_dist > 0:
                        sig.entry_price = entry
                    self._protection_synced = True
                else:
                    self._log_event(
                        f"[TRAIL SL] 修改 SL 失敗: {resp.error_message} → 原 Auto OCO SL 維持不動",
                        "error",
                    )
                    self._trail_sl_triggered = False
            except Exception as e:
                self._log_event(f"[TRAIL SL] 修改 SL 異常: {e} → 原 Auto OCO SL 維持不動", "error")
                self._trail_sl_triggered = False
            return

        if not self._strategy_trail_enabled(sig.strategy):
            return
        mkt = self._last_market_price
        if mkt is None:
            return
        if sig.direction == Direction.BUY:
            ticks_moved = (mkt - self._fill_price) / self.tick_size
        else:
            ticks_moved = (self._fill_price - mkt) / self.tick_size

        # v1.0.6: TP is RR-based (TP = entry ± sl_dist × RR), so the static tp_ticks
        # param is no longer the real target. Derive the trigger from the actual
        # signal's planned TP distance — IDENTICAL to the backtest engine's
        # _check_trailing_sl, so live trails at the same point that was backtested.
        tp_ticks = abs(sig.tp_price - sig.entry_price) / self.tick_size
        if tp_ticks <= 0:
            # Fallback to the legacy static param if the signal has no usable TP.
            tp_ticks = abs(int(self._strategy_param(sig.strategy, 'tp_ticks', 0) or 0))
        trigger_pct = self._strategy_trigger_pct(sig.strategy)
        if trigger_pct <= 0:
            return
        trigger_ticks = max(1.0, tp_ticks * trigger_pct)
        if ticks_moved < trigger_ticks:
            return

        self._trail_sl_triggered = True
        trail_ticks = self._resolved_trail_ticks(sig.strategy)
        trail_pts = trail_ticks * self.tick_size
        if sig.direction == Direction.BUY:
            new_sl = self._fill_price + trail_pts
        else:
            new_sl = self._fill_price - trail_pts
        new_sl = self._round_to_tick(new_sl)
        self._log_event(
            f"[TRAIL SL] +{ticks_moved:.0f} ticks ({trigger_pct:.0%} TP) -> SL {new_sl:.2f} "
            f"(entry={self._fill_price:.2f}, offset={trail_ticks}t)"
        )

        if not self._sl_order_id or not self._protection_synced:
            synced = await self._sync_auto_oco_protection(sig, wait_seconds=2.0)
            if not synced or not self._sl_order_id:
                self._log_event("[TRAIL SL] 找不到可修改的 Auto OCO SL，保留原保護單並等待下次重試", "error")
                self._trail_sl_triggered = False
                return

        try:
            resp = await self.client.modify_order(
                self.account_id,
                self._sl_order_id,
                size=self.contract_size,
                stop_price=new_sl,
            )
            if resp.success:
                self._log_event(f"[TRAIL SL] SL #{self._sl_order_id} -> {new_sl:.2f}")
                sig.sl_price = new_sl
                self._protection_synced = True
            else:
                self._log_event(
                    f"[TRAIL SL] 修改 SL 失敗: {resp.error_message} → 原 Auto OCO SL 維持不動",
                    "error",
                )
                self._trail_sl_triggered = False
        except Exception as e:
            self._log_event(f"[TRAIL SL] 修改 SL 異常: {e} → 原 Auto OCO SL 維持不動", "error")
            self._trail_sl_triggered = False
        return

    async def _cancel_with_retry(self, order_id: Optional[int], label: str):
        """Cancel an order with retry."""
        if not order_id:
            return
        success = await self.client.cancel_order(self.account_id, order_id)
        if success:
            self._log_event(f"取消殘留 {label} #{order_id}")
            return
        # First attempt failed — wait and retry once
        await asyncio.sleep(1)
        success = await self.client.cancel_order(self.account_id, order_id)
        if success:
            self._log_event(f"取消殘留 {label} #{order_id} (重試成功)")
        else:
            self._log_event(f"取消 {label} #{order_id} 失敗 (可能已成交)")

    async def _pending_order_still_open(self, order_id: int) -> Optional[bool]:
        """Best-effort broker check after a pending-entry cancel failure.

        A one-shot limit can fill and close between sync ticks.  In that case
        cancel_order often returns false, but keeping local pending state forever
        blocks the engine (`pending_age` grows far beyond timeout).  Return True
        when the order is still reported open, False when it is no longer open,
        and None when the broker check itself failed.
        """
        try:
            open_orders = await self.client.get_open_orders(self.account_id)
        except Exception as e:
            self._log_event(f"檢查掛單 #{order_id} 狀態失敗: {e}", "error")
            return None
        for order in open_orders or []:
            if self._order_id(order) == order_id:
                return True
        return False

    async def _cancel_pending(self, *, release_breakout_lock: bool = False):
        """Cancel the pending limit order. Retries up to 3 times."""
        if not self._pending_order_id:
            return
        oid = self._pending_order_id
        cancelled = False
        for attempt in range(3):
            try:
                success = await self.client.cancel_order(self.account_id, oid)
                if success:
                    self._log_event(f"取消掛單 #{oid} (attempt {attempt+1})")
                    cancelled = True
                    break
                else:
                    self._log_event(
                        f"取消掛單 #{oid} 失敗 attempt {attempt+1}/3",
                        "error"
                    )
            except Exception as e:
                self._log_event(f"取消掛單 #{oid} 異常 attempt {attempt+1}/3: {e}", "error")
            if attempt < 2:
                await asyncio.sleep(1)

        if not cancelled:
            still_open = await self._pending_order_still_open(oid)
            if still_open is False and not self._open_position:
                maybe_close = await self._latest_topstep_closing_fill(
                    entry_time=self._pending_created_at,
                    attempts=2,
                    delay_seconds=0.3,
                )
                self._log_event(
                    f"取消掛單 #{oid} 失敗，但交易所已無此 open order → "
                    +
                    (
                        "trade_history 已有 close fill，保留 lock"
                        if maybe_close
                        else "未見 Topstep close fill，視為未成交取消並釋放 lock"
                    ),
                    "warn",
                )
                if self._pending_signal:
                    self.trend_follow.notify_order_cancelled()
                    if release_breakout_lock and not maybe_close:
                        self._release_breakout_lock(self._pending_signal)
                    if not maybe_close:
                        self._persist_trade_record(
                            exit_reason="cancelled",
                            entry_time=self._entry_time,
                            exit_time=datetime.utcnow(),
                            entry_price=None,
                            signal=self._pending_signal,
                            conf_payload=self._pending_conf_payload or self._pending_mlc2_payload,
                            trail_triggered=False,
                            status="cancelled",
                        )
                self._pending_order_id = None
                self._pending_signal = None
                self._pending_conf_payload = None
                self._pending_mlc2_payload = None
                self._pending_age = 0
                self._pending_created_at = None
                return
            suffix = "（broker 仍列為 open）" if still_open else "（無法確認 broker 狀態）"
            self._log_event(f"取消掛單 #{oid} 3次均失敗{suffix}! 下個 tick 再試", "error")
            return  # DON'T clear state — retry next tick

        if self._pending_signal:
            self.trend_follow.notify_order_cancelled()
            if release_breakout_lock:
                self._release_breakout_lock(self._pending_signal)
            # Record the unfilled order (placed but price never touched) with its
            # full explainable payload — so cancelled signals are auditable too.
            self._persist_trade_record(
                exit_reason="cancelled",
                entry_time=self._entry_time,
                exit_time=datetime.utcnow(),
                entry_price=None,
                signal=self._pending_signal,
                conf_payload=self._pending_conf_payload or self._pending_mlc2_payload,
                trail_triggered=False,
                status="cancelled",
            )

        self._pending_order_id = None
        self._pending_signal = None
        self._pending_conf_payload = None
        self._pending_mlc2_payload = None
        self._pending_age = 0
        self._pending_created_at = None

    async def _check_pending_fill(self) -> bool:
        """Backup check: if _sync_position already detected fill, just confirm.
        Primary fill detection is now in _sync_position (runs every 5s).
        """
        # If _sync_position already cleared pending and set position, we're done
        if self._open_position and not self._pending_order_id:
            return True
        # If position exists but pending wasn't cleared yet (shouldn't happen)
        if self._open_position:
            self._log_event(f"[BACKUP] 偵測到持倉但掛單未清除 → 清除")
            self._pending_order_id = None
            self._pending_signal = None
            self._pending_conf_payload = None
            self._pending_mlc2_payload = None
            self._pending_age = 0
            self._pending_created_at = None
            return True
        return False

    async def _place_sl_tp(self):
        """Sync TopstepX Auto OCO SL/TP child orders to strategy prices."""
        sig = self._pending_signal or self._active_signal
        if not sig or not self._open_position:
            self._log_event(
                f"[AUTO OCO] _place_sl_tp 跳過: signal={sig is not None} "
                f"position={self._open_position is not None}",
                "error",
            )
            return

        await self._sync_auto_oco_protection(sig, wait_seconds=4.0)

    async def _sync_position(self):
        """Sync position state from exchange.

        Handles three transitions:
          1. pending → filled:  position appears while _pending_order_id is set
          2. filled  → closed:  position disappears (SL/TP hit)
          3. no change:         keep local position state aligned
        """
        try:
            positions = await self.client.get_positions(self.account_id)
            was_open = self._open_position is not None
            has_position = positions and len(positions) > 0
            self._open_position = positions[0] if has_position else None

            # ── Transition 1: Pending order just FILLED ──
            if has_position and self._pending_order_id:
                fill_price_raw = positions[0].get('averagePrice', positions[0].get('avgPrice'))
                try:
                    self._fill_price = float(fill_price_raw) if fill_price_raw else None
                except (ValueError, TypeError):
                    self._fill_price = None

                self._log_event(
                    f"掛單成交! #{self._pending_order_id} | "
                    f"fill={self._fill_price} | size={positions[0].get('size', '?')} | "
                    f"side={'LONG' if positions[0].get('side', 0) == 0 else 'SHORT'}"
                )

                if self._fill_price and self._pending_signal:
                    entry = self._pending_signal.entry_price
                    diff = self._fill_price - entry
                    adverse_slippage = diff if self._pending_signal.direction == Direction.BUY else -diff
                    price_improvement = max(0.0, -adverse_slippage)
                    adverse_slippage = max(0.0, adverse_slippage)
                    adverse_dollars = adverse_slippage * self.point_value * self.contract_size
                    improvement_dollars = price_improvement * self.point_value * self.contract_size
                    if adverse_slippage > 5.0:
                        self._log_event(
                            f"[FILL MISMATCH] entry={entry:.2f} fill={self._fill_price:.2f} "
                            f"不利滑價={adverse_slippage:.2f} pts (${adverse_dollars:.0f})",
                            "error"
                        )
                    elif price_improvement > 0:
                        self._log_event(
                            f"[FILL OK] entry={entry:.2f} fill={self._fill_price:.2f} "
                            f"price improvement={price_improvement:.2f} pts (${improvement_dollars:.0f})"
                        )
                    else:
                        self._log_event(
                            f"[FILL OK] entry={entry:.2f} fill={self._fill_price:.2f} "
                            f"不利滑價={adverse_slippage:.2f} pts (${adverse_dollars:.0f})"
                        )

                if (
                    self._fill_price
                    and self._pending_signal
                    and str(getattr(self._pending_signal, "order_type", "limit")).lower() == "market"
                ):
                    ok, detail = self._reprice_market_signal_to_fill(self._pending_signal, self._fill_price)
                    if not ok:
                        reason_code = "MARKET_FILL_RISK" if "risk too wide" in detail else "MARKET_FILL_INVALID"
                        close_side = 2 if self._pending_signal.direction == Direction.BUY else 1
                        self._active_signal = self._pending_signal
                        self._active_conf_payload = (
                            self._pending_conf_payload or self._pending_mlc2_payload
                        )
                        self._entry_time = datetime.utcnow()
                        self._position_open_ts = time_mod.time()
                        self._force_exit_reason = "flatten"
                        self._log_event(
                            f"[MARKET FILL BLOCK] fill={self._fill_price:.2f} | {detail} "
                            f"→ emergency flatten before SL/TP sync",
                            "error",
                        )
                        await self._emergency_market_close(close_side, reason_code)
                        self._pending_order_id = None
                        self._pending_signal = None
                        self._pending_conf_payload = None
                        self._pending_mlc2_payload = None
                        self._pending_age = 0
                        self._pending_created_at = None
                        return
                    self._log_event(f"[MARKET REPRICE] {detail}")

                # Record entry trade for chart markers
                sig_dir = "buy"
                sig = self._pending_signal
                if sig:
                    sig_dir = sig.direction.value
                conf_payload = self._pending_conf_payload or self._pending_mlc2_payload or {}
                self._trades.append({
                    "time": datetime.utcnow().isoformat(),
                    "type": "entry",
                    "direction": sig_dir,
                    "price": self._fill_price,
                    "strategy": sig.strategy.value if sig else self.strategy_mode,
                    "sl_price": sig.sl_price if sig else None,
                    "tp_price": sig.tp_price if sig else None,
                    "original_sl_price": (
                        getattr(sig, "original_sl_price", sig.sl_price)
                        if sig else None
                    ),
                    "original_tp_price": (
                        getattr(sig, "original_tp_price", sig.tp_price)
                        if sig else None
                    ),
                    "mode": conf_payload.get("mode"),
                    "side": conf_payload.get("side"),
                    "largest_tf": conf_payload.get("largest_tf"),
                    "wall_id": conf_payload.get("wall_id"),
                    "labels": conf_payload.get("labels") or [],
                    "primary_zone": conf_payload.get("primary_zone"),
                })

                # Place SL/TP protection orders
                self._position_open_ts = time_mod.time()
                self._auto_oco_fail_safe_triggered = False
                self._last_auto_oco_retry_ts = 0.0
                if self._pending_signal:
                    self._protection_synced = False
                    self._log_event(
                        f"[AUTO OCO] 等待並修改 SL={self._pending_signal.sl_price:.2f} "
                        f"TP={self._pending_signal.tp_price:.2f} "
                        f"dir={self._pending_signal.direction.value}"
                    )
                    await self._place_sl_tp()
                else:
                    self._log_event("[SL/TP] 無 pending_signal 無法下 SL/TP!", "error")

                # Save signal for SL/TP retry, then clear pending state
                self._active_signal = self._pending_signal  # keep for SL/TP reference
                self._active_conf_payload = (
                    self._pending_conf_payload or self._pending_mlc2_payload
                )  # carry the "why"
                self._pending_conf_payload = None
                self._pending_mlc2_payload = None
                self._entry_time = datetime.utcnow()
                self._force_exit_reason = None
                self._pending_order_id = None
                self._pending_signal = None
                self._pending_age = 0
                self._pending_created_at = None
                self._position_age = 0
                self._trail_sl_triggered = False

            # ── Transition 1b: Position exists but engine didn't place it ──
            # Double-fill scenario: both SL and TP filled in rapid succession,
            # leaving a rogue position. Flatten immediately.
            elif has_position and not was_open and not self._pending_order_id:
                fill_price_raw = positions[0].get('averagePrice', positions[0].get('avgPrice'))
                try:
                    self._fill_price = float(fill_price_raw) if fill_price_raw else None
                except (ValueError, TypeError):
                    self._fill_price = None

                pos_side = positions[0].get('side', 0)

                if self._position_just_closed:
                    # Double-fill: position just closed but a new one appeared
                    # (the residual SL/TP order filled after the first one closed us)
                    close_side = 2 if pos_side == 0 else 1  # opposite side to close
                    self._log_event(
                        f"DOUBLE-FILL 偵測! SL/TP 同時成交 → 緊急平倉 | "
                        f"rogue side={'LONG' if pos_side == 0 else 'SHORT'} | "
                        f"fill={self._fill_price}",
                        "error"
                    )
                    await self._emergency_market_close(close_side, "DOUBLE_FILL")
                else:
                    self._log_event(
                        f"偵測到未追蹤的持倉 | fill={self._fill_price} | "
                        f"side={'LONG' if pos_side == 0 else 'SHORT'} | "
                        f"無 pending_order → 可能是手動入場或重啟後",
                        "error"
                    )

            # ── Transition 2: Position CLOSED (SL/TP hit) ──
            if was_open and not has_position:
                # Snapshot before any awaits / cleanup.
                _sig_for_log = self._active_signal
                _entry_t = self._entry_time
                _conf_payload = self._active_conf_payload
                forced = self._force_exit_reason

                close_fill = await self._latest_topstep_closing_fill(
                    entry_time=_entry_t,
                    attempts=3,
                    delay_seconds=0.4,
                )
                (
                    exit_reason,
                    actual_exit_price,
                    topstep_pnl,
                    topstep_exit_time,
                ) = self._exit_reason_from_topstep_fill(close_fill, _sig_for_log, forced)

                pnl_info = ""
                if self._fill_price:
                    pnl_info = f" | entry_fill={self._fill_price:.2f}"
                if actual_exit_price is not None:
                    pnl_info += f" | topstep_exit={actual_exit_price:.2f}"
                if topstep_pnl is not None:
                    pnl_info += f" | topstep_pnl=${topstep_pnl:+.2f}"

                entry_fill = self._fill_price  # save before clearing
                exit_time_dt = topstep_exit_time or datetime.utcnow()

                self._log_event(
                    f"持倉已平 ({exit_reason.upper()} 觸發){pnl_info}"
                )
                if exit_reason == "tp" and _sig_for_log:
                    lock = self._full_tp_lock_for_strategy(_sig_for_log.strategy)
                    if lock > 0:
                        key = self._strategy_group(_sig_for_log.strategy)
                        self._full_tp_counts[key] = self._full_tp_counts.get(key, 0) + 1
                        self._full_tp_count = sum(self._full_tp_counts.values())
                        self._tp_locked = self._any_full_tp_locked()
                        self._log_event(
                            f"Full TP count: {_sig_for_log.strategy.value} "
                            f"{self._full_tp_counts[key]}/{lock}"
                        )

                # Cancel residual orders — each in own try/except so one failure
                # doesn't block the other
                sl_id = self._sl_order_id
                tp_id = self._tp_order_id
                self._sl_order_id = None
                self._tp_order_id = None
                self._fill_price = None
                self._active_signal = None
                self._active_conf_payload = None
                self._protection_synced = False
                self._entry_time = None
                self._position_open_ts = 0.0
                self._last_auto_oco_retry_ts = 0.0

                for oid, label in [(sl_id, "SL"), (tp_id, "TP")]:
                    if oid:
                        try:
                            await self._cancel_with_retry(oid, label)
                        except Exception as e:
                            self._log_event(f"取消 {label} #{oid} 異常: {e}", "error")

                # Cancel any pending entry order
                if self._pending_order_id:
                    self._log_event(f"取消殘留掛單 #{self._pending_order_id}")
                    try:
                        await self._cancel_with_retry(self._pending_order_id, "ENTRY")
                    except Exception as e:
                        self._log_event(f"取消 ENTRY 異常: {e}", "error")
                self._pending_order_id = None
                self._pending_signal = None
                self._pending_age = 0
                self._pending_created_at = None

                await self._sweep_contract_open_orders("close")

                self._trades.append({
                    "time": exit_time_dt.isoformat(),
                    "type": "closed",
                    "entry_price": entry_fill,
                    "exit_price": actual_exit_price,
                    "topstep_pnl": topstep_pnl,
                    "exit_reason": exit_reason,
                })

                # Persist exit reason so /live/trade-history can bucket the
                # matching TopstepX fill into TP / SL / TRAIL_SL correctly.
                self._persist_exit_record(
                    exit_reason=exit_reason,
                    entry_time=_entry_t,
                    exit_time=exit_time_dt,
                    entry_price=entry_fill,
                    exit_price=actual_exit_price,
                    topstep_pnl=topstep_pnl,
                    sl_price=_sig_for_log.sl_price if _sig_for_log else None,
                    tp_price=_sig_for_log.tp_price if _sig_for_log else None,
                    direction=_sig_for_log.direction.value if _sig_for_log else None,
                    trail_triggered=self._trail_sl_triggered,
                    zone_id=_sig_for_log.zone_id if _sig_for_log else None,
                    conf_payload=_conf_payload,
                    original_sl_price=(
                        getattr(_sig_for_log, "original_sl_price", _sig_for_log.sl_price)
                        if _sig_for_log else None
                    ),
                    original_tp_price=(
                        getattr(_sig_for_log, "original_tp_price", _sig_for_log.tp_price)
                        if _sig_for_log else None
                    ),
                )

                # Durable explainable trade ledger (data/trades.json):
                # weights x features + scorer version + all params + outcome.
                self._persist_trade_record(
                    exit_reason=exit_reason,
                    entry_time=_entry_t,
                    exit_time=exit_time_dt,
                    entry_price=entry_fill,
                    signal=_sig_for_log,
                    conf_payload=_conf_payload,
                    trail_triggered=self._trail_sl_triggered,
                    exit_price=actual_exit_price,
                    topstep_pnl=topstep_pnl,
                )

                # Notify strategy with actual exit reason
                self.trend_follow.notify_trade_closed(exit_reason)
                self._position_just_closed = True  # skip new entry this tick
                self._force_exit_reason = None

                await self._refresh_account_snapshot("平倉後更新", emit_log=True, attempts=3)

            # ── Position size audit (every 5 min, skip 60s after entry) ──
            if has_position:
                now_ts = time_mod.time()
                grace_ok = not self._position_open_ts or (now_ts - self._position_open_ts >= 60)
                if grace_ok and now_ts - self._last_safety_check >= 300:
                    self._last_safety_check = now_ts
                    actual_size = abs(positions[0].get('size', 0) or positions[0].get('qty', 0) or 0)
                    expected = self.contract_size
                    if actual_size > 0 and actual_size != expected:
                        pos_side = positions[0].get('side', 0)
                        self._log_event(
                            f"[SAFETY] 倉位不對等! 預期={expected} 實際={actual_size} "
                            f"side={'LONG' if pos_side == 0 else 'SHORT'} → 緊急全平",
                            "error"
                        )
                        self._force_exit_reason = "flatten"
                        await self.flatten_now()
                        return

            # Account snapshot refresh is handled by _tick and close/flatten transitions.

        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError,
                httpx.WriteError, httpx.PoolTimeout) as e:
            self._log_event(f"[SYNC ERROR] {e}", "error")
            raise  # re-raise network errors for disconnect tracking
        except Exception as e:
            self._log_event(f"[SYNC ERROR] {e}", "error")
            logger.error(f"[SYNC] position sync failed: {e}", exc_info=True)

    def _append_history(self, candle: Candle):
        """Append a 1m candle to the rolling history, capped to ~70 days of bars."""
        self._all_candles.append(candle)
        cap = 100000  # ~69 days of 1m bars; bounds memory
        if len(self._all_candles) > cap:
            self._all_candles = self._all_candles[-cap:]

    def get_candle_history(self) -> List[Candle]:
        """Warm-up + live 1m candles (chronological) for multi-TF zone detection."""
        return list(self._all_candles)

    def _ingest_catchup_candle(self, candle: Candle):
        """Replay a missed candle into local state without placing stale orders."""
        self._last_market_price = candle.close
        self._last_candle_time = candle.timestamp.isoformat()
        self._candles_processed += 1
        if self._candles_processed % 5 == 0:
            self._save_zones()
        self.detector.update(candle)
        if self.confluence is not None:
            self.confluence.update(candle)
        if self.mlc2_evaluator is not None:
            self.mlc2_evaluator.update(candle)
        self._append_history(candle)
        self._update_tf_breakout(candle)
        if hasattr(self.trend_follow, "observe"):
            if self._strategy_breakout_observable() and self._trend_session_allowed(candle.timestamp):
                self.trend_follow.observe(
                    candle,
                    self.detector.get_recent_zones(),
                    self.detector.is_zone_mature,
                )
            else:
                self._reset_breakout_confirmation()
        elif hasattr(self.trend_follow, "warmup"):
            self.trend_follow.warmup(candle)

    async def _fetch_latest_candles(self, unit_number: int = 30) -> List[Candle]:
        """Fetch recent completed 1-minute candles from TopstepX API.

        NOTE: TopstepX 30s bar API has a ~6-hour settle delay — bars from
        sub-minute endpoints are never current. 1m bars are real-time.
        Live trading intentionally drops the newest returned bar as a safety
        buffer, so decisions use only the previous completed candle. This keeps
        live entries aligned with normal backtest timing.

        TopstepX returns bars newest-first, so candles[-1] is the OLDEST.
        Must sort by timestamp to get the actual newest.
        """
        try:
            candles = await self.client.get_historical_bars(
                contract_id=self.contract_id,
                unit=BarUnit.MINUTE,   # 1m bars — no settle delay
                unit_number=1,
                limit=60,
            )
            if candles:
                # Sort chronologically — API returns newest-first
                candles.sort(key=lambda c: c.timestamp)
                closed_candles = candles[:-1] if len(candles) > 1 else []
                if not closed_candles:
                    return []
                self._last_market_price = closed_candles[-1].close
                try:
                    from backend.api.routes import _upsert_historical_candles
                    _upsert_historical_candles(closed_candles)
                except Exception:
                    pass
                return closed_candles
        except Exception as e:
            self._log_event(f"取得K線失敗: {e}", "error")
        return []


