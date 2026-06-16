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

from typing import Dict, List, Tuple
from backend.db.models import Candle, VolumeProfileResult


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
    VA_BAND_PCTS = (20, 40, 60, 80, 100)

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
