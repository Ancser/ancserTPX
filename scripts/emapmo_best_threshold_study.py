# ============================================================
# Research-only EMAPMO BEST threshold / risk / hold study.
#
# This file intentionally does not modify production strategy code, presets,
# routes, or live state.  Every variant uses a fresh production BacktestEngine
# with an in-memory FactorSignalStrategy subclass.
# ============================================================

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    Direction,
    StrategyParams,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.factor import FactorSignalStrategy, _topstep_trade_date, _utc
from backend.strategy.session_filter import (
    MARKET_CLOCK_VERSION,
    MARKET_PHASE_FLATTEN,
    is_allowed_session,
    market_close_phase,
)
from backend.terminal_live import _build_strategy_params


THETAS = (1.0, 0.9, 0.8, 0.7, 0.6)
SL_GRID = (1.5, 1.75, 2.0, 2.25, 2.5)
RR_GRID = (0.8, 1.0, 1.5, 2.0, 3.0)
HOLD_GRID = (0, 12, 24, 48, 96)  # HOFF, 60m, 120m, 240m, 480m
DAILY_POLICIES = (
    ("one_per_day", 1, 0),
    ("original", 3, 1),
    ("retry_after_one_loss", 3, 2),
    ("no_loss_lock", 3, 0),
)
UTC = timezone.utc


class ResearchThresholdFactor(FactorSignalStrategy):
    """Production FACTOR with only the long EMAPMO depth threshold varied."""

    def __init__(self, params=None):
        self.research_long_threshold = float(
            getattr(params, "_research_long_threshold", -0.10)
        )
        super().__init__(params=params)

    def _emapmo_snapshot(self) -> dict[str, Any]:
        snapshot = super()._emapmo_snapshot()
        pmo = snapshot.get("pmo")
        signal = snapshot.get("signal")
        prev_pmo = snapshot.get("prev_pmo")
        prev_signal = snapshot.get("prev_signal")
        q_now = snapshot.get("q_gap_now")
        q_prev = snapshot.get("q_gap_prev")
        q_prev2 = snapshot.get("q_gap_prev2")
        if None not in (pmo, signal, prev_pmo, prev_signal):
            snapshot["normal_long"] = bool(
                float(pmo) < self.research_long_threshold
                and float(pmo) > float(signal)
                and float(prev_pmo) <= float(prev_signal)
            )
        if None not in (pmo, signal, q_now, q_prev, q_prev2):
            snapshot["early_long"] = bool(
                float(signal) < self.research_long_threshold
                and float(pmo) < float(signal)
                and float(q_now) < float(q_prev) < float(q_prev2)
            )
        return snapshot


def _load_best_params() -> StrategyParams:
    data = json.loads((ROOT / "data" / "presets.json").read_text(encoding="utf-8"))
    preset = data["presets"]["BEST"]
    return _build_strategy_params(preset, str(preset.get("contract_id") or ""))


def _make_config(params: StrategyParams) -> BacktestConfig:
    cid = params.contract_id
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=50_000.0,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(ordered[lo])
    frac = pos - lo
    return float(ordered[lo] * (1.0 - frac) + ordered[hi] * frac)


def _sequence_stats(pnls: Iterable[float]) -> dict[str, float]:
    values = [float(x) for x in pnls]
    gain = sum(x for x in values if x > 0)
    loss = sum(-x for x in values if x < 0)
    equity = peak = max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "pnl": round(sum(values), 2),
        "pf": round(gain / loss, 4) if loss > 0 else (999.0 if gain > 0 else 0.0),
        "max_dd": round(max_dd, 2),
    }


def _reason_name(reason: Any) -> str:
    value = getattr(reason, "value", reason)
    return str(value or "unknown").lower()


def _trade_fingerprint(trades) -> str:
    rows = []
    for trade in trades:
        rows.append([
            trade.entry_time.isoformat(),
            trade.exit_time.isoformat() if trade.exit_time else None,
            round(float(trade.entry_price), 6),
            round(float(trade.exit_price or 0.0), 6),
            round(float(trade.pnl or 0.0), 6),
            _reason_name(trade.exit_reason),
        ])
    raw = json.dumps(rows, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _summarize_result(result, params: StrategyParams, job: dict[str, Any], candles) -> dict[str, Any]:
    trades = list(result.trades)
    pnls = [float(t.pnl or 0.0) for t in trades]
    day_pnl: dict[str, float] = defaultdict(float)
    day_count: dict[str, int] = defaultdict(int)
    week_pnl: dict[str, float] = defaultdict(float)
    holds: list[float] = []
    net_rs: list[float] = []
    exits: Counter[str] = Counter()
    hold_limit_min = int(job["hold_bars"]) * 5

    first_day = date.fromisoformat(_topstep_trade_date(candles[0].timestamp))
    last_day = date.fromisoformat(_topstep_trade_date(candles[-1].timestamp))
    span_days = max(1, (last_day - first_day).days + 1)
    seg_pnls = [0.0, 0.0, 0.0]
    seg_gains = [0.0, 0.0, 0.0]
    seg_losses = [0.0, 0.0, 0.0]

    for trade in trades:
        pnl = float(trade.pnl or 0.0)
        dkey = _topstep_trade_date(trade.entry_time)
        dvalue = date.fromisoformat(dkey)
        day_pnl[dkey] += pnl
        day_count[dkey] += 1
        week_pnl[dvalue.strftime("%G-W%V")] += pnl
        offset = max(0, (dvalue - first_day).days)
        seg = min(2, int(offset * 3 / span_days))
        seg_pnls[seg] += pnl
        if pnl > 0:
            seg_gains[seg] += pnl
        elif pnl < 0:
            seg_losses[seg] += -pnl

        duration = float(trade.duration_minutes or 0)
        holds.append(duration)
        reason = _reason_name(trade.exit_reason)
        if reason == "flatten" and hold_limit_min > 0 and duration >= hold_limit_min - 1:
            exits["time_limit"] += 1
        elif reason == "flatten":
            exits["daily_flatten"] += 1
        else:
            exits[reason] += 1
        original_sl = trade.original_sl_price
        if original_sl is None:
            original_sl = trade.sl_price
        risk_dollars = (
            abs(float(trade.entry_price) - float(original_sl or trade.sl_price))
            * float(trade.point_value)
            * int(trade.contracts)
        )
        if risk_dollars > 0:
            net_rs.append(pnl / risk_dollars)

    seg_pfs = [
        (999.0 if gain > 0 and loss <= 0 else (gain / loss if loss > 0 else 0.0))
        for gain, loss in zip(seg_gains, seg_losses)
    ]
    weekly_values = list(week_pnl.values())
    weekly_std = statistics.pstdev(weekly_values) if len(weekly_values) >= 2 else 0.0
    weekly_mean = statistics.mean(weekly_values) if weekly_values else 0.0
    weekly_cv = weekly_std / abs(weekly_mean) if abs(weekly_mean) > 1e-9 else 99.0

    tick_size = 0.25
    extra_per_trade_per_tick_side = 2.0 * tick_size * float(trades[0].point_value if trades else 2.0) * int(
        trades[0].contracts if trades else getattr(params, "contract_size", 1)
    )
    stress = {
        str(ticks): _sequence_stats(
            pnl - ticks * extra_per_trade_per_tick_side for pnl in pnls
        )
        for ticks in (0, 1, 2)
    }
    top_trade = max(pnls) if pnls else 0.0
    total_pnl = sum(pnls)
    daily_distribution = Counter(day_count.values())
    metrics = result.metrics
    return {
        **job,
        "threshold": round(-0.10 * float(job["theta"]), 5) if job.get("theta") else None,
        "trades": int(metrics.total_trades),
        "win_rate": round(float(metrics.win_rate), 4),
        "pnl": round(float(metrics.total_pnl), 2),
        "pnl_x1": round(float(metrics.total_pnl) / max(1, int(params.contract_size)), 2),
        "pf": round(float(metrics.profit_factor), 4),
        "max_dd": round(float(metrics.max_drawdown), 2),
        "max_dd_x1": round(float(metrics.max_drawdown) / max(1, int(params.contract_size)), 2),
        "expectancy": round(float(metrics.expectancy), 2),
        "score_pnl_dd": round(float(metrics.total_pnl) / max(1.0, float(metrics.max_drawdown)), 4),
        "worst_day": round(min(day_pnl.values()) if day_pnl else 0.0, 2),
        "active_days": len(day_count),
        "multi_trade_days": sum(1 for count in day_count.values() if count > 1),
        "daily_trade_distribution": {str(k): int(v) for k, v in sorted(daily_distribution.items())},
        "trades_per_30d": round(len(trades) * 30.44 / span_days, 2),
        "seg_pnls": [round(x, 2) for x in seg_pnls],
        "seg_pfs": [round(x, 3) for x in seg_pfs],
        "wf_pass": bool(all(pnl > 0 and pf > 1.0 for pnl, pf in zip(seg_pnls, seg_pfs))),
        "weekly_cv": round(weekly_cv, 3),
        "avg_hold_min": round(statistics.mean(holds), 1) if holds else 0.0,
        "median_hold_min": round(statistics.median(holds), 1) if holds else 0.0,
        "p95_hold_min": round(_percentile(holds, 0.95), 1),
        "max_hold_min": round(max(holds), 1) if holds else 0.0,
        "total_exposure_hours": round(sum(holds) / 60.0, 1),
        "exit_counts": dict(sorted(exits.items())),
        "avg_net_r": round(statistics.mean(net_rs), 4) if net_rs else 0.0,
        "sum_net_r": round(sum(net_rs), 4),
        "top_trade": round(top_trade, 2),
        "top_trade_share": round(top_trade / total_pnl, 4) if total_pnl > 0 else 0.0,
        "pnl_without_best_trade": round(total_pnl - top_trade, 2),
        "slippage_ticks_each_side": stress,
        "trade_fingerprint": _trade_fingerprint(trades),
        "_ordered_pnls": [round(x, 4) for x in pnls],
    }


def _configure_params(base: StrategyParams, job: dict[str, Any]) -> StrategyParams:
    params = copy.deepcopy(base)
    params.strategy = "factor"
    params.factor_signal_family = "emapmo"
    params.factor_side_mode = "long_only"
    params.factor_pmo_signal_mode = "early"
    params.factor_timeframe_minutes = 5
    params.factor_warmup_bars = 150
    params.factor_session_va_filter = "off"
    params.factor_sl_rule = "atr_blend"
    params.factor_tp_rule = "atr_blend"
    params.factor_sl_value = float(job["sl"])
    params.factor_tp_value = float(job["tp"])
    params.factor_max_hold_bars = int(job["hold_bars"])
    params.factor_max_trades_per_day = int(job["max_trades_day"])
    params.tr_daily_loss_stop = int(job["daily_loss_stop"])
    params.tr_daily_win_stop = 0
    params.tr_exit_mode = "tp"
    params.trail_enabled = False
    params.tr_trail_enabled = False
    params.trail_trigger_pct = 0.0
    params.tr_trail_trigger_pct = 0.0
    params.trail_sl_ticks = 0
    params.tr_trail_sl_ticks = 0
    if job.get("theta") is not None:
        setattr(params, "_research_long_threshold", -0.10 * float(job["theta"]))
    return params


def _run_exact(base: StrategyParams, candles, job: dict[str, Any]) -> dict[str, Any]:
    params = _configure_params(base, job)
    engine = BacktestEngine(
        config=_make_config(params),
        strategy_params=params,
        zone_timeline=None,
        record_equity=False,
    )
    if not job.get("production_baseline"):
        engine.trend_follow = ResearchThresholdFactor(params=params)
    result = engine.run(candles)
    return _summarize_result(result, params, job, candles)


_W: dict[str, Any] = {}


def _init_worker() -> None:
    logging.disable(logging.CRITICAL)
    _W["base"] = _load_best_params()
    _W["candles"] = sorted(candle_store.load("MNQ", 1), key=lambda candle: candle.timestamp)


def _run_job(job: dict[str, Any]) -> dict[str, Any]:
    return _run_exact(_W["base"], _W["candles"], job)


def _job(
    phase: str,
    theta: Optional[float],
    sl: float,
    tp: float,
    hold_bars: int = 0,
    max_trades_day: int = 3,
    daily_loss_stop: int = 1,
    policy: str = "original",
    production_baseline: bool = False,
) -> dict[str, Any]:
    theta_label = "prod" if theta is None else f"{theta:.1f}"
    return {
        "job_id": (
            f"{phase}:theta={theta_label}:sl={sl:g}:tp={tp:g}:h={hold_bars}:"
            f"m={max_trades_day}:dl={daily_loss_stop}:{policy}"
        ),
        "phase": phase,
        "theta": theta,
        "sl": float(sl),
        "tp": float(tp),
        "rr": round(float(tp) / float(sl), 4),
        "hold_bars": int(hold_bars),
        "max_trades_day": int(max_trades_day),
        "daily_loss_stop": int(daily_loss_stop),
        "daily_policy": policy,
        "production_baseline": bool(production_baseline),
    }


def _run_batch(pool, jobs: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    if not jobs:
        return []
    started = time.time()
    results: list[dict[str, Any]] = []
    futures = {pool.submit(_run_job, job): job for job in jobs}
    step = max(1, len(jobs) // 10)
    for index, future in enumerate(as_completed(futures), start=1):
        row = future.result()
        results.append(row)
        if index == 1 or index == len(jobs) or index % step == 0:
            print(
                f"{label} {index}/{len(jobs)} | {row['job_id']} | "
                f"tr={row['trades']} pnl={row['pnl']:.0f} pf={row['pf']:.2f} "
                f"dd={row['max_dd']:.0f}",
                flush=True,
            )
    print(f"{label}_DONE elapsed={time.time() - started:.1f}s", flush=True)
    return sorted(results, key=lambda row: row["job_id"])


def _is_flatten_window(ts: datetime) -> bool:
    return market_close_phase(ts) == MARKET_PHASE_FLATTEN


def _raw_signal_stats_all(
    base: StrategyParams,
    candles,
    thetas: Iterable[float],
) -> dict[float, dict[str, Any]]:
    params = copy.deepcopy(base)
    strategy = FactorSignalStrategy(params=params)
    theta_values = list(thetas)
    signal_times: dict[float, list[datetime]] = {theta: [] for theta in theta_values}
    for candle in candles:
        if not is_allowed_session(candle.timestamp, params.tr_allowed_sessions):
            continue
        old_last = _utc(strategy._bars[-1].timestamp) if strategy._bars else None
        strategy.observe(candle, [], True)
        new_last = _utc(strategy._bars[-1].timestamp) if strategy._bars else None
        if new_last is None or new_last == old_last or len(strategy._bars) < strategy.warmup_bars:
            continue
        if _is_flatten_window(candle.timestamp):
            continue
        snapshot = strategy._emapmo_snapshot()
        pmo = snapshot.get("pmo")
        signal = snapshot.get("signal")
        q_now = snapshot.get("q_gap_now")
        q_prev = snapshot.get("q_gap_prev")
        q_prev2 = snapshot.get("q_gap_prev2")
        if None in (pmo, signal, q_now, q_prev, q_prev2):
            continue
        shape_ok = (
            float(pmo) < float(signal)
            and float(q_now) < float(q_prev) < float(q_prev2)
        )
        if not shape_ok:
            continue
        for theta in theta_values:
            if float(signal) < -0.10 * theta:
                signal_times[theta].append(new_last)

    result = {}
    for theta, times in signal_times.items():
        episodes = 0
        last_signal: Optional[datetime] = None
        for signal_time in times:
            if last_signal is None or (signal_time - last_signal).total_seconds() > 5 * 60 + 1:
                episodes += 1
            last_signal = signal_time
        result[theta] = {
            "theta": theta,
            "threshold": round(-0.10 * theta, 5),
            "raw_signal_bars": len(times),
            "signal_episodes": episodes,
            "signal_days": len({_topstep_trade_date(ts) for ts in times}),
        }
    return result


def _annotate_threshold_plateau(rows: list[dict[str, Any]]) -> None:
    lookup = {
        (round(row["theta"], 1), row["sl"], row["tp"]): row
        for row in rows
    }
    ordered = list(THETAS)
    for row in rows:
        index = ordered.index(round(float(row["theta"]), 1))
        neighbor_thetas = ordered[max(0, index - 1): min(len(ordered), index + 2)]
        neighbors = [
            lookup.get((theta, row["sl"], row["tp"]))
            for theta in neighbor_thetas
        ]
        good = [item for item in neighbors if item and item["pnl"] > 0 and item["pf"] > 1.0]
        row["threshold_plateau_good"] = len(good)
        row["threshold_plateau_total"] = len([item for item in neighbors if item])


def _dedupe_specs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        key = (row["theta"], row["sl"], row["tp"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _fmt_row(row: dict[str, Any]) -> str:
    return (
        f"| {row.get('phase','')} | {row.get('theta')} | {row['sl']:g} | {row['tp']:g} | "
        f"{row['hold_bars']} | {row['daily_policy']} | {row['trades']} | {row['active_days']} | "
        f"{row['pnl']:.0f} | {row['pf']:.2f} | {row['max_dd']:.0f} | "
        f"{row['win_rate']*100:.1f}% | {row['avg_hold_min']:.0f} | "
        f"{'Y' if row['wf_pass'] else 'N'} | {row['score_pnl_dd']:.2f} |"
    )


def main() -> int:
    started = time.time()
    base = _load_best_params()
    candles = sorted(candle_store.load("MNQ", 1), key=lambda candle: candle.timestamp)
    if not candles:
        raise SystemExit("No MNQ candle store data.")
    frozen = {
        "bars": len(candles),
        "start": candles[0].timestamp.isoformat(),
        "end": candles[-1].timestamp.isoformat(),
        "contract_id": base.contract_id,
        "contract_size": base.contract_size,
    }
    print(f"FROZEN_DATA {json.dumps(frozen, ensure_ascii=False)}", flush=True)

    literal_thetas = (9.0, 8.0, 7.0, 6.0)  # literal -0.9/-0.8/-0.7/-0.6
    raw_lookup_all = _raw_signal_stats_all(base, candles, (*THETAS, *literal_thetas))
    raw_signals = [raw_lookup_all[theta] for theta in THETAS]
    literal_sanity = [raw_lookup_all[theta] for theta in literal_thetas]
    print(f"RAW_SIGNALS {json.dumps(raw_signals, ensure_ascii=False)}", flush=True)
    print(f"LITERAL_SANITY {json.dumps(literal_sanity, ensure_ascii=False)}", flush=True)

    workers = max(2, min(6, (os.cpu_count() or 8) - 2))
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as pool:
        production_jobs = [
            _job("production", None, 2.5, 7.5, production_baseline=True),
        ]
        entry_jobs = [_job("entry", theta, 2.5, 7.5) for theta in THETAS]
        production = _run_batch(pool, production_jobs, "PRODUCTION")[0]
        entry = _run_batch(pool, entry_jobs, "ENTRY")

        theta_one = next(row for row in entry if row["theta"] == 1.0)
        if (
            theta_one["trade_fingerprint"] != production["trade_fingerprint"]
            or theta_one["pnl"] != production["pnl"]
        ):
            raise RuntimeError("theta=1.0 failed production parity; aborting research")
        print("PARITY theta=1.0 matches production trade-for-trade", flush=True)

        risk_pairs = {(sl, round(sl * rr, 4)) for sl in SL_GRID for rr in RR_GRID}
        risk_pairs.add((2.5, 4.0))  # exact anchor from the prior production sweep
        risk_jobs = [
            _job("risk", theta, sl, tp)
            for theta in THETAS
            for sl, tp in sorted(risk_pairs)
        ]
        risk = _run_batch(pool, risk_jobs, "RISK")
        _annotate_threshold_plateau(risk)

        robust_risk = [
            row for row in risk
            if row["wf_pass"] and row["pnl"] > 0 and row["pf"] > 1.0
            and row.get("threshold_plateau_good", 0) >= 2
        ]
        selected = [
            next(row for row in risk if row["theta"] == 1.0 and row["sl"] == 2.5 and row["tp"] == 7.5)
        ]
        if robust_risk:
            selected.extend([
                max(robust_risk, key=lambda row: row["pnl"]),
                max(robust_risk, key=lambda row: row["score_pnl_dd"]),
                max(robust_risk, key=lambda row: (row["trades"], row["pnl"])),
            ])
        selected = _dedupe_specs(selected)
        print(
            "POLICY_BASES " + json.dumps(
                [{key: row[key] for key in ("theta", "sl", "tp", "trades", "pnl", "pf", "max_dd")}
                 for row in selected],
                ensure_ascii=False,
            ),
            flush=True,
        )

        policy_jobs = []
        for selected_row in selected:
            for hold in HOLD_GRID:
                for policy, max_day, loss_stop in DAILY_POLICIES:
                    policy_jobs.append(_job(
                        "policy",
                        selected_row["theta"],
                        selected_row["sl"],
                        selected_row["tp"],
                        hold_bars=hold,
                        max_trades_day=max_day,
                        daily_loss_stop=loss_stop,
                        policy=policy,
                    ))
        policy = _run_batch(pool, policy_jobs, "POLICY")

    all_candidates = entry + risk + policy
    robust = [row for row in all_candidates if row["wf_pass"] and row["pnl"] > 0 and row["pf"] > 1.0]
    baseline = production
    best_pnl = max(robust, key=lambda row: row["pnl"]) if robust else baseline
    best_quality = max(
        (row for row in robust if row["trades"] >= baseline["trades"]),
        key=lambda row: row["score_pnl_dd"],
        default=baseline,
    )
    best_more = max(
        (row for row in robust if row["trades"] >= 24),
        key=lambda row: (row["pnl"], row["score_pnl_dd"]),
        default=None,
    )
    entry_challengers = [
        row for row in entry
        if row["theta"] != 1.0
        and row["trades"] > baseline["trades"]
        and row["pnl"] > baseline["pnl"]
        and row["pf"] >= baseline["pf"] * 0.8
        and row["max_dd"] <= baseline["max_dd"] * 1.25
        and row["wf_pass"]
    ]

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")
    out_dir = ROOT / "data" / "machinelearning"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / f"emapmo_best_threshold_study_{stamp}.json"
    out_md = out_dir / f"emapmo_best_threshold_study_{stamp}.md"
    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.time() - started, 1),
        "production_files_modified": False,
        "interpretation": (
            "entry strength theta keeps PMO<SIG and two-step gap contraction hard; "
            "requires SIG < -0.10*theta"
        ),
        "stability_screen": (
            "wf_pass means positive PnL and PF>1 in each of three fixed calendar thirds "
            "of the same in-sample data; it is not walk-forward or out-of-sample testing"
        ),
        "frozen_data": frozen,
        "best_preset": {
            "signal": "EMAPMO early long-only 5m",
            "sl_tp": "atr_blend 2.5 / 7.5",
            "max_trades_day": 3,
            "daily_loss_stop": 1,
            "max_hold_bars": 0,
            "market_clock_version": MARKET_CLOCK_VERSION,
            "daily_flatten_et": "15:45",
        },
        "parity": {
            "passed": True,
            "production_fingerprint": production["trade_fingerprint"],
            "theta_1_fingerprint": theta_one["trade_fingerprint"],
        },
        "raw_signals": raw_signals,
        "literal_threshold_sanity": literal_sanity,
        "production_baseline": production,
        "entry_results": entry,
        "risk_results": risk,
        "policy_results": policy,
        "policy_base_specs": [
            {key: row[key] for key in ("theta", "sl", "tp", "trades", "pnl", "pf", "max_dd")}
            for row in selected
        ],
        "conclusions": {
            "entry_relaxation_beats_original": bool(entry_challengers),
            "entry_challengers": [row["job_id"] for row in entry_challengers],
            "best_pnl_job": best_pnl["job_id"],
            "best_quality_job": best_quality["job_id"],
            "best_more_trades_job": best_more["job_id"] if best_more else None,
        },
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    entry_lookup = {row["theta"]: row for row in entry}
    raw_lookup = {row["theta"]: row for row in raw_signals}
    lines = [
        f"# EMAPMO BEST threshold/risk study {stamp}",
        "",
        "Production code/preset modified: **NO**. Research-only subclass + production BacktestEngine.",
        f"Data: {frozen['bars']} 1m bars, {frozen['start']} -> {frozen['end']}, MNQ x{frozen['contract_size']}.",
        "",
        "## Interpretation",
        "",
        "`theta=1.0/.9/.8/.7/.6` keeps `PMO<SIG` and two consecutive gap contractions, "
        "while changing the oversold gate to `SIG < -0.10*theta`.",
        "Literal `-0.6/-0.7/-0.8/-0.9` produced no raw signals in this data.",
        f"Parity check: **PASS** (`theta=1.0` trade fingerprint equals production BEST).",
        "",
        "## Entry threshold only -- original SL2.5 / TP7.5 / HOFF / daily policy",
        "",
        "| theta | SIG gate | raw bars | episodes | trades | pnl x3 | PF | DD x3 | WR | avg hold | 3/3 periods |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for theta in THETAS:
        row = entry_lookup[theta]
        raw = raw_lookup[theta]
        lines.append(
            f"| {theta:.1f} | {row['threshold']:.2f} | {raw['raw_signal_bars']} | "
            f"{raw['signal_episodes']} | {row['trades']} | {row['pnl']:.0f} | {row['pf']:.2f} | "
            f"{row['max_dd']:.0f} | {row['win_rate']*100:.1f}% | {row['avg_hold_min']:.0f}m | "
            f"{'Y' if row['wf_pass'] else 'N'} |"
        )

    top_risk = sorted(robust_risk, key=lambda row: (-row["pnl"], -row["score_pnl_dd"]))[:15]
    top_policy = sorted(
        (row for row in policy if row["wf_pass"] and row["pnl"] > 0),
        key=lambda row: (-row["pnl"], -row["score_pnl_dd"]),
    )[:15]
    table_header = [
        "| phase | theta | SL | TP | hold bars | daily policy | trades | days | pnl x3 | PF | DD x3 | WR | avg hold | 3/3 periods | PnL/DD |",
        "|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|:---:|---:|",
    ]
    lines += ["", "## Top risk rows passing the 3-period in-sample screen", "", *table_header]
    lines += [_fmt_row(row) for row in top_risk]
    lines += ["", "## Top hold / daily-policy rows", "", *table_header]
    lines += [_fmt_row(row) for row in top_policy]
    lines += [
        "",
        "## Decision summary",
        "",
        f"- Entry relaxation alone beats original under the predeclared in-sample screen: "
        f"**{'YES' if entry_challengers else 'NO'}**.",
        f"- Original production: `{baseline['job_id']}` -- {baseline['trades']} trades, "
        f"PnL ${baseline['pnl']:.0f}, PF {baseline['pf']:.2f}, DD ${baseline['max_dd']:.0f}.",
        f"- Highest tested PnL passing the 3-period screen: `{best_pnl['job_id']}` -- {best_pnl['trades']} trades, "
        f"PnL ${best_pnl['pnl']:.0f}, PF {best_pnl['pf']:.2f}, DD ${best_pnl['max_dd']:.0f}.",
        f"- Highest tested PnL/DD passing the 3-period screen: `{best_quality['job_id']}` -- {best_quality['trades']} trades, "
        f"PnL ${best_quality['pnl']:.0f}, PF {best_quality['pf']:.2f}, DD ${best_quality['max_dd']:.0f}.",
    ]
    if best_more:
        lines.append(
            f"- Best tested >=24-trade row passing the 3-period screen: `{best_more['job_id']}` -- {best_more['trades']} trades, "
            f"PnL ${best_more['pnl']:.0f}, PF {best_more['pf']:.2f}, DD ${best_more['max_dd']:.0f}."
        )
    else:
        lines.append("- No >=24-trade row passed all three fixed time segments with PF>1.")
    lines += [
        "",
        "Caution: only about two months of data are available. The 3-period screen uses three fixed "
        "calendar thirds of the same in-sample data; it is not walk-forward or out-of-sample evidence. "
        "Grid winners are research candidates, not deployment recommendations; require a fresh forward "
        "window before changing BEST.",
        "",
        f"Full JSON: `{out_json.name}`",
    ]
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"STUDY_DONE elapsed={time.time() - started:.1f}s", flush=True)
    print(f"OUTPUT_JSON {out_json}", flush=True)
    print(f"OUTPUT_MD {out_md}", flush=True)
    print(
        f"DECISION entry_relaxation={'YES' if entry_challengers else 'NO'} | "
        f"baseline pnl={baseline['pnl']:.0f} pf={baseline['pf']:.2f} dd={baseline['max_dd']:.0f} | "
        f"best_pnl={best_pnl['job_id']} pnl={best_pnl['pnl']:.0f} pf={best_pnl['pf']:.2f} "
        f"dd={best_pnl['max_dd']:.0f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
