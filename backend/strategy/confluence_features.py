# ============================================================
# 文件: backend/strategy/confluence_features.py
# 狀態: v0.19.0 (explainable confluence — feature extraction)
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
)

_TF_RANK = {tf: i for i, tf in enumerate(AREA_TIMEFRAME_MINUTES.keys())}


def extract_features(sig: "ConfluenceSignal", current_price: float, tick_size: float) -> Dict[str, float]:
    """Compute the interpretable feature dict for a fully-built signal.

    SL/entry geometry is already validated by build_signal, so risk_ticks is
    well-defined here.
    """
    cl = sig.cluster
    levels: List = cl.levels
    prices = [lv.price for lv in levels]
    width_ticks = (max(prices) - min(prices)) / tick_size if len(prices) > 1 else 0.0
    band_pcts = [lv.band_pct for lv in levels]
    mean_band = (sum(band_pcts) / len(band_pcts) / 100.0) if band_pcts else 0.0
    return {
        "total_weight": float(cl.total_weight),
        "n_distinct_tf": float(len(cl.distinct_tfs)),
        "n_levels": float(len(levels)),
        "largest_tf_rank": float(_TF_RANK.get(cl.largest_tf, 0)),
        "cluster_width_ticks": float(width_ticks),
        "dist_to_price_ticks": abs(sig.entry_price - current_price) / tick_size,
        "risk_ticks": abs(sig.entry_price - sig.sl_price) / tick_size,
        "side_is_vah": 1.0 if cl.side == "VAH" else 0.0,
        "mode_is_reversion": 1.0 if sig.direction_mode == "reversion" else 0.0,
        "mean_band_pct": float(mean_band),
    }


def features_to_vector(feats: Dict[str, float]) -> List[float]:
    """Stable ordering matching FEATURE_NAMES (for matrix building in training)."""
    return [feats.get(name, 0.0) for name in FEATURE_NAMES]
