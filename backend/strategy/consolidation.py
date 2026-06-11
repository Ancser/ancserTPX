# ============================================================
# 文件: backend/strategy/consolidation.py
# 狀態: 已完成
# 問題: 無
# 關聯文件:
#   ← backend/strategy/reversion.py     (查詢 active zone)
#   ← backend/strategy/trend_follow.py  (查詢 left zone + breakout)
#   ← backend/backtest/engine.py        (回測主循環調用)
#   ← backend/api/websocket.py          (即時推送 zone 狀態)
#   → backend/strategy/volume_profile.py (計算 VP)
#   → backend/db/models.py              (數據模型)
# 函數結構:
#   - ConsolidationDetector.__init__(config)
#   - ConsolidationDetector.update(candle) -> Optional[ZoneEvent]
#   - ConsolidationDetector._try_detect_new_zone() -> Optional[CZ]
#   - ConsolidationDetector._check_zone_stability(candle) -> bool
#   - ConsolidationDetector._detect_zone_exit(candle) -> Optional[str]
#   - ConsolidationDetector._analyze_breakout(candle, direction) -> BreakoutAnalysis
#   - ConsolidationDetector.get_active_zone() -> Optional[CZ]
#   - ConsolidationDetector.get_all_zones() -> List[CZ]
# ============================================================
"""
盤整區間自動偵測引擎

這是整個系統最核心的模組 —— 自動化你手動畫 Fixed Range VP 的過程。

你手動做的流程：
  1. 看到價格在一個區間內來回 → 手動畫框
  2. 框出 VP → 得到 POC / VAH / VAL
  3. 等價格離開 → 刪除舊框
  4. 等新的盤整形成 → 畫新框

代碼自動化的做法：
  1. 持續收集 K 線，用滾動窗口計算 VP
  2. 當 VP 穩定（POC 不漂移 + 價格在 VA 內來回）→ 確認盤整
  3. 當收盤連續突破 100% 邊界 → 標記 "left"
  4. 重置窗口，等待新的盤整形成

區分「新區間 vs 舊區間」的關鍵：
  - zone_id 不同
  - formed_at 時間不同
  - 舊 zone.status = "left", 新 zone.status = "active"
  - 舊 zone 有 left_at 和 exit_direction
"""

from __future__ import annotations
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from backend.db.models import (
    Candle, ConsolidationZone, ZoneStatus,
    BreakoutAnalysis, VolumeProfileResult
)
from backend.strategy.volume_profile import VolumeProfileCalculator


# ── Zone 事件（通知上層模組）────────────────────────────

@dataclass
class ZoneEvent:
    """盤整區間狀態變更事件"""
    event_type: str          # "forming" | "active" | "left" | "updated"
    zone: ConsolidationZone
    breakout: Optional[BreakoutAnalysis] = None  # 離開時附帶突破分析
    message: str = ""


class ConsolidationDetector:
    """
    盤整區間偵測狀態機

    狀態流轉:
      [idle] ──K 線累積──→ [forming]
      [forming] ──穩定──→ [active]
      [forming] ──不穩定──→ [idle]（重置）
      [active] ──價格離開──→ [left] → [idle]（等新盤整）
    """

    def __init__(
        self,
        min_candles: int = 6,
        max_candles: int = 50,
        poc_drift_threshold: float = 3.0,
        min_touches: int = 2,
        exit_confirm_candles: int = 2,
        value_area_pct: float = 0.80,
        tick_size: float = 0.25,
    ):
        """
        Args:
            min_candles:          最少 K 線數才開始判定盤整（6 根 1min = 6 分鐘）
            max_candles:          盤整區間最大 K 線數（超過重新計算）
            poc_drift_threshold:  POC 允許漂移量（點數）, 超過 = 不穩定
            min_touches:          價格觸及 VAH 或 VAL 的最少次數
            exit_confirm_candles: 需要幾根 K 線收盤在外部才確認離開
            value_area_pct:       Value Area 百分比
            tick_size:            NQ tick size = 0.25
        """
        self.min_candles = min_candles
        self.max_candles = max_candles
        self.poc_drift_threshold = poc_drift_threshold
        self.min_touches = min_touches
        self.exit_confirm_candles = exit_confirm_candles
        self.value_area_pct = value_area_pct

        self.vp_calc = VolumeProfileCalculator(tick_size, value_area_pct)

        # 內部狀態
        self._candle_buffer: List[Candle] = []   # 當前窗口 K 線
        self._active_zone: Optional[ConsolidationZone] = None
        self._all_zones: List[ConsolidationZone] = []
        self._prev_poc: Optional[float] = None   # 上一次計算的 POC
        self._exit_count: int = 0                 # 連續在外部的 K 線計數
        self._exit_direction: Optional[str] = None
        self._zone_counter: int = 0

    def update(self, candle: Candle) -> Optional[ZoneEvent]:
        """
        每根新 K 線調用一次 —— 主入口

        Returns:
            ZoneEvent 如果有狀態變更, 否則 None
        """
        self._candle_buffer.append(candle)

        # === 情況 1: 有 active zone → 檢查是否離開 ===
        if self._active_zone and self._active_zone.status == ZoneStatus.ACTIVE:
            return self._handle_active_zone(candle)

        # === 情況 2: 無 active zone → 嘗試偵測新盤整 ===
        return self._handle_no_zone(candle)

    def _handle_active_zone(self, candle: Candle) -> Optional[ZoneEvent]:
        """已有 active zone，監控是否離開

        CRITICAL: Exit detection uses ORIGINAL zone boundaries (high_100 / low_100)
        that were set when the zone was first detected. These are NEVER updated.
        Only POC / VAH / VAL are updated for display (developing profile).
        This prevents the "drifting zone" bug where boundaries shift with price
        and the zone never exits.
        """

        zone = self._active_zone

        # 更新 zone 的 K 線數
        zone.num_candles += 1
        zone.candles.append(candle)

        # Safety: force expire zone after 120 bars (2 hours for 1min candles)
        # Prevents zombie zones that accumulate forever
        MAX_ZONE_LIFETIME = 120
        if zone.num_candles > MAX_ZONE_LIFETIME:
            zone.status = ZoneStatus.LEFT
            zone.left_at = candle.timestamp
            zone.exit_direction = "expired"
            self._active_zone = None
            self._candle_buffer = []
            self._exit_count = 0
            self._exit_direction = None
            self._prev_poc = None
            return ZoneEvent(
                "left", zone,
                message=f"Zone {zone.zone_id} 超時強制過期 ({zone.num_candles} bars)"
            )

        # 檢查是否離開 100% 範圍 (uses ORIGINAL high_100/low_100)
        exit_dir = self._detect_zone_exit(candle)

        if exit_dir:
            self._exit_count += 1
            self._exit_direction = exit_dir

            # 需要連續 N 根確認
            if self._exit_count >= self.exit_confirm_candles:
                return self._confirm_zone_exit(candle)
        else:
            # 回到區間內 → 重置計數
            self._exit_count = 0
            self._exit_direction = None

        # Update DISPLAY values every 5 candles (developing profile) — NOT exit boundaries
        if zone.num_candles >= self.min_candles and zone.num_candles % 5 == 0:
            candles_for_vp = zone.candles[-self.max_candles:] if len(zone.candles) > self.max_candles else zone.candles
            try:
                vp = self.vp_calc.calculate(candles_for_vp)
                zone.poc = vp.poc
                zone.vah_80 = vp.vah
                zone.val_80 = vp.val
                # DO NOT update high_100 / low_100 — exit boundaries stay at original
                zone.total_volume = vp.total_volume
            except ValueError:
                pass
            return ZoneEvent("updated", zone, message="zone VP 更新 (邊界不變)")

        return None

    def _handle_no_zone(self, candle: Candle) -> Optional[ZoneEvent]:
        """沒有 active zone，嘗試偵測新盤整"""

        # 不夠 K 線 → 繼續收集
        if len(self._candle_buffer) < self.min_candles:
            return None

        # 限制 buffer 大小
        if len(self._candle_buffer) > self.max_candles * 2:
            self._candle_buffer = self._candle_buffer[-self.max_candles:]

        # 嘗試偵測盤整
        zone = self._try_detect_new_zone()

        if zone:
            # 檢查穩定性
            if self._check_zone_stability(zone):
                zone.status = ZoneStatus.ACTIVE
                self._active_zone = zone
                self._all_zones.append(zone)
                self._candle_buffer = []  # 重置 buffer
                self._exit_count = 0
                return ZoneEvent("active", zone,
                                 message=f"新盤整區間確認 POC={zone.poc:.2f}")
            else:
                zone.status = ZoneStatus.FORMING
                return ZoneEvent("forming", zone,
                                 message="盤整區間形成中...")

        return None

    def _try_detect_new_zone(self) -> Optional[ConsolidationZone]:
        """
        嘗試從 buffer 中偵測新盤整區間

        條件：
        1. 計算 VP
        2. 價格大部分在 VA 內
        3. POC 穩定（與上次計算比較偏移量）
        """
        candles = self._candle_buffer[-self.max_candles:]

        try:
            vp = self.vp_calc.calculate(candles)
        except ValueError:
            return None

        # 條件 1: 至少 70% 的 K 線收盤在 100% range 內
        closes_in_range = sum(
            1 for c in candles
            if vp.low_100 <= c.close <= vp.high_100
        )
        if closes_in_range / len(candles) < 0.70:
            self._prev_poc = vp.poc
            return None

        # 條件 2: 至少 60% 的 K 線收盤在 80% VA 內
        closes_in_va = sum(
            1 for c in candles
            if vp.val <= c.close <= vp.vah
        )
        if closes_in_va / len(candles) < 0.50:
            self._prev_poc = vp.poc
            return None

        # 條件 3: POC 穩定性（與上次比較偏移量）
        if self._prev_poc is not None:
            drift = abs(vp.poc - self._prev_poc)
            if drift > self.poc_drift_threshold:
                self._prev_poc = vp.poc
                return None

        self._prev_poc = vp.poc

        # 條件 4: 計算 VAH/VAL 觸及次數
        touches = self._count_boundary_touches(candles, vp.vah, vp.val)
        if touches < self.min_touches:
            return None

        # 通過所有條件 → 建立新盤整區間
        self._zone_counter += 1
        zone_id = f"Z{self._zone_counter:04d}"

        first_ts = candles[0].timestamp
        duration = int((candles[-1].timestamp - first_ts).total_seconds() / 60)

        return ConsolidationZone(
            zone_id=zone_id,
            formed_at=first_ts,
            left_at=None,
            poc=vp.poc,
            vah_80=vp.vah,
            val_80=vp.val,
            high_100=vp.high_100,
            low_100=vp.low_100,
            total_volume=vp.total_volume,
            duration_minutes=duration,
            num_candles=len(candles),
            status=ZoneStatus.FORMING,
            exit_direction=None,
            candles=list(candles),
        )

    def _check_zone_stability(self, zone: ConsolidationZone) -> bool:
        """
        穩定性判定

        條件：
        - K 線數 ≥ min_candles
        - 最近 3 根 K 線沒有連續突破 100% 邊界
        - 80% range 不要太小（至少 2 點，避免假盤整）
        """
        if zone.num_candles < self.min_candles:
            return False

        # 80% range 至少要有意義（NQ 2 點 = $40）
        if zone.range_80 < 2.0:
            return False

        # 最近 K 線是否在範圍內
        recent = zone.candles[-3:] if len(zone.candles) >= 3 else zone.candles
        outside_count = sum(
            1 for c in recent
            if c.close > zone.high_100 or c.close < zone.low_100
        )
        if outside_count >= 2:
            return False

        return True

    def _detect_zone_exit(self, candle: Candle) -> Optional[str]:
        """
        檢測單根 K 線是否在 active zone 外部

        Returns:
            "up"   → 收盤 > 100% high
            "down" → 收盤 < 100% low
            None   → 在範圍內
        """
        zone = self._active_zone
        if not zone:
            return None

        if candle.close > zone.high_100:
            return "up"
        elif candle.close < zone.low_100:
            return "down"
        return None

    def _confirm_zone_exit(self, candle: Candle) -> ZoneEvent:
        """確認離開盤整區間，進行突破分析"""
        zone = self._active_zone

        zone.status = ZoneStatus.LEFT
        zone.left_at = candle.timestamp
        zone.exit_direction = self._exit_direction

        # 分析突破成交量
        breakout = self._analyze_breakout(candle, self._exit_direction)

        # 重置狀態
        self._active_zone = None
        self._candle_buffer = []
        self._exit_count = 0
        self._exit_direction = None
        self._prev_poc = None

        return ZoneEvent(
            "left", zone, breakout=breakout,
            message=f"離開盤整區間 → {zone.exit_direction} | "
                    f"vol_ratio={breakout.vol_ratio:.2f}"
        )

    def _analyze_breakout(
        self, candle: Candle, direction: str
    ) -> BreakoutAnalysis:
        """
        突破成交量分析

        你的規則:
          - 在 80% 外監控 volume
          - 如果 outside volume > 離開 80% 前最後 2 根 K 線的 volume
          - → 趨勢跟隨信號

        vol_ratio ≥ 2.0 → 大趨勢模式 ($600:$1800)
        """
        zone = self._all_zones[-1] if self._all_zones else None

        # 取離開前最後 2 根 K 線（在 VA 內的最後 2 根）
        if zone and len(zone.candles) >= 3:
            # 倒數第 3, 4 根（倒數 1,2 是已經在外部的確認 K 線）
            inside_candles = [
                c for c in zone.candles[:-self.exit_confirm_candles]
                if zone.val_80 <= c.close <= zone.vah_80
            ]
            if len(inside_candles) >= 2:
                prev_2 = inside_candles[-2:]
                vol_before_avg = sum(c.volume for c in prev_2) / 2.0
            elif inside_candles:
                vol_before_avg = float(inside_candles[-1].volume)
            else:
                vol_before_avg = float(candle.volume)  # fallback
        else:
            vol_before_avg = float(candle.volume)

        vol_outside = float(candle.volume)
        vol_ratio = vol_outside / vol_before_avg if vol_before_avg > 0 else 0

        breakout_id = f"B{uuid.uuid4().hex[:8]}"

        return BreakoutAnalysis(
            breakout_id=breakout_id,
            from_zone_id=zone.zone_id if zone else "unknown",
            direction=direction,
            breakout_time=candle.timestamp,
            vol_before_avg=vol_before_avg,
            vol_outside=vol_outside,
            vol_ratio=vol_ratio,
            is_trend_signal=vol_ratio > 1.0,
        )

    def _count_boundary_touches(
        self,
        candles: List[Candle],
        vah: float,
        val: float,
        tolerance: float = 2.0
    ) -> int:
        """
        計算價格觸及 VAH/VAL 的次數

        tolerance: 容差點數（NQ 2 點 = $40）
        """
        touches = 0
        for c in candles:
            if abs(c.high - vah) <= tolerance or abs(c.close - vah) <= tolerance:
                touches += 1
            elif abs(c.low - val) <= tolerance or abs(c.close - val) <= tolerance:
                touches += 1
        return touches

    # ── 公開查詢方法 ──────────────────────────────────

    def get_active_zone(self) -> Optional[ConsolidationZone]:
        """返回當前 active 盤整區間"""
        return self._active_zone

    def get_all_zones(self) -> List[ConsolidationZone]:
        """返回所有歷史盤整區間"""
        return list(self._all_zones)

    def get_last_left_zone(self) -> Optional[ConsolidationZone]:
        """返回最近一個 left 狀態的區間（用於趨勢跟隨策略）"""
        for zone in reversed(self._all_zones):
            if zone.status == ZoneStatus.LEFT:
                return zone
        return None

    def reset(self):
        """重置所有狀態"""
        self._candle_buffer = []
        self._active_zone = None
        self._all_zones = []
        self._prev_poc = None
        self._exit_count = 0
        self._exit_direction = None
        self._zone_counter = 0


# ══════════════════════════════════════════════════════════
# Session-based Zone Detector (for live overnight trading)
# ══════════════════════════════════════════════════════════

import logging
_logger = logging.getLogger(__name__)

class SessionZoneDetector:
    """
    Penta-session zone detector — 亞盤 (ASIA) + 歐盤 (EURO) + 盤前 (PRE) + 早盤 (RTH) + 盤後 (AH).

    五段 Session (每天 5 個區間):
      - 亞盤 (ASIA): 22:00 UTC (18:00 ET) → 07:00 UTC (03:00 ET)
      - 歐盤 (EURO): 07:00 UTC (03:00 ET) → 11:00 UTC (07:00 ET / 04:00 PT)
      - 盤前 (PRE):  11:00 UTC (07:00 ET / 04:00 PT) → 13:30 UTC (09:30 ET / 06:30 PT)
      - 早盤 (RTH):  13:30 UTC (09:30 ET) → 20:00 UTC (16:00 ET)
      - 盤後 (AH):   20:00 UTC (16:00 ET) → 22:00 UTC (18:00 ET)

    算法:
      1. 每段 session 開始收集 K 線
      2. 等待 60 分鐘發展 → 成熟區間
      3. 成熟後由策略負責突破判斷

    Zone 每 5 根 K 線重新計算 VP (POC / VAH / VAL)
    high_100 / low_100 會持續更新 (反映整個 session 範圍)
    """

    # Session boundaries (all UTC)
    ASIA_START_HOUR = 22            # 18:00 ET = 22:00 UTC — 亞盤開始
    EURO_START_HOUR = 7              # 03:00 ET = 07:00 UTC — 歐盤開始
    PRE_START_HOUR = 11             # 07:00 ET / 04:00 PT = 11:00 UTC — NY 盤前開始
    RTH_START_HOUR = 13             # 09:30 ET = 13:30 UTC — 早盤開始
    RTH_START_MINUTE = 30
    AH_START_HOUR = 20              # 16:00 ET = 20:00 UTC — 盤後開始

    # Maturity: all sessions require 60 minutes development
    VP_RECALC_INTERVAL = 5         # 每 5 根重算 VP


    def __init__(
        self,
        value_area_pct: float = 0.80,
        tick_size: float = 0.25,
    ):
        self.vp_calc = VolumeProfileCalculator(tick_size, value_area_pct)
        self.tick_size = tick_size

        self._active_zone: Optional[ConsolidationZone] = None
        self._all_zones: List[ConsolidationZone] = []
        self._zone_counter: int = 0
        self._zone_mature: bool = False
        self._vah_history: List[float] = [] # VAH value per candle
        self._val_history: List[float] = [] # VAL value per candle
        self._session_date: Optional[str] = None  # "YYYY-MM-DD" of session start day
        self._candle_count_since_recalc: int = 0

    # ── 公開方法 ──

    def update(self, candle: Candle) -> Optional[ZoneEvent]:
        """每根 1m K 線調用一次."""
        # Detect session boundary
        session_id = self._get_session_id(candle)

        # Debug: log first candle and every transition
        if self._session_date is None:
            _logger.info(
                f"[SessionZone] FIRST candle: ts={candle.timestamp} "
                f"h={candle.timestamp.hour} m={candle.timestamp.minute} "
                f"tzinfo={candle.timestamp.tzinfo} → session_id={session_id}"
            )

        if session_id != self._session_date:
            if self._session_date is not None:
                _logger.info(
                    f"[SessionZone] SESSION CHANGE: {self._session_date} → {session_id} "
                    f"@ ts={candle.timestamp} (h={candle.timestamp.hour}:{candle.timestamp.minute})"
                )
            # New session → close old zone, start fresh
            event = self._end_session(candle)
            self._session_date = session_id
            self._zone_mature = False
            self._vah_history = []
            self._val_history = []
            self._candle_count_since_recalc = 0
            if event:
                _logger.info(f"[SessionZone] 新 Session {session_id} | 上個 zone 已關閉")
            # Start collecting from this candle
            self._create_zone(candle)
            return event

        # No active zone yet (shouldn't happen after create, but safety)
        if not self._active_zone:
            self._create_zone(candle)
            return None

        # ── Add candle to zone ──
        zone = self._active_zone
        zone.candles.append(candle)
        zone.num_candles += 1
        zone.duration_minutes = int(
            (candle.timestamp - zone.formed_at).total_seconds() / 60
        )

        # Update 100% boundaries (session range grows)
        if candle.high > zone.high_100:
            zone.high_100 = candle.high
        if candle.low < zone.low_100:
            zone.low_100 = candle.low

        # ── Recalculate VP periodically ──
        self._candle_count_since_recalc += 1
        if self._candle_count_since_recalc >= self.VP_RECALC_INTERVAL:
            self._candle_count_since_recalc = 0
            self._recalculate_vp()

        # ── Track VAH and VAL drift for maturity ──
        self._vah_history.append(zone.vah_80)
        self._val_history.append(zone.val_80)
        if len(self._vah_history) > 300:
            self._vah_history = self._vah_history[-300:]
            self._val_history = self._val_history[-300:]

        # ── Check maturity ──
        if not self._zone_mature:
            was_mature = False
            self._check_maturity(candle)
            if self._zone_mature and not was_mature:
                zone.status = ZoneStatus.ACTIVE
                zone.mature = True  # permanent flag
                _logger.info(
                    f"[SessionZone] 區間成熟! {zone.zone_id} | "
                    f"POC={zone.poc:.2f} VAH={zone.vah_80:.2f} VAL={zone.val_80:.2f} | "
                    f"bars={zone.num_candles}"
                )
                return ZoneEvent("active", zone, message="區間成熟 — 等待突破")

        return None

    @property
    def is_zone_mature(self) -> bool:
        return self._zone_mature

    def get_active_zone(self) -> Optional[ConsolidationZone]:
        return self._active_zone

    def get_all_zones(self) -> List[ConsolidationZone]:
        return list(self._all_zones)

    def close_final_zone(self, last_candle: Candle):
        """Close any remaining active zone at end of backtest."""
        if self._active_zone:
            zone = self._active_zone
            zone.status = ZoneStatus.LEFT
            zone.left_at = last_candle.timestamp
            zone.exit_direction = "backtest_end"
            self._active_zone = None

    def get_last_left_zone(self) -> Optional[ConsolidationZone]:
        for z in reversed(self._all_zones):
            if z.status == ZoneStatus.LEFT:
                return z
        return None

    def reset(self):
        self._active_zone = None
        self._all_zones = []
        self._zone_counter = 0
        self._zone_mature = False
        self._vah_history = []
        self._val_history = []
        self._session_date = None
        self._candle_count_since_recalc = 0

    # ── 內部方法 ──

    def _get_session_id(self, candle: Candle) -> str:
        """
        Penta-session ID:
          - 22:00 UTC → 06:59 UTC  = "YYYY-MM-DD-ASIA" (亞盤)
          - 07:00 UTC → 10:59 UTC  = "YYYY-MM-DD-EURO" (歐盤)
          - 11:00 UTC → 13:29 UTC  = "YYYY-MM-DD-PRE"  (NY 盤前)
          - 13:30 UTC → 19:59 UTC  = "YYYY-MM-DD-RTH"  (早盤)
          - 20:00 UTC → 21:59 UTC  = "YYYY-MM-DD-AH"   (盤後)

        ASIA date = date when 22:00 UTC occurs
        """
        ts = candle.timestamp
        h, m = ts.hour, ts.minute

        # 22:00+ UTC → ASIA of today's date
        if h >= self.ASIA_START_HOUR:
            return ts.strftime("%Y-%m-%d") + "-ASIA"

        # 20:00 ~ 21:59 UTC → AH of today
        if h >= self.AH_START_HOUR:
            return ts.strftime("%Y-%m-%d") + "-AH"

        # 13:30 ~ 19:59 UTC → RTH of today
        if h > self.RTH_START_HOUR or (
            h == self.RTH_START_HOUR and m >= self.RTH_START_MINUTE
        ):
            return ts.strftime("%Y-%m-%d") + "-RTH"

        # 11:00 ~ 13:29 UTC → NY PRE of today
        if h >= self.PRE_START_HOUR:
            return ts.strftime("%Y-%m-%d") + "-PRE"

        # 07:00 ~ 10:59 UTC → EURO of today
        if h >= self.EURO_START_HOUR:
            return ts.strftime("%Y-%m-%d") + "-EURO"

        # 00:00 ~ 06:59 UTC → still ASIA of PREVIOUS date
        prev = ts - timedelta(days=1)
        return prev.strftime("%Y-%m-%d") + "-ASIA"

    def _end_session(self, candle: Candle) -> Optional[ZoneEvent]:
        """Close current session zone."""
        if not self._active_zone:
            return None
        zone = self._active_zone
        zone.status = ZoneStatus.LEFT
        zone.left_at = candle.timestamp
        zone.exit_direction = "session_end"
        self._active_zone = None
        return ZoneEvent("left", zone, message=f"Session 結束 — zone {zone.zone_id} 關閉")

    def _create_zone(self, candle: Candle):
        """Create a new session zone starting from this candle."""
        self._zone_counter += 1
        zone_id = f"S{self._zone_counter:04d}"
        self._active_zone = ConsolidationZone(
            zone_id=zone_id,
            formed_at=candle.timestamp,
            left_at=None,
            poc=candle.close,
            vah_80=candle.high,
            val_80=candle.low,
            high_100=candle.high,
            low_100=candle.low,
            total_volume=candle.volume,
            duration_minutes=0,
            num_candles=1,
            status=ZoneStatus.FORMING,
            exit_direction=None,
            candles=[candle],
        )
        self._all_zones.append(self._active_zone)
        _logger.info(f"[SessionZone] 建立 session zone {zone_id} @ {candle.timestamp}")

    def _recalculate_vp(self):
        """Recalculate VP from all session candles."""
        zone = self._active_zone
        if not zone or len(zone.candles) < 3:
            return
        try:
            vp = self.vp_calc.calculate(zone.candles)
            zone.poc = vp.poc
            zone.vah_80 = vp.vah
            zone.val_80 = vp.val
            zone.total_volume = vp.total_volume
            last_candle = zone.candles[-1]
            zone.va_curve.append({
                "ts": last_candle.timestamp.isoformat(),
                "vah": vp.vah,
                "val": vp.val,
            })
        except ValueError:
            pass

    def _check_maturity(self, candle: Candle):
        """成熟條件: Zone 發展 ≥ 60 分鐘即可"""
        zone = self._active_zone
        if not zone:
            return
        age_minutes = (candle.timestamp - zone.formed_at).total_seconds() / 60
        if age_minutes >= 60:
            self._zone_mature = True
