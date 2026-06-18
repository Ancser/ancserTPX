# ============================================================
# 文件: backend/strategy/confluence_scorer.py
# 狀態: v0.19.0 (explainable confluence — linear scorer)
# 關聯文件:
#   ← backend/strategy/confluence_features.py (FEATURE_NAMES, extract_features)
#   ← scripts/train_confluence.py             (fits & saves weights JSON)
#   → backend/strategy/confluence.py          (evaluate_confluence_scored)
# ============================================================
"""A fully transparent linear / logistic scorer.

    logit  = bias + Σ  weight[feature] * value[feature]
    prob   = sigmoid(logit)         # estimated P(win)

There is NO hidden transform: the saved weights live in RAW feature space, so
`explain()` can attribute the decision to each named input. The same JSON file
is loaded by the backtester AND the live engine, so a trade's score is
identical and reproducible in both. Training (sklearn, offline) folds any
standardisation back into raw-space weights before saving — see
scripts/train_confluence.py.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from backend.strategy.confluence_features import FEATURE_NAMES


def default_scorer_path() -> Path:
    """Canonical trained-scorer location: <repo>/data/models/confluence_scorer.json.
    Single source of truth shared by the backtester, live engine and trainer."""
    return Path(__file__).resolve().parents[2] / "data" / "models" / "confluence_scorer.json"


def default_ev_scorer_path() -> Path:
    """Variable-RR (EV) scorer location: <repo>/data/models/confluence_scorer_ev.json,
    written by scripts/train_confluence_ev.py and used when RR optimisation is on."""
    return Path(__file__).resolve().parents[2] / "data" / "models" / "confluence_scorer_ev.json"


def resolve_scorer(use_scorer: bool, rr_grid=None, scorer_path=None) -> "ConfluenceScorer":
    """Pick the right scorer for the run, shared by live + backtest so both load
    the SAME model: explicit path wins; else the EV scorer when RR optimisation
    is requested and it exists; else the fixed-RR scorer; else the heuristic."""
    if scorer_path:
        return ConfluenceScorer.load_or_heuristic(scorer_path, use_scorer)
    if rr_grid:
        ev = default_ev_scorer_path()
        if use_scorer and ev.exists():
            return ConfluenceScorer.load(ev)
    return ConfluenceScorer.load_or_heuristic(None, use_scorer)


@dataclass
class ConfluenceScorer:
    weights: Dict[str, float] = field(default_factory=dict)
    bias: float = 0.0
    meta: Dict = field(default_factory=dict)   # training provenance (date, n, auc...)

    # ── scoring ──

    def logit(self, feats: Dict[str, float]) -> float:
        s = self.bias
        for name in FEATURE_NAMES:
            s += self.weights.get(name, 0.0) * feats.get(name, 0.0)
        return s

    def score(self, feats: Dict[str, float]) -> float:
        """Raw decision score (the logit). Higher = more preferred."""
        return self.logit(feats)

    def probability(self, feats: Dict[str, float]) -> float:
        """Estimated win probability via sigmoid (only meaningful once trained)."""
        z = max(-60.0, min(60.0, self.logit(feats)))
        return 1.0 / (1.0 + math.exp(-z))

    def explain(self, feats: Dict[str, float]) -> List[Tuple[str, float, float, float]]:
        """Per-feature (name, value, weight, contribution=weight*value),
        sorted by |contribution| desc — the human-readable 'why'."""
        out = []
        for name in FEATURE_NAMES:
            v = feats.get(name, 0.0)
            w = self.weights.get(name, 0.0)
            out.append((name, v, w, w * v))
        out.sort(key=lambda t: abs(t[3]), reverse=True)
        return out

    def reason(self, feats: Dict[str, float], top: int = 3) -> str:
        """Short human string of the top contributing features."""
        parts = [f"{n}={v:.2f}×{w:+.2f}" for n, v, w, _ in self.explain(feats)[:top]]
        return f"score={self.score(feats):+.2f} p={self.probability(feats):.0%} [" + ", ".join(parts) + "]"

    # ── persistence ──

    def save(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"weights": self.weights, "bias": self.bias,
                   "feature_names": list(FEATURE_NAMES), "meta": self.meta}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "ConfluenceScorer":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(weights=dict(data.get("weights", {})),
                   bias=float(data.get("bias", 0.0)),
                   meta=dict(data.get("meta", {})))

    @classmethod
    def load_or_heuristic(cls, path=None, use_scorer: bool = True) -> "ConfluenceScorer":
        """Trained JSON if present & requested, else the interpretable prior.
        Shared by the backtester (routes) and the live engine so 'which scorer'
        is resolved identically in both."""
        p = Path(path) if path else default_scorer_path()
        if use_scorer and p.exists():
            return cls.load(p)
        return cls.heuristic()

    def source_name(self) -> str:
        """Short label for UI/logs: the trained file's stem, or 'heuristic'."""
        if not self.meta.get("trained"):
            return "heuristic"
        if self.meta.get("multi_rr"):
            return "confluence_scorer_ev.json (變動RR)"
        return "confluence_scorer.json"

    # ── default (untrained) heuristic ──

    @classmethod
    def heuristic(cls) -> "ConfluenceScorer":
        """Untrained baseline that ranks like the old engine (by confluence
        strength) but already prefers reversion + tight clusters. Used until a
        trained model exists, so the system is sensible out-of-the-box."""
        return cls(
            weights={
                "total_weight": 1.0,        # stronger confluence preferred
                "n_distinct_tf": 0.5,       # more agreeing TFs preferred
                "n_levels": 0.0,
                "largest_tf_rank": 0.3,     # higher-TF anchoring preferred
                "cluster_width_ticks": -0.05,  # tighter cluster preferred
                "dist_to_price_ticks": 0.0,
                "risk_ticks": 0.0,
                "side_is_vah": 0.0,
                "mode_is_reversion": 0.8,   # 60d study: fading the wall won
                "mean_band_pct": 0.0,
                "rel_dist_to_price": -0.10,  # nearer entry (in R units) preferred
                "rr": -0.30,                 # higher RR → lower win prob (prior)
            },
            bias=0.0,
            meta={"kind": "heuristic", "trained": False},
        )
