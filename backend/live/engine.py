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
# 僅支援原始策略 (trend_follow / reversion), 5 分鐘 K 線
# ============================================================
"""
Live Trading Engine

每 60 秒輪詢 5m K 線 → 盤整偵測 → 策略評估 → 下真實 limit order
支援：掛單 / 取消 / SL-TP / 收盤前平倉 / 每日交易上限
"""

from __future__ import annotations
import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

from backend.db.models import (
    Candle, TradeSignal, OrderRequest, OrderResponse,
    ConsolidationZone, Direction, StrategyType, ZoneStatus, BarUnit,
)
from backend.strategy.consolidation import ConsolidationDetector
from backend.strategy.trend_follow import TrendFollowStrategy
from backend.strategy.reversion import ReversionStrategy
from backend.broker.topstepx import TopstepXClient

logger = logging.getLogger(__name__)

POINT_VALUE = 20.0
TICK_SIZE = 0.25


class LiveTradingEngine:
    """即時交易引擎 — 原始模式 (5m K 線)"""

    FLATTEN_TIME = time(15, 5)      # CT 15:05 = flatten
    PRE_FLATTEN = time(14, 50)      # CT 14:50 = cancel pending

    def __init__(
        self,
        client: TopstepXClient,
        account_id: int,
        contract_id: str,
        # Strategy params
        strategies: List[str] = None,
        sl_dollars: float = 300.0,
        tp_dollars: float = 900.0,
        reversion_tp_mode: str = "poc",
        trend_tp_mode: str = "multiplier",
        trend_tp_multiplier: float = 4.0,
        max_daily_trades: int = 5,
        # Zone detection params
        min_candles_for_zone: int = 6,
        poc_drift_threshold: float = 3.0,
        value_area_pct: float = 0.80,
        # Slippage
        slippage_ticks: int = 1,
        # Safety: let TopstepX Position Bracket handle SL/TP instead of engine
        skip_engine_sl_tp: bool = True,
    ):
        self.client = client
        self.account_id = account_id
        self.contract_id = contract_id
        self.strategies = strategies or ["trend_follow"]
        self.slippage_ticks = slippage_ticks
        self.max_daily_trades = max_daily_trades
        self.reversion_tp_mode = reversion_tp_mode

        sl_pts = sl_dollars / POINT_VALUE
        tp_pts_rev = tp_dollars / POINT_VALUE
        tp_pts_trend = tp_dollars / POINT_VALUE
        if trend_tp_mode == "multiplier":
            tp_pts_trend = sl_pts * trend_tp_multiplier

        self.detector = ConsolidationDetector(
            min_candles=min_candles_for_zone,
            poc_drift_threshold=poc_drift_threshold,
            value_area_pct=value_area_pct,
        )
        self.reversion = ReversionStrategy(
            sl_points=sl_pts,
            tp_points=tp_pts_rev,
            min_zone_candles=10,
            entry_pct_high=0.90,
            entry_pct_low=0.10,
        )
        self.trend_follow = TrendFollowStrategy(
            sl_points=sl_pts,
            tp_points=tp_pts_trend,
            confirm_candles=4,
            range_pct=0.90,
        )

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
        self._daily_trade_count: int = 0
        self._daily_pnl: float = 0.0
        self._today: str = ""
        self._capital: float = 0.0
        self._candles_processed: int = 0
        self._skip_engine_sl_tp: bool = skip_engine_sl_tp
        self._last_market_price: Optional[float] = None
        self._last_candle_time: Optional[str] = None
        self._trades: List[Dict] = []
        self._log: List[str] = []

    @property
    def is_running(self) -> bool:
        return self._running

    def get_status(self) -> Dict:
        """Return current engine state for frontend."""
        return {
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

        trend_state = self.trend_follow.raw_state
        if trend_state == "watching":
            return self.trend_follow.get_phase_label()
        if trend_state == "confirmed":
            return "入場準備"
        active = self.detector.get_active_zone()
        if active:
            return "盤整"
        return "等待盤整"

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

        # Warm up: feed to detector + strategy
        for c in historical_candles:
            self.detector.update(c)
            active_zone = self.detector.get_active_zone()
            all_zones = self.detector.get_all_zones()
            if "trend_follow" in self.strategies:
                self.trend_follow.evaluate(c, active_zone, all_zones, False)

        self._candles_processed = len(historical_candles)

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
        self._log_event(
            f"引擎啟動 | 帳戶={self.account_id} | "
            f"區間={len(all_z)} | 活躍={active.zone_id if active else 'None'} | "
            f"策略={self.strategies}"
        )

        # Start main loop
        self._task = asyncio.create_task(self._main_loop())

    async def stop(self):
        """Stop the engine. Cancel pending orders."""
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

    # ── Main Loop ──────────────────────────────────────────

    async def _main_loop(self):
        """Main trading loop — runs every 60 seconds (5m candles)."""
        interval = 60
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
        """One iteration of the trading loop (5m candles)."""
        now = datetime.utcnow()

        # Reset daily counters
        today_str = now.strftime("%Y-%m-%d")
        if today_str != self._today:
            self._today = today_str
            self._daily_trade_count = 0
            self._daily_pnl = 0.0
            self._log_event("新交易日 — 重置計數")

        # Check position status from API
        await self._sync_position()

        # Get latest 5m candle
        candle = await self._fetch_latest_candle(unit_number=5)
        if not candle:
            return

        # Skip if same candle as last tick
        candle_ts = candle.timestamp.isoformat()
        if candle_ts == self._last_candle_time:
            return
        self._last_candle_time = candle_ts
        self._candles_processed += 1

        # Track latest market price for safety checks
        self._last_market_price = candle.close

        # Convert to CT for time checks (CDT = UTC-5)
        ct_time = (now - timedelta(hours=5)).time()

        # ── Flatten time ──
        if ct_time >= self.FLATTEN_TIME:
            if self._open_position:
                self._log_event("收盤平倉")
                await self.flatten_now()
            if self._pending_order_id:
                await self._cancel_pending()
            return

        # ── Pre-flatten: cancel pending ──
        if ct_time >= self.PRE_FLATTEN and self._pending_order_id:
            self._log_event("收盤前取消掛單")
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
            if self._pending_age > 20:
                self._log_event("掛單超時取消")
                await self._cancel_pending()

        # ── If position open, managed by SL/TP on exchange ──
        if self._open_position:
            return

        # ── Update zone detector ──
        self.detector.update(candle)

        # ── Strategy evaluation ──
        active_zone = self.detector.get_active_zone()
        all_zones = self.detector.get_all_zones()

        # Trend follow
        if "trend_follow" in self.strategies:
            signal = self.trend_follow.evaluate(candle, active_zone, all_zones, False)
            if signal and not self._pending_order_id:
                await self._place_order(signal)
                return

        # Reversion
        if "reversion" in self.strategies and not self._pending_order_id:
            if active_zone:
                signal = self.reversion.evaluate(candle, active_zone)
                if signal:
                    if self.reversion_tp_mode == "poc" and active_zone.poc:
                        signal.tp_price = active_zone.poc
                    await self._place_order(signal)
                    return

    # ── Order Management ──────────────────────────────────

    async def _place_order(self, signal: TradeSignal):
        """Place a limit order on the exchange.

        Safety checks:
        1. Entry price vs market price — block if too far (instant fill risk)
        2. No market price reference — block entirely
        """
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
                return
            if signal.direction == Direction.BUY and signal.entry_price > mkt + PRICE_SAFETY_MARGIN:
                self._log_event(
                    f"[SAFETY BLOCK] BUY LIMIT @ {signal.entry_price:.2f} 遠高於市價 {mkt:.2f} "
                    f"(差 {signal.entry_price - mkt:.1f} pts) → 攔截",
                    "error"
                )
                return
            self._log_event(
                f"[SAFETY OK] {dir_label} LIMIT @ {signal.entry_price:.2f} | 市價={mkt:.2f} | "
                f"差距={abs(signal.entry_price - mkt):.1f} pts"
            )
        else:
            self._log_event(
                f"[SAFETY BLOCK] 無市價參考, 拒絕下單! entry={signal.entry_price:.2f}",
                "error"
            )
            return

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
            else:
                self._log_event(f"掛單失敗: {resp.error_message}", "error")
        except Exception as e:
            self._log_event(f"下單異常: {e}", "error")

    async def _cancel_pending(self):
        """Cancel the pending limit order."""
        if not self._pending_order_id:
            return
        try:
            await self.client.cancel_order(self._pending_order_id)
            self._log_event(f"取消掛單 #{self._pending_order_id}")
        except Exception as e:
            self._log_event(f"取消失敗: {e}", "error")

        if self._pending_signal:
            if self._pending_signal.strategy == StrategyType.TREND_FOLLOW:
                self.trend_follow.notify_order_cancelled()

        self._pending_order_id = None
        self._pending_signal = None
        self._pending_age = 0

    async def _check_pending_fill(self) -> bool:
        """Check if our pending order resulted in a position."""
        try:
            positions = await self.client.get_positions(self.account_id)
            if positions and len(positions) > 0:
                self._open_position = positions[0]
                self._daily_trade_count += 1

                # Track fill price
                fill_price_raw = positions[0].get('avgPrice', positions[0].get('averagePrice'))
                try:
                    self._fill_price = float(fill_price_raw) if fill_price_raw else None
                except (ValueError, TypeError):
                    self._fill_price = None

                self._log_event(
                    f"掛單成交! 持倉: {positions[0].get('side', '?')} @ "
                    f"fill={self._fill_price} | position_raw={positions[0]}"
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

                # SL/TP: let TopstepX Position Bracket handle it
                if not self._skip_engine_sl_tp:
                    await self._place_sl_tp()
                else:
                    self._log_event("[SL/TP] 由 TopstepX Position Bracket 管理")
                return True
        except Exception as e:
            self._log_event(f"檢查成交失敗: {e}", "error")
        return False

    async def _place_sl_tp(self):
        """Place SL and TP orders for the current position.

        NOTE: If skip_engine_sl_tp=True (default), this is NOT called.
        TopstepX Position Bracket manages SL/TP automatically.
        """
        if not self._pending_signal or not self._open_position:
            return

        sig = self._pending_signal
        sl_side = 2 if sig.direction == Direction.BUY else 1
        sl_order = OrderRequest(
            account_id=self.account_id,
            contract_id=self.contract_id,
            order_type=3,  # Stop
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

        try:
            sl_resp = await self.client.place_order(sl_order)
            if sl_resp.success:
                self._sl_order_id = sl_resp.order_id
                self._log_event(f"SL 掛單 #{sl_resp.order_id} @ {sig.sl_price:.2f}")
            else:
                self._log_event(f"SL 掛單失敗: {sl_resp.error_message}", "error")
        except Exception as e:
            self._log_event(f"SL 下單異常: {e}", "error")

        try:
            tp_resp = await self.client.place_order(tp_order)
            if tp_resp.success:
                self._tp_order_id = tp_resp.order_id
                self._log_event(f"TP 掛單 #{tp_resp.order_id} @ {sig.tp_price:.2f}")
            else:
                self._log_event(f"TP 掛單失敗: {tp_resp.error_message}", "error")
        except Exception as e:
            self._log_event(f"TP 下單異常: {e}", "error")

    async def _sync_position(self):
        """Sync position state from exchange."""
        try:
            positions = await self.client.get_positions(self.account_id)
            was_open = self._open_position is not None
            self._open_position = positions[0] if positions else None

            # Position closed (SL or TP hit)
            if was_open and not self._open_position:
                pnl_info = ""
                if self._fill_price:
                    pnl_info = f" | entry_fill={self._fill_price:.2f}"

                self._log_event(f"持倉已平 (SL/TP 觸發){pnl_info}")
                self._sl_order_id = None
                self._tp_order_id = None
                self._fill_price = None

                self._trades.append({
                    "time": datetime.utcnow().isoformat(),
                    "type": "closed",
                })

                # Notify strategy
                if self._pending_signal:
                    if self._pending_signal.strategy == StrategyType.TREND_FOLLOW:
                        self.trend_follow.notify_trade_closed("tp")
                else:
                    self.trend_follow.notify_trade_closed("tp")

            # Update capital
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
            logger.error(f"[SYNC] position sync failed: {e}")

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
