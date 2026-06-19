# ============================================================
# 文件: backend/strategy/confluence_scorer.py
# 狀態: v1.0.6 (explainable confluence — linear scorer)
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
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from backend.strategy.confluence_features import FEATURE_NAMES


def default_scorer_path() -> Path:
    """Active scorer copied from the immutable model registry."""
    return Path(__file__).resolve().parents[2] / "data" / "models" / "confluence_scorer.json"


MODEL_TRAINERS = ("codex", "claude")


def model_registry_dir(active_path=None) -> Path:
    """Versioned scorers live beside the active scorer under registry/."""
    active = Path(active_path) if active_path else default_scorer_path()
    return active.parent / "registry"


def _normalise_description(description: str) -> str:
    value = " ".join(str(description or "").split())
    if not value:
        raise ValueError("Model description is required")
    if len(value) > 120:
        raise ValueError("Model description must be 120 characters or fewer")
    return value


def _description_slug(description: str) -> str:
    """Create a Windows-safe, readable slug while preserving CJK text."""
    value = _normalise_description(description).lower()
    invalid = '<>:"/\\|?*'
    chars = []
    for char in value:
        if char.isalnum():
            chars.append(char)
        elif char.isspace() or char in "-_." or char in invalid:
            chars.append("-")
        else:
            chars.append("-")
    slug = re.sub(r"-+", "-", "".join(chars)).strip("-.")
    return (slug or "model")[:48].rstrip("-.")


def _normalise_trainer(trainer: str) -> str:
    value = str(trainer or "").strip().lower()
    if value not in MODEL_TRAINERS:
        raise ValueError(f"trainer must be one of: {', '.join(MODEL_TRAINERS)}")
    return value


def build_model_id(trained_at, trainer: str, description: str, registry=None) -> str:
    """Build YYYYMMDD_trainer_description, adding -02/-03 on collisions."""
    trainer = _normalise_trainer(trainer)
    description = _normalise_description(description)
    if isinstance(trained_at, datetime):
        day = trained_at.strftime("%Y%m%d")
    else:
        raw = str(trained_at or "")
        day = raw[:10].replace("-", "")
        if len(day) != 8 or not day.isdigit():
            day = datetime.now().strftime("%Y%m%d")
    base = f"{day}_{trainer}_{_description_slug(description)}"
    if registry is None:
        return base
    registry = Path(registry)
    candidate = base
    suffix = 2
    while (registry / f"{candidate}.json").exists():
        candidate = f"{base}-{suffix:02d}"
        suffix += 1
    return candidate


def save_model_version(
    scorer: "ConfluenceScorer",
    trainer: str,
    description: str,
    *,
    activate: bool = True,
    active_path=None,
) -> Tuple[str, Path]:
    """Append one immutable model version and optionally make it active."""
    active = Path(active_path) if active_path else default_scorer_path()
    registry = model_registry_dir(active)
    registry.mkdir(parents=True, exist_ok=True)
    trainer = _normalise_trainer(trainer)
    description = _normalise_description(description)
    trained_at = scorer.meta.get("trained_at") or datetime.now().isoformat(timespec="seconds")
    while True:
        model_id = build_model_id(trained_at, trainer, description, registry)
        scorer.meta.update(
            model_id=model_id,
            model_name=model_id,
            trainer=trainer,
            description=description,
            trained_at=trained_at,
        )
        version_path = registry / f"{model_id}.json"
        payload = {
            "weights": scorer.weights,
            "bias": scorer.bias,
            "feature_names": list(FEATURE_NAMES),
            "meta": scorer.meta,
        }
        try:
            with version_path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            break
        except FileExistsError:
            continue
    if activate:
        scorer.save(active)
    return model_id, version_path


def activate_model_version(model_id: str, active_path=None) -> Tuple[Path, Dict]:
    """Copy a registry version to the canonical active scorer path."""
    active = Path(active_path) if active_path else default_scorer_path()
    name = str(model_id or "").strip()
    if not name or Path(name).name != name or name.endswith(".json"):
        raise ValueError("Invalid model id")
    source = model_registry_dir(active) / f"{name}.json"
    if not source.exists():
        raise FileNotFoundError(name)
    active.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, active)
    return source, ConfluenceScorer.load(source).meta


def ensure_model_registry_seeded(active_path=None) -> None:
    """Archive a legacy active-only scorer as the first registry version."""
    active = Path(active_path) if active_path else default_scorer_path()
    if not active.exists():
        return
    scorer = ConfluenceScorer.load(active)
    registry = model_registry_dir(active)
    current_id = str(scorer.meta.get("model_id") or "").strip()
    if Path(current_id).name != current_id or current_id.endswith(".json"):
        current_id = ""
    if current_id and (registry / f"{current_id}.json").exists():
        return
    cfg = scorer.meta.get("cfg") or {}
    trainer = scorer.meta.get("trainer") or "codex"
    description = scorer.meta.get("description")
    if not description:
        rr = float(cfg.get("rr", 3.0))
        band = float(cfg.get("band_ticks", 4.0))
        tf = int(cfg.get("min_distinct_tf", 2))
        description = f"RR{rr:g} Band{band:g} MinTF{tf} production baseline"
    if current_id:
        registry.mkdir(parents=True, exist_ok=True)
        scorer.meta.update(
            model_name=current_id,
            trainer=_normalise_trainer(trainer),
            description=_normalise_description(description),
        )
        scorer.save(registry / f"{current_id}.json")
        scorer.save(active)
        return
    save_model_version(
        scorer, trainer, description, activate=True, active_path=active,
    )


def list_model_versions(active_path=None) -> Tuple[List[Tuple[Path, "ConfluenceScorer"]], str]:
    """Return newest-first registry versions and the active model id."""
    active = Path(active_path) if active_path else default_scorer_path()
    ensure_model_registry_seeded(active)
    active_id = ""
    if active.exists():
        active_id = str(ConfluenceScorer.load(active).meta.get("model_id") or "")
    versions = []
    registry = model_registry_dir(active)
    if registry.exists():
        for path in registry.glob("*.json"):
            try:
                versions.append((path, ConfluenceScorer.load(path)))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    versions.sort(
        key=lambda item: (
            str(item[1].meta.get("trained_at") or ""),
            item[0].stem,
        ),
        reverse=True,
    )
    return versions, active_id


def resolve_scorer(use_scorer: bool, rr_grid=None, scorer_path=None) -> "ConfluenceScorer":
    """Load the active production scorer shared by live and backtest."""
    if scorer_path:
        return ConfluenceScorer.load_or_heuristic(scorer_path, use_scorer)
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
        return str(self.meta.get("model_id") or "confluence_scorer.json")

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
