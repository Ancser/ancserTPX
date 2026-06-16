# ============================================================
# 文件: backend/strategy/confluence.py
# 狀態: v0.18.0 (multi-timeframe weighted level confluence — research engine)
# 關聯文件:
#   ← backend/strategy/consolidation.py  (per-TF ClockBucketZoneDetector zones)
#   ← backend/db/models.py               (ConsolidationZone, Direction)
#   → backend/backtest/engine.py         (P3: consumes ConfluenceSignal)
#   → backend/api/routes.py              (P4: optimizer sweep)
# ============================================================
"""Multi-timeframe weighted horizontal-level confluence.

Replaces the binary "VAL=buy / VAH=sell" rule with a level universe:

  level = (timeframe, recency, band_pct, side, price, weight)

  - timeframe ∈ {5m,10m,15m,30m,1h,2h,4h}        — larger TF = higher weight
  - recency   ∈ {0,-1,-2,-3}                       — newer zone = higher weight
  - band_pct  ∈ {20,40,60,80,100}                  — value-area band
  - side      ∈ {VAH, VAL}                          — POC handled separately
  - weight    = w_tf(timeframe) * w_recency(recency)

When >= MIN_DISTINCT_TF same-side levels from distinct timeframes fall within a
price band (`band_ticks`), they form a CLUSTER. The (weighted) average of the
clustered prices is the entry. Direction depends on the cluster's position vs
the current price and the chosen `direction_mode`:

  - "momentum"  : cluster ABOVE price -> long  (trade into the wall)
                  cluster BELOW price -> short
  - "reversion" : cluster ABOVE price -> short (fade the wall)
                  cluster BELOW price -> long

Both modes are search dimensions — the optimizer tests each and reports results.

SL = the lowest-volume node within the LARGEST contributing timeframe's
VAH->POC (resistance/VAH cluster) or VAL->POC (support/VAL cluster) span.
TP = entry +/- RR * |entry - SL|.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from backend.db.models import ConsolidationZone, Direction
from backend.strategy.consolidation import AREA_TIMEFRAME_MINUTES


# ── tunables (the optimizer overrides these via ConfluenceConfig) ──

# Timeframe weight: monotonic in bucket minutes so larger TF always weighs more
# (honours 4h > 2h > 1h > 30m > 15m > 10m > 5m).
DEFAULT_TF_WEIGHT: Dict[str, float] = {
    tf: float(i + 1) for i, tf in enumerate(AREA_TIMEFRAME_MINUTES.keys())
}

# Recency decay: newest generation (0) weighs most.
DEFAULT_RECENCY_DECAY = 0.6
MAX_RECENCY_DEPTH = 3  # generations back per TF: {0, -1, -2, -3} (confirmed)

VA_BAND_PCTS = (20, 40, 60, 80, 100)
SIDES = ("VAH", "VAL")


def recency_label(tf: str, recency: int) -> str:
    """0 -> '4h', -1 -> '4h-1', ... (the per-trade metadata label)."""
    return tf if recency == 0 else f"{tf}{recency}"


@dataclass
class Level:
    """One horizontal level in the confluence universe."""
    tf: str
    recency: int          # 0 (newest completed) .. -MAX_RECENCY_DEPTH
    band_pct: int         # 20/40/60/80/100
    side: str             # "VAH" | "VAL"
    price: float
    weight: float
    zone_id: str
    poc: float            # owning zone POC (for SL span)
    tf_minutes: int       # for "largest TF" selection

    @property
    def label(self) -> str:
        return f"{recency_label(self.tf, self.recency)}:{self.side}{self.band_pct}"


@dataclass
class ConfluenceConfig:
    """Search-tunable parameters. The optimizer sweeps these; the 'fixed' run
    uses the defaults."""
    band_ticks: float = 8.0            # cluster proximity (optimizer-searched)
    tick_size: float = 0.25
    min_distinct_tf: int = 3           # >= 3 distinct timeframes (confirmed)
    direction_mode: str = "momentum"   # "momentum" | "reversion" (both tested)
    rr: float = 2.0                    # fixed reward:risk
    recency_decay: float = DEFAULT_RECENCY_DECAY
    max_recency_depth: int = MAX_RECENCY_DEPTH
    tf_weight: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TF_WEIGHT))
    weighted_entry: bool = True        # True = weighted avg, False = simple avg
    bands: Tuple[int, ...] = VA_BAND_PCTS

    def recency_weight(self, recency: int) -> float:
        return self.recency_decay ** abs(recency)


@dataclass
class Cluster:
    """A group of same-side levels spanning >= min_distinct_tf timeframes."""
    side: str
    levels: List[Level]
    price: float                    # (weighted) average entry price
    total_weight: float
    distinct_tfs: List[str]
    largest_tf: str
    largest_tf_minutes: int

    @property
    def labels(self) -> List[str]:
        return [lv.label for lv in self.levels]


@dataclass
class ConfluenceSignal:
    direction: Direction
    entry_price: float
    sl_price: float
    tp_price: float
    cluster: Cluster
    direction_mode: str
    reason: str
    # ── explainable scoring (populated by evaluate_confluence_scored) ──
    score: float = 0.0
    prob: float = 0.0
    features: Dict[str, float] = field(default_factory=dict)


# ── level universe extraction ──

def extract_levels(
    zones_by_tf: Dict[str, List[ConsolidationZone]],
    cfg: ConfluenceConfig,
) -> List[Level]:
    """Build the full level universe from recent completed zones per timeframe.

    Args:
        zones_by_tf: {tf: [completed zones, OLDEST first ... NEWEST last]}.
                     Only the newest (max_recency_depth+1) zones per TF are used.
        cfg: weighting / depth config.

    Returns:
        Flat list of Level (VAH+VAL per band, per recency, per TF).
    """
    levels: List[Level] = []
    depth = cfg.max_recency_depth
    for tf, zones in zones_by_tf.items():
        if not zones:
            continue
        tf_w = cfg.tf_weight.get(tf, 1.0)
        tf_min = AREA_TIMEFRAME_MINUTES.get(tf, 0)
        # newest = recency 0; previous = -1; ... down to -depth
        recent = zones[-(depth + 1):]
        n = len(recent)
        for idx, zone in enumerate(recent):
            recency = -(n - 1 - idx)        # last element -> 0, earlier -> negative
            if recency < -depth:
                continue
            w = tf_w * cfg.recency_weight(recency)
            bands = zone.va_bands or {}
            for pct in cfg.bands:
                band = bands.get(pct)
                if not band:
                    continue
                vah, val = band
                levels.append(Level(tf, recency, pct, "VAH", vah, w, zone.zone_id, zone.poc, tf_min))
                levels.append(Level(tf, recency, pct, "VAL", val, w, zone.zone_id, zone.poc, tf_min))
    return levels


# ── clustering ──

def cluster_levels(levels: List[Level], cfg: ConfluenceConfig) -> List[Cluster]:
    """Greedy 1-D clustering of same-side levels within `band_ticks`.

    A valid cluster must contain levels from >= cfg.min_distinct_tf distinct
    timeframes. Levels are processed per side (VAH/VAL never mix).
    """
    band = cfg.band_ticks * cfg.tick_size
    clusters: List[Cluster] = []

    for side in SIDES:
        side_levels = sorted((lv for lv in levels if lv.side == side), key=lambda l: l.price)
        i = 0
        n = len(side_levels)
        while i < n:
            group = [side_levels[i]]
            j = i + 1
            # extend while within band of the group's first price (sorted, so monotone)
            while j < n and (side_levels[j].price - group[0].price) <= band:
                group.append(side_levels[j])
                j += 1
            distinct = sorted({lv.tf for lv in group}, key=lambda t: AREA_TIMEFRAME_MINUTES.get(t, 0))
            if len(distinct) >= cfg.min_distinct_tf:
                clusters.append(_build_cluster(side, group, cfg))
            i = j if j > i + 1 else i + 1
    return clusters


def _build_cluster(side: str, group: List[Level], cfg: ConfluenceConfig) -> Cluster:
    total_w = sum(lv.weight for lv in group)
    if cfg.weighted_entry and total_w > 0:
        price = sum(lv.price * lv.weight for lv in group) / total_w
    else:
        price = sum(lv.price for lv in group) / len(group)
    largest = max(group, key=lambda l: l.tf_minutes)
    distinct = sorted({lv.tf for lv in group}, key=lambda t: AREA_TIMEFRAME_MINUTES.get(t, 0))
    return Cluster(
        side=side,
        levels=group,
        price=price,
        total_weight=total_w,
        distinct_tfs=distinct,
        largest_tf=largest.tf,
        largest_tf_minutes=largest.tf_minutes,
    )


# ── signal construction ──

def _resolve_direction(cluster: Cluster, current_price: float, mode: str) -> Optional[Direction]:
    above = cluster.price > current_price
    if mode == "momentum":
        return Direction.BUY if above else Direction.SELL
    if mode == "reversion":
        return Direction.SELL if above else Direction.BUY
    return None


def _sl_from_largest_tf(
    cluster: Cluster,
    direction: Direction,
    zones_by_tf: Dict[str, List[ConsolidationZone]],
) -> Optional[float]:
    """Lowest-volume node in the largest contributing TF's stop span.

    The span depends on whether the trade goes INTO the cluster (stop on the
    inner / POC side) or FADES it (stop on the outer / 100%-extreme side):

      VAH cluster + BUY  (momentum into wall)  -> inner  [POC, VAH]
      VAH cluster + SELL (fade resistance)     -> outer  [VAH, high_100]
      VAL cluster + SELL (momentum into wall)  -> inner  [VAL, POC]
      VAL cluster + BUY  (fade support)        -> outer  [low_100, VAL]

    Falls back to the span's outer edge if the histogram has no node there.
    """
    zones = zones_by_tf.get(cluster.largest_tf) or []
    if not zones:
        return None
    zone = zones[-1]  # newest completed zone of the largest TF
    poc = zone.poc

    if cluster.side == "VAH":
        if direction == Direction.BUY:        # momentum into resistance → stop below VAH
            lo, hi, fallback = poc, zone.vah_80, zone.vah_80
        else:                                  # fade resistance → stop above VAH
            lo, hi, fallback = zone.vah_80, zone.high_100, zone.high_100
    else:  # VAL cluster
        if direction == Direction.SELL:       # momentum into support → stop above VAL
            lo, hi, fallback = zone.val_80, poc, zone.val_80
        else:                                  # fade support → stop below VAL
            lo, hi, fallback = zone.low_100, zone.val_80, zone.val_80

    node = zone.lowest_volume_price_between(lo, hi)
    return node if node is not None else fallback


def build_signal(
    cluster: Cluster,
    current_price: float,
    zones_by_tf: Dict[str, List[ConsolidationZone]],
    cfg: ConfluenceConfig,
) -> Optional[ConfluenceSignal]:
    direction = _resolve_direction(cluster, current_price, cfg.direction_mode)
    if direction is None:
        return None

    entry = cluster.price
    sl = _sl_from_largest_tf(cluster, direction, zones_by_tf)
    if sl is None:
        return None

    # SL must sit on the protective side of entry; skip degenerate geometry.
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    if direction == Direction.BUY and sl >= entry:
        return None
    if direction == Direction.SELL and sl <= entry:
        return None

    tp = entry + cfg.rr * risk if direction == Direction.BUY else entry - cfg.rr * risk

    reason = (
        f"confluence/{cfg.direction_mode} {cluster.side} "
        f"x{len(cluster.distinct_tfs)}TF [{','.join(cluster.labels)}] "
        f"w={cluster.total_weight:.1f}"
    )
    return ConfluenceSignal(
        direction=direction,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        cluster=cluster,
        direction_mode=cfg.direction_mode,
        reason=reason,
    )


def evaluate_confluence(
    zones_by_tf: Dict[str, List[ConsolidationZone]],
    current_price: float,
    cfg: ConfluenceConfig,
) -> List[ConfluenceSignal]:
    """Top-level entry point: zones -> levels -> clusters -> signals.

    Returns every valid signal for this bar (caller picks the best, e.g. by
    cluster.total_weight, and applies position/session limits)."""
    levels = extract_levels(zones_by_tf, cfg)
    if not levels:
        return []
    clusters = cluster_levels(levels, cfg)
    signals: List[ConfluenceSignal] = []
    for cl in clusters:
        sig = build_signal(cl, current_price, zones_by_tf, cfg)
        if sig is not None:
            signals.append(sig)
    return signals


def evaluate_confluence_scored(
    zones_by_tf: Dict[str, List[ConsolidationZone]],
    current_price: float,
    cfg: ConfluenceConfig,
    scorer,
    modes: Tuple[str, ...] = ("momentum", "reversion"),
) -> List[ConfluenceSignal]:
    """Explainable, per-step evaluation.

    Clusters are computed ONCE (mode-independent), then EACH cluster is tried
    under EVERY direction mode. Every resulting signal gets an interpretable
    feature vector + a scorer score/probability attached. The list is returned
    sorted by score (desc) so the caller just takes the top one above its
    threshold — this is the 'auto-select best action this bar' behaviour.
    """
    from backend.strategy.confluence_features import extract_features  # local: avoid cycle

    levels = extract_levels(zones_by_tf, cfg)
    if not levels:
        return []
    clusters = cluster_levels(levels, cfg)
    out: List[ConfluenceSignal] = []
    for cl in clusters:
        for mode in modes:
            sig = _build_signal_mode(cl, current_price, zones_by_tf, cfg, mode)
            if sig is None:
                continue
            feats = extract_features(sig, current_price, cfg.tick_size)
            sig.features = feats
            sig.score = scorer.score(feats)
            sig.prob = scorer.probability(feats)
            sig.reason = f"{sig.reason} | {scorer.reason(feats)}"
            out.append(sig)
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def _build_signal_mode(cluster, current_price, zones_by_tf, cfg, mode) -> Optional[ConfluenceSignal]:
    """build_signal but with an explicit mode (so one cluster can yield both a
    momentum and a reversion candidate without mutating cfg)."""
    direction = _resolve_direction(cluster, current_price, mode)
    if direction is None:
        return None
    entry = cluster.price
    sl = _sl_from_largest_tf(cluster, direction, zones_by_tf)
    if sl is None:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    if direction == Direction.BUY and sl >= entry:
        return None
    if direction == Direction.SELL and sl <= entry:
        return None
    tp = entry + cfg.rr * risk if direction == Direction.BUY else entry - cfg.rr * risk
    reason = (
        f"confluence/{mode} {cluster.side} "
        f"x{len(cluster.distinct_tfs)}TF [{','.join(cluster.labels)}] "
        f"w={cluster.total_weight:.1f}"
    )
    return ConfluenceSignal(
        direction=direction,
        entry_price=entry,
        sl_price=sl,
        tp_price=tp,
        cluster=cluster,
        direction_mode=mode,
        reason=reason,
    )
