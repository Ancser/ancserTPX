# ============================================================
# 文件: backend/strategy/ml_trend.py
# 狀態: v1.0.6 (ML Trend — Value Area mean reversion with ML scoring)
# 核心邏輯:
#   - 滾動 Volume Profile 持續計算 VAL/VAH/POC
#   - 價格觸及 VAL → 做多至 POC (區間50%)
#   - 價格觸及 VAH → 做空至 POC (區間50%)
#   - 特徵: VA位置 + 整點/半點時間 + RSI + ATR/趨勢/效率
# 關聯文件:
#   → backend/backtest/ml_trend_backtest.py  (回測引擎)
#   → scripts/train_sweep_ml_trend.py        (訓練 + 參數掃描)
# ============================================================
"""ML Trend: Value Area mean reversion with ML scoring.

Rolling volume profile computes VAL/VAH/POC continuously.
  - LONG when price touches VAL (mean reversion up to POC / 50% of range).
  - SHORT when price touches VAH (mean reversion down to POC / 50% of range).

Time features give higher weight near hour / half-hour marks.
RSI serves as an auxiliary momentum/overbought-oversold indicator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from backend.db.models import Candle, Direction


# ═══════════════════════════════════════════════════════════
# Feature schema — append-only, name-keyed (same convention
# as confluence_features.py so old model JSONs stay valid).
# ═══════════════════════════════════════════════════════════

ML_TREND_FEATURE_NAMES: tuple = (
    # Value Area position (6)
    "va_position_pct",        # (close − VAL) / (VAH − VAL); 0 = at VAL, 1 = at VAH
    "dist_to_val_ticks",      # (close − VAL) / tick; negative = below VAL
    "dist_to_vah_ticks",      # (VAH − close) / tick; negative = above VAH
    "dist_to_poc_ticks",      # (close − POC) / tick; signed
    "va_width_ticks",         # (VAH − VAL) / tick
    "price_vs_poc",           # +1 above POC, −1 below, 0 at
    # Time (4) — 整點/半點 proximity as key features
    "hour_proximity",         # 1 at :00, 0 at :30 (linear decay)
    "half_hour_proximity",    # 1 at :00/:30, 0 at :15/:45
    "hour_sin",               # sin(2π · hour/24) — cyclical encoding
    "hour_cos",               # cos(2π · hour/24)
    # RSI (3) — auxiliary momentum indicator
    "rsi",                    # 14-period RSI normalised to [−1, +1]
    "rsi_oversold",           # 1 if RSI < 30
    "rsi_overbought",         # 1 if RSI > 70
    # Context (4) — market condition
    "atr_ticks",              # 14-period ATR in ticks
    "atr_to_va_ratio",        # ATR / VA width (volatility vs range)
    "trend_drift_R",          # recent drift in R-units (risk-normalised, signed)
    "efficiency_ratio",       # Kaufman ER: 0 = chop, 1 = clean trend
    # Signal geometry (3)
    "is_long",                # 1 for BUY, 0 for SELL
    "risk_ticks",             # |entry − SL| in ticks
    "rr",                     # |TP − entry| / |entry − SL|
)

ML_TREND_DEAD_FEATURES: frozenset = frozenset()


# ═══════════════════════════════════════════════════════════
# Technical indicators
# ═══════════════════════════════════════════════════════════

def compute_rsi(closes: List[float], period: int = 14) -> float:
    """Wilder's RSI with exponential smoothing."""
    if len(closes) < period + 1:
        return 50.0  # neutral default
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    # seed with simple averages
    avg_gain = sum(max(0.0, c) for c in changes[:period]) / period
    avg_loss = sum(max(0.0, -c) for c in changes[:period]) / period
    # Wilder smoothing
    for c in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(0.0, c)) / period
        avg_loss = (avg_loss * (period - 1) + max(0.0, -c)) / period
    if avg_loss < 1e-10:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_atr(candles: List[Candle], period: int = 14) -> float:
    """True-Range based ATR."""
    if len(candles) < 2:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(candles)):
        c = candles[i]
        pc = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - pc), abs(c.low - pc))
        trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


# ═══════════════════════════════════════════════════════════
# Config & Signal
# ═══════════════════════════════════════════════════════════

@dataclass
class MLTrendConfig:
    """Tuneable parameters for one backtest run."""
    lookback: int = 120            # rolling VP window (1m bars)
    band_ticks: float = 4.0        # proximity threshold to VAL/VAH (ticks)
    sl_buffer_ticks: float = 4.0   # SL beyond 100% range (ticks)
    rsi_period: int = 14
    atr_period: int = 14
    trend_lookback: int = 30
    tick_size: float = 0.25
    tp_mode: str = "poc"           # "poc" = TP at POC; "rr" = fixed RR from SL
    rr: float = 2.0                # only used when tp_mode == "rr"
    max_risk_ticks: float = 80.0
    min_risk_ticks: float = 4.0


@dataclass
class MLTrendSignal:
    timestamp: datetime
    direction: Direction
    entry_price: float
    sl_price: float
    tp_price: float
    features: Dict[str, float]
    score: float = 0.0
    prob: float = 0.5
    reason: str = ""
    bar_index: int = 0


# ═══════════════════════════════════════════════════════════
# Feature extraction
# ═══════════════════════════════════════════════════════════

def extract_features(
    candle: Candle,
    val: float,
    vah: float,
    poc: float,
    recent_candles: List[Candle],
    direction: Direction,
    entry: float,
    sl: float,
    tp: float,
    tick_size: float = 0.25,
) -> Dict[str, float]:
    """Compute the interpretable feature dict for one ML Trend signal candidate."""
    price = candle.close
    va_width = vah - val

    # ── Value Area position ──
    va_pos = ((price - val) / va_width) if va_width > 0 else 0.5
    dist_val = (price - val) / tick_size
    dist_vah = (vah - price) / tick_size
    dist_poc = (price - poc) / tick_size
    va_width_t = va_width / tick_size
    price_vs = 1.0 if price > poc + tick_size else (-1.0 if price < poc - tick_size else 0.0)

    # ── Time features — 整點/半點 proximity ──
    minute = candle.timestamp.minute
    min_from_hour = min(minute, 60 - minute)
    hour_prox = 1.0 - min_from_hour / 30.0
    min_from_half = min(minute % 30, 30 - minute % 30)
    half_prox = 1.0 - min_from_half / 15.0
    hour_frac = candle.timestamp.hour + minute / 60.0
    h_sin = math.sin(2.0 * math.pi * hour_frac / 24.0)
    h_cos = math.cos(2.0 * math.pi * hour_frac / 24.0)

    # ── RSI ──
    closes = [c.close for c in recent_candles]
    rsi_raw = compute_rsi(closes, 14) if len(closes) >= 16 else 50.0
    rsi_norm = (rsi_raw - 50.0) / 50.0  # [−1, +1]
    rsi_os = 1.0 if rsi_raw < 30.0 else 0.0
    rsi_ob = 1.0 if rsi_raw > 70.0 else 0.0

    # ── ATR ──
    atr = compute_atr(recent_candles, 14)
    atr_t = atr / tick_size
    atr_va = (atr / va_width) if va_width > 0 else 0.0

    # ── Trend drift + efficiency ──
    risk = abs(entry - sl)
    risk_safe = risk if risk > 1e-9 else tick_size
    lb = min(30, len(closes) - 1)
    drift = (closes[-1] - closes[max(0, len(closes) - lb - 1)]) if lb > 0 else 0.0
    is_buy = direction == Direction.BUY
    aligned = drift if is_buy else -drift
    drift_r = aligned / risk_safe

    if lb > 1:
        net = abs(closes[-1] - closes[-lb - 1])
        path = sum(abs(closes[k] - closes[k - 1]) for k in range(len(closes) - lb, len(closes)))
        eff = (net / path) if path > 1e-9 else 0.0
    else:
        eff = 0.0

    # ── Signal geometry ──
    risk_t = risk / tick_size
    reward = abs(tp - entry)
    rr_val = reward / risk_safe

    return {
        "va_position_pct": float(va_pos),
        "dist_to_val_ticks": float(dist_val),
        "dist_to_vah_ticks": float(dist_vah),
        "dist_to_poc_ticks": float(dist_poc),
        "va_width_ticks": float(va_width_t),
        "price_vs_poc": float(price_vs),
        "hour_proximity": float(hour_prox),
        "half_hour_proximity": float(half_prox),
        "hour_sin": float(h_sin),
        "hour_cos": float(h_cos),
        "rsi": float(rsi_norm),
        "rsi_oversold": float(rsi_os),
        "rsi_overbought": float(rsi_ob),
        "atr_ticks": float(atr_t),
        "atr_to_va_ratio": float(atr_va),
        "trend_drift_R": float(drift_r),
        "efficiency_ratio": float(eff),
        "is_long": 1.0 if is_buy else 0.0,
        "risk_ticks": float(risk_t),
        "rr": float(rr_val),
    }


def features_to_vector(feats: Dict[str, float]) -> List[float]:
    """Stable ordering matching ML_TREND_FEATURE_NAMES."""
    return [feats.get(name, 0.0) for name in ML_TREND_FEATURE_NAMES]
