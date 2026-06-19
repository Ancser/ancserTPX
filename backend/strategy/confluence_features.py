# ============================================================
# 文件: backend/strategy/confluence_features.py
# 狀態: v1.0.6 (explainable confluence — feature extraction)
# 關聯文件:
#   ← backend/strategy/confluence.py         (ConfluenceSignal, Cluster, Level)
#   → backend/strategy/confluence_scorer.py  (linear scorer consumes these)
#   → scripts/train_confluence.py            (trainer builds X from these)
# ============================================================
"""Interpretable feature vector for one confluence candidate.

Every feature is a plain, human-readable number computed from the cluster +
the built signal geometry. The scorer is a linear model over EXACTLY these
features, so each coefficient maps 1:1 to a named, explainable input — no
hidden transforms, no black box. backtest and live compute features the same
way, guaranteeing reproducibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List

from backend.strategy.consolidation import AREA_TIMEFRAME_MINUTES

if TYPE_CHECKING:
    from backend.strategy.confluence import ConfluenceSignal

# Ordered, frozen feature schema. The scorer's weight dict uses these keys.
# NOTE: append-only. New names go at the END so an older trained JSON (which
# keys weights by name) keeps mapping correctly and simply contributes 0 for
# any name it lacks (ConfluenceScorer uses weights.get(name, 0.0)).
FEATURE_NAMES: tuple = (
    "total_weight",        # Σ level weights in the cluster (confluence strength)
    "n_distinct_tf",       # how many distinct timeframes agree
    "n_levels",            # total clustered levels
    "largest_tf_rank",     # 0(5m)..6(4h) — bigger = higher TF anchoring
    "cluster_width_ticks", # price spread of the cluster (tight = sharper)
    "dist_to_price_ticks", # |entry - current price| in ticks (how far to fill)
    "risk_ticks",          # |entry - SL| in ticks (stop size)
    "side_is_vah",         # 1 = resistance(VAH) cluster, 0 = support(VAL)
    "mode_is_reversion",   # 1 = fade the wall, 0 = momentum into it
    "mean_band_pct",       # avg value-area band hit (0.2..1.0)
    # ── distance-aware (v1.0.6): scale-free proximity so a NEAR older-recency
    # zone (-1/-2/-3) scores like a near newest one regardless of absolute price.
    "rel_dist_to_price",   # |entry - price| / risk  (distance in R units)
    # ── decision variable (v1.0.6, variable-RR / EV optimisation): the chosen
    # reward:risk, read from geometry. Constant in fixed-RR training (≈0 weight),
    # informative once the multi-RR trainer feeds several RRs per setup.
    "rr",                  # |tp - entry| / |entry - SL|
    # ── CONTEXT features (v1.0.6): the setup's SURROUNDINGS, not just its shape.
    # These need extra inputs (the full level universe + recent candles); when an
    # input is absent (old caller / old JSON) the feature is 0.0 and the scorer's
    # weights.get default keeps everything backward-compatible.
    # NOTE: 'opposing_weight_ahead' was removed in v1.0.6 — two retrains shrank it
    # to ~0 (dead) and its full path-scan wasted compute. Removal is name-keyed
    # safe; old JSONs that still list it are simply ignored by weights.get.
    "dist_to_obstacle_R",     # entry→nearest blocking level, in R units (= reward_R
                              # when the path is clear). small = obstacle right ahead.
    "atr_R",                  # recent ATR ÷ risk_ticks. volatility vs our stop size.
    "trend_R",                # recent drift ÷ risk, oriented to trade direction.
                              # + = trading WITH the move, − = fading a fresh move.
    # ── REGIME features (v1.0.6): let the model learn WHEN a fade works. The
    # linear model can't form interactions on its own, so the trend/range regime
    # is supplied raw AND pre-multiplied by the reversion flag.
    "efficiency_ratio",       # Kaufman ER over TREND_LOOKBACK: |net move| / Σ|steps|.
                              # ~1 = clean trend (fading is dangerous), ~0 = chop/range.
    "reversion_in_trend",     # mode_is_reversion × efficiency_ratio. A negative
                              # weight = "fading INTO a strong trend loses". This is
                              # the term that breaks the always-reversion behaviour.
    # ── BREAKOUT mode (v1.0.6): one-hot for the breakout-retrace candidate (baseline
    # = momentum). Lets the model learn the breakout-retrace base win-rate separately
    # from reversion/momentum. See confluence._breakout_geometry.
    "is_breakout",            # 1 = breakout-retrace setup, 0 = momentum/reversion.
)

DEAD_FEATURES: frozenset = frozenset({
    "n_levels", "side_is_vah", "largest_tf_rank", "cluster_width_ticks",
    "n_distinct_tf",
})

_TF_RANK = {tf: i for i, tf in enumerate(AREA_TIMEFRAME_MINUTES.keys())}

# Context windows (bars). Both trainers, the backtester and the live engine feed
# the SAME trailing window so atr_R / trend_R are identical train==backtest==live.
ATR_WINDOW = 14
TREND_LOOKBACK = 30
CONTEXT_WINDOW = TREND_LOOKBACK + 1   # recent-candle buffer length to supply


def _context_features(sig, current_price, tick_size, risk_safe, reward_ticks,
                      levels, recent_candles) -> Dict[str, float]:
    """The four v1.0.6 context features. Every value falls back to a neutral
    default when its input is missing, so a caller that supplies neither levels
    nor candles reproduces the pre-v1.0.6 behaviour exactly (all four = 0/clear)."""
    entry, tp = sig.entry_price, sig.tp_price
    reward_R = reward_ticks / risk_safe

    # ── nearest opposing level in the path entry→TP (excludes our own cluster) ──
    dist_obstacle_R = reward_R            # clear path defaults to the full target
    if levels:
        own = {id(lv) for lv in sig.cluster.levels}
        lo, hi = (entry, tp) if entry <= tp else (tp, entry)
        nearest = None
        for lv in levels:
            if id(lv) in own:
                continue
            if lo < lv.price < hi:
                d = abs(lv.price - entry)
                if nearest is None or d < nearest:
                    nearest = d
        if nearest is not None:
            dist_obstacle_R = (nearest / tick_size) / risk_safe

    # ── recent-price regime: volatility + directional drift, both in R ──
    atr_R = 0.0
    trend_R = 0.0
    efficiency = 0.0
    if recent_candles and len(recent_candles) >= 2:
        cs = recent_candles[-(ATR_WINDOW + 1):]
        trs = [(c.high - c.low) for c in cs]
        if trs:
            atr_price = sum(trs) / len(trs)
            atr_R = (atr_price / tick_size) / risk_safe
        look = recent_candles[-(TREND_LOOKBACK + 1):]
        drift = current_price - look[0].close          # signed price drift
        is_buy = str(getattr(sig.direction, "value", sig.direction)).lower() in ("buy", "long")
        aligned = drift if is_buy else -drift           # + = with the move
        trend_R = (aligned / tick_size) / risk_safe
        # Kaufman efficiency ratio: how DIRECTIONAL the recent path is. net move
        # over the summed bar-to-bar travel. 1 = straight trend, 0 = pure chop.
        closes = [c.close for c in look]
        net = abs(closes[-1] - closes[0])
        path = sum(abs(closes[k] - closes[k - 1]) for k in range(1, len(closes)))
        efficiency = (net / path) if path > 1e-9 else 0.0
    is_reversion = 1.0 if getattr(sig, "direction_mode", "") == "reversion" else 0.0
    return {
        "dist_to_obstacle_R": float(dist_obstacle_R),
        "atr_R": float(atr_R),
        "trend_R": float(trend_R),
        "efficiency_ratio": float(efficiency),
        "reversion_in_trend": float(is_reversion * efficiency),
    }


def extract_features(sig: "ConfluenceSignal", current_price: float, tick_size: float,
                     levels: "List" = None, recent_candles: "List" = None) -> Dict[str, float]:
    """Compute the interpretable feature dict for a fully-built signal.

    SL/entry geometry is already validated by build_signal, so risk_ticks is
    well-defined here. ``levels`` (the full level universe this bar) and
    ``recent_candles`` (trailing price window) are optional CONTEXT inputs; when
    omitted the four v1.0.6 context features default to neutral so behaviour is
    identical to the pre-context scorer. Callers that want context MUST pass the
    SAME inputs in live, backtest and training to keep the three in lock-step.
    """
    cl = sig.cluster
    cl_levels: List = cl.levels
    prices = [lv.price for lv in cl_levels]
    width_ticks = (max(prices) - min(prices)) / tick_size if len(prices) > 1 else 0.0
    band_pcts = [lv.band_pct for lv in cl_levels]
    mean_band = (sum(band_pcts) / len(band_pcts) / 100.0) if band_pcts else 0.0
    dist_ticks = abs(sig.entry_price - current_price) / tick_size
    risk_ticks = abs(sig.entry_price - sig.sl_price) / tick_size
    reward_ticks = abs(sig.tp_price - sig.entry_price) / tick_size
    risk_safe = risk_ticks if risk_ticks > 1e-9 else 1.0
    feats = {
        "total_weight": float(cl.total_weight),
        "n_distinct_tf": float(len(cl.distinct_tfs)),
        "n_levels": float(len(cl_levels)),
        "largest_tf_rank": float(_TF_RANK.get(cl.largest_tf, 0)),
        "cluster_width_ticks": float(width_ticks),
        "dist_to_price_ticks": dist_ticks,
        "risk_ticks": risk_ticks,
        "side_is_vah": 1.0 if cl.side == "VAH" else 0.0,
        "mode_is_reversion": 1.0 if sig.direction_mode == "reversion" else 0.0,
        "mean_band_pct": float(mean_band),
        "rel_dist_to_price": dist_ticks / risk_safe,
        "rr": reward_ticks / risk_safe,
        "is_breakout": 1.0 if sig.direction_mode == "breakout" else 0.0,
    }
    feats.update(_context_features(sig, current_price, tick_size, risk_safe,
                                   reward_ticks, levels, recent_candles))
    return feats


def features_to_vector(feats: Dict[str, float]) -> List[float]:
    """Stable ordering matching FEATURE_NAMES (for matrix building in training)."""
    return [feats.get(name, 0.0) for name in FEATURE_NAMES]
