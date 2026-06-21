"""Reasoned parameter optimization for the current ML confluence UI.

The expensive work (zones -> clusters -> geometry -> features) is performed
once. For every bar and RR 1..6 we retain only the exact score/risk Pareto
frontier per direction. Replays then reproduce the production order lifecycle
without re-running signal extraction.

Outputs:
  data/machinelearning/parameter_study_progress.txt
  data/machinelearning/parameter_study_latest.json
  data/machinelearning/parameter_study_results.csv
"""

from __future__ import annotations

import csv
import json
import math
import os
import pickle
import sys
import time
from array import array
from dataclasses import asdict, dataclass
from datetime import timezone, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.backtest.intrabar import resolve_same_bar_exit
from backend.db.models import (
    get_commission_rt,
    get_fees_rt,
    get_point_value,
    get_tick_size,
)
from backend.strategy.confluence import (
    MAX_RECENCY_DEPTH,
    ConfluenceConfig,
    _make_signal,
    _signal_geometry,
    cluster_levels,
    extract_levels,
)
from backend.strategy.confluence_features import CONTEXT_WINDOW, extract_features
from backend.strategy.confluence_scorer import (
    ConfluenceScorer,
    default_scorer_path,
    list_model_versions,
)
from backend.strategy.consolidation import timeframes_for_base
from backend.backtest.confluence_backtest import build_zone_timeline
from scripts.confluence_common import load_store


CONTRACT = "CON.F.US.MNQ.M26"
SIZE = 3
TICK = get_tick_size(CONTRACT)
POINT_VALUE = get_point_value(CONTRACT)
ROUND_TRIP_COST = get_commission_rt(CONTRACT) + get_fees_rt(CONTRACT)
WAIT_BARS = 1  # live parity: conf_wait_minutes=1, base=1m
RRS = tuple(range(1, 7))
TARGET_MAX_DD = 2_000.0
MIN_TRADES = 20
_CT = ZoneInfo("America/Chicago")
ZONE_NAMES = tuple(timeframes_for_base(1))
ZONE_TO_CODE = {tf: i for i, tf in enumerate(ZONE_NAMES)}

# Frontend runtime controls. min_prob=0.50 is intentionally omitted because the
# backend maps both OFF (0) and 0.50 to score >= 0 / probability >= 50%.
GATES = (
    ("prob_off", "prob", 0.50),
    ("prob_0.55", "prob", 0.55),
    ("prob_0.60", "prob", 0.60),
    ("prob_0.65", "prob", 0.65),
    ("ev_0.00", "ev", 0.00),
    ("ev_0.10", "ev", 0.10),
    ("ev_0.20", "ev", 0.20),
    ("ev_0.30", "ev", 0.30),
)
STYLES = (
    (False, True),
    (True, True),
    (False, False),
    (True, False),
)
COARSE_RISK_CAPS = (0, 40, 80, 120, 160, 200, 240, 300, 400)

OUT_DIR = ROOT / "data" / "machinelearning"
PROGRESS = OUT_DIR / "parameter_study_progress.txt"
RESULT_JSON = OUT_DIR / "parameter_study_latest.json"
RESULT_CSV = OUT_DIR / "parameter_study_results.csv"
CHECKPOINT = OUT_DIR / "parameter_study_checkpoint.pkl"
LOCK = OUT_DIR / "parameter_study.lock"


@dataclass(slots=True)
class Candidate:
    direction: str
    zone_id: str
    mode: str
    entry: float
    sl: float
    score: float
    prob: float
    risk_ticks: float


MODE_TO_CODE = {"momentum": 0, "reversion": 1, "breakout": 2}
CODE_TO_MODE = ("momentum", "reversion", "breakout")
DIR_TO_INDEX = {"BUY": 0, "SELL": 1}


@dataclass
class CompactBook:
    """Compact per-RR frontier storage (~20 bytes/candidate, not Python objects)."""

    offsets: np.ndarray
    entry: array
    sl: array
    score: array
    prob: array
    risk: array
    zone: array
    mode: array

    @classmethod
    def create(cls, n_bars: int) -> "CompactBook":
        return cls(
            offsets=np.full((n_bars, 2, 2), -1, dtype=np.int32),
            entry=array("f"),
            sl=array("f"),
            score=array("d"),
            prob=array("d"),
            risk=array("f"),
            zone=array("B"),
            mode=array("B"),
        )

    def append_frontier(
        self,
        bar_index: int,
        direction_index: int,
        frontier: Sequence[Candidate],
    ) -> None:
        if not frontier:
            return
        start = len(self.entry)
        self.offsets[bar_index, direction_index, 0] = start
        self.offsets[bar_index, direction_index, 1] = len(frontier)
        for candidate in frontier:
            self.entry.append(candidate.entry)
            self.sl.append(candidate.sl)
            self.score.append(candidate.score)
            self.prob.append(candidate.prob)
            self.risk.append(candidate.risk_ticks)
            self.zone.append(ZONE_TO_CODE.get(candidate.zone_id, 255))
            self.mode.append(MODE_TO_CODE[candidate.mode])

    def best(
        self,
        bar_index: int,
        direction: str,
        max_risk_ticks: int,
    ) -> Optional[Candidate]:
        direction_index = DIR_TO_INDEX[direction]
        start = int(self.offsets[bar_index, direction_index, 0])
        count = int(self.offsets[bar_index, direction_index, 1])
        if start < 0 or count <= 0:
            return None
        end = start + count
        for index in range(start, end):
            if not max_risk_ticks or self.risk[index] <= max_risk_ticks:
                return Candidate(
                    direction=direction,
                    zone_id=ZONE_NAMES[self.zone[index]] if self.zone[index] < len(ZONE_NAMES) else "unknown",
                    mode=CODE_TO_MODE[self.mode[index]],
                    entry=float(self.entry[index]),
                    sl=float(self.sl[index]),
                    score=float(self.score[index]),
                    prob=float(self.prob[index]),
                    risk_ticks=float(self.risk[index]),
                )
        return None


@dataclass(frozen=True, slots=True)
class Params:
    model_id: str
    rr: int
    gate: str
    gate_kind: str
    gate_value: float
    max_risk_ticks: int
    trail: bool
    session_limit: bool


_log_handle = None


def log(message: str) -> None:
    global _log_handle
    if _log_handle is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        _log_handle = PROGRESS.open("w", encoding="utf-8")
    _log_handle.write(message + "\n")
    _log_handle.flush()
    os.fsync(_log_handle.fileno())
    print(message, flush=True)


def acquire_lock() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise SystemExit(f"Study already running; lock exists: {LOCK}") from exc
    os.write(fd, str(os.getpid()).encode("ascii"))
    return fd


def _frontier(candidates: Iterable[Candidate]) -> Tuple[Candidate, ...]:
    """Exact best-score lookup under any upper risk cap.

    A lower-scored candidate is useful only when it has less risk than every
    higher-scored candidate. The resulting frontier is normally tiny.
    """
    ranked = sorted(candidates, key=lambda item: item.score, reverse=True)
    kept: List[Candidate] = []
    least_risk = math.inf
    for candidate in ranked:
        if candidate.risk_ticks + 1e-9 < least_risk:
            kept.append(candidate)
            least_risk = candidate.risk_ticks
    return tuple(kept)


def _model_paths() -> List[Tuple[Path, ConfluenceScorer]]:
    versions, active_id = list_model_versions()
    active = []
    older = []
    for path, scorer in versions:
        model_id = str(scorer.meta.get("model_id") or path.stem)
        (active if model_id == active_id else older).append((path, scorer))
    # There are currently two selectable versions. Keep every UI-selectable
    # model, active first.
    return active + older


def _checkpoint_key(candles, models) -> dict:
    return {
        "storage_version": 3,
        "execution_parity": "live_zone_direction_lock_wait1",
        "bars": len(candles),
        "start": candles[0].timestamp.isoformat(),
        "end": candles[-1].timestamp.isoformat(),
        "models": [
            {
                "id": scorer.meta.get("model_id", path.stem),
                "trained_at": scorer.meta.get("trained_at"),
            }
            for path, scorer in models
        ],
        "rrs": RRS,
        "band": 4.0,
        "min_tf": 2,
        "breakout": False,
    }


def precompute_books(candles, timeline, models):
    """Return compact books[model_id][rr] with exact per-bar frontiers."""
    cfg = ConfluenceConfig(band_ticks=4.0, min_distinct_tf=2, rr=6.0)
    cfg.direction_mode = "auto"
    cfg.tick_size = TICK
    cfg.enable_breakout = False
    cfg.max_risk_ticks = None
    modes = cfg.auto_modes()
    model_items = [
        (str(scorer.meta.get("model_id") or path.stem), scorer)
        for path, scorer in models
    ]
    books = {
        model_id: {rr: CompactBook.create(len(candles)) for rr in RRS}
        for model_id, _ in model_items
    }
    total_frontier = {model_id: {rr: 0 for rr in RRS} for model_id, _ in model_items}
    t0 = time.perf_counter()
    step = max(1, len(candles) // 20)

    for index, candle in enumerate(candles):
        zones = timeline[index]
        if len(zones) < cfg.min_distinct_tf:
            continue
        recent = candles[max(0, index - CONTEXT_WINDOW + 1):index + 1]
        levels = extract_levels(zones, cfg)
        if not levels:
            continue
        clusters = cluster_levels(levels, cfg)
        if not clusters:
            continue
        per_model_rr = {
            model_id: {rr: {"BUY": [], "SELL": []} for rr in RRS}
            for model_id, _ in model_items
        }
        for cluster in clusters:
            for mode in modes:
                geometry = _signal_geometry(
                    cluster, candle.close, zones, mode, cfg,
                    recent_candles=recent,
                )
                if geometry is None:
                    continue
                # RR6 reveals the nearest obstacle up to the maximum UI RR.
                signal6 = _make_signal(cluster, mode, geometry, 6.0)
                features = extract_features(
                    signal6,
                    candle.close,
                    TICK,
                    levels=levels,
                    recent_candles=recent,
                )
                obstacle_cap = float(features["dist_to_obstacle_R"])
                direction = signal6.direction.value.upper()
                risk_ticks = abs(signal6.entry_price - signal6.sl_price) / TICK
                for rr in RRS:
                    features["rr"] = float(rr)
                    features["dist_to_obstacle_R"] = min(obstacle_cap, float(rr))
                    for model_id, scorer in model_items:
                        score = scorer.score(features)
                        prob = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, score))))
                        per_model_rr[model_id][rr][direction].append(Candidate(
                            direction=direction,
                            zone_id=signal6.cluster.largest_tf,
                            mode=mode,
                            entry=signal6.entry_price,
                            sl=signal6.sl_price,
                            score=score,
                            prob=prob,
                            risk_ticks=risk_ticks,
                        ))

        for model_id, _ in model_items:
            for rr in RRS:
                buy = _frontier(per_model_rr[model_id][rr]["BUY"])
                sell = _frontier(per_model_rr[model_id][rr]["SELL"])
                books[model_id][rr].append_frontier(index, 0, buy)
                books[model_id][rr].append_frontier(index, 1, sell)
                total_frontier[model_id][rr] += len(buy) + len(sell)

        if (index + 1) % step == 0:
            elapsed = time.perf_counter() - t0
            rate = (index + 1) / elapsed
            eta = (len(candles) - index - 1) / rate
            counts = ", ".join(
                f"{mid[-8:]}:{sum(total_frontier[mid].values())}"
                for mid, _ in model_items
            )
            log(
                f"  {index + 1}/{len(candles)} ({100 * (index + 1) // len(candles)}%) "
                f"{rate:.0f} bars/s ETA {eta:.0f}s frontiers={counts}"
            )

    log(f"Precompute complete in {time.perf_counter() - t0:.0f}s")
    for model_id, _ in model_items:
        log(
            f"  {model_id}: "
            + ", ".join(f"RR{rr}={total_frontier[model_id][rr]}" for rr in RRS)
        )
    return books


def load_or_build_books(candles, timeline, models):
    expected = _checkpoint_key(candles, models)
    if CHECKPOINT.exists():
        try:
            with CHECKPOINT.open("rb") as handle:
                payload = pickle.load(handle)
            if payload.get("key") == expected:
                log(f"Loaded compatible checkpoint: {CHECKPOINT}")
                return payload["books"]
        except Exception as exc:  # noqa: BLE001
            log(f"Ignoring unreadable checkpoint: {exc}")
    books = precompute_books(candles, timeline, models)
    tmp = CHECKPOINT.with_suffix(".pkl.tmp")
    with tmp.open("wb") as handle:
        pickle.dump({"key": expected, "books": books}, handle, protocol=pickle.HIGHEST_PROTOCOL)
    tmp.replace(CHECKPOINT)
    log(f"Saved checkpoint: {CHECKPOINT}")
    return books


def _passes_gate(candidate: Candidate, params: Params) -> bool:
    if params.gate_kind == "prob":
        return candidate.prob >= params.gate_value
    ev = candidate.prob * params.rr - (1.0 - candidate.prob)
    return ev >= params.gate_value


def replay(
    candles,
    books,
    params: Params,
    *,
    start: int = 0,
    end: Optional[int] = None,
) -> dict:
    """Production-equivalent order/fill/exit replay for one parameter set."""
    end = len(candles) if end is None else min(end, len(candles))
    edge = WAIT_BARS + 2
    pnls: List[float] = []
    risks: List[float] = []
    modes: Dict[str, int] = {}
    exits: Dict[str, int] = {}
    capital = peak = max_dd = 0.0
    open_candidate: Optional[Candidate] = None
    open_entry = open_sl = open_tp = 0.0
    trail_triggered = False
    pending: Optional[Candidate] = None
    pending_age = 0
    session_used = set()

    def session_key(timestamp) -> str:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        ct = timestamp.astimezone(_CT)
        if ct.hour >= 17:
            ct = ct + timedelta(days=1)
        return ct.strftime("%Y-%m-%d")

    def lock_key(timestamp, candidate: Candidate):
        direction = "up" if candidate.direction == "BUY" else "down"
        return (session_key(timestamp), str(candidate.zone_id), direction)

    def close(price: float, reason: str) -> None:
        nonlocal capital, peak, max_dd, open_candidate, trail_triggered
        direction = open_candidate.direction
        gross = (
            price - open_entry if direction == "BUY" else open_entry - price
        ) * POINT_VALUE * SIZE
        pnl = gross - ROUND_TRIP_COST * SIZE
        pnls.append(pnl)
        capital += pnl
        peak = max(peak, capital)
        max_dd = max(max_dd, peak - capital)
        exits[reason] = exits.get(reason, 0) + 1
        open_candidate = None
        trail_triggered = False

    for index in range(start, end):
        candle = candles[index]

        if open_candidate is not None:
            if open_candidate.direction == "BUY":
                hit_sl = candle.low <= open_sl
                hit_tp = candle.high >= open_tp
            else:
                hit_sl = candle.high >= open_sl
                hit_tp = candle.low <= open_tp
            sl_reason = "trail_sl" if trail_triggered else "sl"
            if hit_sl and hit_tp:
                if resolve_same_bar_exit(candle.open, open_sl, open_tp) == "sl":
                    close(open_sl, sl_reason)
                else:
                    close(open_tp, "tp")
            elif hit_sl:
                close(open_sl, sl_reason)
            elif hit_tp:
                close(open_tp, "tp")
            elif params.trail:
                tp_distance = abs(open_tp - open_entry)
                moved = (
                    candle.close - open_entry
                    if open_candidate.direction == "BUY"
                    else open_entry - candle.close
                )
                if not trail_triggered and moved >= 0.50 * tp_distance:
                    lock = 0.05 * tp_distance
                    open_sl = (
                        open_entry + lock
                        if open_candidate.direction == "BUY"
                        else open_entry - lock
                    )
                    trail_triggered = True
            if open_candidate is not None:
                continue

        if pending is not None:
            filled = (
                candle.low <= pending.entry
                if pending.direction == "BUY"
                else candle.high >= pending.entry
            )
            if filled:
                open_candidate = pending
                open_entry = pending.entry
                open_sl = pending.sl
                risk = abs(open_entry - open_sl)
                open_tp = (
                    open_entry + params.rr * risk
                    if pending.direction == "BUY"
                    else open_entry - params.rr * risk
                )
                risks.append(pending.risk_ticks)
                modes[pending.mode] = modes.get(pending.mode, 0) + 1
                trail_triggered = False
                pending = None
                pending_age = 0
                # Production permits only an immediate SL on the fill candle.
                if open_candidate.direction == "BUY" and candle.low <= open_sl:
                    close(open_sl, "sl")
                elif open_candidate.direction == "SELL" and candle.high >= open_sl:
                    close(open_sl, "sl")
                continue
            pending_age += 1
            if pending_age >= WAIT_BARS:
                if params.session_limit:
                    session_used.discard(lock_key(candle.timestamp, pending))
                pending = None
                pending_age = 0
            continue

        if index >= end - edge:
            continue
        candidates = []
        for direction in ("BUY", "SELL"):
            candidate = books.best(index, direction, params.max_risk_ticks)
            if candidate is not None and _passes_gate(candidate, params):
                if params.session_limit and lock_key(candle.timestamp, candidate) in session_used:
                    continue
                candidates.append(candidate)
        if candidates:
            pending = max(candidates, key=lambda item: item.score)
            if params.session_limit:
                session_used.add(lock_key(candle.timestamp, pending))
            pending_age = 0

    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    consecutive = current = 0
    for pnl in pnls:
        if pnl <= 0:
            current += 1
            consecutive = max(consecutive, current)
        else:
            current = 0
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "pnl": sum(pnls),
        "max_drawdown": max_dd,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (999.0 if gross_profit > 0 else 0.0)
        ),
        "calmar": sum(pnls) / max_dd if sum(pnls) > 0 and max_dd > 0 else 0.0,
        "expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "max_consecutive_losses": consecutive,
        "mean_risk_ticks": sum(risks) / len(risks) if risks else 0.0,
        "max_risk_ticks": max(risks) if risks else 0.0,
        "modes": modes,
        "exits": exits,
    }


def _full_rank(row: dict) -> tuple:
    metrics = row["metrics"]
    qualified = (
        metrics["pnl"] > 0
        and metrics["max_drawdown"] <= TARGET_MAX_DD
        and metrics["trades"] >= MIN_TRADES
        and metrics["profit_factor"] > 1.0
    )
    return (
        int(qualified),
        metrics["calmar"],
        metrics["pnl"],
        metrics["profit_factor"],
        -metrics["max_drawdown"],
        metrics["trades"],
    )


def _robust_rank(row: dict) -> tuple:
    metrics = row["metrics"]
    folds = row.get("folds") or []
    positive_folds = sum(1 for fold in folds if fold["pnl"] > 0)
    worst_fold = min((fold["pnl"] for fold in folds), default=-math.inf)
    qualified = (
        metrics["pnl"] > 0
        and metrics["max_drawdown"] <= TARGET_MAX_DD
        and metrics["trades"] >= MIN_TRADES
        and metrics["profit_factor"] > 1.0
        and positive_folds >= 2
    )
    return (
        int(qualified),
        positive_folds,
        worst_fold,
        metrics["calmar"],
        metrics["pnl"],
        -metrics["max_drawdown"],
    )


def _row(params: Params, metrics: dict, stage: str) -> dict:
    return {"stage": stage, "params": asdict(params), "metrics": metrics}


def core_sweep(candles, model_id: str, books_by_rr) -> List[dict]:
    rows = []
    for rr in RRS:
        for gate, gate_kind, gate_value in GATES:
            for trail, session_limit in STYLES:
                params = Params(
                    model_id=model_id,
                    rr=rr,
                    gate=gate,
                    gate_kind=gate_kind,
                    gate_value=gate_value,
                    max_risk_ticks=0,
                    trail=trail,
                    session_limit=session_limit,
                )
                rows.append(_row(
                    params,
                    replay(candles, books_by_rr[rr], params),
                    "core_no_risk_cap",
                ))
    return rows


def select_risk_families(core: List[dict]) -> List[Params]:
    """Reasoned elimination before max-risk scanning.

    Keep the top two styles for every RR+gate family plus the strongest overall
    rows. This preserves broad RR/gate coverage without blindly crossing every
    weak style with every risk cap.
    """
    selected = {}
    groups: Dict[Tuple[int, str], List[dict]] = {}
    for row in core:
        p = row["params"]
        groups.setdefault((p["rr"], p["gate"]), []).append(row)
    for rows in groups.values():
        for row in sorted(rows, key=_full_rank, reverse=True)[:2]:
            params = Params(**row["params"])
            selected[params] = params
    for row in sorted(core, key=_full_rank, reverse=True)[:40]:
        params = Params(**row["params"])
        selected[params] = params
    return list(selected.values())


def risk_sweep(candles, books_by_rr, families: Sequence[Params]) -> List[dict]:
    rows = []
    for family in families:
        for cap in COARSE_RISK_CAPS:
            params = Params(
                model_id=family.model_id,
                rr=family.rr,
                gate=family.gate,
                gate_kind=family.gate_kind,
                gate_value=family.gate_value,
                max_risk_ticks=cap,
                trail=family.trail,
                session_limit=family.session_limit,
            )
            rows.append(_row(
                params,
                replay(candles, books_by_rr[params.rr], params),
                "coarse_risk",
            ))
    return rows


def fine_risk_sweep(candles, books_by_rr, coarse: Sequence[dict]) -> List[dict]:
    rows = []
    seen = set()
    for row in sorted(coarse, key=_full_rank, reverse=True)[:30]:
        base = Params(**row["params"])
        center = base.max_risk_ticks
        if center == 0:
            caps = (0, 320, 340, 360, 380, 400)
        else:
            caps = tuple(
                cap for cap in range(max(10, center - 30), min(400, center + 30) + 1, 10)
            )
        for cap in caps:
            params = Params(
                model_id=base.model_id,
                rr=base.rr,
                gate=base.gate,
                gate_kind=base.gate_kind,
                gate_value=base.gate_value,
                max_risk_ticks=cap,
                trail=base.trail,
                session_limit=base.session_limit,
            )
            if params in seen:
                continue
            seen.add(params)
            rows.append(_row(
                params,
                replay(candles, books_by_rr[params.rr], params),
                "fine_risk",
            ))
    return rows


def add_fold_metrics(candles, books_by_rr, rows: List[dict]) -> None:
    n = len(candles)
    bounds = (0, n // 3, 2 * n // 3, n)
    for row in rows:
        params = Params(**row["params"])
        row["folds"] = [
            replay(
                candles,
                books_by_rr[params.rr],
                params,
                start=bounds[index],
                end=bounds[index + 1],
            )
            for index in range(3)
        ]
        row["positive_folds"] = sum(1 for fold in row["folds"] if fold["pnl"] > 0)
        row["worst_fold_pnl"] = min(fold["pnl"] for fold in row["folds"])


def parameter_effects(core: Sequence[dict], risk_rows: Sequence[dict]) -> dict:
    def summarize(rows: Sequence[dict], field: str) -> List[dict]:
        groups: Dict[str, List[dict]] = {}
        for row in rows:
            key = str(row["params"][field])
            groups.setdefault(key, []).append(row["metrics"])
        output = []
        for value, metrics in groups.items():
            output.append({
                "value": value,
                "n": len(metrics),
                "mean_pnl": sum(m["pnl"] for m in metrics) / len(metrics),
                "mean_calmar": sum(m["calmar"] for m in metrics) / len(metrics),
                "mean_max_drawdown": sum(m["max_drawdown"] for m in metrics) / len(metrics),
                "profitable_rate": sum(m["pnl"] > 0 for m in metrics) / len(metrics),
            })
        return sorted(output, key=lambda item: item["mean_calmar"], reverse=True)

    return {
        "core_rr": summarize(core, "rr"),
        "core_gate": summarize(core, "gate"),
        "core_trail": summarize(core, "trail"),
        "core_session_limit": summarize(core, "session_limit"),
        "selected_family_max_risk": summarize(risk_rows, "max_risk_ticks"),
    }


def write_outputs(payload: dict, rows: Sequence[dict]) -> None:
    RESULT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    columns = [
        "rank", "stage", "model_id", "rr", "gate", "max_risk_ticks", "trail",
        "session_limit", "trades", "win_rate", "pnl", "max_drawdown",
        "profit_factor", "calmar", "expectancy", "max_consecutive_losses",
        "positive_folds", "worst_fold_pnl",
    ]
    with RESULT_CSV.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for rank, row in enumerate(rows, 1):
            p = row["params"]
            m = row["metrics"]
            writer.writerow({
                "rank": rank,
                "stage": row["stage"],
                "model_id": p["model_id"],
                "rr": p["rr"],
                "gate": p["gate"],
                "max_risk_ticks": p["max_risk_ticks"],
                "trail": p["trail"],
                "session_limit": p["session_limit"],
                "trades": m["trades"],
                "win_rate": m["win_rate"],
                "pnl": m["pnl"],
                "max_drawdown": m["max_drawdown"],
                "profit_factor": m["profit_factor"],
                "calmar": m["calmar"],
                "expectancy": m["expectancy"],
                "max_consecutive_losses": m["max_consecutive_losses"],
                "positive_folds": row.get("positive_folds", ""),
                "worst_fold_pnl": row.get("worst_fold_pnl", ""),
            })


def main() -> None:
    lock_fd = acquire_lock()
    started = time.perf_counter()
    try:
        log("=" * 96)
        log("ML CONFLUENCE PARAMETER OPTIMIZATION STUDY")
        log("=" * 96)
        candles = sorted(load_store(1, "MNQ"), key=lambda candle: candle.timestamp)
        if not candles:
            raise SystemExit("No MNQ accumulated store data")
        models = _model_paths()
        if not models:
            raise SystemExit("No model versions found")
        log(
            f"Data: {len(candles)} bars "
            f"({candles[0].timestamp.isoformat()} .. {candles[-1].timestamp.isoformat()})"
        )
        log(
            f"Economics: {SIZE} MNQ, point=${POINT_VALUE:g}, "
            f"round-trip cost=${ROUND_TRIP_COST:g}/contract, target DD<=${TARGET_MAX_DD:,.0f}"
        )
        for path, scorer in models:
            log(
                f"Model: {scorer.meta.get('model_id', path.stem)} "
                f"trained={scorer.meta.get('trained_at')} OOS_AUC={scorer.meta.get('oos_auc')}"
            )

        log("\nBuilding shared zone timeline...")
        timeline_start = time.perf_counter()
        timeline = build_zone_timeline(
            candles,
            timeframes_for_base(1),
            TICK,
            MAX_RECENCY_DEPTH,
        )
        log(f"Timeline complete in {time.perf_counter() - timeline_start:.0f}s")

        log("\nBuilding exact RR/model score-risk frontiers...")
        books = load_or_build_books(candles, timeline, models)

        all_rows = []
        per_model = {}
        for path, scorer in models:
            model_id = str(scorer.meta.get("model_id") or path.stem)
            log(f"\n--- {model_id}: core sweep (no max-risk cap) ---")
            core = core_sweep(candles, model_id, books[model_id])
            families = select_risk_families(core)
            log(
                f"Core: {len(core)} combinations; retained {len(families)} "
                "RR/gate/style families for risk study"
            )
            coarse = risk_sweep(candles, books[model_id], families)
            log(f"Coarse risk: {len(coarse)} combinations")
            fine = fine_risk_sweep(candles, books[model_id], coarse)
            log(f"Fine risk: {len(fine)} combinations")
            unique = {}
            for row in core + coarse + fine:
                unique[Params(**row["params"])] = row
            ranked_full = sorted(unique.values(), key=_full_rank, reverse=True)
            finalists = ranked_full[:80]
            add_fold_metrics(candles, books[model_id], finalists)
            ranked_robust = sorted(finalists, key=_robust_rank, reverse=True)
            per_model[model_id] = {
                "model_meta": scorer.meta,
                "counts": {
                    "core": len(core),
                    "risk_families": len(families),
                    "coarse": len(coarse),
                    "fine": len(fine),
                    "unique": len(unique),
                },
                "effects": parameter_effects(core, coarse + fine),
                "top": ranked_robust[:30],
            }
            all_rows.extend(ranked_robust)
            best = ranked_robust[0]
            p, m = best["params"], best["metrics"]
            log(
                f"Best robust: RR{p['rr']} {p['gate']} risk={p['max_risk_ticks'] or 'OFF'} "
                f"trail={'ON' if p['trail'] else 'OFF'} "
                f"session={'ON' if p['session_limit'] else 'OFF'} | "
                f"{m['trades']} trades PnL=${m['pnl']:,.0f} DD=${m['max_drawdown']:,.0f} "
                f"PF={m['profit_factor']:.2f} Calmar={m['calmar']:.2f} "
                f"positive_folds={best['positive_folds']}/3"
            )

        ranked_all = sorted(all_rows, key=_robust_rank, reverse=True)
        payload = {
            "kind": "ml_confluence_parameter_optimization",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_seconds": time.perf_counter() - started,
            "data": {
                "bars": len(candles),
                "start": candles[0].timestamp.isoformat(),
                "end": candles[-1].timestamp.isoformat(),
            },
            "objective": {
                "contract": CONTRACT,
                "size": SIZE,
                "target_max_drawdown": TARGET_MAX_DD,
                "minimum_trades": MIN_TRADES,
                "robustness": "positive PnL in at least 2 of 3 chronological folds",
            },
            "fixed": {
                "band_ticks": 4,
                "min_distinct_tf": 2,
                "base_minutes": 1,
                "wait_minutes": 1,
                "breakout": False,
                "trail_trigger_when_on": 0.50,
                "trail_lock_when_on": 0.05,
            },
            "eliminated_by_reasoning": {
                "min_prob_0.50": "backend-identical to MIN PROB OFF",
                "trainer_description_loss_weight": "training controls, not runtime strategy knobs",
                "full_tp_lock": "backend field exists but no ML panel control is exposed",
                "band_min_tf": "current UI/model expose only Band=4 and MinTF=2",
                "size": "linear risk scaling; evaluated after selecting signal parameters",
            },
            "models": per_model,
            "top_overall": ranked_all[:40],
        }
        write_outputs(payload, ranked_all)
        log(f"\nSaved JSON: {RESULT_JSON}")
        log(f"Saved CSV: {RESULT_CSV}")
        log(f"Total elapsed: {time.perf_counter() - started:.0f}s")
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        try:
            LOCK.unlink(missing_ok=True)
        except OSError:
            pass
        if _log_handle is not None:
            _log_handle.close()


if __name__ == "__main__":
    main()
