# ============================================================
# 文件: backend/live/confluence_live.py
# 狀態: v1.0.6 (explainable confluence — LIVE evaluator)
# 用途: 即時引擎用的多時間框加權匯流評估器。與回測完全一致:
#       同一套 per-TF ClockBucketZoneDetector + 同一個 trained scorer +
#       evaluate_confluence_scored → live == backtest，可解釋、可複刻。
# 關聯文件:
#   ← backend/strategy/confluence.py          (ConfluenceConfig, evaluate_confluence_scored)
#   ← backend/strategy/confluence_scorer.py   (ConfluenceScorer.load / heuristic)
#   ← backend/strategy/consolidation.py       (ClockBucketZoneDetector, AREA_TIMEFRAME_MINUTES)
#   → backend/live/engine.py                  (LiveTradingEngine confluence 模式)
# ============================================================
"""Live confluence signal evaluator.

This is the live-trading counterpart of ``ConfluenceBacktester``. It keeps one
``ClockBucketZoneDetector`` per timeframe, fed the same completed 1m (or 5m)
candles the engine already processes, and every bar it:

  1. snapshots ``zones_by_tf`` (recent completed zones per TF),
  2. runs ``evaluate_confluence_scored`` with the trained scorer (both modes),
  3. gates by ``min_score`` (derived from a win-probability threshold),
  4. returns the highest-scoring action as a ``TradeSignal`` carrying the full
     explainable reason (weights · features) + meta so the live log and chart
     justify every order exactly like the out-of-sample report.

Because the detectors, depth, config and scorer are identical to the backtest,
a live decision is reproducible: replay the same candles through the backtester
and you get the same signal.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional

from backend.db.models import (
    Candle, TradeSignal, Direction, StrategyType, get_tick_size,
)
from backend.strategy.consolidation import (
    ClockBucketZoneDetector, timeframes_for_base,
)
from backend.strategy.confluence import (
    ConfluenceConfig, MAX_RECENCY_DEPTH, evaluate_confluence_scored, gate_signals,
    snapshot_zones_by_tf,
)
from backend.strategy.confluence_scorer import (
    ConfluenceScorer, default_scorer_path, resolve_scorer,
)
from backend.strategy.confluence_features import CONTEXT_WINDOW


# Backwards-compatible alias — canonical path now lives in confluence_scorer.py.
confluence_scorer_path = default_scorer_path


class ConfluenceLiveEvaluator:
    """Per-bar confluence signal generator for the live engine."""

    def __init__(
        self,
        contract_id: str,
        band_ticks: float = 4.0,
        min_distinct_tf: int = 2,
        rr: float = 3.0,
        base_minutes: int = 1,
        min_prob: float = 0.0,
        ev_floor: Optional[float] = None,
        rr_grid: Optional[List[float]] = None,
        use_scorer: bool = True,
        scorer_path: Optional[str] = None,
        enable_breakout: bool = False,
        max_risk_ticks: Optional[int] = None,
    ):
        self.contract_id = contract_id
        self.tick_size = get_tick_size(contract_id)
        self.base_minutes = max(1, int(base_minutes or 1))

        # TFs strictly larger than the base candle (5m base drops the 5m TF),
        # single source of truth shared with backtest / training.
        self.timeframes = timeframes_for_base(self.base_minutes)

        # signal-level config (auto = score momentum + reversion, take best)
        self.cfg = ConfluenceConfig(
            band_ticks=band_ticks, min_distinct_tf=min_distinct_tf, rr=rr,
        )
        self.cfg.direction_mode = "auto"
        self.cfg.tick_size = self.tick_size
        # EV-priority gate (option C): when set, admit positive-EV setups
        # instead of the raw win-prob threshold. None = legacy prob gate.
        self.cfg.ev_floor = ev_floor
        self.cfg.rr_grid = None
        self.cfg.enable_breakout = bool(enable_breakout)
        self.cfg.max_risk_ticks = max_risk_ticks
        self.modes = self.cfg.auto_modes()

        # probability gate -> raw logit (score) threshold
        self.min_score = 0.0
        if min_prob and 0.0 < min_prob < 1.0:
            self.min_score = math.log(min_prob / (1.0 - min_prob))

        self.scorer = resolve_scorer(use_scorer, None, scorer_path)
        self.scorer_source = self.scorer.source_name()

        # one detector per timeframe — same params as ConfluenceBacktester
        self.detectors: Dict[str, ClockBucketZoneDetector] = {
            tf: ClockBucketZoneDetector(
                area_timeframe=tf,
                value_area_pct=0.80,
                tick_size=self.tick_size,
                max_recent=self.cfg.max_recency_depth + 2,
                recalc_active_each_bar=False,
            )
            for tf in self.timeframes
        }
        self._warmed = False
        # trailing raw-candle window for the v1.0.6 context features (atr_R /
        # trend_R). Same window the backtester / trainers feed → live==backtest.
        self._recent: List[Candle] = []

    # ── feeding ──────────────────────────────────────────

    def update(self, candle: Candle) -> None:
        """Feed one completed candle to every timeframe detector."""
        for det in self.detectors.values():
            det.update(candle)
        self._recent.append(candle)
        if len(self._recent) > CONTEXT_WINDOW:
            self._recent = self._recent[-CONTEXT_WINDOW:]

    def warmup(self, candles: List[Candle]) -> None:
        """Replay historical candles through the detectors before going live."""
        for c in sorted(candles, key=lambda x: x.timestamp):
            self.update(c)
        self._warmed = True

    # ── snapshot ─────────────────────────────────────────

    def zones_by_tf(self) -> Dict[str, list]:
        return snapshot_zones_by_tf(self.detectors, self.cfg.max_recency_depth + 1)

    def level_universe(self, current_price: Optional[float]) -> List[dict]:
        """Every recent completed zone per timeframe (4h, 4h-1, 4h-2, … 2h, 2h-1,
        …) with its confluence WEIGHT and DISTANCE to the current price — the full
        explainable input the clusterer/scorer sees, for the chart overlay.

        weight = tf_weight(timeframe) × recency_decay**|recency|   (newer + bigger
        TF weighs more). This is exactly the per-level weight used in clustering,
        so the overlay shows the real numbers, not a cosmetic proxy.
        """
        from backend.strategy.confluence import recency_label
        from backend.strategy.consolidation import AREA_TIMEFRAME_MINUTES

        cfg = self.cfg
        depth = cfg.max_recency_depth
        tick = self.tick_size or 0.25
        rows: List[dict] = []
        for tf, zones in self.zones_by_tf().items():
            if not zones:
                continue
            tf_w = cfg.tf_weight.get(tf, 1.0)
            tf_min = AREA_TIMEFRAME_MINUTES.get(tf, 0)
            recent = zones[-(depth + 1):]
            n = len(recent)
            for idx, z in enumerate(recent):
                recency = -(n - 1 - idx)
                if recency < -depth:
                    continue
                w = tf_w * cfg.recency_weight(recency)
                val, vah = z.val_80, z.vah_80
                d_val = (val - current_price) / tick if current_price else None
                d_vah = (vah - current_price) / tick if current_price else None
                # nearest 80% edge distance (signed ticks): + above price, - below
                near = None
                if d_val is not None:
                    near = d_val if abs(d_val) <= abs(d_vah) else d_vah
                rows.append({
                    "label": recency_label(tf, recency),
                    "tf": tf, "tf_min": tf_min, "recency": recency,
                    "weight": round(w, 2),
                    "val": round(val, 2), "vah": round(vah, 2), "poc": round(z.poc, 2),
                    "dist_ticks": round(near, 1) if near is not None else None,
                    "dist_val_ticks": round(d_val, 1) if d_val is not None else None,
                    "dist_vah_ticks": round(d_vah, 1) if d_vah is not None else None,
                })
        # 4h first (largest TF), newest recency first within each TF
        rows.sort(key=lambda r: (-r["tf_min"], -r["recency"]))
        return rows

    # ── evaluation ───────────────────────────────────────

    def evaluate(self, candle: Candle) -> Optional[TradeSignal]:
        """Return the best confluence signal at this bar (>= min_score) or None.

        The returned TradeSignal is a one-shot LIMIT order: entry at the cluster
        price, structural SL, RR-based TP — fed through the engine's existing
        order path so brackets/session-locks/wait-timeout all apply unchanged.
        """
        zbt = self.zones_by_tf()
        if len(zbt) < self.cfg.min_distinct_tf:
            return None
        signals = evaluate_confluence_scored(
            zbt, candle.close, self.cfg, self.scorer, modes=self.modes,
            recent_candles=self._recent,
        )
        signals = gate_signals(signals, self.cfg, self.min_score)
        if not signals:
            return None
        sig = signals[0]  # already sorted by EV desc
        cl = sig.cluster
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=sig.direction,
            entry_price=sig.entry_price,
            sl_price=sig.sl_price,
            tp_price=sig.tp_price,
            zone_id=cl.largest_tf,
            zone_source="confluence",
            reason=sig.reason,
            timestamp=candle.timestamp,
            order_type="limit",
        )

    def top_candidate(self) -> Optional[dict]:
        """Best scorer candidate at the LAST fed bar, IGNORING the admission gate.

        Lets the live chart show what the model is currently considering (drawn
        faded) even when nothing clears ``min_score`` / the EV floor — so the user
        can watch the weights track new zones in real time instead of a frozen
        chart. Returns the same explainable payload as ``explain`` plus
        ``admitted`` (whether this candidate would actually be traded). Identical
        scoring path to ``evaluate``/``explain`` → still live == backtest.
        """
        if not self._recent:
            return None
        candle = self._recent[-1]
        zbt = self.zones_by_tf()
        if len(zbt) < self.cfg.min_distinct_tf:
            return None
        signals = evaluate_confluence_scored(
            zbt, candle.close, self.cfg, self.scorer, modes=self.modes,
            recent_candles=self._recent,
        )
        if not signals:
            return None
        sig = signals[0]  # un-gated, already sorted by EV desc
        admitted = bool(gate_signals([sig], self.cfg, self.min_score))
        payload = self._signal_payload(sig, candle)
        payload["admitted"] = admitted
        return payload

    def explain(self, candle: Candle) -> Optional[dict]:
        """Full explainable payload for the current best signal (for logging /
        the live chart): direction, prices, score, probability, per-feature
        contributions, cluster timeframes + labels."""
        zbt = self.zones_by_tf()
        if len(zbt) < self.cfg.min_distinct_tf:
            return None
        signals = evaluate_confluence_scored(
            zbt, candle.close, self.cfg, self.scorer, modes=self.modes,
            recent_candles=self._recent,
        )
        signals = gate_signals(signals, self.cfg, self.min_score)
        if not signals:
            return None
        return self._signal_payload(signals[0], candle)

    def _signal_payload(self, sig, candle: Candle) -> dict:
        """Build the explainable payload for one scored signal (shared by
        ``explain`` and ``top_candidate`` so both render identically)."""
        cl = sig.cluster
        # per-timeframe weight contribution within the cluster (各自的權重),
        # sorted small->large TF; sum == cl.total_weight (縂權重).
        tf_w: Dict[str, float] = {}
        tf_min: Dict[str, int] = {}
        for lv in cl.levels:
            tf_w[lv.tf] = tf_w.get(lv.tf, 0.0) + lv.weight
            tf_min[lv.tf] = lv.tf_minutes
        tf_weights = [
            {"tf": tf, "weight": round(w, 2)}
            for tf, w in sorted(tf_w.items(), key=lambda kv: tf_min.get(kv[0], 0))
        ]
        return {
            "time": candle.timestamp.isoformat() if candle.timestamp else "",
            "mode": sig.direction_mode,
            "side": cl.side,
            "direction": sig.direction.value,
            "entry": round(sig.entry_price, 2),
            "sl": round(sig.sl_price, 2),
            "tp": round(sig.tp_price, 2),
            "score": round(sig.score, 4),
            "prob": round(sig.prob, 4),
            "ev": round(sig.ev, 4),
            "weight": round(cl.total_weight, 2),
            "tfs": cl.distinct_tfs,
            "tf_weights": tf_weights,
            "largest_tf": cl.largest_tf,
            "labels": cl.labels,
            "explain": self.scorer.explain(sig.features),
            "reason": sig.reason,
        }
