# ============================================================
# 文件: backend/live/engine.py
# 狀態: 正在修改 — 已移除 MTF, 僅保留原始模式
# 問題:
#   1. [BUG] 僅監控模式 (status=僅監控) 原因不明
#   2. [BUG] 歷史 zone 來自連接時加載的數據
#   3. [TODO] _sync_position 中 capital 追蹤邏輯可能不準
#   4. [TODO] SL/TP order 狀態追蹤不完整
# 關聯文件:
#   ← backend/api/routes.py     (/live/start, /live/stop, /live/status)
#   → backend/strategy/consolidation.py  (zone 偵測)
#   → backend/strategy/trend_follow.py   (趨勢跟隨)
#   → backend/strategy/reversion.py      (均值回歸)
#   → backend/broker/topstepx.py         (API 下單)
# ============================================================
# 即時交易引擎 — 在 Practice 帳戶上執行策略下單
# 僅支援原始策略 (trend_follow / reversion), 1 分鐘 K 線
# ============================================================
"""
Live Trading Engine

每 30 秒輪詢 1m K 線 → 盤整偵測 → 策略評估 → 下真實 limit order
支援：掛單 / 取消 / SL-TP / 收盤前平倉 / 每日交易上限
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time as time_mod
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

from backend.db.models import (
    Candle, TradeSignal, OrderRequest, OrderResponse,
    ConsolidationZone, Direction, StrategyType, ZoneStatus, BarUnit,
    StrategyParams,
)
from backend.strategy.consolidation import SessionZoneDetector
from backend.strategy.trend_follow import SessionTrendFollow
from backend.broker.topstepx import TopstepXClient

logger = logging.getLogger(__name__)

ENGINE_VERSION = "v4.0-session-2026-03-29"  # Session-based overnight zone
POINT_VALUE = 20.0
TICK_SIZE = 0.25


class LiveTradingEngine:
    """即時交易引擎 — Session 模式 (1m K 線, 晚盤 overnight zone)"""

    # 加州 12:45 PT = 15:45 ET = 14:45 CT = 19:45 UTC
    FLATTEN_TIME_UTC = time(19, 45)     # UTC 19:45 = PT 12:45 flatten
    PRE_FLATTEN_UTC = time(19, 30)      # UTC 19:30 = PT 12:30 cancel pending

    def __init__(
        self,
        client: TopstepXClient,
        account_id: int,
        contract_id: str,
        # Strategy params (simplified — SessionTrendFollow only)
        max_daily_trades: int = 5,
        value_area_pct: float = 0.80,
        slippage_ticks: int = 1,
        # Engine manages SL/TP — MUST be False to protect positions
        skip_engine_sl_tp: bool = False,
        # Configurable strategy params
        strategy_params: Optional[StrategyParams] = None,
        # Legacy params (ignored, kept for API compatibility)
        strategies: List[str] = None,
        sl_dollars: float = 300.0,
        tp_dollars: float = 900.0,
        reversion_tp_mode: str = "poc",
        trend_tp_mode: str = "multiplier",
        trend_tp_multiplier: float = 4.0,
        min_candles_for_zone: int = 6,
        poc_drift_threshold: float = 3.0,
    ):
        self.client = client
        self.account_id = account_id
        self.contract_id = contract_id
        self.strategies = ["trend_follow"]  # Only SessionTrendFollow
        self.slippage_ticks = slippage_ticks
        self.max_daily_trades = max_daily_trades
        self.strategy_params = strategy_params or StrategyParams()

        # Session-based zone detector (overnight zone with maturity)
        self.detector = SessionZoneDetector(
            value_area_pct=value_area_pct,
        )
        # SessionTrendFollow with configurable params
        self.trend_follow = SessionTrendFollow(params=self.strategy_params)

        # Live state
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._pending_order_id: Optional[int] = None
        self._pending_signal: Optional[TradeSignal] = None
        self._pending_age: int = 0
        self._open_position: Optional[Dict] = None
        self._fill_price: Optional[float] = None
        self._sl_order_id: Optional[int] = None
        self._tp_order_id: Optional[int] = None
        self._active_signal: Optional[TradeSignal] = None  # preserved after fill for SL/TP
        self._position_just_closed: bool = False  # skip strategy eval on same tick as close
        self._position_age: int = 0              # candles since position opened
        self._tp_timeout_triggered: bool = False  # prevent re-triggering
        self._daily_trade_count: int = 0
        self._daily_pnl: float = 0.0
        self._today: str = ""
        self._capital: float = 0.0
        self._candles_processed: int = 0
        self._skip_engine_sl_tp: bool = skip_engine_sl_tp
        self._last_market_price: Optional[float] = None
        self._last_candle_time: Optional[str] = None
        self._last_pnl_check: float = 0.0  # timestamp of last PNL check
        self._trades: List[Dict] = []
        self._log: List[str] = []
        self._last_status_log_minute: int = -1  # track minute for periodic status log
        self._zone_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "live_zones.json"
        )

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

            active_id = data.get("active_zone_id")
            loaded = 0
            for zd in data.get("zones", []):
                # Skip legacy ConsolidationDetector zones (Z prefix)
                zid = zd.get("zone_id", "")
                if not zid.startswith("S"):
                    logger.info(f"跳過舊版 zone: {zid}")
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
                )
                self.detector._all_zones.append(zone)
                if zone.zone_id == active_id and zone.status == ZoneStatus.ACTIVE:
                    self.detector._active_zone = zone
                self.detector._zone_counter = max(
                    self.detector._zone_counter,
                    int(zone.zone_id.lstrip("S")) if zone.zone_id.startswith("S") else 0
                )
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

    def get_status(self) -> Dict:
        """Return current engine state for frontend."""
        return {
            "engine_version": ENGINE_VERSION,
            "running": self._running,
            "account_id": self.account_id,
            "contract_id": self.contract_id,
            "position": self._open_position,
            "pending_order_id": self._pending_order_id,
            "pending_signal": {
                "direction": self._pending_signal.direction.value,
                "entry_price": self._pending_signal.entry_price,
                "sl_price": self._pending_signal.sl_price,
                "tp_price": self._pending_signal.tp_price,
                "strategy": self._pending_signal.strategy.value,
            } if self._pending_signal else None,
            "daily_trades": self._daily_trade_count,
            "max_daily_trades": self.max_daily_trades,
            "daily_pnl": self._daily_pnl,
            "capital": self._capital,
            "candles_processed": self._candles_processed,
            "last_market_price": self._last_market_price,
            "fill_price": self._fill_price,
            "skip_engine_sl_tp": self._skip_engine_sl_tp,
            "zones": self._get_zone_summary(),
            "phase": self._get_phase(),
            "trades": self._trades[-10:],
            "log": self._log[-20:],
        }

    def _get_phase(self) -> str:
        if self._open_position:
            return "持倉中"
        if self._pending_order_id:
            return "掛單中"

        active = self.detector.get_active_zone()
        is_mature = self.detector.is_zone_mature

        trend_state = self.trend_follow.raw_state
        if trend_state == "watching":
            return self.trend_follow.get_phase_label()
        if trend_state == "confirmed":
            return "入場準備"

        if active and is_mature:
            return "成熟區間"
        if active:
            age_min = active.duration_minutes
            hours = age_min // 60
            mins = age_min % 60
            return f"區間發展中({hours}h{mins:02d}m)"
        return "等待晚盤"

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
            })
        return result

    def _log_event(self, msg: str, level: str = "info"):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self._log.append(entry)
        if len(self._log) > 100:
            self._log = self._log[-50:]
        if level == "error":
            logger.error(f"[LIVE] {msg}")
        else:
            logger.info(f"[LIVE] {msg}")

    async def start(self, historical_candles: List[Candle]):
        """Start the live engine. Feed historical candles first for zone state."""
        if self._running:
            return

        self._running = True
        self._today = datetime.utcnow().strftime("%Y-%m-%d")
        self._daily_trade_count = 0
        self._daily_pnl = 0.0
        self._trades = []
        self._log = []

        # Log candle date range
        if historical_candles:
            first_ts = historical_candles[0].timestamp.strftime("%Y-%m-%d %H:%M")
            last_ts = historical_candles[-1].timestamp.strftime("%Y-%m-%d %H:%M")
            self._log_event(
                f"載入 {len(historical_candles)} 根歷史K線 | "
                f"範圍: {first_ts} ~ {last_ts}"
            )
        else:
            self._log_event("⚠ 無歷史K線! warm-up 跳過", "error")

        # Warm up: feed historical candles to session zone detector
        # MUST sort chronologically — API returns newest-first
        historical_candles = sorted(historical_candles, key=lambda c: c.timestamp)
        for c in historical_candles:
            self.detector.update(c)

        self._candles_processed = len(historical_candles)

        # Reset strategy after warm-up (prevent stale state)
        if "trend_follow" in self.strategies:
            self.trend_follow.reset()

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

        # Get initial account balance
        try:
            positions = await self.client.get_positions(self.account_id)
            self._open_position = positions[0] if positions else None
            accounts = await self.client.get_accounts()
            for acc in accounts:
                if acc.get("id") == self.account_id:
                    self._capital = acc.get("balance", 0)
                    break
        except Exception as e:
            self._log_event(f"取得帳戶資訊失敗: {e}", "error")

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
                await self.client.cancel_order(self._pending_order_id)
                self._log_event(f"取消掛單 #{self._pending_order_id}")
            except Exception as e:
                self._log_event(f"取消掛單失敗: {e}", "error")
            self._pending_order_id = None
            self._pending_signal = None

        self._log_event("引擎已停止")

    async def flatten_now(self):
        """Emergency flatten all positions."""
        try:
            results = await self.client.flatten_all(self.account_id)
            self._log_event(f"緊急平倉完成: {len(results)} orders")
            self._open_position = None
            self._sl_order_id = None
            self._tp_order_id = None
        except Exception as e:
            self._log_event(f"緊急平倉失敗: {e}", "error")

    async def _recalc_tp_live(self, new_factor: int):
        """Recalculate TP with new factor: cancel old TP, place new one.
        Uses SL distance as base: TP = entry ± |entry - SL| × new_factor
        """
        if not self._active_signal:
            self._log_event("TP recalc skipped — no active signal", "warn")
            return

        sig = self._active_signal
        sl_distance = abs(sig.entry_price - sig.sl_price)
        if sig.direction == Direction.BUY:
            new_tp = sig.entry_price + sl_distance * new_factor
        else:
            new_tp = sig.entry_price - sl_distance * new_factor

        # Cancel existing TP order
        if self._tp_order_id:
            try:
                await self.client.cancel_order(self.account_id, self._tp_order_id)
                self._log_event(f"舊 TP 取消 (order {self._tp_order_id})")
            except Exception as e:
                self._log_event(f"取消 TP 失敗: {e}", "error")
            self._tp_order_id = None

        # Place new TP limit order
        try:
            tp_side = 2 if sig.direction == Direction.BUY else 1  # sell to close / buy to close
            tp_order = OrderRequest(
                account_id=self.account_id,
                contract_id=self.contract_id,
                order_type=1,  # Limit
                side=tp_side,
                size=1,
                limit_price=new_tp,
            )
            resp = await self.client.place_order(tp_order)
            if resp and resp.success:
                self._tp_order_id = resp.order_id
                self._log_event(f"新 TP @ {new_tp:.2f} ({new_factor}x) order={resp.order_id}")
            else:
                self._log_event(f"新 TP 下單失敗: {resp}", "error")
        except Exception as e:
            self._log_event(f"新 TP 下單失敗: {e}", "error")

    # ── Main Loop ──────────────────────────────────────────

    async def _main_loop(self):
        """Main trading loop — runs every 5 seconds."""
        interval = 5
        self._log_event(f"主循環啟動 — 每{interval}秒輪詢")

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                self._log_event(f"Tick error: {e}", "error")

            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    async def _tick(self):
        """One iteration of the trading loop (1m candles)."""
        now = datetime.utcnow()

        # Reset daily counters
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self._today:
            self._today = today_str
            self._daily_trade_count = 0
            self._daily_pnl = 0.0
            self._log_event("新交易日 — 重置計數")

        # Check position status from API (ALWAYS, even without new candle)
        await self._sync_position()

        # Skip strategy evaluation on the same tick a position was just closed
        if self._position_just_closed:
            self._position_just_closed = False
            return

        # Get latest 1m candle
        candle = await self._fetch_latest_candle(unit_number=1)
        if not candle:
            return

        # Always update market price from latest candle close (even if same candle)
        self._last_market_price = candle.close

        # Skip strategy evaluation if same candle as last tick
        candle_ts = candle.timestamp.isoformat()
        if candle_ts == self._last_candle_time:
            return
        self._last_candle_time = candle_ts
        self._candles_processed += 1

        # Auto-save zones every 5 new candles (~5 minutes for 1m bars)
        if self._candles_processed % 5 == 0:
            self._save_zones()

        # ── ALWAYS update zone detector first (even during flatten/limits) ──
        self.detector.update(candle)

        # ── Periodic status log every minute (POC/VAH/VAL) ──
        current_minute = now.minute
        if current_minute != self._last_status_log_minute:
            self._last_status_log_minute = current_minute
            active = self.detector.get_active_zone()
            is_mature = self.detector.is_zone_mature
            phase = self._get_phase()
            if active:
                spread_ticks = (active.vah_80 - active.val_80) / TICK_SIZE
                self._log_event(
                    f"狀態={phase} | "
                    f"{'🟢成熟' if is_mature else '🟡發展中'} | "
                    f"POC={active.poc:.2f} | "
                    f"VAH={active.vah_80:.2f} | "
                    f"VAL={active.val_80:.2f} | "
                    f"spread={spread_ticks:.0f}tick | "
                    f"市價={self._last_market_price or 0:.2f}"
                )
            else:
                self._log_event(
                    f"狀態={phase} | 無活躍區間 | "
                    f"市價={self._last_market_price or 0:.2f}"
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
                await self._cancel_pending()
            return  # no new trades during flatten, but detector already updated

        # ── Pre-flatten: cancel pending (PT 12:30 = UTC 19:30) ──
        if utc_time >= self.PRE_FLATTEN_UTC and utc_time < session_start and self._pending_order_id:
            self._log_event("PT 12:30 收盤前取消掛單")
            await self._cancel_pending()

        # ── Daily trade limit ──
        if self._daily_trade_count >= self.max_daily_trades and not self._open_position:
            if self._pending_order_id:
                await self._cancel_pending()
            return

        # ── Check if pending order filled ──
        if self._pending_order_id and not self._open_position:
            filled = await self._check_pending_fill()
            if filled:
                self._pending_order_id = None
                self._pending_signal = None
                self._pending_age = 0
                return
            self._pending_age += 1
            timeout = self.trend_follow.PENDING_TIMEOUT_CANDLES
            if self._pending_age > timeout:
                self._log_event(f"掛單超時 {timeout} 分鐘取消")
                await self._cancel_pending()

        # ── If position open, verify SL/TP are in place ──
        if self._open_position:
            if not self._skip_engine_sl_tp:
                if self._active_signal:
                    if not self._sl_order_id and not self._tp_order_id:
                        self._log_event("⚠ 持倉中但無 SL/TP → 補掛 SL/TP", "error")
                        self._pending_signal = self._active_signal
                        await self._place_sl_tp()
                        self._pending_signal = None
                else:
                    if not self._sl_order_id and not self._tp_order_id:
                        self._log_event(
                            "⚠ 持倉中無 SL/TP 且無 signal（重啟後或手動入場）→ 需手動設定 SL/TP",
                            "error"
                        )

            # ── TP Timeout check ──
            self._position_age += 1
            tp_timeout = self.trend_follow.TP_TIMEOUT_CANDLES
            if tp_timeout > 0 and not self._tp_timeout_triggered and self._position_age >= tp_timeout:
                self._tp_timeout_triggered = True
                action = self.trend_follow.TP_TIMEOUT_ACTION
                if action == "flat":
                    self._log_event(f"TP timeout ({tp_timeout} min) → flat close", "warn")
                    await self.flatten_now()
                else:
                    new_factor = int(action)
                    self._log_event(f"TP timeout ({tp_timeout} min) → TP factor → {new_factor}x", "warn")
                    await self._recalc_tp_live(new_factor)
            return

        # ── Safety: cancel orphaned SL/TP if FLAT ──
        if not self._open_position and not self._pending_order_id:
            if self._sl_order_id or self._tp_order_id:
                self._log_event(
                    f"⚠ FLAT 但有殘留單 SL=#{self._sl_order_id} TP=#{self._tp_order_id} → 清除",
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

        # ── Strategy evaluation ──
        active_zone = self.detector.get_active_zone()
        is_mature = self.detector.is_zone_mature

        # Session Trend Follow (primary strategy)
        if "trend_follow" in self.strategies:
            # Safety: if strategy thinks it's confirmed but no order exists, reset
            if self.trend_follow.raw_state == "confirmed" and not self._pending_order_id:
                self._log_event(
                    "⚠ SessionTrend stuck in 'confirmed' but no pending order → reset"
                )
                self.trend_follow.reset()

            signal = self.trend_follow.evaluate(candle, active_zone, is_mature)
            if signal and not self._pending_order_id:
                placed = await self._place_order(signal)
                if not placed:
                    self.trend_follow.notify_order_cancelled()
                return


    # ── Order Management ──────────────────────────────────

    @staticmethod
    def _round_to_tick(price: float) -> float:
        """Round price to nearest NQ tick (0.25)."""
        return round(round(price / TICK_SIZE) * TICK_SIZE, 2)

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

        order = OrderRequest(
            account_id=self.account_id,
            contract_id=self.contract_id,
            order_type=1,  # Limit
            side=side,
            size=1,
            limit_price=signal.entry_price,
        )

        try:
            resp = await self.client.place_order(order)
            if resp.success:
                self._pending_order_id = resp.order_id
                self._pending_signal = signal
                self._pending_age = 0
                self._log_event(
                    f"掛單成功 #{resp.order_id} | {dir_label} LIMIT @ {signal.entry_price:.2f} | "
                    f"SL={signal.sl_price:.2f} TP={signal.tp_price:.2f} | "
                    f"策略={signal.strategy.value}"
                )
                return True
            else:
                self._log_event(
                    f"掛單失敗: code={resp.error_code} msg={resp.error_message} "
                    f"| entry={signal.entry_price:.2f} side={'BUY' if side == 1 else 'SELL'} "
                    f"(api_side={0 if side == 1 else 1})",
                    "error"
                )
                return False
        except Exception as e:
            self._log_event(f"下單異常: {e}", "error")
            return False

    async def _cancel_with_retry(self, order_id: Optional[int], label: str):
        """Cancel an order with retry. Handles 400 errors by retrying once."""
        if not order_id:
            return
        success = await self.client.cancel_order(order_id)
        if success:
            self._log_event(f"取消殘留 {label} #{order_id}")
            return
        # First attempt failed (400) — wait and retry once
        await asyncio.sleep(1)
        success = await self.client.cancel_order(order_id)
        if success:
            self._log_event(f"取消殘留 {label} #{order_id} (重試成功)")
        else:
            self._log_event(f"取消 {label} #{order_id} 失敗 (可能已成交)")

    async def _cancel_pending(self):
        """Cancel the pending limit order."""
        if not self._pending_order_id:
            return
        success = await self.client.cancel_order(self._pending_order_id)
        if success:
            self._log_event(f"取消掛單 #{self._pending_order_id}")
        else:
            self._log_event(f"取消掛單 #{self._pending_order_id} (已成交或已取消)")

        if self._pending_signal:
            if self._pending_signal.strategy == StrategyType.TREND_FOLLOW:
                self.trend_follow.notify_order_cancelled()

        self._pending_order_id = None
        self._pending_signal = None
        self._pending_age = 0

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
            self._pending_age = 0
            return True
        return False

    async def _place_sl_tp(self):
        """Place SL and TP orders for the current position.

        Called immediately after fill detection in _sync_position.
        Also retried in _tick if SL/TP orders are missing.
        """
        if not self._pending_signal or not self._open_position:
            self._log_event(
                f"[SL/TP] _place_sl_tp 跳過: signal={self._pending_signal is not None} "
                f"position={self._open_position is not None}",
                "error"
            )
            return

        sig = self._pending_signal
        # SL/TP side = opposite of entry direction (close the position)
        sl_side = 2 if sig.direction == Direction.BUY else 1
        side_label = "SELL" if sl_side == 2 else "BUY"

        self._log_event(
            f"[SL/TP] 下單中: {side_label} SL(StopMarket)@{sig.sl_price:.2f} "
            f"TP(Limit)@{sig.tp_price:.2f} | contract={self.contract_id}"
        )

        sl_order = OrderRequest(
            account_id=self.account_id,
            contract_id=self.contract_id,
            order_type=3,  # Internal 3 → broker converts to API type 4 (Stop)
            side=sl_side,
            size=1,
            stop_price=sig.sl_price,
        )
        tp_order = OrderRequest(
            account_id=self.account_id,
            contract_id=self.contract_id,
            order_type=1,  # Limit
            side=sl_side,
            size=1,
            limit_price=sig.tp_price,
        )

        # Place SL order
        try:
            sl_resp = await self.client.place_order(sl_order)
            if sl_resp.success:
                self._sl_order_id = sl_resp.order_id
                self._log_event(f"✅ SL 掛單成功 #{sl_resp.order_id} @ {sig.sl_price:.2f}")
            else:
                self._log_event(
                    f"❌ SL 掛單失敗: code={sl_resp.error_code} msg={sl_resp.error_message}",
                    "error"
                )
        except Exception as e:
            self._log_event(f"❌ SL 下單異常: {e}", "error")

        # Place TP order
        try:
            tp_resp = await self.client.place_order(tp_order)
            if tp_resp.success:
                self._tp_order_id = tp_resp.order_id
                self._log_event(f"✅ TP 掛單成功 #{tp_resp.order_id} @ {sig.tp_price:.2f}")
            else:
                self._log_event(
                    f"❌ TP 掛單失敗: code={tp_resp.error_code} msg={tp_resp.error_message}",
                    "error"
                )
        except Exception as e:
            self._log_event(f"❌ TP 下單異常: {e}", "error")

    async def _sync_position(self):
        """Sync position state from exchange.

        Handles three transitions:
          1. pending → filled:  position appears while _pending_order_id is set
          2. filled  → closed:  position disappears (SL/TP hit)
          3. no change:         update capital only
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

                self._daily_trade_count += 1
                self._log_event(
                    f"掛單成交! #{self._pending_order_id} | "
                    f"fill={self._fill_price} | size={positions[0].get('size', '?')} | "
                    f"side={'LONG' if positions[0].get('side', 0) == 0 else 'SHORT'}"
                )

                if self._fill_price and self._pending_signal:
                    entry = self._pending_signal.entry_price
                    slippage = abs(self._fill_price - entry)
                    slippage_dollars = slippage * POINT_VALUE
                    if slippage > 5.0:
                        self._log_event(
                            f"⚠ [FILL MISMATCH] entry={entry:.2f} fill={self._fill_price:.2f} "
                            f"差距={slippage:.2f} pts (${slippage_dollars:.0f})",
                            "error"
                        )
                    else:
                        self._log_event(
                            f"[FILL OK] entry={entry:.2f} fill={self._fill_price:.2f} "
                            f"滑價={slippage:.2f} pts (${slippage_dollars:.0f})"
                        )

                # Record entry trade for chart markers
                sig_dir = "buy"
                if self._pending_signal:
                    sig_dir = self._pending_signal.direction.value
                self._trades.append({
                    "time": datetime.utcnow().isoformat(),
                    "type": "entry",
                    "direction": sig_dir,
                    "price": self._fill_price,
                    "strategy": "trend_follow",
                })

                # Place SL/TP protection orders
                if not self._skip_engine_sl_tp and self._pending_signal:
                    self._log_event(
                        f"[SL/TP] 準備下單 SL={self._pending_signal.sl_price:.2f} "
                        f"TP={self._pending_signal.tp_price:.2f} "
                        f"dir={self._pending_signal.direction.value}"
                    )
                    await self._place_sl_tp()
                elif self._skip_engine_sl_tp:
                    self._log_event("[SL/TP] 由 TopstepX Position Bracket 管理")
                else:
                    self._log_event("⚠ [SL/TP] 無 pending_signal 無法下 SL/TP!", "error")

                # Save signal for SL/TP retry, then clear pending state
                self._active_signal = self._pending_signal  # keep for SL/TP reference
                self._pending_order_id = None
                self._pending_signal = None
                self._pending_age = 0
                self._position_age = 0
                self._tp_timeout_triggered = False

            # ── Transition 1b: Position exists but engine didn't place it ──
            # (restart scenario or manual entry — still need SL/TP)
            elif has_position and not was_open and not self._pending_order_id:
                fill_price_raw = positions[0].get('averagePrice', positions[0].get('avgPrice'))
                try:
                    self._fill_price = float(fill_price_raw) if fill_price_raw else None
                except (ValueError, TypeError):
                    self._fill_price = None

                pos_side = positions[0].get('side', 0)
                self._log_event(
                    f"⚠ 偵測到未追蹤的持倉 | fill={self._fill_price} | "
                    f"side={'LONG' if pos_side == 0 else 'SHORT'} | "
                    f"無 pending_order → 可能是手動入場或重啟後",
                    "error"
                )

            # ── Transition 2: Position CLOSED (SL/TP hit) ──
            if was_open and not has_position:
                pnl_info = ""
                if self._fill_price:
                    pnl_info = f" | entry_fill={self._fill_price:.2f}"

                # Detect exit reason by comparing market price to SL/TP levels
                exit_reason = "unknown"
                if self._active_signal and self._last_market_price:
                    sl_p = self._active_signal.sl_price
                    tp_p = self._active_signal.tp_price
                    mkt = self._last_market_price
                    # Whichever level is closer to current market price is the one that hit
                    if abs(mkt - sl_p) < abs(mkt - tp_p):
                        exit_reason = "sl"
                    else:
                        exit_reason = "tp"
                if exit_reason == "unknown":
                    exit_reason = "tp"

                entry_fill = self._fill_price  # save before clearing
                self._log_event(
                    f"持倉已平 ({exit_reason.upper()} 觸發){pnl_info}"
                )

                # Cancel residual orders — each in own try/except so one failure
                # doesn't block the other
                sl_id = self._sl_order_id
                tp_id = self._tp_order_id
                self._sl_order_id = None
                self._tp_order_id = None
                self._fill_price = None
                self._active_signal = None

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

                self._trades.append({
                    "time": datetime.utcnow().isoformat(),
                    "type": "closed",
                    "entry_price": entry_fill,
                })

                # Notify strategy with actual exit reason
                self.trend_follow.notify_trade_closed(exit_reason)
                self._position_just_closed = True  # skip new entry this tick

            # Update capital (every 60s to reduce API calls)
            now_ts = time_mod.time()
            if now_ts - self._last_pnl_check >= 60:
                self._last_pnl_check = now_ts
                accounts = await self.client.get_accounts()
                for acc in accounts:
                    if acc.get("id") == self.account_id:
                        new_balance = acc.get("balance", self._capital)
                        if self._capital > 0:
                            pnl_change = new_balance - self._capital
                            if abs(pnl_change - self._daily_pnl) > 1.0:
                                self._log_event(
                                    f"[PNL] balance={new_balance:.2f} daily_pnl={pnl_change:.2f}"
                                )
                            self._daily_pnl = pnl_change
                        break

        except Exception as e:
            self._log_event(f"[SYNC ERROR] {e}", "error")
            logger.error(f"[SYNC] position sync failed: {e}", exc_info=True)

    async def _fetch_latest_candle(self, unit_number: int = 5) -> Optional[Candle]:
        """Fetch the most recent candle from TopstepX API."""
        try:
            candles = await self.client.get_historical_bars(
                contract_id=self.contract_id,
                unit=BarUnit.MINUTE,
                unit_number=unit_number,
                limit=5,
            )
            if candles:
                try:
                    from backend.api.routes import _historical_candles
                    last_stored = _historical_candles[-1] if _historical_candles else None
                    for c in candles:
                        if not last_stored or c.timestamp > last_stored.timestamp:
                            _historical_candles.append(c)
                except Exception:
                    pass
                return candles[-1]
        except Exception as e:
            self._log_event(f"取得K線失敗: {e}", "error")
        return None
