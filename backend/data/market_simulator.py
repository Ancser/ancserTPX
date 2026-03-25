# ============================================================
# 文件: backend/data/market_simulator.py
# 狀態: 已完成 (已檢查 1 次)
# 問題: 無
# 關聯文件:
#   <- backend/api/routes.py        (啟動/停止/速度控制)
#   <- backend/api/websocket.py     (推送 tick + candle 給前端)
#   -> backend/db/models.py         (Candle)
# 函數結構:
#   - MarketSimulator: 主模擬器
#   - PriceEngine: 價格動力學引擎 (GBM + OU + momentum)
#   - OrderFlowEngine: 訂單流模擬 (機構/散戶/做市商)
#   - CandleBuilder: tick -> candle 構建器
# ============================================================
"""
NQ Live Market Simulator v2 -- Realistic Price Dynamics

核心改進:
  - 價格動力學: GBM (趨勢) + OU (盤整回歸) + momentum (慣性)
  - 訂單流: 機構大單推動方向, 散戶噪音, 做市商提供流動性
  - 市場微觀結構: bid/ask spread, 滑價, 成交量聚集
  - 真實模式: 趨勢有慣性, 盤整有邊界, 突破有成交量確認

模擬流程:
  PriceEngine 決定下一個「公允價格」
    -> OrderFlowEngine 圍繞公允價格產生 bid/ask 訂單
    -> 撮合產生 tick
    -> CandleBuilder 累積成 1m/5m candle
"""

from __future__ import annotations
import asyncio
import math
import random
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple
from enum import Enum

from backend.db.models import Candle

logger = logging.getLogger(__name__)

TICK_SIZE = 0.25  # NQ minimum tick
POINT_VALUE = 20.0  # NQ $20 per point


# -- Data Types -------------------------------------------------

class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class MarketRegime(Enum):
    CONSOLIDATION = "consolidation"
    BREAKOUT_UP = "breakout_up"
    BREAKOUT_DOWN = "breakout_down"
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    PULLBACK = "pullback"


@dataclass
class Tick:
    timestamp: datetime
    price: float
    size: int
    aggressor: OrderSide
    bid: float
    ask: float


# -- Price Engine -----------------------------------------------

class StopLossPool:
    """
    止損池 -- 模擬真實市場的止損掛單

    買方持倉 -> 止損在下方 (觸發 = 賣單湧出 -> 加速下跌)
    賣方持倉 -> 止損在上方 (觸發 = 買單湧出 -> 加速上漲)

    止損連鎖觸發 = 突破的真正動力
    """

    @dataclass
    class StopOrder:
        price: float        # 止損價位
        size: int           # 合約數
        side: str           # "buy_stop" (空頭止損) / "sell_stop" (多頭止損)
        placed_at: float    # 進場價格

    def __init__(self):
        self.stops: List[StopLossPool.StopOrder] = []
        self.triggered_volume = 0  # 本 tick 被觸發的止損量

    def add_position(self, entry_price: float, size: int, direction: str):
        """
        新持倉自動掛止損

        做多 -> sell_stop 掛在 entry - SL_distance
        做空 -> buy_stop 掛在 entry + SL_distance
        """
        sl_distance = random.uniform(4, 15)  # 4-15 點止損

        if direction == "buy":
            # 多頭止損 = 賣出止損 (在下方)
            sl_price = round((entry_price - sl_distance) / TICK_SIZE) * TICK_SIZE
            self.stops.append(self.StopOrder(
                price=sl_price, size=size, side="sell_stop", placed_at=entry_price
            ))
        else:
            # 空頭止損 = 買入止損 (在上方)
            sl_price = round((entry_price + sl_distance) / TICK_SIZE) * TICK_SIZE
            self.stops.append(self.StopOrder(
                price=sl_price, size=size, side="buy_stop", placed_at=entry_price
            ))

        # 限制止損池大小
        if len(self.stops) > 200:
            self.stops = self.stops[-200:]

    def check_triggers(self, current_price: float) -> Tuple[int, int]:
        """
        檢查哪些止損被觸發

        Returns: (triggered_buy_volume, triggered_sell_volume)
        """
        buy_vol = 0
        sell_vol = 0
        remaining = []

        for stop in self.stops:
            if stop.side == "sell_stop" and current_price <= stop.price:
                # 多頭止損觸發 -> 賣單湧出
                sell_vol += stop.size
            elif stop.side == "buy_stop" and current_price >= stop.price:
                # 空頭止損觸發 -> 買單湧出
                buy_vol += stop.size
            else:
                remaining.append(stop)

        self.stops = remaining
        self.triggered_volume = buy_vol + sell_vol
        return buy_vol, sell_vol

    def get_stop_clusters(self, current_price: float, range_pts: float = 30) -> dict:
        """返回止損分布 (for UI visualization)"""
        buy_stops = {}  # 上方空頭止損
        sell_stops = {}  # 下方多頭止損

        for stop in self.stops:
            if abs(stop.price - current_price) > range_pts:
                continue
            bucket = round(stop.price, 2)
            if stop.side == "buy_stop":
                buy_stops[bucket] = buy_stops.get(bucket, 0) + stop.size
            else:
                sell_stops[bucket] = sell_stops.get(bucket, 0) + stop.size

        return {"buy_stops": buy_stops, "sell_stops": sell_stops}


class PriceEngine:
    """
    真實價格動力學引擎

    核心機制:
      1. 買賣博弈: 買方/賣方力量推動價格
      2. 止損觸發: 價格觸及止損叢集 -> 連鎖反應 -> 突破
      3. 動量 (momentum): 止損觸發 -> 加速 -> 更多止損 -> cascade
      4. 均值回歸 (mean reversion): 止損耗盡 -> 新盤整形成
      5. 波動率聚集: 高波動引發更多止損觸發

    狀態機:
      CONSOLIDATION -> BREAKOUT -> TREND -> PULLBACK -> CONSOLIDATION
      (突破由止損連鎖觸發驅動, 不是隨機切換)
    """

    def __init__(self, start_price: float = 23500.0):
        self.price = start_price
        self.prev_price = start_price

        # 動量追蹤
        self.momentum = 0.0
        self.momentum_decay = 0.985
        self.momentum_strength = 0.4

        # 波動率
        self.base_volatility = 0.8
        self.current_vol = 0.8
        self.vol_mean = 0.8
        self.vol_reversion = 0.02

        # 盤整區間
        self.range_center = start_price
        self.range_half = 12.0
        self.reversion_strength = 0.08

        # Regime
        self.regime = MarketRegime.CONSOLIDATION
        self.regime_ticks = 0
        self.regime_target = random.randint(300, 800)
        self.trend_direction = 0
        self.trend_target_price = 0.0

        # 日內趨勢
        self.day_drift = random.uniform(-0.003, 0.003)

        # 止損池
        self.stop_pool = StopLossPool()
        self._stop_cascade_active = False
        self._cascade_direction = 0
        self._cascade_fuel = 0  # 連鎖反應剩餘燃料

        # 買賣雙方力量
        self.buyer_pressure = 0.0   # 0 to 1
        self.seller_pressure = 0.0  # 0 to 1

        # 歷史
        self.price_history: List[float] = [start_price]

    def next_price(self) -> Tuple[float, MarketRegime, float]:
        """
        計算下一個公允價格

        流程:
          1. 基礎價格變動 (regime 決定)
          2. 檢查止損觸發 -> 連鎖反應
          3. 累積新持倉的止損
          4. 更新動量
          5. 檢查 regime 轉換

        Returns: (price, regime, volume_multiplier)
        """
        self.regime_ticks += 1
        self.prev_price = self.price

        # 波動率均值回歸
        vol_shock = random.gauss(0, 0.05)
        self.current_vol += self.vol_reversion * (self.vol_mean - self.current_vol) + vol_shock
        self.current_vol = max(0.2, min(3.0, self.current_vol))

        vol_mult = 1.0

        # -- Step 1: 基礎價格變動 --
        if self.regime == MarketRegime.CONSOLIDATION:
            price_change, vol_mult = self._consolidation_step()
        elif self.regime in (MarketRegime.BREAKOUT_UP, MarketRegime.BREAKOUT_DOWN):
            price_change, vol_mult = self._breakout_step()
        elif self.regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            price_change, vol_mult = self._trend_step()
        elif self.regime == MarketRegime.PULLBACK:
            price_change, vol_mult = self._pullback_step()
        else:
            price_change = random.gauss(0, self.current_vol)

        # 日內漂移
        price_change += self.day_drift

        # -- Step 2: 止損連鎖反應 --
        if self._stop_cascade_active and self._cascade_fuel > 0:
            # 連鎖進行中: 強力推動
            cascade_push = self._cascade_direction * self._cascade_fuel * 0.3
            price_change += cascade_push
            self._cascade_fuel *= 0.85  # 燃料衰減
            vol_mult = max(vol_mult, 2.5)
            if self._cascade_fuel < 0.5:
                self._stop_cascade_active = False
                self._cascade_fuel = 0

        # 應用價格變動
        self.price += price_change
        self.price = round(self.price / TICK_SIZE) * TICK_SIZE
        self.price = round(self.price, 2)

        # -- Step 3: 檢查止損觸發 --
        triggered_buy, triggered_sell = self.stop_pool.check_triggers(self.price)
        if triggered_buy > 0 or triggered_sell > 0:
            net_force = triggered_buy - triggered_sell
            if abs(net_force) > 5:
                # 止損連鎖啟動!
                self._stop_cascade_active = True
                self._cascade_direction = 1 if net_force > 0 else -1
                self._cascade_fuel = min(abs(net_force) * 0.1, 5.0)
                self.current_vol *= 1.3  # 波動率跳升
                vol_mult = max(vol_mult, 2.0)

                logger.debug(
                    f"STOP CASCADE: buy_triggered={triggered_buy} "
                    f"sell_triggered={triggered_sell} "
                    f"direction={'UP' if self._cascade_direction > 0 else 'DOWN'} "
                    f"fuel={self._cascade_fuel:.1f}"
                )

            # 止損觸發產生的額外價格衝擊
            impact = net_force * 0.02
            self.price += impact
            self.price = round(self.price / TICK_SIZE) * TICK_SIZE
            self.price = round(self.price, 2)

        # -- Step 4: 累積新持倉止損 --
        # 每個 tick 都有新的交易者進場, 他們掛止損
        if random.random() < 0.4:  # 40% 的 tick 有新持倉
            entry_size = random.randint(1, 8)
            # 方向偏好受 momentum 影響
            if random.random() < 0.5 + self.momentum * 0.2:
                self.stop_pool.add_position(self.price, entry_size, "buy")
                self.buyer_pressure = min(1.0, self.buyer_pressure + 0.01)
            else:
                self.stop_pool.add_position(self.price, entry_size, "sell")
                self.seller_pressure = min(1.0, self.seller_pressure + 0.01)

        # 壓力衰減
        self.buyer_pressure *= 0.998
        self.seller_pressure *= 0.998

        # -- Step 5: 更新動量 --
        if abs(price_change) > 0.01:
            direction = 1 if price_change > 0 else -1
            self.momentum = self.momentum * self.momentum_decay + direction * 0.05
            self.momentum = max(-1.0, min(1.0, self.momentum))

        # 動量貢獻
        self.price += self.momentum * self.momentum_strength * self.current_vol * 0.15
        self.price = round(self.price / TICK_SIZE) * TICK_SIZE
        self.price = round(self.price, 2)

        # 記錄歷史
        self.price_history.append(self.price)
        if len(self.price_history) > 2000:
            self.price_history.pop(0)

        # -- Step 6: Regime 轉換 --
        self._check_regime_transition()

        return self.price, self.regime, vol_mult

    def _consolidation_step(self) -> Tuple[float, float]:
        """盤整: 價格在 range_center +/- range_half 內來回"""
        deviation = self.price - self.range_center
        # 越靠近邊界, 回歸力越強
        reversion = -deviation * self.reversion_strength * (1 + abs(deviation) / self.range_half)
        noise = random.gauss(0, self.current_vol * 0.6)
        return reversion + noise, 0.7

    def _breakout_step(self) -> Tuple[float, float]:
        """突破: 強方向性 + 高波動"""
        direction = 1.0 if self.regime == MarketRegime.BREAKOUT_UP else -1.0
        # 突破力度隨時間遞減
        progress = self.regime_ticks / max(1, self.regime_target)
        force = direction * self.current_vol * 1.5 * (1.0 - progress * 0.5)
        noise = random.gauss(0, self.current_vol * 0.8)
        return force + noise, 2.5  # 成交量放大 2.5x

    def _trend_step(self) -> Tuple[float, float]:
        """趨勢: 穩定方向 + 偶爾小回調"""
        direction = 1.0 if self.regime == MarketRegime.TREND_UP else -1.0

        # 80% 順勢, 20% 小回調
        if random.random() < 0.80:
            move = direction * abs(random.gauss(0, self.current_vol * 0.7))
        else:
            move = -direction * abs(random.gauss(0, self.current_vol * 0.3))

        noise = random.gauss(0, self.current_vol * 0.2)
        return move + noise, 1.4

    def _pullback_step(self) -> Tuple[float, float]:
        """回調: 反向移動, 力度較弱"""
        # 回調方向 = 前一個趨勢的反方向
        direction = -self.trend_direction
        move = direction * abs(random.gauss(0, self.current_vol * 0.5))
        noise = random.gauss(0, self.current_vol * 0.4)

        # 接近 range center 時減速
        dist_to_center = abs(self.price - self.range_center)
        if dist_to_center < self.range_half * 0.5:
            move *= 0.3

        return move + noise, 1.0

    def _check_regime_transition(self):
        """檢查是否需要切換 regime"""

        if self.regime == MarketRegime.CONSOLIDATION:
            # 條件: 時間到了, 或價格觸及邊界且動量足夠
            at_boundary = abs(self.price - self.range_center) > self.range_half * 0.85
            strong_momentum = abs(self.momentum) > 0.4
            time_up = self.regime_ticks >= self.regime_target

            if (at_boundary and strong_momentum) or time_up:
                if self.momentum > 0 or (time_up and random.random() < 0.5):
                    self.regime = MarketRegime.BREAKOUT_UP
                    self.trend_direction = 1
                else:
                    self.regime = MarketRegime.BREAKOUT_DOWN
                    self.trend_direction = -1

                self.regime_ticks = 0
                self.regime_target = random.randint(30, 80)  # 突破持續較短
                self.current_vol *= 1.5
                logger.debug(f"REGIME -> {self.regime.value} @ {self.price:.2f}")

        elif self.regime in (MarketRegime.BREAKOUT_UP, MarketRegime.BREAKOUT_DOWN):
            if self.regime_ticks >= self.regime_target:
                if self.trend_direction > 0:
                    self.regime = MarketRegime.TREND_UP
                else:
                    self.regime = MarketRegime.TREND_DOWN

                self.regime_ticks = 0
                self.regime_target = random.randint(150, 500)
                logger.debug(f"REGIME -> {self.regime.value} @ {self.price:.2f}")

        elif self.regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            # 趨勢結束條件: 時間到 或 動量反轉
            momentum_reversed = (
                (self.regime == MarketRegime.TREND_UP and self.momentum < -0.2) or
                (self.regime == MarketRegime.TREND_DOWN and self.momentum > 0.2)
            )
            if self.regime_ticks >= self.regime_target or momentum_reversed:
                # 70% 回調後新盤整, 30% 直接新盤整
                if random.random() < 0.7:
                    self.regime = MarketRegime.PULLBACK
                    self.regime_ticks = 0
                    self.regime_target = random.randint(50, 150)
                else:
                    self._enter_new_consolidation()
                logger.debug(f"REGIME -> {self.regime.value} @ {self.price:.2f}")

        elif self.regime == MarketRegime.PULLBACK:
            if self.regime_ticks >= self.regime_target:
                self._enter_new_consolidation()
                logger.debug(f"REGIME -> CONSOLIDATION @ {self.price:.2f}")

    def _enter_new_consolidation(self):
        """進入新的盤整區間"""
        self.regime = MarketRegime.CONSOLIDATION
        self.regime_ticks = 0
        self.regime_target = random.randint(300, 800)
        self.range_center = self.price
        self.range_half = random.uniform(8, 20)
        self.momentum *= 0.3  # 重置大部分動量
        self.current_vol = self.vol_mean  # 波動率回歸

    def get_regime_simple(self) -> str:
        """返回簡化的 regime 名稱 (for UI)"""
        mapping = {
            MarketRegime.CONSOLIDATION: "consolidation",
            MarketRegime.BREAKOUT_UP: "breakout",
            MarketRegime.BREAKOUT_DOWN: "breakout",
            MarketRegime.TREND_UP: "trending",
            MarketRegime.TREND_DOWN: "trending",
            MarketRegime.PULLBACK: "pullback",
        }
        return mapping.get(self.regime, "unknown")


# -- Order Flow Engine ------------------------------------------

class OrderFlowEngine:
    """
    訂單流模擬器

    三類參與者:
      1. 機構 (Institutional): 大單, 方向性, 推動價格
      2. 散戶 (Retail): 小單, 隨機, 噪音
      3. 做市商 (Market Maker): 雙邊掛單, 提供流動性, 縮小 spread

    根據 regime 調整各類參與者的行為
    """

    def __init__(self):
        self.bid_depth: List[Tuple[float, int]] = []  # [(price, size), ...]
        self.ask_depth: List[Tuple[float, int]] = []

    def generate_tick(
        self,
        fair_price: float,
        regime: MarketRegime,
        vol_mult: float,
        momentum: float,
        session_vol_mult: float = 1.0,
    ) -> Tick:
        """
        圍繞公允價格生成一個 tick

        Active vs Passive 邏輯:
          - Active (aggressor): 穿越 spread, 付出 spread 成本
          - Passive (maker): 在 bid/ask 掛單, 被 aggressor 打到時成交
          - 盤整: 60% passive (做市商主導)
          - 突破: 80% active (激進市價單)

        session_vol_mult: 時段成交量乘數, 影響大單概率

        Returns: Tick
        """
        # Spread: 正常 0.25-0.50, 突破時可能擴大
        if regime in (MarketRegime.BREAKOUT_UP, MarketRegime.BREAKOUT_DOWN):
            spread = TICK_SIZE * random.choice([1, 1, 2, 2, 3])
        elif regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            spread = TICK_SIZE * random.choice([1, 1, 1, 2])
        else:
            spread = TICK_SIZE * random.choice([1, 1, 1, 1, 2])

        half_spread = spread / 2
        bid = round((fair_price - half_spread) / TICK_SIZE) * TICK_SIZE
        ask = bid + spread

        # 決定成交方向: regime + momentum 影響
        buy_prob = 0.5
        if regime == MarketRegime.BREAKOUT_UP:
            buy_prob = 0.75
        elif regime == MarketRegime.BREAKOUT_DOWN:
            buy_prob = 0.25
        elif regime == MarketRegime.TREND_UP:
            buy_prob = 0.62
        elif regime == MarketRegime.TREND_DOWN:
            buy_prob = 0.38
        elif regime == MarketRegime.PULLBACK:
            buy_prob = 0.5 - momentum * 0.15  # 回調反向

        # 動量微調
        buy_prob += momentum * 0.08
        buy_prob = max(0.15, min(0.85, buy_prob))

        is_buy = random.random() < buy_prob
        aggressor = OrderSide.BUY if is_buy else OrderSide.SELL

        # -- Active vs Passive 決定 --
        # 突破: 80% active, 盤整: 40% active (60% passive)
        if regime in (MarketRegime.BREAKOUT_UP, MarketRegime.BREAKOUT_DOWN):
            active_prob = 0.80
        elif regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            active_prob = 0.65
        elif regime == MarketRegime.CONSOLIDATION:
            active_prob = 0.40
        else:
            active_prob = 0.55

        is_active = random.random() < active_prob

        # 成交價格 (根據 active/passive)
        if is_active:
            # Active (aggressor): 穿越 spread
            if is_buy:
                # 買方吃 ask
                trade_price = ask
                # 偶爾穿透 (aggressive 買入多個 tick)
                if random.random() < 0.08 * vol_mult:
                    trade_price += TICK_SIZE * random.randint(1, 2)
            else:
                # 賣方吃 bid
                trade_price = bid
                if random.random() < 0.08 * vol_mult:
                    trade_price -= TICK_SIZE * random.randint(1, 2)
        else:
            # Passive (maker): 掛在 bid/ask, 被打到時成交
            if is_buy:
                # Passive buy: 掛在 bid, 被賣方 aggressor 打到
                trade_price = bid
            else:
                # Passive sell: 掛在 ask, 被買方 aggressor 打到
                trade_price = ask

        trade_price = round(trade_price, 2)

        # 成交量
        size = self._generate_size(regime, vol_mult, session_vol_mult)

        # 更新 depth (簡化)
        self._update_depth(fair_price, spread)

        return Tick(
            timestamp=datetime.now(timezone.utc),  # placeholder, caller sets real time
            price=trade_price,
            size=size,
            aggressor=aggressor,
            bid=round(bid, 2),
            ask=round(ask, 2),
        )

    def _generate_size(self, regime: MarketRegime, vol_mult: float, session_vol_mult: float = 1.0) -> int:
        """
        生成成交量

        參與者類型:
          - Institutional (機構): 大單 20-100, 在 BREAKOUT/TREND 時 30% 概率
          - Algo sweep (算法掃單): 大單 50-200, 在高成交量時段 15% 概率
          - Retail (散戶): 小單, 更偏向 1-2 手

        分布: 大量小單 + 少量大單 (power law, 更偏態)
        """
        # -- 特殊參與者類型 --
        is_breakout_or_trend = regime in (
            MarketRegime.BREAKOUT_UP, MarketRegime.BREAKOUT_DOWN,
            MarketRegime.TREND_UP, MarketRegime.TREND_DOWN,
        )
        high_volume_session = session_vol_mult > 1.5  # open / close

        # Institutional order: BREAKOUT/TREND 時 30% 概率
        if is_breakout_or_trend and random.random() < 0.30:
            base = random.randint(20, 100)
            return max(1, int(base * vol_mult * 0.7))

        # Algo sweep: 高成交量時段 15% 概率
        if high_volume_session and random.random() < 0.15:
            base = random.randint(50, 200)
            return max(1, int(base * vol_mult * 0.5))

        # -- 普通訂單 (更偏態分布) --
        if session_vol_mult < 0.5:
            # 低成交量時段 (lunch/after-hours): 幾乎都是小單
            r = random.random()
            if r < 0.75:
                base = random.randint(1, 2)    # 超小單 75%
            elif r < 0.92:
                base = random.randint(2, 4)    # 小單 17%
            elif r < 0.98:
                base = random.randint(4, 8)    # 中小單 6%
            else:
                base = random.randint(8, 15)   # 中單 2%
        else:
            # 正常/高活躍時段: 偏態分布 (更多 1-2 手)
            r = random.random()
            if r < 0.55:
                base = random.randint(1, 2)    # 超小單 55%
            elif r < 0.75:
                base = random.randint(2, 5)    # 小單 20%
            elif r < 0.88:
                base = random.randint(5, 10)   # 中單 13%
            elif r < 0.95:
                base = random.randint(10, 25)  # 大單 7%
            elif r < 0.98:
                base = random.randint(25, 60)  # 機構單 3%
            else:
                base = random.randint(60, 150) # 巨單 2%

        # 突破/趨勢時放大
        if regime in (MarketRegime.BREAKOUT_UP, MarketRegime.BREAKOUT_DOWN):
            base = int(base * vol_mult * 0.8)
        elif regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
            base = int(base * vol_mult * 0.6)

        return max(1, base)

    def _update_depth(self, fair_price: float, spread: float):
        """更新簡化的 order book depth"""
        self.bid_depth = []
        self.ask_depth = []

        bid_base = round((fair_price - spread / 2) / TICK_SIZE) * TICK_SIZE
        ask_base = bid_base + spread

        for i in range(5):
            bid_price = round(bid_base - i * TICK_SIZE, 2)
            ask_price = round(ask_base + i * TICK_SIZE, 2)
            # 越遠的檔位 size 越大 (做市商掛單)
            bid_size = random.randint(10, 40) + i * random.randint(5, 20)
            ask_size = random.randint(10, 40) + i * random.randint(5, 20)
            self.bid_depth.append((bid_price, bid_size))
            self.ask_depth.append((ask_price, ask_size))

    def get_book_snapshot(self) -> dict:
        return {
            "bids": [{"price": p, "size": s} for p, s in self.bid_depth[:5]],
            "asks": [{"price": p, "size": s} for p, s in self.ask_depth[:5]],
            "spread": round(self.ask_depth[0][0] - self.bid_depth[0][0], 2) if self.bid_depth and self.ask_depth else 0.25,
        }


# -- Candle Builder ---------------------------------------------

@dataclass
class CandleBuilder:
    interval_seconds: int
    start_time: Optional[datetime] = None
    open: float = 0
    high: float = 0
    low: float = 0
    close: float = 0
    volume: int = 0
    tick_count: int = 0

    def add_tick(self, tick: Tick) -> Optional[Candle]:
        ts = tick.timestamp
        epoch = ts.timestamp()
        candle_start_epoch = epoch - (epoch % self.interval_seconds)
        candle_start = datetime.fromtimestamp(candle_start_epoch, tz=timezone.utc)

        if self.start_time is None or candle_start > self.start_time:
            completed = None
            if self.start_time is not None and self.tick_count > 0:
                completed = Candle(
                    timestamp=self.start_time,
                    open=round(self.open, 2),
                    high=round(self.high, 2),
                    low=round(self.low, 2),
                    close=round(self.close, 2),
                    volume=self.volume,
                    symbol="NQ",
                    interval=f"{self.interval_seconds // 60}m" if self.interval_seconds >= 60 else f"{self.interval_seconds}s",
                )
            self.start_time = candle_start
            self.open = tick.price
            self.high = tick.price
            self.low = tick.price
            self.close = tick.price
            self.volume = tick.size
            self.tick_count = 1
            return completed

        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.close = tick.price
        self.volume += tick.size
        self.tick_count += 1
        return None

    def flush(self) -> Optional[Candle]:
        if self.start_time is not None and self.tick_count > 0:
            return Candle(
                timestamp=self.start_time,
                open=round(self.open, 2),
                high=round(self.high, 2),
                low=round(self.low, 2),
                close=round(self.close, 2),
                volume=self.volume,
                symbol="NQ",
                interval=f"{self.interval_seconds // 60}m" if self.interval_seconds >= 60 else f"{self.interval_seconds}s",
            )
        return None


# -- Main Simulator ---------------------------------------------

class MarketSimulator:
    """
    NQ Live Market Simulator v2

    流程:
      PriceEngine.next_price() -> fair_price, regime
      OrderFlowEngine.generate_tick(fair_price) -> Tick
      CandleBuilder.add_tick(tick) -> Candle (when complete)
    """

    def __init__(
        self,
        base_price: float = 23500.0,
        speed: float = 1.0,
        seed: Optional[int] = None,
    ):
        if seed is not None:
            random.seed(seed)

        self.base_price = base_price
        self.speed = speed
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # 模擬時間
        self.sim_time = datetime(2026, 3, 23, 9, 30, 0, tzinfo=timezone.utc)

        # 引擎
        self.price_engine = PriceEngine(base_price)
        self.order_flow = OrderFlowEngine()
        self.builder_1m = CandleBuilder(interval_seconds=60)
        self.builder_5m = CandleBuilder(interval_seconds=300)

        # 數據存儲
        self.ticks: List[Tick] = []
        self.candles_1m: List[Candle] = []
        self.candles_5m: List[Candle] = []
        self.recent_orders: List[dict] = []

        # 統計
        self.total_ticks = 0
        self.total_buy_volume = 0
        self.total_sell_volume = 0

        # Callbacks
        self._on_tick: Optional[Callable] = None
        self._on_candle_1m: Optional[Callable] = None
        self._on_candle_5m: Optional[Callable] = None
        self._on_order: Optional[Callable] = None

    @property
    def current_price(self) -> float:
        return self.price_engine.price

    @property
    def is_running(self) -> bool:
        return self._running

    def set_callbacks(self, on_tick=None, on_candle_1m=None, on_candle_5m=None, on_order=None):
        self._on_tick = on_tick
        self._on_candle_1m = on_candle_1m
        self._on_candle_5m = on_candle_5m
        self._on_order = on_order

    def set_speed(self, multiplier: float):
        self.speed = max(0.1, min(1500.0, multiplier))
        logger.info(f"Simulator speed set to {self.speed}x")

    def _get_session_context(self) -> dict:
        """
        根據模擬時間返回 session 上下文 (成交量/波動率乘數)

        CME NQ Sessions (ET):
          Pre-market:  06:00-09:30  (low vol 0.4x, low volatility 0.6x)
          Open:        09:30-10:00  (high vol 2.5x, high volatility 1.8x)
          Morning:     10:00-11:30  (medium vol 1.2x, med volatility 1.0x)
          Lunch:       11:30-13:00  (low vol 0.5x, low volatility 0.6x)
          Afternoon:   13:00-15:00  (medium vol 1.3x, med volatility 1.1x)
          Close:       15:00-16:00  (high vol 2.0x, high volatility 1.5x)
          After-hours: 16:00-18:00  (low vol 0.3x, low volatility 0.5x)
        """
        h = self.sim_time.hour
        m = self.sim_time.minute
        t = h * 60 + m  # 分鐘數

        if t < 6 * 60:
            # 夜盤 (before 06:00)
            return {"session": "overnight", "vol_mult": 0.3, "volatility_mult": 0.5}
        elif t < 9 * 60 + 30:
            # Pre-market 06:00-09:30
            return {"session": "pre_market", "vol_mult": 0.4, "volatility_mult": 0.6}
        elif t < 10 * 60:
            # Open 09:30-10:00
            return {"session": "open", "vol_mult": 2.5, "volatility_mult": 1.8}
        elif t < 11 * 60 + 30:
            # Morning 10:00-11:30
            return {"session": "morning", "vol_mult": 1.2, "volatility_mult": 1.0}
        elif t < 13 * 60:
            # Lunch 11:30-13:00
            return {"session": "lunch", "vol_mult": 0.5, "volatility_mult": 0.6}
        elif t < 15 * 60:
            # Afternoon 13:00-15:00
            return {"session": "afternoon", "vol_mult": 1.3, "volatility_mult": 1.1}
        elif t < 16 * 60:
            # Close 15:00-16:00
            return {"session": "close", "vol_mult": 2.0, "volatility_mult": 1.5}
        elif t < 18 * 60:
            # After-hours 16:00-18:00
            return {"session": "after_hours", "vol_mult": 0.3, "volatility_mult": 0.5}
        else:
            # Evening (after 18:00)
            return {"session": "evening", "vol_mult": 0.2, "volatility_mult": 0.4}

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"Simulator v2 started @ {self.base_price:.2f}, speed={self.speed}x")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        c1 = self.builder_1m.flush()
        if c1:
            self.candles_1m.append(c1)
        c5 = self.builder_5m.flush()
        if c5:
            self.candles_5m.append(c5)
        logger.info(f"Simulator stopped. Ticks={self.total_ticks}, 1m={len(self.candles_1m)}, 5m={len(self.candles_5m)}")

    async def _run_loop(self):
        """
        主循環

        每次迭代:
          1. PriceEngine 計算新公允價格
          2. 根據 regime 決定本批次 tick 數量
          3. OrderFlowEngine 生成 tick
          4. CandleBuilder 累積
        """
        try:
            while self._running:
                # 0. Session 上下文 (時段乘數)
                session_ctx = self._get_session_context()
                session_vol_mult = session_ctx["vol_mult"]
                session_volatility_mult = session_ctx["volatility_mult"]

                # 暫時調整 PriceEngine 波動率
                original_vol = self.price_engine.current_vol
                self.price_engine.current_vol *= session_volatility_mult

                # 1. 計算新價格
                fair_price, regime, vol_mult = self.price_engine.next_price()

                # 恢復原始波動率 (next_price 內部也會更新, 所以乘回去)
                self.price_engine.current_vol = self.price_engine.current_vol / session_volatility_mult if session_volatility_mult > 0 else original_vol

                # 2. 本批次 tick 數: 盤整少, 突破多, session 調整
                base_count = 6
                if regime in (MarketRegime.BREAKOUT_UP, MarketRegime.BREAKOUT_DOWN):
                    batch_size = int(base_count * vol_mult * session_vol_mult * random.uniform(0.8, 1.4))
                elif regime in (MarketRegime.TREND_UP, MarketRegime.TREND_DOWN):
                    batch_size = int(base_count * vol_mult * session_vol_mult * random.uniform(0.7, 1.2))
                else:
                    batch_size = int(base_count * session_vol_mult * random.uniform(0.5, 1.0))
                batch_size = max(2, batch_size)

                # 3. 生成 tick
                for _ in range(batch_size):
                    if not self._running:
                        break

                    tick = self.order_flow.generate_tick(
                        fair_price=fair_price,
                        regime=regime,
                        vol_mult=vol_mult,
                        momentum=self.price_engine.momentum,
                        session_vol_mult=session_vol_mult,
                    )

                    # 設定模擬時間
                    self.sim_time += timedelta(milliseconds=random.randint(30, 300))
                    tick.timestamp = self.sim_time

                    self._process_tick(tick)

                # 4. 等待 (速度控制)
                wait = 1.0 / self.speed
                await asyncio.sleep(max(0.005, wait))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Simulator error: {e}", exc_info=True)
            self._running = False

    def _process_tick(self, tick: Tick):
        self.total_ticks += 1
        if tick.aggressor == OrderSide.BUY:
            self.total_buy_volume += tick.size
        else:
            self.total_sell_volume += tick.size

        self.ticks.append(tick)
        if len(self.ticks) > 500:
            self.ticks.pop(0)

        # 記錄為 order
        order = {
            "time": tick.timestamp.isoformat(),
            "side": tick.aggressor.value,
            "size": tick.size,
            "fill_price": tick.price,
            "regime": self.price_engine.get_regime_simple(),
            "type": "market",
        }
        self.recent_orders.append(order)
        if len(self.recent_orders) > 50:
            self.recent_orders.pop(0)

        # Tick callback
        if self._on_tick:
            try:
                asyncio.get_event_loop().create_task(self._on_tick(tick))
            except Exception:
                pass

        # 1m candle
        c1 = self.builder_1m.add_tick(tick)
        if c1:
            self.candles_1m.append(c1)
            if self._on_candle_1m:
                try:
                    asyncio.get_event_loop().create_task(self._on_candle_1m(c1))
                except Exception:
                    pass

        # 5m candle
        c5 = self.builder_5m.add_tick(tick)
        if c5:
            self.candles_5m.append(c5)
            if self._on_candle_5m:
                try:
                    asyncio.get_event_loop().create_task(self._on_candle_5m(c5))
                except Exception:
                    pass

    def get_status(self) -> dict:
        pe = self.price_engine
        book = self.order_flow.get_book_snapshot()
        stop_clusters = pe.stop_pool.get_stop_clusters(pe.price)
        return {
            "running": self._running,
            "speed": self.speed,
            "sim_time": self.sim_time.isoformat(),
            "price": round(self.current_price, 2),
            "regime": pe.get_regime_simple(),
            "regime_detail": pe.regime.value,
            "momentum": round(pe.momentum, 3),
            "volatility": round(pe.current_vol, 3),
            "range_center": round(pe.range_center, 2),
            "range_half": round(pe.range_half, 1),
            "buyer_pressure": round(pe.buyer_pressure, 3),
            "seller_pressure": round(pe.seller_pressure, 3),
            "stop_count": len(pe.stop_pool.stops),
            "cascade_active": pe._stop_cascade_active,
            "total_ticks": self.total_ticks,
            "buy_volume": self.total_buy_volume,
            "sell_volume": self.total_sell_volume,
            "candles_1m": len(self.candles_1m),
            "candles_5m": len(self.candles_5m),
            "order_book": book,
        }

    def get_current_candle_1m(self) -> Optional[dict]:
        b = self.builder_1m
        if b.start_time and b.tick_count > 0:
            return {
                "time": b.start_time.isoformat(),
                "open": round(b.open, 2),
                "high": round(b.high, 2),
                "low": round(b.low, 2),
                "close": round(b.close, 2),
                "volume": b.volume,
                "tick_count": b.tick_count,
            }
        return None

    def get_current_candle_5m(self) -> Optional[dict]:
        b = self.builder_5m
        if b.start_time and b.tick_count > 0:
            return {
                "time": b.start_time.isoformat(),
                "open": round(b.open, 2),
                "high": round(b.high, 2),
                "low": round(b.low, 2),
                "close": round(b.close, 2),
                "volume": b.volume,
                "tick_count": b.tick_count,
            }
        return None


# -- Singleton --------------------------------------------------

_simulator: Optional[MarketSimulator] = None


def get_simulator() -> Optional[MarketSimulator]:
    return _simulator


def create_simulator(
    base_price: float = 23500.0,
    speed: float = 1.0,
    seed: Optional[int] = None,
) -> MarketSimulator:
    global _simulator
    _simulator = MarketSimulator(base_price=base_price, speed=speed, seed=seed)
    return _simulator
