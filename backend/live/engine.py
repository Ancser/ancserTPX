# ============================================================
# 文件: backend/live/engine.py
# 功能: 即時交易引擎 — Practice 帳戶上執行 SessionTrendFollow / MACD / Reversion
# 主要職責:
#   1. 30s 輪詢 1m K 線 → SessionZoneDetector → 策略 evaluate
#   2. Limit / Market 入場；fill 後等待 Auto OCO 子單並修改到算法 SL/TP
#   3. Trail SL: UPNL reaches configured TP% → modify Auto OCO SL to entry ± trail_sl_ticks
#   4. 收盤前 (12:30 PT 取消 pending / 12:45 PT flatten) 強制平倉
#   5. _sync_position 偵測 fill / close → 寫 trade 到 _trades + data/live_exits.json
# 版本變更 (v0.11):
#   - 接受 contract_size 參數 — 所有 OrderRequest 都用 self.contract_size
#   - POINT_VALUE 動態解析 (NQ=$20, MNQ=$2)
#   - 部位平倉時把 exit_reason (TP/SL/TRAIL_SL/FLATTEN) 寫入 live_exits.json
#     讓 routes._pair_fills_to_trades 能正確分類 (修復 trail_sl 被歸入 TP/SL bug)
#   - strategy_params.trail_enabled=False 時 _check_trailing_sl_live 立即返回
# 關聯:
#   ← backend/api/routes.py
#   → backend/strategy/consolidation.py / trend_follow.py / reversion.py / macd_strategy.py
#   → backend/broker/topstepx.py
# ============================================================
"""
Live Trading Engine

每 30 秒輪詢 1m K 線 → 盤整偵測 → 策略評估 → 下真實 limit order
支援：掛單 / 取消 / SL-TP / 收盤前平倉
"""

from __future__ import annotations
import asyncio
import json
import logging
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
from backend.strategy.consolidation import SessionZoneDetector
from backend.strategy.trend_follow import SessionTrendFollow
from backend.broker.topstepx import TopstepXClient

logger = logging.getLogger(__name__)

ENGINE_VERSION = "v4.0-session-2026-03-29"  # Session-based overnight zone
# Default fallbacks for legacy paths — actual values come from contract on init.
POINT_VALUE = 20.0
TICK_SIZE = 0.25

_CT = ZoneInfo("America/Chicago")
_UTC_TZ = ZoneInfo("UTC")



class LiveTradingEngine:
    """即時交易引擎 — Session 模式 (1m K 線, 晚盤 overnight zone)"""

    # 加州 12:45 PT = 15:45 ET = 14:45 CT = 19:45 UTC
    TRAIL_TICK_STEP = 5
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
        slippage_ticks: int = 1,
        contract_size: int = 1,
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
        # Strategy label set after strategy object is created (below)
        self.strategies: List[str] = []
        self.slippage_ticks = slippage_ticks
        self.strategy_params = strategy_params or StrategyParams()
        # Contract sizing — prefer the strategy_params value, fall back to ctor arg.
        self.contract_size = max(
            1,
            int(getattr(self.strategy_params, "contract_size", contract_size) or contract_size or 1),
        )
        # Per-contract market specs (NQ=$20/pt, MNQ=$2/pt; both 0.25 tick).
        self.point_value = get_point_value(contract_id)
        self.tick_size = get_tick_size(contract_id)

        # Session-based zone detector (overnight zone with maturity)
        self.detector = SessionZoneDetector(
            value_area_pct=value_area_pct,
            skip_stability_wait=getattr(self.strategy_params, "skip_zone_stability", False),
        )
        # Strategy selection
        _strat = (self.strategy_params.strategy or "trend").lower()
        if _strat == "macd":
            from backend.strategy.macd_strategy import MACDOnlyStrategy
            self.trend_follow = MACDOnlyStrategy(params=self.strategy_params)
            self.strategies = ["macd"]
        elif _strat == "reversion":
            from backend.strategy.reversion import SessionReversion
            self.trend_follow = SessionReversion(params=self.strategy_params)
            self.strategies = ["reversion"]
        elif _strat == "trend_reversion":
            from backend.strategy.reversion import SessionTrendReversion
            self.trend_follow = SessionTrendReversion(params=self.strategy_params)
            self.strategies = ["trend_reversion"]
        else:
            self.trend_follow = SessionTrendFollow(params=self.strategy_params)
            self.strategies = ["trend_follow"]

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
        self._position_age: int = 0              # candles since position opened (for display)
        self._trail_sl_triggered: bool = False    # trailing SL: one-time trigger per position
        self._protection_synced: bool = False     # Auto OCO child orders moved to strategy prices
        self._auto_oco_fail_safe_triggered: bool = False
        self._last_auto_oco_retry_ts: float = 0.0
        self._entry_time: Optional[datetime] = None  # when current position opened (UTC)
        self._force_exit_reason: Optional[str] = None  # set by flatten_now / emergency close
        self._daily_pnl: float = 0.0
        self._today: str = ""
        self._max_profit_lock: int = getattr(self.strategy_params, 'max_profit_lock', 0) or 0
        self._profit_locked: bool = False
        self._capital: float = 0.0
        self._candles_processed: int = 0
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
        # daily_capital.json removed — PnL now read directly from API

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

    def _persist_exit_record(
        self,
        exit_reason: str,
        entry_time: Optional[datetime],
        exit_time: datetime,
        entry_price: Optional[float],
        sl_price: Optional[float],
        tp_price: Optional[float],
        direction: Optional[str],
        trail_triggered: bool,
        zone_id: Optional[str] = None,
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
                "sl_price": sl_price,
                "tp_price": tp_price,
                "direction": direction,
                "zone_id": zone_id,
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
        prev = self.detector.get_last_left_zone()
        for z in (active, prev):
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
        return keys

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

    def _unlock_signal_breakout(self, signal: TradeSignal):
        direction = self._breakout_direction_from_trade_direction(signal.direction.value)
        if hasattr(self.trend_follow, "unlock_breakout") and signal.zone_id and direction:
            self.trend_follow.unlock_breakout(signal.zone_id, direction)

    def _remove_breakout_lock(self, signal: Optional[TradeSignal]):
        if not signal:
            return
        direction = self._breakout_direction_from_trade_direction(signal.direction.value)
        if not signal.zone_id or not direction:
            return

        self._unlock_signal_breakout(signal)

        today = self._get_topstep_trade_date()
        data = self._read_json_list_or_dict(self._breakout_locks_file)
        if not isinstance(data, dict):
            return
        records = data.get("locks")
        if not isinstance(records, list):
            return

        before = len(records)
        data["locks"] = [
            row for row in records
            if not (
                isinstance(row, dict)
                and row.get("trade_date") == today
                and row.get("account_id") == self.account_id
                and row.get("contract_id") == self.contract_id
                and str(row.get("zone_id")) == str(signal.zone_id)
                and row.get("direction") == direction
            )
        ]
        if len(data["locks"]) == before:
            return
        data["saved_at"] = datetime.utcnow().isoformat()
        try:
            os.makedirs(os.path.dirname(self._breakout_locks_file), exist_ok=True)
            with open(self._breakout_locks_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to remove breakout lock: {e}")

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
            "daily_pnl": self._daily_pnl,
            "profit_locked": self._profit_locked,
            "max_profit_lock": self._max_profit_lock,
            "disconnected": self._disconnected,
            "capital": self._capital,
            "candles_processed": self._candles_processed,
            "last_market_price": self._last_market_price,
            "fill_price": self._fill_price,
            "zones": self._get_zone_summary(),
            "phase": self._get_phase() if self._running else "引擎已停止",
            "trades": self._trades[-10:],
            "log": self._log[-20:],
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
            prev = self.detector.get_last_left_zone()
            if prev:
                return f"發展({hours}h{mins:02d}m)|用前區間"
            return f"發展({hours}h{mins:02d}m)"
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

    def _get_phase(self) -> str:
        """Combined phase for frontend display."""
        return f"區間:{self._get_zone_phase()} | 訂單:{self._get_order_phase()}"

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

        # Warm up: feed historical candles to session zone detector + strategy
        # MUST sort chronologically — API returns newest-first
        historical_candles = sorted(historical_candles, key=lambda c: c.timestamp)
        for c in historical_candles:
            self.detector.update(c)
            # Feed to strategy for indicator warm-up (MACD EMA history)
            if hasattr(self.trend_follow, 'warmup'):
                self.trend_follow.warmup(c)

        self._candles_processed = len(historical_candles)
        self._last_candle_time = historical_candles[-1].timestamp.isoformat() if historical_candles else None

        # Soft reset: clear state machine but keep indicator history (MACD EMA values)
        if hasattr(self.trend_follow, 'reset_state_only'):
            self.trend_follow.reset_state_only()
        else:
            self.trend_follow.reset()

        locked = self._load_breakout_locks()
        if locked:
            labels = ", ".join(f"{zid}:{direction}" for zid, direction in sorted(locked))
            self._log_event(f"載入 breakout 鎖: {labels}")

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
                    self._remove_breakout_lock(self._pending_signal)
                else:
                    self._log_event(f"取消掛單 #{self._pending_order_id} 失敗，保留 breakout 鎖", "error")
            except Exception as e:
                self._log_event(f"取消掛單失敗: {e}", "error")
            self._pending_order_id = None
            self._pending_signal = None

        self._log_event("引擎已停止")

    async def cancel_pending_now(self):
        """Cancel pending order from UI. Returns True if cancelled."""
        if not self._pending_order_id:
            self._log_event("無掛單可取消")
            return False
        await self._cancel_pending()
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
            self._profit_locked = False
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

        # The newest bar may still be forming; older missed bars are used only
        # to repair detector/strategy state, never to submit stale orders.
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

        # ── Periodic status log every minute ──
        current_minute = now.minute
        if current_minute != self._last_status_log_minute:
            self._last_status_log_minute = current_minute
            self._log_event(
                f"區間:{self._get_zone_phase()} | "
                f"訂單:{self._get_order_phase()} | "
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

        # ── Max profit lock — block new trades when daily PnL ≥ threshold ──
        if self._max_profit_lock > 0 and self._daily_pnl >= self._max_profit_lock:
            if not self._profit_locked:
                self._profit_locked = True
                self._log_event(
                    f"每日獲利鎖定: ${self._daily_pnl:,.0f} ≥ ${self._max_profit_lock} — "
                    f"暫停新單至 CT 17:00"
                )
            if self._pending_order_id:
                await self._cancel_pending()
            return

        # ── Strategy evaluation ──
        active_zone = self.detector.get_active_zone()
        is_mature = self.detector.is_zone_mature

        # Previous-zone fallback is temporarily disabled; trade only the
        # current mature session zone.
        eval_zone = active_zone
        eval_mature = is_mature

        # Strategy evaluation
        strat = self.trend_follow
        # Safety: if strategy thinks it's confirmed but no order exists, reset
        if strat.raw_state == "confirmed" and not self._pending_order_id:
            self._log_event(
                f"Strategy stuck in 'confirmed' but no pending order → reset"
            )
            strat.reset()

        signal = self.trend_follow.evaluate(candle, eval_zone, eval_mature)

        if signal and not self._pending_order_id:
            if getattr(signal, 'order_type', 'limit') == 'market':
                placed = await self._place_market_entry(signal)
            else:
                placed = await self._place_order(signal)
            if not placed:
                self._unlock_signal_breakout(signal)
                strat.notify_order_cancelled()
            return


    # ── Order Management ──────────────────────────────────

    @staticmethod
    def _round_to_tick(price: float) -> float:
        """Round price to nearest NQ tick (0.25)."""
        return round(round(price / TICK_SIZE) * TICK_SIZE, 2)

    def _entry_brackets_for_signal(self, signal: TradeSignal) -> tuple[Dict[str, int], Dict[str, int]]:
        """Build ProjectX bracket payload using signed offsets from the entry price."""
        sl_ticks = int(round((signal.sl_price - signal.entry_price) / self.tick_size))
        tp_ticks = int(round((signal.tp_price - signal.entry_price) / self.tick_size))
        if sl_ticks == 0:
            sl_ticks = -1 if signal.direction == Direction.BUY else 1
        if tp_ticks == 0:
            tp_ticks = 1 if signal.direction == Direction.BUY else -1
        return (
            {"ticks": sl_ticks, "type": 4},  # Stop Market
            {"ticks": tp_ticks, "type": 1},  # Limit
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
                self._persist_breakout_lock(signal)
                self._log_event(
                    f"掛單成功 #{resp.order_id} | {dir_label} LIMIT @ {signal.entry_price:.2f} | "
                    f"SL={signal.sl_price:.2f} TP={signal.tp_price:.2f} | "
                    f"bracket SL={stop_loss_bracket['ticks']}t TP={take_profit_bracket['ticks']}t | "
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

    async def _place_market_entry(self, signal: TradeSignal) -> bool:
        """Place a market order with attached SL/TP brackets."""
        signal.entry_price = self._round_to_tick(signal.entry_price)
        signal.sl_price = self._round_to_tick(signal.sl_price)
        signal.tp_price = self._round_to_tick(signal.tp_price)

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
                self._persist_breakout_lock(signal)
                self._log_event(
                    f"市價單 #{resp.order_id} | {dir_label} MKT @ ~{signal.entry_price:.2f} | "
                    f"SL={signal.sl_price:.2f} TP={signal.tp_price:.2f} | "
                    f"bracket SL={stop_loss_bracket['ticks']}t TP={take_profit_bracket['ticks']}t"
                )
                return True
            else:
                self._log_event(
                    f"市價單失敗: code={resp.error_code} msg={resp.error_message}",
                    "error"
                )
                return False
        except Exception as e:
            self._log_event(f"市價單異常: {e}", "error")
            return False

    async def _check_trailing_sl_live(self):
        """Live trailing SL: trigger at a configured fraction of TP, once."""
        if not getattr(self.strategy_params, 'trail_enabled', True):
            return
        if self._trail_sl_triggered or not self._active_signal or not self._fill_price:
            return
        sig = self._active_signal
        mkt = self._last_market_price
        if sig.direction == Direction.BUY:
            ticks_moved = (mkt - self._fill_price) / self.tick_size
        else:
            ticks_moved = (self._fill_price - mkt) / self.tick_size

        tp_ticks = abs(int(getattr(self.strategy_params, 'tp_ticks', 0) or 0))
        trigger_pct = getattr(self.strategy_params, 'trail_trigger_pct', 0.30)
        if trigger_pct > 1:
            trigger_pct = trigger_pct / 100.0
        if trigger_pct <= 0:
            return
        trigger_ticks = max(1.0, tp_ticks * trigger_pct)
        if ticks_moved < trigger_ticks:
            return

        self._trail_sl_triggered = True
        trail_ticks = self._resolved_trail_ticks()
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

    async def _cancel_pending(self):
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
            self._log_event(f"取消掛單 #{oid} 3次均失敗! 下個 tick 再試", "error")
            return  # DON'T clear state — retry next tick

        if self._pending_signal:
            self._remove_breakout_lock(self._pending_signal)
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
                    slippage = abs(self._fill_price - entry)
                    slippage_dollars = slippage * self.point_value * self.contract_size
                    if slippage > 5.0:
                        self._log_event(
                            f"[FILL MISMATCH] entry={entry:.2f} fill={self._fill_price:.2f} "
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
                sig = self._pending_signal
                if sig:
                    sig_dir = sig.direction.value
                self._trades.append({
                    "time": datetime.utcnow().isoformat(),
                    "type": "entry",
                    "direction": sig_dir,
                    "price": self._fill_price,
                    "strategy": "trend_follow",
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
                self._entry_time = datetime.utcnow()
                self._force_exit_reason = None
                self._pending_order_id = None
                self._pending_signal = None
                self._pending_age = 0
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
                pnl_info = ""
                if self._fill_price:
                    pnl_info = f" | entry_fill={self._fill_price:.2f}"

                # Exit reason resolution order:
                #   1. Force-exit flag set by flatten_now / emergency close
                #   2. Heuristic: closer of SL/TP to last market price wins,
                #      with SL-side hits reclassified as trail_sl when the
                #      trail trigger fired earlier in this position.
                exit_reason = "unknown"
                forced = self._force_exit_reason
                if forced:
                    if forced == "flatten" and self._trail_sl_triggered:
                        exit_reason = "trail_sl"
                    else:
                        exit_reason = forced
                elif self._active_signal and self._last_market_price:
                    sl_p = self._active_signal.sl_price
                    tp_p = self._active_signal.tp_price
                    mkt = self._last_market_price
                    if abs(mkt - sl_p) < abs(mkt - tp_p):
                        exit_reason = "trail_sl" if self._trail_sl_triggered else "sl"
                    else:
                        exit_reason = "tp"
                if exit_reason == "unknown":
                    exit_reason = "trail_sl" if self._trail_sl_triggered else "tp"

                entry_fill = self._fill_price  # save before clearing
                exit_time_dt = datetime.utcnow()
                # Snapshot for persistence before we clear active_signal.
                _sig_for_log = self._active_signal
                _entry_t = self._entry_time

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

                await self._sweep_contract_open_orders("close")

                self._trades.append({
                    "time": exit_time_dt.isoformat(),
                    "type": "closed",
                    "entry_price": entry_fill,
                    "exit_reason": exit_reason,
                })

                # Persist exit reason so /live/trade-history can bucket the
                # matching TopstepX fill into TP / SL / TRAIL_SL correctly.
                self._persist_exit_record(
                    exit_reason=exit_reason,
                    entry_time=_entry_t,
                    exit_time=exit_time_dt,
                    entry_price=entry_fill,
                    sl_price=_sig_for_log.sl_price if _sig_for_log else None,
                    tp_price=_sig_for_log.tp_price if _sig_for_log else None,
                    direction=_sig_for_log.direction.value if _sig_for_log else None,
                    trail_triggered=self._trail_sl_triggered,
                    zone_id=_sig_for_log.zone_id if _sig_for_log else None,
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

    def _ingest_catchup_candle(self, candle: Candle):
        """Replay a missed candle into local state without placing stale orders."""
        self._last_market_price = candle.close
        self._last_candle_time = candle.timestamp.isoformat()
        self._candles_processed += 1
        if self._candles_processed % 5 == 0:
            self._save_zones()
        self.detector.update(candle)
        if hasattr(self.trend_follow, "warmup"):
            self.trend_follow.warmup(candle)

    async def _fetch_latest_candles(self, unit_number: int = 30) -> List[Candle]:
        """Fetch the newest available 1-minute candle from TopstepX API.

        NOTE: TopstepX 30s bar API has a ~6-hour settle delay — bars from
        sub-minute endpoints are never current. 1m bars are real-time.
        MACD/VWAP indicators run on 1m bars in live mode.

        The newest 1m bar can still be forming; live trading intentionally uses
        that timely bar. All fetched bars are still merged into the shared data
        store by timestamp, so early forming snapshots get replaced by final bars.

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
                self._last_market_price = candles[-1].close
                try:
                    from backend.api.routes import _upsert_historical_candles
                    _upsert_historical_candles(candles)
                except Exception:
                    pass
                return candles
        except Exception as e:
            self._log_event(f"取得K線失敗: {e}", "error")
        return []
