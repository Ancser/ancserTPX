# ============================================================
# 文件: backend/strategy/volume_profile.py
# 狀態: 已完成
# 問題: 無
# 關聯文件:
#   ← backend/strategy/consolidation.py (調用本模組計算 VP)
#   ← backend/api/routes.py (API 端點調用)
#   → backend/db/models.py (Candle, VolumeProfileResult)
# 函數結構:
#   - VolumeProfileCalculator.__init__(tick_size, value_area_pct)
#   - VolumeProfileCalculator.calculate(candles) -> VolumeProfileResult
#   - VolumeProfileCalculator._build_price_volume_map(candles) -> dict
#   - VolumeProfileCalculator._find_poc(pv_map) -> float
#   - VolumeProfileCalculator._calculate_value_area(pv_map, poc, pct) -> (vah, val)
# ============================================================
"""
Volume Profile 計算引擎

核心概念：
  把一組 K 線的成交量按價格分配，形成水平直方圖。
  - POC (Point of Control) = 最大成交量的價格
  - VAH/VAL = 從 POC 向外擴展直到涵蓋 80% 總量

這就是你手動用 Fixed Range Volume Profile 做的事情。
代碼化後，可以對任意 K 線區間自動計算。

注意: TopstepX 的 volume 是平台量（非 CME 全量），
     但對偵測相對分布仍然有效。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.db.models import (
    Candle,
    Direction,
    StrategyType,
    TradeSignal,
    VolumeProfileResult,
    get_tick_size,
)
from backend.strategy.session_filter import (
    market_session_code,
    rth_session_bounds,
    rth_session_date,
)
from backend.strategy.research_lab import _ResearchBase
from backend.timebase import as_utc

# 1.0.8: single source of truth — 原本 confluence.py 另有一份重複定義。
# Value-area 百分位帶 (POC 向外擴展涵蓋 20/40/60/80/100% 成交量)。
VA_BAND_PCTS = (20, 40, 60, 80, 100)
PREVIOUS_DAY_VALUE_AREA_PCT = 0.70


def calculate_previous_day_value_areas(
    candles: List[Candle],
    *,
    tick_size: float = 0.25,
    value_area_pct: float = PREVIOUS_DAY_VALUE_AREA_PCT,
) -> List[dict]:
    """Calculate one prior-RTH-day 70% profile reference for every RTH day.

    The returned row is keyed by the day on which its lines are displayed;
    ``source_trade_date`` identifies the completed RTH day whose candles
    produced the values.  Only candles classified as New York ``RTH`` are
    included, so overnight volume cannot widen or move the reference levels.
    Empty calendar days are skipped, so Monday correctly uses Friday's
    completed RTH profile.  The current day's partial profile is never used as
    the source for its own lines.

    This is a chart annotation helper, deliberately separate from strategy
    parameters.  Legacy strategy consumers continue to use their configured
    value-area width; the new ``VolumeProfileStrategy`` defaults to the
    requested 70%.
    """
    if not candles:
        return []

    by_trade_date: Dict[str, List[Candle]] = {}
    for candle in candles:
        if market_session_code(candle.timestamp) != "RTH":
            continue
        trade_date = rth_session_date(candle.timestamp).isoformat()
        by_trade_date.setdefault(trade_date, []).append(candle)

    calculator = VolumeProfileCalculator(
        tick_size=tick_size,
        value_area_pct=float(value_area_pct),
    )
    completed_profiles: List[tuple[str, List[Candle], VolumeProfileResult]] = []
    for trade_date in sorted(by_trade_date):
        day_candles = by_trade_date[trade_date]
        try:
            profile = calculator.calculate(day_candles)
        except ValueError:
            # A day containing no positive-volume bars cannot produce a
            # meaningful reference level and should not break later days.
            continue
        completed_profiles.append((trade_date, day_candles, profile))

    areas: List[dict] = []
    for index in range(1, len(completed_profiles)):
        display_date, _display_candles, _display_profile = completed_profiles[index]
        source_date, source_candles, source_profile = completed_profiles[index - 1]
        start_at, end_at = rth_session_bounds(display_date)
        areas.append({
            "trade_date": display_date,
            "source_trade_date": source_date,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "poc": float(source_profile.poc),
            "vah_70": float(source_profile.vah),
            "val_70": float(source_profile.val),
            "source_candles": len(source_candles),
            "source_volume": int(source_profile.total_volume),
            "value_area_pct": float(value_area_pct),
        })
    return areas


class VolumeProfileCalculator:
    """
    Fixed Range Volume Profile 計算器

    等同於你在 TradingView 上手動框選一段 K 線後
    計算出的 Volume Profile。

    Usage:
        calc = VolumeProfileCalculator(tick_size=0.25, value_area_pct=0.80)
        result = calc.calculate(candles)
        print(result.poc, result.vah, result.val)
    """

    def __init__(self, tick_size: float = 0.25, value_area_pct: float = 0.80):
        """
        Args:
            tick_size: NQ 最小跳動 = 0.25 點 ($5 per tick, $20 per point)
            value_area_pct: Value Area 百分比, 0.80 = 80%
        """
        self.tick_size = tick_size
        self.value_area_pct = value_area_pct

    def calculate(self, candles: List[Candle]) -> VolumeProfileResult:
        """
        計算 Volume Profile

        Args:
            candles: K 線列表（通常是某個時間區間內的 5 分鐘 K 線）

        Returns:
            VolumeProfileResult 含 POC, VAH, VAL, 100% range, profile data
        """
        if not candles:
            raise ValueError("candles 列表不能為空")

        # Step 1: 建立 price → volume 映射
        pv_map = self._build_price_volume_map(candles)

        if not pv_map:
            raise ValueError("無法建立 price-volume map")

        # Step 2: 找 POC
        poc = self._find_poc(pv_map)

        # Step 3: 計算 Value Area (80% 邊界)
        vah, val = self._calculate_value_area(pv_map, poc, self.value_area_pct)

        # Step 4: 100% range
        all_prices = list(pv_map.keys())
        high_100 = max(all_prices)
        low_100 = min(all_prices)

        total_volume = sum(pv_map.values())

        # Step 5: multi-band value areas (20/40/60/80/100%) for the confluence
        # level universe. 100% is just the full range (no expansion needed).
        va_bands = self._calculate_value_area_bands(pv_map, poc, high_100, low_100)

        return VolumeProfileResult(
            poc=poc,
            vah=vah,
            val=val,
            high_100=high_100,
            low_100=low_100,
            total_volume=total_volume,
            profile=pv_map,
            value_area_pct=self.value_area_pct,
            va_bands=va_bands,
        )

    # value-area percentages that make up the confluence level universe
    VA_BAND_PCTS = VA_BAND_PCTS  # 1.0.8: 引用模組級單一來源

    def _calculate_value_area_bands(
        self,
        pv_map: Dict[float, int],
        poc: float,
        high_100: float,
        low_100: float,
    ) -> Dict[int, Tuple[float, float]]:
        """Compute (VAH, VAL) for each band in VA_BAND_PCTS.

        Returns {pct: (vah, val)} with integer pct keys. The 100% band is the
        full price range; the rest reuse the standard POC-outward expansion.
        """
        bands: Dict[int, Tuple[float, float]] = {}
        for pct in self.VA_BAND_PCTS:
            if pct >= 100:
                bands[pct] = (high_100, low_100)
            else:
                bands[pct] = self._calculate_value_area(pv_map, poc, pct / 100.0)
        return bands

    def _build_price_volume_map(self, candles: List[Candle]) -> Dict[float, int]:
        """
        將每根 K 線的 volume 按價格分配到各 tick 價位

        方法：假設每根 K 線的成交量在其 high-low 範圍內均勻分布
        (TPO 近似法，實際交易所有更精確的分法，但對 5 分 K 線足夠)

        Example:
            K 線: O=100, H=102, L=99, C=101, V=1000
            range = 102 - 99 = 3 點 = 12 ticks (tick_size=0.25)
            每個 tick 分配 volume = 1000 / 12 ≈ 83
        """
        pv_map: Dict[float, int] = {}

        for candle in candles:
            if candle.volume <= 0:
                continue

            # 計算 K 線覆蓋的 tick 數
            bar_high = self._round_to_tick(candle.high)
            bar_low = self._round_to_tick(candle.low)

            if bar_high <= bar_low:
                # 十字線或零 range → 全部集中在一個價位
                price = self._round_to_tick(candle.close)
                pv_map[price] = pv_map.get(price, 0) + candle.volume
                continue

            # 計算 tick 數量
            num_ticks = round((bar_high - bar_low) / self.tick_size) + 1

            # 每 tick 分配的 volume
            vol_per_tick = candle.volume / num_ticks

            # 分配到每個 tick 價位
            price = bar_low
            while price <= bar_high + self.tick_size * 0.5:  # 浮點容差
                rounded = self._round_to_tick(price)
                pv_map[rounded] = pv_map.get(rounded, 0) + int(vol_per_tick)
                price += self.tick_size

        return pv_map

    def _find_poc(self, pv_map: Dict[float, int]) -> float:
        """
        找出最大成交量對應的價格 = POC

        如果多個價位成交量相同，取中間值（偏向中位數）
        """
        max_vol = max(pv_map.values())
        poc_candidates = [p for p, v in pv_map.items() if v == max_vol]

        # 取中間值
        poc_candidates.sort()
        mid_idx = len(poc_candidates) // 2
        return poc_candidates[mid_idx]

    def _calculate_value_area(
        self,
        pv_map: Dict[float, int],
        poc: float,
        pct: float
    ) -> Tuple[float, float]:
        """
        從 POC 向兩側擴展，直到累計成交量達到 pct% 的總量

        算法（CME 標準做法）：
        1. 從 POC 開始，累計 = pv_map[poc]
        2. 比較 POC 上方下一個 tick 和下方下一個 tick 的 volume
        3. 加入 volume 較大的一側
        4. 重複直到累計 ≥ pct × total

        Returns:
            (vah, val) = (Value Area High, Value Area Low)
        """
        total_volume = sum(pv_map.values())
        target_volume = total_volume * pct

        # 排序所有價位
        sorted_prices = sorted(pv_map.keys())

        # 找到 POC 在排序列表中的索引
        poc_idx = None
        for i, p in enumerate(sorted_prices):
            if abs(p - poc) < self.tick_size * 0.5:
                poc_idx = i
                break

        if poc_idx is None:
            # fallback: 最接近的
            poc_idx = min(range(len(sorted_prices)),
                         key=lambda i: abs(sorted_prices[i] - poc))

        # 從 POC 向兩側擴展
        cumulative = pv_map.get(poc, 0)
        upper_idx = poc_idx  # 當前上邊界索引
        lower_idx = poc_idx  # 當前下邊界索引

        while cumulative < target_volume:
            can_go_up = upper_idx + 1 < len(sorted_prices)
            can_go_down = lower_idx - 1 >= 0

            if not can_go_up and not can_go_down:
                break

            # 計算兩側下一步的 volume（看 2 個 tick 的總和，CME 方法）
            vol_up = 0
            if can_go_up:
                vol_up = pv_map.get(sorted_prices[upper_idx + 1], 0)
                if upper_idx + 2 < len(sorted_prices):
                    vol_up += pv_map.get(sorted_prices[upper_idx + 2], 0)

            vol_down = 0
            if can_go_down:
                vol_down = pv_map.get(sorted_prices[lower_idx - 1], 0)
                if lower_idx - 2 >= 0:
                    vol_down += pv_map.get(sorted_prices[lower_idx - 2], 0)

            # 擴展 volume 較大的一側
            if not can_go_down or (can_go_up and vol_up >= vol_down):
                upper_idx += 1
                cumulative += pv_map.get(sorted_prices[upper_idx], 0)
            elif not can_go_up or (can_go_down and vol_down > vol_up):
                lower_idx -= 1
                cumulative += pv_map.get(sorted_prices[lower_idx], 0)

        vah = sorted_prices[upper_idx]
        val = sorted_prices[lower_idx]

        return vah, val

    def _round_to_tick(self, price: float) -> float:
        """將價格四捨五入到最近的 tick"""
        return round(round(price / self.tick_size) * self.tick_size, 4)


class PreviousRthValueAreaTracker:
    """Causally expose the last completed New-York RTH value area.

    The chart helper above can calculate a whole history in one pass.  A live
    strategy cannot do that: at 09:30 it may only use the RTH candles that have
    already completed.  This tracker keeps the two paths on the same
    ``VolumeProfileCalculator`` definition while never including the current
    RTH session in its own reference levels.
    """

    def __init__(
        self,
        *,
        tick_size: float = 0.25,
        value_area_pct: float = PREVIOUS_DAY_VALUE_AREA_PCT,
        min_source_candles: int = 60,
    ) -> None:
        self.tick_size = float(tick_size)
        self.value_area_pct = float(value_area_pct)
        self.min_source_candles = max(1, int(min_source_candles))
        self.current_date: Optional[str] = None
        self.previous_date: Optional[str] = None
        self._current_candles: list[Candle] = []
        self._levels: Optional[dict[str, Any]] = None

    @property
    def levels(self) -> Optional[dict[str, Any]]:
        return dict(self._levels) if self._levels is not None else None

    def reset(self) -> None:
        self.current_date = None
        self.previous_date = None
        self._current_candles = []
        self._levels = None

    def _complete_current_session(self) -> None:
        if self.current_date is None:
            return
        candles = list(self._current_candles)
        self.previous_date = self.current_date
        self._levels = None
        if len(candles) < self.min_source_candles:
            return
        try:
            profile = VolumeProfileCalculator(
                tick_size=self.tick_size,
                value_area_pct=self.value_area_pct,
            ).calculate(candles)
        except ValueError:
            return
        # ``date`` is the display/trading RTH date.  The source date is kept
        # separately for auditability and to prevent accidental look-ahead.
        self._levels = {
            "date": None,
            "source_trade_date": self.previous_date,
            "poc": float(profile.poc),
            "vah": float(profile.vah),
            "val": float(profile.val),
            "high_100": float(profile.high_100),
            "low_100": float(profile.low_100),
            "source_candles": len(candles),
            "source_volume": int(profile.total_volume),
            "value_area_pct": self.value_area_pct,
        }

    def update(self, candle: Candle) -> Optional[dict[str, Any]]:
        """Consume one candle and return the currently usable prior-RTH levels."""
        if market_session_code(candle.timestamp) != "RTH":
            return self.levels

        current_date = rth_session_date(candle.timestamp).isoformat()
        if self.current_date is None:
            self.current_date = current_date
        elif current_date != self.current_date:
            self._complete_current_session()
            self.current_date = current_date
            self._current_candles = []
            if self._levels is not None:
                self._levels["date"] = current_date

        self._current_candles.append(candle)
        return self.levels


VP_ENTRY_MODES = ("auto", "range", "breakout", "failed_break")
VP_TARGET_MODES = ("atr", "poc", "opposite_edge")
VP_SIDE_MODES = ("all", "long_only", "short_only")


def normalize_volume_profile_entry_mode(value: object) -> str:
    name = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "fade": "range",
        "range_fade": "range",
        "breakout_retest": "breakout",
        "failed": "failed_break",
        "reclaim": "failed_break",
    }
    name = aliases.get(name, name)
    return name if name in VP_ENTRY_MODES else "auto"


def normalize_volume_profile_target_mode(value: object) -> str:
    name = str(value or "atr").strip().lower().replace("-", "_")
    aliases = {"value": "poc", "value_area": "poc", "edge": "opposite_edge"}
    name = aliases.get(name, name)
    return name if name in VP_TARGET_MODES else "atr"


def normalize_volume_profile_side_mode(value: object) -> str:
    name = str(value or "all").strip().lower()
    return name if name in VP_SIDE_MODES else "all"


class VolumeProfileStrategy(_ResearchBase):
    """Prior-RTH 70% VA edge strategy with an explicit break-state machine.

    ``auto`` is deliberately state based rather than a hindsight regime label:

    * before an accepted close outside VAL/VAH, trade a confirmed rejection
      back toward the POC;
    * after ``vp_confirm_bars`` closes outside, wait for a later retest and
      trade continuation;
    * if an outside attempt is reclaimed, optionally trade the failed break
      back toward value.

    One edge can produce at most one signal per displayed RTH day.  A signal
    consumes that edge only after its ATR risk and target have validated; an
    order cancellation releases it so broker/risk rejection does not create a
    permanent lock.  This prevents repeated VAL/VAH crossings from becoming a
    stream of duplicate entries while preserving a later, properly confirmed
    reclaim.
    """

    NAME = "VOLUME PROFILE"
    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, params: Any):
        super().__init__(params)
        self.tick_size = get_tick_size(getattr(params, "contract_id", "") or "")
        self.entry_mode = normalize_volume_profile_entry_mode(
            getattr(params, "vp_entry_mode", "auto")
        )
        self.target_mode = normalize_volume_profile_target_mode(
            getattr(params, "vp_target_mode", "atr")
        )
        self.side_mode = normalize_volume_profile_side_mode(
            getattr(params, "vp_side_mode", "all")
        )
        self.vp_sl_atr = max(
            0.1, float(getattr(params, "vp_sl_atr", 1.5) or 1.5)
        )
        self.vp_tp_atr = max(
            0.1, float(getattr(params, "vp_tp_atr", 2.0) or 2.0)
        )
        self.confirm_bars = max(
            1, min(10, int(getattr(params, "vp_confirm_bars", 2) or 2))
        )
        self.breakout_buffer_ticks = max(
            0, min(40, int(getattr(params, "vp_breakout_buffer_ticks", 2) or 0))
        )
        self.touch_tolerance_ticks = max(
            0, min(40, int(getattr(params, "vp_touch_tolerance_ticks", 2) or 0))
        )
        self.reclaim_buffer_ticks = max(
            0, min(40, int(getattr(params, "vp_reclaim_buffer_ticks", 1) or 0))
        )
        self.max_trades_per_day = max(
            0, int(getattr(params, "vp_max_trades_per_day", 2) or 0)
        )
        self.min_source_candles = max(
            1, int(getattr(params, "vp_min_source_candles", 60) or 60)
        )
        self.value_area_pct = min(
            0.95,
            max(
                0.50,
                float(
                    getattr(params, "vp_value_area_pct", PREVIOUS_DAY_VALUE_AREA_PCT)
                    or PREVIOUS_DAY_VALUE_AREA_PCT
                ),
            ),
        )
        self._profile_tracker = PreviousRthValueAreaTracker(
            tick_size=self.tick_size,
            value_area_pct=self.value_area_pct,
            min_source_candles=self.min_source_candles,
        )
        self._levels: Optional[dict[str, Any]] = None
        self._profile_day: Optional[str] = None
        self._edge_used: set[str] = set()
        self._last_signal_edge: Optional[str] = None
        self._outside_side: Optional[str] = None
        self._outside_count = 0
        self._accepted_breakout: Optional[str] = None
        self._accepted_at: Optional[int] = None
        self._failed_break_side: Optional[str] = None
        self._last_observed_key: Optional[int] = None
        self._last_status = "waiting_for_prior_rth_profile"

    def reset(self) -> None:
        # A full reset is used for a new backtest.  It must clear the profile
        # and ATR histories as well as the entry locks.
        super().reset()
        self._bars.clear()
        self._cur = None
        self._daily = {}
        self._profile_tracker.reset()
        self._levels = None
        self._profile_day = None
        self._edge_used = set()
        self._last_signal_edge = None
        self._outside_side = None
        self._outside_count = 0
        self._accepted_breakout = None
        self._accepted_at = None
        self._failed_break_side = None
        self._last_observed_key = None
        self._last_status = "waiting_for_prior_rth_profile"

    def reset_state_only(self) -> None:
        # Session filters should clear only an in-flight signal state.  Erasing
        # profile/ATR history here would make live and backtest diverge.
        self._state = "idle"
        self._last_signal_edge = None

    def reset_breakout_confirmation(self) -> None:
        self.reset_state_only()

    def warmup(self, candle: Candle) -> None:
        self.observe(candle, [], True)

    def set_levels(self, levels: Optional[dict[str, Any]]) -> None:
        """Inject levels for deterministic fixtures without bypassing live code."""
        self._levels = dict(levels) if levels else None
        self._profile_day = (self._levels or {}).get("date")
        self._reset_profile_state()

    def get_levels(self) -> Optional[dict[str, Any]]:
        return dict(self._levels) if self._levels is not None else None

    def set_traded_breakouts(self, keys) -> None:
        self._edge_used = {str(key) for key in (keys or [])}

    def mark_breakout_used(self, zone_id, direction) -> None:
        return None

    def unlock_breakout(self, zone_id, direction) -> None:
        return None

    def notify_trade_closed(self, exit_reason: str) -> None:
        self._state = "idle"
        self._last_signal_edge = None

    def notify_order_cancelled(self) -> None:
        self._state = "idle"
        if self._last_signal_edge is not None:
            self._edge_used.discard(self._last_signal_edge)
            self._last_signal_edge = None

    def get_phase_label(self) -> str:
        if self._state == "confirmed":
            return f"VOLUME PROFILE {self._last_status} · ORDER PENDING"
        if self._state == "in_trade":
            return "VOLUME PROFILE · IN POSITION"
        if not self._levels:
            return "VOLUME PROFILE · WAITING FOR PRIOR RTH 70% PROFILE"
        source = self._levels.get("source_trade_date") or "?"
        if self._accepted_breakout:
            return (
                f"VOLUME PROFILE · {source} · "
                f"{self._accepted_breakout.upper()} BREAK ACCEPTED; WAIT RETEST"
            )
        return f"VOLUME PROFILE · {source} · {self._last_status}"

    @staticmethod
    def _candle_key(candle: Candle) -> int:
        return int(as_utc(candle.timestamp).timestamp())

    def _reset_profile_state(self) -> None:
        self._edge_used = set()
        self._last_signal_edge = None
        self._outside_side = None
        self._outside_count = 0
        self._accepted_breakout = None
        self._accepted_at = None
        self._failed_break_side = None
        self._last_status = "waiting_for_edge"

    def _track_profile(self, candle: Candle) -> None:
        levels = self._profile_tracker.update(candle)
        profile_day = self._profile_tracker.current_date
        if profile_day != self._profile_day:
            self._profile_day = profile_day
            self._levels = levels
            self._reset_profile_state()
        elif levels is not None:
            self._levels = levels

    def _outside_thresholds(self, levels: dict[str, Any]) -> tuple[float, float, float, float]:
        vah = float(levels["vah"])
        val = float(levels["val"])
        breakout_buffer = self.breakout_buffer_ticks * self.tick_size
        reclaim_buffer = self.reclaim_buffer_ticks * self.tick_size
        return vah + breakout_buffer, val - breakout_buffer, vah - reclaim_buffer, val + reclaim_buffer

    def _update_break_state(self, candle: Candle) -> None:
        levels = self._levels
        if not levels or market_session_code(candle.timestamp) != "RTH":
            return
        try:
            vah = float(levels["vah"])
            val = float(levels["val"])
        except (KeyError, TypeError, ValueError):
            return
        close = float(candle.close)
        up_threshold, down_threshold, up_reclaim, down_reclaim = self._outside_thresholds(levels)
        outside = "up" if close > up_threshold else "down" if close < down_threshold else None

        if outside is not None:
            if outside == self._outside_side:
                self._outside_count += 1
            else:
                self._outside_side = outside
                self._outside_count = 1
            self._failed_break_side = None
            if self._outside_count >= self.confirm_bars and self._accepted_breakout != outside:
                self._accepted_breakout = outside
                self._accepted_at = self._candle_key(candle)
                self._last_status = f"{outside}_break_confirmed"
            elif self._accepted_breakout == outside:
                self._last_status = f"{outside}_break_accepted"
            else:
                self._last_status = f"{outside}_break_watching_{self._outside_count}/{self.confirm_bars}"
            return

        # Any close back through the boundary invalidates the outside attempt.
        # This includes both a shallow false break and a previously accepted
        # breakout that failed on the next reclaim.
        if self._outside_side == "up" and close < up_reclaim:
            self._failed_break_side = "up"
            self._accepted_breakout = None
            self._accepted_at = None
            self._last_status = "failed_up_break"
        elif self._outside_side == "down" and close > down_reclaim:
            self._failed_break_side = "down"
            self._accepted_breakout = None
            self._accepted_at = None
            self._last_status = "failed_down_break"
        elif self._accepted_breakout is None:
            self._last_status = "inside_value"
        self._outside_side = None
        self._outside_count = 0

    def observe(self, candle: Candle, zones=None, is_mature: bool = True) -> None:
        key = self._candle_key(candle)
        if key == self._last_observed_key:
            return
        # The base class owns the completed 5m ATR blend.  Profile state is
        # updated after the same candle has been admitted to that history.
        super().observe(candle, zones, is_mature)
        self._track_profile(candle)
        self._update_break_state(candle)
        self._last_observed_key = key

    def _edge_key(self, edge: str) -> str:
        display_date = (self._levels or {}).get("date") or self._profile_day or "?"
        return f"{display_date}:{edge}"

    def _candidate_set(self, candle: Candle) -> list[tuple[Direction, str, str, str]]:
        levels = self._levels
        if not levels:
            self._last_status = "waiting_for_prior_rth_profile"
            return []
        try:
            poc = float(levels["poc"])
            vah = float(levels["vah"])
            val = float(levels["val"])
        except (KeyError, TypeError, ValueError):
            return []
        if not val < poc < vah:
            self._last_status = "invalid_profile_geometry"
            return []

        touch = self.touch_tolerance_ticks * self.tick_size
        reclaim = self.reclaim_buffer_ticks * self.tick_size
        close = float(candle.close)
        candidates: list[tuple[Direction, str, str, str]] = []

        # A failed outside attempt has priority in AUTO: do not also treat the
        # reclaim bar as a fresh blind edge touch.
        failed = self._failed_break_side
        if self.entry_mode in ("auto", "failed_break") and failed == "up":
            key = self._edge_key("vah")
            if key not in self._edge_used and candle.high >= vah - touch and close < vah - reclaim:
                candidates.append((Direction.SELL, "vah", "failed_break", "failed_break"))
        elif self.entry_mode in ("auto", "failed_break") and failed == "down":
            key = self._edge_key("val")
            if key not in self._edge_used and candle.low <= val + touch and close > val + reclaim:
                candidates.append((Direction.BUY, "val", "failed_break", "failed_break"))

        if candidates:
            return candidates

        # Once a breakout is accepted, AUTO switches from fading to retest
        # continuation.  ``accepted_at`` forces a later candle, avoiding a
        # chase on the confirmation candle itself.
        accepted = self._accepted_breakout
        accepted_at = self._accepted_at
        current_key = self._candle_key(candle)
        if self.entry_mode in ("auto", "breakout") and accepted and (
            accepted_at is None or current_key > accepted_at
        ):
            if accepted == "up":
                key = self._edge_key("vah")
                if key not in self._edge_used and candle.low <= vah + touch and close > vah + self.breakout_buffer_ticks * self.tick_size:
                    candidates.append((Direction.BUY, "vah", "breakout_retest", "trend"))
            else:
                key = self._edge_key("val")
                if key not in self._edge_used and candle.high >= val - touch and close < val - self.breakout_buffer_ticks * self.tick_size:
                    candidates.append((Direction.SELL, "val", "breakout_retest", "trend"))
            return candidates

        # Range rejection: a wick/touch plus a close back inside value.  A
        # plain close outside never qualifies, so pin-breaks do not create a
        # market entry until they reclaim the boundary.
        if self.entry_mode in ("auto", "range") and accepted is None:
            if (
                self._side_ok(Direction.BUY)
                and self._edge_key("val") not in self._edge_used
                and candle.low <= val + touch
                and close > val + reclaim
                and close < poc
            ):
                candidates.append((Direction.BUY, "val", "range_rejection", "range"))
            if (
                self._side_ok(Direction.SELL)
                and self._edge_key("vah") not in self._edge_used
                and candle.high >= vah - touch
                and close < vah - reclaim
                and close > poc
            ):
                candidates.append((Direction.SELL, "vah", "range_rejection", "range"))
        return candidates

    def _make_volume_profile_signal(
        self,
        candle: Candle,
        direction: Direction,
        edge: str,
        setup: str,
        regime: str,
    ) -> Optional[TradeSignal]:
        levels = self._levels
        if not levels or not self._side_ok(direction):
            return None
        trade_date = self._trade_date(candle.timestamp)
        if self.max_trades_per_day and self._daily.get(trade_date, 0) >= self.max_trades_per_day:
            self._last_status = "daily_trade_limit"
            return None
        atr = self._atr_blend()
        if atr is None or atr <= 0:
            self._last_status = "waiting_for_atr_blend"
            return None

        entry = self._round(float(candle.close))
        risk_distance = max(self.tick_size, atr * self.vp_sl_atr)
        reward_distance = max(self.tick_size, atr * self.vp_tp_atr)
        try:
            poc = float(levels["poc"])
            vah = float(levels["vah"])
            val = float(levels["val"])
        except (KeyError, TypeError, ValueError):
            return None
        if self.target_mode == "poc":
            target = poc
        elif self.target_mode == "opposite_edge":
            target = vah if direction == Direction.BUY else val
        else:
            target = entry + reward_distance if direction == Direction.BUY else entry - reward_distance
        sl = entry - risk_distance if direction == Direction.BUY else entry + risk_distance
        sl = self._round(sl)
        target = self._round(target)
        if direction == Direction.BUY and not (sl < entry < target):
            self._last_status = "target_not_above_entry"
            return None
        if direction == Direction.SELL and not (target < entry < sl):
            self._last_status = "target_not_below_entry"
            return None

        edge_key = self._edge_key(edge)
        self._edge_used.add(edge_key)
        self._last_signal_edge = edge_key
        self._daily[trade_date] = self._daily.get(trade_date, 0) + 1
        self._state = "confirmed"
        self._last_status = f"{setup}_{edge}"
        source_date = levels.get("source_trade_date") or "?"
        side = "LONG" if direction == Direction.BUY else "SHORT"
        signal = TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=target,
            zone_id=f"VP:{levels.get('date') or self._profile_day}:{edge}",
            zone_source="volume_profile",
            reason=(
                f"VOLUME PROFILE {setup.upper()} {side} | "
                f"prior RTH {source_date} 70% {edge.upper()} | "
                f"ATR blend {atr:.2f} | SL {self.vp_sl_atr:g}x | "
                f"TP {self.target_mode}:{target:.2f}"
            ),
            timestamp=candle.timestamp,
            breakout_range=abs(vah - val),
            order_type="market",
            meta={
                "strategy_family": "volume_profile",
                "entry_mode": self.entry_mode,
                "setup": setup,
                "regime": regime,
                "edge": edge,
                "profile_date": levels.get("date"),
                "source_trade_date": source_date,
                "poc": poc,
                "vah": vah,
                "val": val,
                "value_area_pct": self.value_area_pct,
                "atr_blend": atr,
                "sl_atr": self.vp_sl_atr,
                "tp_atr": self.vp_tp_atr,
                "target_mode": self.target_mode,
                "breakout_confirm_bars": self.confirm_bars,
                "accepted_breakout": self._accepted_breakout,
                "failed_break_side": self._failed_break_side,
                "edge_key": edge_key,
                "labels": [
                    f"vp:{setup}",
                    f"regime:{regime}",
                    f"edge:{edge}",
                    f"target:{self.target_mode}",
                ],
            },
        )
        return signal

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True) -> Optional[TradeSignal]:
        if self._candle_key(candle) != self._last_observed_key:
            self.observe(candle, zones, is_mature)
        if self._state == "in_trade":
            return None
        if market_session_code(candle.timestamp) != "RTH":
            self._last_status = "outside_rth"
            return None
        candidates = self._candidate_set(candle)
        if len(candidates) != 1:
            if len(candidates) > 1:
                self._last_status = "ambiguous_two_sided_edge"
            return None
        direction, edge, setup, regime = candidates[0]
        return self._make_volume_profile_signal(candle, direction, edge, setup, regime)
