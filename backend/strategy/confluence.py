# ============================================================
# 文件: backend/strategy/confluence.py
# 狀態: v0.21.0 (multi-timeframe weighted level confluence — core engine)
# 關聯文件:
#   ← backend/strategy/consolidation.py      (per-TF ClockBucketZoneDetector zones)
#   ← backend/db/models.py                   (ConsolidationZone, Direction)
#   → backend/backtest/confluence_backtest.py (consumes ConfluenceSignal)
#   → backend/live/confluence_live.py        (live evaluator, same path)
#   → backend/strategy/confluence_features.py (interpretable feature vector)
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
MAX_RECENCY_DEPTH = 3  # generations back per TF: {0, -1, -2, -3}

# Minimum risk (entry->SL distance) for a tradeable signal, in ticks. Below this
# the SL sits on top of the entry: a real-world hazard (instant stop-out / absurd
# TP) AND a training poison — sub-tick risk makes the `rr` feature collapse to ~0
# instead of cfg.rr, giving an otherwise-constant feature spurious variance that
# survives the drop-constant guard and grabs a runaway weight.
MIN_RISK_TICKS = 5

VA_BAND_PCTS = (20, 40, 60, 80, 100)
SIDES = ("VAH", "VAL")


def recency_label(tf: str, recency: int) -> str:
    """0 -> '4h', -1 -> '4h-1', ... (the per-trade metadata label)."""
    return tf if recency == 0 else f"{tf}{recency}"


def snapshot_zones_by_tf(detectors: Dict[str, object], depth: int) -> Dict[str, list]:
    """Recent-completed-zones-per-timeframe snapshot, shared by the live
    evaluator, the backtester and build_zone_timeline so all three read the SAME
    `zones_by_tf` shape (live == backtest). `depth` is max_recency_depth + 1."""
    out: Dict[str, list] = {}
    for tf, det in detectors.items():
        zs = det.get_recent_zones(depth)
        if zs:
            out[tf] = zs
    return out


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
    min_distinct_tf: int = 3           # >= 3 distinct timeframes form a cluster
    direction_mode: str = "momentum"   # "momentum" | "reversion" (both tested)
    rr: float = 2.0                    # fixed reward:risk
    # EV gate (option C). When ev_floor is not None, signals are kept only if
    # expected value per unit risk (prob*rr - (1-prob)) >= ev_floor, INSTEAD of
    # the raw win-prob/score gate. ev_floor=0.0 trades every positive-EV setup
    # (breakeven prob = 1/(1+rr): RR1.5→40%, RR2→33%, RR3→25%), so a lower-prob
    # but higher-RR setup is admitted when its EV beats a high-prob low-RR one.
    # None = legacy behaviour (gate by win-prob/score only). Identical in live
    # and backtest, so reproducibility holds.
    ev_floor: Optional[float] = None
    # Variable-RR / EV optimisation (option C phase 2). When set, each candidate
    # is evaluated at every RR in the grid and the EV-maximising RR is chosen
    # (TP placed at that RR). Requires a multi-RR-trained scorer (one whose
    # 'rr' weight is informative) to be meaningful. None = fixed cfg.rr.
    rr_grid: Optional[Tuple[float, ...]] = None
    recency_decay: float = DEFAULT_RECENCY_DECAY
    max_recency_depth: int = MAX_RECENCY_DEPTH
    tf_weight: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TF_WEIGHT))
    weighted_entry: bool = True        # True = weighted avg, False = simple avg
    # v0.23: where the limit sits inside a cluster. "edge" = the clustered level
    # NEAREST the current price (the first-touch structural boundary), so the
    # order sits ON a real level instead of floating at the weighted centroid
    # ("半空中"). "centroid" = legacy weighted/simple average (cluster.price).
    # Geometry-affecting → models trained under one value must run under the same.
    entry_mode: str = "edge"
    # v0.23 breakout-retrace candidate (see _breakout_geometry). Default ON to
    # preserve current behaviour; the optimizer / panel can disable it to A/B
    # whether breakout trades (low win-rate, high volume) help or hurt.
    enable_breakout: bool = True
    bands: Tuple[int, ...] = VA_BAND_PCTS

    def auto_modes(self) -> Tuple[str, ...]:
        """The candidate modes for the auto (scored) path, honouring
        enable_breakout. Single source so live==backtest==train stay in lock-step."""
        return ("momentum", "reversion", "breakout") if self.enable_breakout \
            else ("momentum", "reversion")

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
    ev: float = 0.0          # expected value per unit risk = prob*rr - (1-prob)
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


def _entry_price(cluster: Cluster, current_price: float, cfg: ConfluenceConfig,
                 direction: Direction) -> float:
    """Where the limit order sits inside the cluster.

    "edge" (default): the BEST-priced clustered level for the trade direction —
    SELL rests on the HIGHEST level (sell as high as possible), BUY on the LOWEST
    (buy as low as possible). This puts the limit on the FAR structural edge of
    the zone (e.g. the top of a stacked resistance for a fade-short), which both
    rests on a real level (not the floating weighted centroid, "半空中") AND
    maximises reward:risk — the opposite of resting on the near edge, which sells
    the bottom of resistance and halves RR. "centroid": legacy weighted/simple
    average (cluster.price). Direction is still decided from cluster.price, so
    only the fill location moves, not the long/short choice.
    """
    if getattr(cfg, "entry_mode", "edge") == "edge" and cluster.levels:
        prices = [lv.price for lv in cluster.levels]
        return max(prices) if direction == Direction.SELL else min(prices)
    return cluster.price


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

    entry = _entry_price(cluster, current_price, cfg, direction)
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
    modes: Tuple[str, ...] = ("momentum", "reversion", "breakout"),
    recent_candles: Optional[list] = None,
) -> List[ConfluenceSignal]:
    """Explainable, per-step evaluation.

    Clusters are computed ONCE (mode-independent), then EACH cluster is tried
    under EVERY direction mode. Every resulting signal gets an interpretable
    feature vector + a scorer score/probability attached. The list is returned
    sorted by score (desc) so the caller just takes the top one above its
    threshold — this is the 'auto-select best action this bar' behaviour.

    ``recent_candles`` is the trailing price window used for the v0.22 context
    features (atr_R / trend_R). Live, backtest and training MUST all pass the
    same-length window (see confluence_features.CONTEXT_WINDOW) to stay in
    lock-step; omitting it makes those features neutral (pre-context behaviour).
    """
    levels = extract_levels(zones_by_tf, cfg)
    if not levels:
        return []
    clusters = cluster_levels(levels, cfg)
    out: List[ConfluenceSignal] = []
    for cl in clusters:
        for mode in modes:
            geom = _signal_geometry(cl, current_price, zones_by_tf, mode, cfg,
                                    recent_candles=recent_candles)
            if geom is None:
                continue
            best = _best_rr_signal(cl, mode, geom, current_price, cfg, scorer,
                                   levels=levels, recent_candles=recent_candles)
            if best is not None:
                out.append(best)
    # EV-priority sort, win-rate (prob) as the secondary key (option C). With a
    # fixed RR this matches the old score-desc order, so existing runs are
    # unchanged; with a per-signal RR it surfaces the best risk-adjusted setup.
    out.sort(key=lambda s: (s.ev, s.prob, s.score), reverse=True)
    return out


def _best_rr_signal(cluster, mode, geom, current_price, cfg, scorer,
                    levels=None, recent_candles=None) -> Optional[ConfluenceSignal]:
    """Score the candidate at every RR in cfg.rr_grid (or just cfg.rr when no
    grid) and return the EV-maximising signal, fully populated. Each RR's EV
    uses its own reward:risk (read from geometry), so a lower-prob/higher-RR
    setup can win when its expected value is larger.

    ``levels`` (full level universe) + ``recent_candles`` feed the v0.22 context
    features; they are threaded through unchanged so live/backtest/train match."""
    from backend.strategy.confluence_features import extract_features  # local: avoid cycle
    rrs = cfg.rr_grid if cfg.rr_grid else (cfg.rr,)
    best: Optional[ConfluenceSignal] = None
    for rr in rrs:
        sig = _make_signal(cluster, mode, geom, rr)
        feats = extract_features(sig, current_price, cfg.tick_size,
                                 levels=levels, recent_candles=recent_candles)
        sig.features = feats
        sig.score = scorer.score(feats)
        sig.prob = scorer.probability(feats)
        sig.ev = sig.prob * rr - (1.0 - sig.prob)  # EV per unit risk at this RR
        if best is None or sig.ev > best.ev:
            best = sig
    if best is not None:
        best.reason = f"{best.reason} | {scorer.reason(best.features)}"
        if cfg.rr_grid:
            best.reason += f" | RR*={best.features.get('rr', cfg.rr):.2f}"
    return best


def gate_signals(signals: List[ConfluenceSignal], cfg: ConfluenceConfig,
                 min_score: float = 0.0) -> List[ConfluenceSignal]:
    """Shared admission gate for live AND backtest (keeps them identical).

    When ``cfg.ev_floor`` is set, admit only positive-(or-above-floor)-EV
    signals — this is the EV-priority gate from option C. Otherwise fall back
    to the legacy win-prob/score gate (``score >= min_score``).
    """
    if cfg.ev_floor is not None:
        return [s for s in signals if s.ev >= cfg.ev_floor]
    return [s for s in signals if s.score >= min_score]


def _breakout_geometry(cluster, current_price, zones_by_tf, cfg, recent_candles):
    """Breakout-retrace momentum setup (Fabio-style).

    After price has CLOSED through the dominant TF's 80% value-area edge, enter
    on a ~50% retrace back toward that edge, IN the breakout direction. SL = the
    low-volume node (same span machinery as the other modes). Returns None unless
    a real, still-intact breakout is present in the recent window — so when no
    candles are supplied (old caller) breakout simply yields no candidate and the
    pre-v0.23 universe is reproduced exactly.

      VAH cluster: close > vah_80 → BUY the pullback (entry between vah_80 and high)
      VAL cluster: close < val_80 → SELL the pullback (entry between low and val_80)
    """
    if not recent_candles or len(recent_candles) < 3:
        return None
    zones = zones_by_tf.get(cluster.largest_tf) or []
    if not zones:
        return None
    zone = zones[-1]
    if cluster.side == "VAH":
        level = zone.vah_80
        extreme = max(c.high for c in recent_candles)
        broke = any(c.close > level for c in recent_candles)
        if not broke or extreme <= level or current_price <= level:
            return None  # never broke, or already fell back inside the value area
        direction = Direction.BUY
        entry = level + 0.5 * (extreme - level)          # 50% retrace of the thrust
    else:  # VAL — downside breakout
        level = zone.val_80
        extreme = min(c.low for c in recent_candles)
        broke = any(c.close < level for c in recent_candles)
        if not broke or extreme >= level or current_price >= level:
            return None
        direction = Direction.SELL
        entry = level - 0.5 * (level - extreme)
    sl = _sl_from_largest_tf(cluster, direction, zones_by_tf)
    if sl is None:
        return None
    risk = abs(entry - sl)
    if risk < MIN_RISK_TICKS * cfg.tick_size:
        return None
    if direction == Direction.BUY and sl >= entry:
        return None
    if direction == Direction.SELL and sl <= entry:
        return None
    return direction, entry, sl, risk


def _signal_geometry(cluster, current_price, zones_by_tf, mode, cfg, recent_candles=None):
    """Direction + entry + SL for one cluster/mode, independent of RR.

    Returns (direction, entry, sl, risk) or None when the geometry is
    degenerate (no SL / SL on the wrong side / risk below MIN_RISK_TICKS).
    ``recent_candles`` is required only by the "breakout" mode; momentum and
    reversion ignore it (kept signature-compatible)."""
    if mode == "breakout":
        return _breakout_geometry(cluster, current_price, zones_by_tf, cfg, recent_candles)
    direction = _resolve_direction(cluster, current_price, mode)
    if direction is None:
        return None
    entry = _entry_price(cluster, current_price, cfg, direction)
    sl = _sl_from_largest_tf(cluster, direction, zones_by_tf)
    if sl is None:
        return None
    risk = abs(entry - sl)
    # Reject sub-minimum risk: SL essentially on the entry. Both a live hazard
    # and a training poison (collapses the `rr` feature to ~0).
    if risk < MIN_RISK_TICKS * cfg.tick_size:
        return None
    if direction == Direction.BUY and sl >= entry:
        return None
    if direction == Direction.SELL and sl <= entry:
        return None
    return direction, entry, sl, risk


def _make_signal(cluster, mode, geom, rr: float) -> ConfluenceSignal:
    """Build a ConfluenceSignal from precomputed geometry at a given RR."""
    direction, entry, sl, risk = geom
    tp = entry + rr * risk if direction == Direction.BUY else entry - rr * risk
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
