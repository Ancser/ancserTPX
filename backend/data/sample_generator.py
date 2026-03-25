# ============================================================
# 文件: backend/data/sample_generator.py
# 狀態: 已完成
# 問題: 無
# 關聯文件:
#   <- backend/api/routes.py     (生成模擬數據供回測)
#   -> backend/db/models.py      (Candle)
# 函數結構:
#   - generate_nq_sample(days, interval_minutes, base_price) -> List[Candle]
#   - _generate_day(date, interval, base, volatility) -> List[Candle]
#   - _create_consolidation_block(candles, start_idx, length, center, range_pts) -> None
# ============================================================
"""
NQ 模擬數據生成器

用途: 週末或 API 不可用時, 生成逼真的 NQ 5 分鐘 K 線數據用於回測測試。

數據特徵:
  - 模擬真實 NQ 日內走勢 (開盤波動 -> 盤整 -> 突破 -> 新盤整)
  - 包含 2-3 個盤整區間 per day
  - 成交量模式: 開盤高 -> 午間低 -> 尾盤回升
  - 價格在 20000-22000 區間波動
"""

import random
import math
from datetime import datetime, timedelta, timezone
from typing import List

from backend.db.models import Candle


def generate_nq_sample(
    days: int = 5,
    interval_minutes: int = 5,
    base_price: float = 21200.0,
    seed: int = 42,
) -> List[Candle]:
    """
    生成多天的 NQ 模擬 K 線數據

    Args:
        days: 交易日數量
        interval_minutes: K 線間隔 (分鐘)
        base_price: 起始價格
        seed: 隨機種子 (固定結果可重現)

    Returns:
        List[Candle]: 模擬 K 線列表
    """
    random.seed(seed)
    all_candles: List[Candle] = []
    current_price = base_price

    # 從最近的週一開始生成
    start_date = datetime(2026, 3, 16, tzinfo=timezone.utc)  # Monday

    for day_idx in range(days):
        date = start_date + timedelta(days=day_idx)
        # 跳過週末
        if date.weekday() >= 5:
            days += 1
            continue

        day_candles = _generate_day(
            date=date,
            interval_minutes=interval_minutes,
            base_price=current_price,
            day_idx=day_idx,
        )

        if day_candles:
            all_candles.extend(day_candles)
            current_price = day_candles[-1].close

    return all_candles


def _generate_day(
    date: datetime,
    interval_minutes: int,
    base_price: float,
    day_idx: int,
) -> List[Candle]:
    """
    生成單日 K 線數據

    CME NQ 交易時段: 6:00 PM - 5:00 PM CT (next day)
    簡化為 RTH: 8:30 AM - 3:00 PM CT = 9:30 AM - 4:00 PM ET

    模擬走勢模式:
      Phase 1 (09:30-10:30): 開盤波動, 形成第一個盤整區
      Phase 2 (10:30-12:00): 突破第一個區間, 形成第二個盤整區
      Phase 3 (12:00-13:30): 午間低波動盤整
      Phase 4 (13:30-15:00): 下午走勢, 可能再次突破
      Phase 5 (15:00-16:00): 收盤前波動收斂
    """
    candles: List[Candle] = []

    # RTH session: 9:30 - 16:00 ET
    session_start = date.replace(hour=9, minute=30, second=0)
    session_end = date.replace(hour=16, minute=0, second=0)
    bars_per_session = int((session_end - session_start).total_seconds() / 60 / interval_minutes)

    price = base_price
    # 日內趨勢方向 (隨機)
    day_trend = random.choice([-1, 1]) * random.uniform(10, 40)

    # 定義盤整區間參數
    zones = _plan_zones(bars_per_session, base_price, day_trend, day_idx)

    for bar_idx in range(bars_per_session):
        bar_time = session_start + timedelta(minutes=bar_idx * interval_minutes)
        progress = bar_idx / bars_per_session  # 0.0 to 1.0

        # 基礎波動率 (日內 U 形)
        hour = 9.5 + progress * 6.5
        if hour < 10.5:
            volatility = 4.0  # 開盤高波動
        elif hour < 12.0:
            volatility = 2.5
        elif hour < 13.5:
            volatility = 1.5  # 午間低波動
        elif hour < 15.0:
            volatility = 3.0  # 下午回升
        else:
            volatility = 2.0  # 收盤

        # 檢查是否在盤整區間內
        in_zone = False
        zone_center = price
        for z_start, z_end, z_center, z_range in zones:
            if z_start <= bar_idx < z_end:
                in_zone = True
                zone_center = z_center
                volatility *= 0.5  # 盤整區間內波動減半
                break

        # 生成價格變動
        trend_component = day_trend / bars_per_session
        noise = random.gauss(0, volatility)

        if in_zone:
            # 盤整: 向中心回歸
            reversion = (zone_center - price) * 0.15
            price += reversion + noise * 0.6
        else:
            # 非盤整: 趨勢 + 噪音
            price += trend_component + noise

        # 生成 OHLC
        open_price = price
        intra_moves = [random.gauss(0, volatility * 0.4) for _ in range(4)]
        prices = [open_price + m for m in intra_moves]
        high = max(open_price, max(prices)) + abs(random.gauss(0, volatility * 0.3))
        low = min(open_price, min(prices)) - abs(random.gauss(0, volatility * 0.3))
        close = open_price + random.gauss(0, volatility * 0.5)

        # 確保 OHLC 合理
        high = max(high, open_price, close)
        low = min(low, open_price, close)

        price = close  # 下一根的起始

        # 成交量 (日內 U 形 + 突破時放大)
        base_vol = 800
        vol_pattern = 1.0 + 0.8 * math.cos(math.pi * (progress - 0.5))
        vol_noise = random.uniform(0.7, 1.3)

        if not in_zone and bar_idx > 2:
            # 突破時成交量放大
            vol_pattern *= 1.8

        volume = int(base_vol * vol_pattern * vol_noise)

        candles.append(Candle(
            timestamp=bar_time,
            open=round(open_price, 2),
            high=round(high, 2),
            low=round(low, 2),
            close=round(close, 2),
            volume=volume,
            symbol="NQ",
            interval=f"{interval_minutes}m",
        ))

    return candles


def _plan_zones(
    total_bars: int,
    base_price: float,
    day_trend: float,
    day_idx: int,
) -> list:
    """
    規劃一天中的盤整區間位置

    Returns:
        List of (start_bar, end_bar, center_price, range_pts)
    """
    zones = []
    random.seed(42 + day_idx * 7)

    # Zone 1: 開盤後盤整 (bar 3 to 12-18)
    z1_start = 3
    z1_length = random.randint(8, 15)
    z1_center = base_price + random.gauss(0, 5)
    z1_range = random.uniform(8, 15)
    zones.append((z1_start, z1_start + z1_length, z1_center, z1_range))

    # Zone 2: 突破後新盤整 (bar 20-30 to 35-50)
    z2_start = z1_start + z1_length + random.randint(4, 8)
    z2_length = random.randint(10, 20)
    z2_center = z1_center + day_trend * 0.4 + random.gauss(0, 3)
    z2_range = random.uniform(8, 15)
    zones.append((z2_start, z2_start + z2_length, z2_center, z2_range))

    # Zone 3: 下午盤整 (偶爾出現)
    if random.random() > 0.3:
        z3_start = z2_start + z2_length + random.randint(5, 10)
        z3_length = random.randint(8, 15)
        if z3_start + z3_length < total_bars - 5:
            z3_center = z2_center + day_trend * 0.3 + random.gauss(0, 3)
            z3_range = random.uniform(6, 12)
            zones.append((z3_start, z3_start + z3_length, z3_center, z3_range))

    return zones
