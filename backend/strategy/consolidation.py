# ============================================================
# 文件: backend/strategy/consolidation.py
# 狀態: 已完成
# 關聯文件:
#   ← backend/strategy/trend_follow.py  (查詢 left zone + breakout)
#   ← backend/backtest/engine.py        (回測主循環調用)
#   ← backend/api/websocket.py          (即時推送 zone 狀態)
#   → backend/strategy/volume_profile.py (計算 VP)
#   → backend/db/models.py              (數據模型)
# 現行偵測器 (由 build_zone_detector 工廠選用):
#   - SessionZoneDetector      (五段 session 區間, live overnight)
#   - ClockBucketZoneDetector  (固定時鐘分桶區間, 依 TF)
#   - OverlapZoneDetector      (重疊/最小 TF 區間)
#   共用事件型別 ZoneEvent。
# 1.0.8: 已移除死類 ConsolidationDetector(~397 行, 零呼叫者)。
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
import copy
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from backend.db.models import (
    Candle, ConsolidationZone, ZoneStatus,
    BreakoutAnalysis
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
            zone.profile = dict(vp.profile)  # v1.0.6: histogram for lowest-volume-node SL
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


# ══════════════════════════════════════════════════════════
# Fixed clock-bucket Zone Detector (timeframe-selectable areas)
# ══════════════════════════════════════════════════════════

# area timeframe → bucket length in minutes
AREA_TIMEFRAME_MINUTES = {
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
}


def timeframes_for_base(base_minutes: int) -> tuple:
    """Area timeframes strictly LARGER than the base candle (a TF == base would
    be a 1-candle bucket → degenerate volume profile, so it is dropped).

    Single source of truth shared by live / backtest / training so every path
    uses an identical TF set (reproducibility)."""
    return tuple(tf for tf, m in AREA_TIMEFRAME_MINUTES.items() if m > base_minutes)


class ClockBucketZoneDetector:
    """Groups 1m candles into fixed clock buckets of the selected area timeframe
    (5m/15m/30m/1h/4h). Each completed bucket becomes a finalized zone carrying
    POC / VAH / VAL plus the full volume-profile histogram. The in-progress bucket
    is the "active" zone. Strategies reference the most recent N completed zones.

    Buckets are aligned to clock boundaries in the candle timezone:
      - sub-hour (5/15/30m): aligned within the hour by minute
      - 1h: top of the hour
      - 4h: aligned to 0/4/8/12/16/20
    """

    def __init__(
        self,
        area_timeframe: str = "5m",
        value_area_pct: float = 0.80,
        tick_size: float = 0.25,
        max_recent: int = 10,
        recalc_active_each_bar: bool = True,
    ):
        self.area_timeframe = area_timeframe if area_timeframe in AREA_TIMEFRAME_MINUTES else "5m"
        self.bucket_minutes = AREA_TIMEFRAME_MINUTES[self.area_timeframe]
        self.value_area_pct = value_area_pct
        self.tick_size = tick_size
        self.max_recent = max_recent
        # When False, the in-progress (active) zone's VP is NOT recomputed every
        # bar — only on bucket finalisation. Consumers that read only COMPLETED
        # reference zones (e.g. the confluence backtester) set this False for a
        # large speedup; live display keeps it True for an evolving active VP.
        self.recalc_active_each_bar = recalc_active_each_bar
        self.vp_calc = VolumeProfileCalculator(tick_size, value_area_pct)

        self._active_zone: Optional[ConsolidationZone] = None
        self._completed_zones: List[ConsolidationZone] = []
        self._zone_counter: int = 0
        self._bucket_key: Optional[str] = None

    # ── bucket alignment ──

    def _bucket_start(self, ts: datetime) -> datetime:
        # Hour-multiple buckets (1h/2h/4h): align the hour to a multiple of the
        # bucket size (e.g. 2h → 0/2/4/…; 4h → 0/4/8/…). Sub-hour buckets
        # (5/10/15/30m) align the minute within the hour.
        if self.bucket_minutes >= 60:
            hours = self.bucket_minutes // 60
            aligned_hour = (ts.hour // hours) * hours
            return ts.replace(hour=aligned_hour, minute=0, second=0, microsecond=0)
        aligned_min = (ts.minute // self.bucket_minutes) * self.bucket_minutes
        return ts.replace(minute=aligned_min, second=0, microsecond=0)

    def _bucket_id(self, ts: datetime) -> str:
        return self._bucket_start(ts).strftime("%Y-%m-%dT%H:%M")

    # ── public API ──

    def update(self, candle: Candle) -> Optional[ZoneEvent]:
        bucket = self._bucket_id(candle.timestamp)
        event = None
        if bucket != self._bucket_key:
            # finalize previous bucket → becomes a completed reference zone
            event = self._finalize_active()
            self._bucket_key = bucket
            self._create_zone(candle)
            return event

        if not self._active_zone:
            self._bucket_key = bucket
            self._create_zone(candle)
            return None

        zone = self._active_zone
        zone.candles.append(candle)
        zone.num_candles += 1
        zone.duration_minutes = int((candle.timestamp - zone.formed_at).total_seconds() / 60)
        if candle.high > zone.high_100:
            zone.high_100 = candle.high
        if candle.low < zone.low_100:
            zone.low_100 = candle.low
        if self.recalc_active_each_bar:
            self._recalculate_vp(zone)
        return None

    @property
    def is_zone_mature(self) -> bool:
        # A reference zone is available once at least one bucket has completed.
        return len(self._completed_zones) > 0

    def get_active_zone(self) -> Optional[ConsolidationZone]:
        """Most recent COMPLETED bucket zone (the primary reference)."""
        return self._completed_zones[-1] if self._completed_zones else None

    def get_forming_zone(self) -> Optional[ConsolidationZone]:
        """The in-progress bucket zone (not yet a reference)."""
        return self._active_zone

    @property
    def completed_zone_count(self) -> int:
        return len(self._completed_zones)

    def get_recent_zones(self, n: Optional[int] = None) -> List[ConsolidationZone]:
        """Last n completed zones (most recent last)."""
        n = n if n is not None else self.max_recent
        return self._completed_zones[-n:]

    def get_completed_zones(self) -> List[ConsolidationZone]:
        return list(self._completed_zones)

    def get_all_zones(self) -> List[ConsolidationZone]:
        zones = list(self._completed_zones)
        if self._active_zone:
            zones.append(self._active_zone)
        return zones

    def get_last_left_zone(self) -> Optional[ConsolidationZone]:
        return self._completed_zones[-1] if self._completed_zones else None

    def close_final_zone(self, last_candle: Candle):
        self._finalize_active()

    def reset(self):
        self._active_zone = None
        self._completed_zones = []
        self._zone_counter = 0
        self._bucket_key = None

    # ── internal ──

    def _create_zone(self, candle: Candle):
        self._zone_counter += 1
        zone_id = f"B{self._zone_counter:04d}"
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
            timeframe=self.area_timeframe,
        )
        self._recalculate_vp(self._active_zone)

    def _finalize_active(self) -> Optional[ZoneEvent]:
        zone = self._active_zone
        if not zone:
            return None
        self._recalculate_vp(zone)
        zone.status = ZoneStatus.LEFT
        zone.left_at = zone.candles[-1].timestamp if zone.candles else zone.formed_at
        zone.mature = True
        self._completed_zones.append(zone)
        # keep memory bounded — only need the most recent buckets as references
        if len(self._completed_zones) > max(self.max_recent * 4, 50):
            self._completed_zones = self._completed_zones[-(self.max_recent * 4):]
        self._active_zone = None
        return ZoneEvent("left", zone, message=f"Bucket {zone.zone_id} 完成 (TF={self.area_timeframe})")

    def _recalculate_vp(self, zone: ConsolidationZone):
        if not zone or len(zone.candles) < 1:
            return
        try:
            vp = self.vp_calc.calculate(zone.candles)
            zone.poc = vp.poc
            zone.vah_80 = vp.vah
            zone.val_80 = vp.val
            zone.high_100 = vp.high_100
            zone.low_100 = vp.low_100
            zone.total_volume = vp.total_volume
            zone.profile = dict(vp.profile)
            zone.va_bands = dict(vp.va_bands)
        except ValueError:
            pass


# ══════════════════════════════════════════════════════════
# Multi-timeframe OVERLAP Zone Detector (live mirror of backtest)
# ══════════════════════════════════════════════════════════

def _normalize_overlap_trade_tf(value: str) -> str:
    return "smallest" if str(value or "").strip().lower() == "smallest" else "merged"


def _synthesize_overlap_zone(
    actives: List[ConsolidationZone],
    tfs,
    overlap_trade_tf: str = "merged",
) -> ConsolidationZone:
    """Average the overlapping reference zones into one synthetic zone.

    Mirrors backend.api.routes._synthesize_merged_zone EXACTLY so live overlap
    behaves identically to the backtest/ML overlap sweep. Entry levels
    (VAH/VAL/POC) are the mean across timeframes; the VP histogram is summed so
    the lowest-volume-node SL still works.
    """
    ids = [str(z.zone_id) for z in actives]
    if _normalize_overlap_trade_tf(overlap_trade_tf) == "smallest":
        zone = copy.copy(actives[0])
        zone.candles = []
        zone.zone_id = "OS:" + "+".join(tfs) + ":" + "+".join(ids)
        zone.parent_zone_id = "+".join(ids)
        zone.timeframe = tfs[0]
        return zone

    n = len(actives)
    vah = sum(z.vah_80 for z in actives) / n
    val = sum(z.val_80 for z in actives) / n
    poc = sum(z.poc for z in actives) / n
    profile: Dict[float, float] = {}
    for z in actives:
        for p, v in (z.profile or {}).items():
            profile[p] = profile.get(p, 0) + v
    zid = "M:" + "+".join(ids)
    return ConsolidationZone(
        zone_id=zid,
        formed_at=actives[-1].formed_at,
        left_at=actives[-1].left_at,
        poc=poc, vah_80=vah, val_80=val,
        high_100=max(z.high_100 for z in actives),
        low_100=min(z.low_100 for z in actives),
        total_volume=sum(z.total_volume for z in actives),
        duration_minutes=0, num_candles=0,
        status=ZoneStatus.LEFT, exit_direction=None, candles=[],
        timeframe="+".join(tfs), profile=profile,
    )


class OverlapZoneDetector:
    """Multi-timeframe overlap detector exposing the SAME public interface as
    ClockBucketZoneDetector, so the live engine can use it as a drop-in.

    Internally holds one ClockBucketZoneDetector per timeframe in the combo.
    On each candle, a merged reference zone exists ONLY when every timeframe has
    a completed active zone and all their value areas overlap (intersection
    non-empty). This is the exact rule used by the backtest/ML overlap sweep
    (routes._merge_zone_timelines): lo = max(val_80), hi = min(vah_80); merged
    exists iff lo <= hi. Entry happens at the AVERAGE overlapping VAH/VAL level.
    """

    def __init__(
        self,
        tf_combo: List[str],
        value_area_pct: float = 0.80,
        tick_size: float = 0.25,
        max_recent: int = 10,
        overlap_trade_tf: str = "merged",
    ):
        order = {tf: i for i, tf in enumerate(AREA_TIMEFRAME_MINUTES.keys())}
        tfs = [t for t in tf_combo if t in AREA_TIMEFRAME_MINUTES]
        # dedupe + stable timeframe order
        seen = set()
        tfs = [t for t in tfs if not (t in seen or seen.add(t))]
        tfs.sort(key=lambda t: order[t])
        self.tfs: Tuple[str, ...] = tuple(tfs)
        self.area_timeframe = "+".join(self.tfs)
        self.value_area_pct = value_area_pct
        self.tick_size = tick_size
        self.max_recent = max_recent
        self.overlap_trade_tf = _normalize_overlap_trade_tf(overlap_trade_tf)

        self._detectors: List[ClockBucketZoneDetector] = [
            ClockBucketZoneDetector(
                area_timeframe=tf,
                value_area_pct=value_area_pct,
                tick_size=tick_size,
                max_recent=max_recent,
            )
            for tf in self.tfs
        ]
        self._merged_active: Optional[ConsolidationZone] = None
        self._merged_history: List[ConsolidationZone] = []
        self._last_key: Optional[tuple] = None

    # ── public API (mirrors ClockBucketZoneDetector) ──

    def update(self, candle: Candle) -> Optional[ZoneEvent]:
        for d in self._detectors:
            d.update(candle)
        self._recompute_merged()
        return None

    def _recompute_merged(self):
        actives = [d.get_active_zone() for d in self._detectors]
        if any(a is None for a in actives):
            self._merged_active = None
            self._last_key = None
            return
        lo = max(a.val_80 for a in actives)
        hi = min(a.vah_80 for a in actives)
        if lo <= hi:
            key = tuple(a.zone_id for a in actives)
            if key != self._last_key:
                mz = _synthesize_overlap_zone(actives, self.tfs, self.overlap_trade_tf)
                self._merged_active = mz
                self._last_key = key
                self._merged_history.append(mz)
                if len(self._merged_history) > max(self.max_recent * 4, 50):
                    self._merged_history = self._merged_history[-(self.max_recent * 4):]
        else:
            self._merged_active = None
            self._last_key = None

    @property
    def is_zone_mature(self) -> bool:
        return self._merged_active is not None

    def get_active_zone(self) -> Optional[ConsolidationZone]:
        return self._merged_active

    def get_forming_zone(self) -> Optional[ConsolidationZone]:
        # No single forming bucket for an overlap; expose nothing.
        return None

    def get_recent_zones(self, n: Optional[int] = None) -> List[ConsolidationZone]:
        return [self._merged_active] if self._merged_active else []

    def get_completed_zones(self) -> List[ConsolidationZone]:
        return [self._merged_active] if self._merged_active else []

    def get_all_zones(self) -> List[ConsolidationZone]:
        return [self._merged_active] if self._merged_active else []

    def get_last_left_zone(self) -> Optional[ConsolidationZone]:
        return self._merged_active

    def close_final_zone(self, last_candle: Candle):
        for d in self._detectors:
            d.close_final_zone(last_candle)
        self._recompute_merged()

    def reset(self):
        for d in self._detectors:
            d.reset()
        self._merged_active = None
        self._merged_history = []
        self._last_key = None


def build_zone_detector(
    area_timeframe: str = "5m",
    value_area_pct: float = 0.80,
    tick_size: float = 0.25,
    max_recent: int = 10,
    tf_combo: Optional[List[str]] = None,
    overlap_trade_tf: str = "merged",
):
    """Factory: returns a zone detector keyed by the selected method.

    - When ``tf_combo`` has >= 2 valid timeframes → OverlapZoneDetector
      (multi-timeframe overlap, identical to the backtest/ML overlap sweep).
    - Otherwise → single-timeframe ClockBucketZoneDetector keyed by
      ``area_timeframe`` (or the single timeframe in tf_combo if given).
    """
    valid_combo = [t for t in (tf_combo or []) if t in AREA_TIMEFRAME_MINUTES]
    if len(valid_combo) >= 2:
        return OverlapZoneDetector(
            tf_combo=valid_combo,
            value_area_pct=value_area_pct,
            tick_size=tick_size,
            max_recent=max_recent,
            overlap_trade_tf=overlap_trade_tf,
        )
    if len(valid_combo) == 1:
        area_timeframe = valid_combo[0]
    return ClockBucketZoneDetector(
        area_timeframe=area_timeframe,
        value_area_pct=value_area_pct,
        tick_size=tick_size,
        max_recent=max_recent,
    )
