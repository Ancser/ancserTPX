# ============================================================
# 文件: backend/live/ml_consolidation_v2_live.py
# 狀態: v1.0.6 (ML Consolidation V2 — LIVE evaluator)
# 核心邏輯:
#   - 滾動 Volume Profile 計算 VAL/VAH/POC
#   - 價格觸及 VAL → LONG (均值回歸)
#   - 價格觸及 VAH → SHORT (均值回歸)
#   - SL = 100% range 邊界 + buffer ticks
#   - TP = POC (poc 模式) 或 entry +/- risk*RR (rr 模式)
#   - Market order entry at bar close
# 關聯文件:
#   ← backend/strategy/ml_trend.py         (MLTrendConfig, extract_features, compute_rsi, compute_atr)
#   ← backend/strategy/volume_profile.py   (VolumeProfileCalculator)
#   ← backend/strategy/session_filter.py   (is_allowed_session)
#   → backend/live/engine.py               (LiveTradingEngine ml_consolidation_v2 模式)
# ============================================================
"""ML Consolidation V2: Value Area mean reversion with rolling Volume Profile.

Live evaluator — same pattern as ``ConfluenceLiveEvaluator``.  Keeps a rolling
window of 1m candles, recomputes VP every 5 bars, and emits a market-order
``TradeSignal`` when bar close touches VAL (LONG) or VAH (SHORT) within
``band_ticks``.

Optional ML scorer loaded from ``data/models/ml_trend_scorer.json`` gates
low-quality setups when ``min_score > 0``.
"""

from __future__ import annotations

import json
import math
import logging
from pathlib import Path
from typing import Dict, List, Optional

from backend.db.models import (
    Candle, TradeSignal, Direction, StrategyType, get_tick_size, get_point_value,
)
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.strategy.ml_trend import (
    MLTrendConfig, extract_features, compute_rsi, compute_atr,
)
from backend.strategy.session_filter import is_allowed_session

log = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────
_SCORER_PATH = Path("data/models/ml_trend_scorer.json")
_RECENT_WINDOW = 45          # 供 RSI / ATR / drift 特徵使用
_VP_RECALC_INTERVAL = 5      # 每 N 根 K 線重算 VP
_MIN_VA_WIDTH_TICKS = 4      # VA 太窄時不交易


# ═══════════════════════════════════════════════════════════
# Simple logistic scorer (weights + bias from JSON)
# ═══════════════════════════════════════════════════════════

class _LinearScorer:
    """Minimal linear scorer: score = sum(w_i * x_i) + bias."""

    def __init__(self, weights: Dict[str, float], bias: float):
        self.weights = weights
        self.bias = bias

    def score(self, features: Dict[str, float]) -> float:
        s = self.bias
        for name, w in self.weights.items():
            s += w * features.get(name, 0.0)
        return s

    @staticmethod
    def prob(score: float) -> float:
        return 1.0 / (1.0 + math.exp(-score))

    def explain(self, features: Dict[str, float]) -> List[dict]:
        """Per-feature contribution breakdown for explainability."""
        rows = []
        for name, w in self.weights.items():
            val = features.get(name, 0.0)
            rows.append({"feature": name, "weight": round(w, 4),
                         "value": round(val, 4), "contrib": round(w * val, 4)})
        rows.sort(key=lambda r: -abs(r["contrib"]))
        return rows


def _load_scorer() -> Optional[_LinearScorer]:
    """嘗試從 JSON 載入 scorer；失敗則回傳 None（不阻止策略運行）。"""
    path = _SCORER_PATH
    if not path.exists():
        log.info("ml_consol_v2: scorer JSON 不存在 (%s)，不用 ML gate", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        weights = data.get("weights", {})
        bias = float(data.get("bias", 0.0))
        log.info("ml_consol_v2: 已載入 scorer (%d 個特徵, bias=%.4f)", len(weights), bias)
        return _LinearScorer(weights, bias)
    except Exception as exc:
        log.warning("ml_consol_v2: scorer 載入失敗: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════
# Live evaluator
# ═══════════════════════════════════════════════════════════

class MLConsolidationV2LiveEvaluator:
    """Per-bar Value Area mean reversion signal generator for the live engine."""

    def __init__(
        self,
        contract_id: str,
        lookback: int = 30,
        band_ticks: float = 2.0,
        sl_buffer_ticks: float = 4.0,
        tp_mode: str = "rr",
        rr: float = 4.0,
        min_score: float = 0.0,
        allowed_sessions=None,
    ):
        self.contract_id = contract_id
        self.tick_size = get_tick_size(contract_id)
        self.lookback = max(10, int(lookback))
        self.band_ticks = band_ticks
        self.sl_buffer_ticks = sl_buffer_ticks
        self.tp_mode = tp_mode          # "poc" | "rr"
        self.rr = rr
        self.min_score = min_score
        self.allowed_sessions = allowed_sessions

        self.vp_calc = VolumeProfileCalculator(
            tick_size=self.tick_size, value_area_pct=0.80,
        )
        self._window: List[Candle] = []     # 滾動 VP 計算用
        self._recent: List[Candle] = []     # 特徵提取用 (last 45)
        self._bar_count = 0

        # VP 結果暫存
        self.val: Optional[float] = None
        self.vah: Optional[float] = None
        self.poc: Optional[float] = None
        self.low_100: Optional[float] = None
        self.high_100: Optional[float] = None

        # ML scorer
        self.scorer = _load_scorer()

    # ── feeding ──────────────────────────────────────────

    def update(self, candle: Candle) -> None:
        """Feed one completed 1m candle."""
        self._window.append(candle)
        if len(self._window) > self.lookback:
            self._window = self._window[-self.lookback:]

        self._recent.append(candle)
        if len(self._recent) > _RECENT_WINDOW:
            self._recent = self._recent[-_RECENT_WINDOW:]

        self._bar_count += 1
        if self._bar_count % _VP_RECALC_INTERVAL == 0 or self.val is None:
            self._recompute_vp()

    def warmup(self, candles: List[Candle]) -> None:
        """Replay historical candles before going live."""
        for c in sorted(candles, key=lambda x: x.timestamp):
            self.update(c)

    def _recompute_vp(self) -> None:
        """Recompute VP from the rolling window."""
        if len(self._window) < 10:
            return
        try:
            vp = self.vp_calc.calculate(self._window)
            self.val = vp.val
            self.vah = vp.vah
            self.poc = vp.poc
            self.low_100 = vp.low_100
            self.high_100 = vp.high_100
        except Exception as exc:
            log.debug("ml_consol_v2: VP 計算失敗: %s", exc)

    # ── evaluation ───────────────────────────────────────

    def evaluate(self, candle: Candle) -> Optional[TradeSignal]:
        """Return a market-order TradeSignal if bar close touches VA boundary."""
        sig = self._core_signal(candle)
        if sig is None:
            return None
        direction, entry, sl, tp, features = sig

        # ML gate
        if self.scorer and self.min_score > 0:
            score = self.scorer.score(features)
            if score < self.min_score:
                return None

        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=round(tp, 2),
            zone_id="ml_consol_v2",
            zone_source="ml_consolidation_v2",
            reason=self._build_reason(direction, entry, sl, tp),
            timestamp=candle.timestamp,
            order_type="market",
        )

    def explain(self, candle: Candle) -> Optional[dict]:
        """Full explainable payload (for logging / chart overlay)."""
        sig = self._core_signal(candle)
        if sig is None:
            return None
        direction, entry, sl, tp, features = sig

        score = 0.0
        prob = 0.5
        explain_rows: List[dict] = []
        if self.scorer:
            score = self.scorer.score(features)
            prob = _LinearScorer.prob(score)
            explain_rows = self.scorer.explain(features)

        return {
            "time": candle.timestamp.isoformat() if candle.timestamp else "",
            "direction": direction.value,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "score": round(score, 4),
            "prob": round(prob, 4),
            "tp_mode": self.tp_mode,
            "rr": round(abs(tp - entry) / max(abs(entry - sl), 1e-9), 2),
            "val": round(self.val, 2) if self.val else None,
            "vah": round(self.vah, 2) if self.vah else None,
            "poc": round(self.poc, 2) if self.poc else None,
            "low_100": round(self.low_100, 2) if self.low_100 else None,
            "high_100": round(self.high_100, 2) if self.high_100 else None,
            "features": {k: round(v, 4) for k, v in features.items()},
            "explain": explain_rows,
            "reason": self._build_reason(direction, entry, sl, tp),
        }

    # ── internals ────────────────────────────────────────

    def _core_signal(self, candle: Candle):
        """Shared signal logic for evaluate() and explain().

        Returns (direction, entry, sl, tp, features) or None.
        """
        # VP 準備就緒?
        if self.val is None or self.vah is None or self.poc is None:
            return None
        if self.low_100 is None or self.high_100 is None:
            return None

        # VA 太窄不交易
        va_width = self.vah - self.val
        if va_width < _MIN_VA_WIDTH_TICKS * self.tick_size:
            return None

        # Session filter
        if not is_allowed_session(candle.timestamp, self.allowed_sessions):
            return None

        price = candle.close
        band = self.band_ticks * self.tick_size
        buf = self.sl_buffer_ticks * self.tick_size
        direction: Optional[Direction] = None
        sl: float = 0.0
        tp: float = 0.0
        entry = float(price)

        # LONG: price 觸及 VAL（close <= val + band）
        if price <= self.val + band:
            direction = Direction.BUY
            sl = self.low_100 - buf
            risk = entry - sl
            if risk <= 0:
                return None
            if self.tp_mode == "poc":
                tp = self.poc
            else:
                tp = entry + risk * self.rr

        # SHORT: price 觸及 VAH（close >= vah - band）
        elif price >= self.vah - band:
            direction = Direction.SELL
            sl = self.high_100 + buf
            risk = sl - entry
            if risk <= 0:
                return None
            if self.tp_mode == "poc":
                tp = self.poc
            else:
                tp = entry - risk * self.rr
        else:
            return None

        # Build features
        features = extract_features(
            candle=candle,
            val=self.val,
            vah=self.vah,
            poc=self.poc,
            recent_candles=self._recent,
            direction=direction,
            entry=entry,
            sl=sl,
            tp=tp,
            tick_size=self.tick_size,
        )

        return direction, entry, sl, tp, features

    @staticmethod
    def _build_reason(direction: Direction, entry: float, sl: float, tp: float) -> str:
        side = "LONG" if direction == Direction.BUY else "SHORT"
        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 1e-9 else 0.0
        return f"ML-Consol-V2 {side} entry={entry:.2f} SL={sl:.2f} TP={tp:.2f} RR={rr:.1f}"
