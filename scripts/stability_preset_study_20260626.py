"""Stability preset study for Trend / Confluence / ML Consolidation V2.

Independent research script.  It does not start FastAPI and never touches live
orders.  The goal is deliberately conservative:

* 1x MNQ only
* prefer many trades, but not at the expense of weekly/monthly collapse
* require May and June to be separately sane, not just total PnL
* report if a strategy class is structurally weak instead of forcing presets

Outputs:
    data/machinelearning/stability_preset_study_20260626.csv
    data/machinelearning/stability_preset_study_20260626.md
    data/machinelearning/stability_preset_study_20260626.json
"""

from __future__ import annotations

import csv
import json
import math
import pickle
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestEngine  # noqa: E402
from backend.backtest.confluence_backtest import (  # noqa: E402
    ConfluenceBacktester,
    ConfluenceBacktestConfig,
    build_zone_timeline,
)
from backend.backtest.ml_trend_backtest import (  # noqa: E402
    MLTrendBacktester,
    MLTrendBacktestConfig,
    precompute_vp_timeline,
)
from backend.db.models import (  # noqa: E402
    BacktestConfig,
    Candle,
    StrategyParams,
    get_commission_rt,
    get_fees_rt,
    get_tick_size,
)
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH  # noqa: E402
from backend.strategy.confluence_scorer import resolve_scorer  # noqa: E402
from backend.strategy.consolidation import build_zone_detector, timeframes_for_base  # noqa: E402
from backend.strategy.ml_trend import MLTrendConfig  # noqa: E402


CONTRACT_ID = "CON.F.US.MNQ.U26"
SYMBOL = "MNQ"
SIZE = 1
INITIAL_CAPITAL = 50_000.0
STORE = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
OUT_DIR = ROOT / "data" / "machinelearning"
CSV_OUT = OUT_DIR / "stability_preset_study_20260626.csv"
JSON_OUT = OUT_DIR / "stability_preset_study_20260626.json"
MD_OUT = OUT_DIR / "stability_preset_study_20260626.md"


@dataclass
class Row:
    strategy: str
    name_hint: str
    params: Dict[str, Any]
    trades: int
    win_rate: float
    pnl: float
    max_dd: float
    pf: float
    calmar: float
    worst_week: float
    week_cv: float
    positive_week_ratio: float
    may_pnl: float
    jun_pnl: float
    apr_pnl: float
    score: float
    verdict: str


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def load_candles() -> List[Candle]:
    candles = sorted(pickle.loads(STORE.read_bytes()), key=lambda c: c.timestamp)
    log(f"Loaded {len(candles):,} candles: {candles[0].timestamp} -> {candles[-1].timestamp}")
    return candles


def _trade_pnl_by_period(trades) -> Tuple[Dict[str, float], Dict[str, float]]:
    by_week: Dict[str, float] = {}
    by_month: Dict[str, float] = {}
    for t in trades:
        ts = getattr(t, "entry_time", None) or getattr(t, "exit_time", None)
        if ts is None:
            continue
        pnl = float(getattr(t, "pnl", 0.0) or 0.0)
        iso = ts.isocalendar()
        wk = f"{iso.year}-W{iso.week:02d}"
        mo = ts.strftime("%Y-%m")
        by_week[wk] = by_week.get(wk, 0.0) + pnl
        by_month[mo] = by_month.get(mo, 0.0) + pnl
    return by_week, by_month


def _score_result(strategy: str, name_hint: str, params: Dict[str, Any], result) -> Row:
    m = result.metrics
    trades = list(result.trades)
    by_week, by_month = _trade_pnl_by_period(trades)
    weekly = list(by_week.values())
    week_mean = statistics.mean(weekly) if weekly else 0.0
    week_std = statistics.pstdev(weekly) if len(weekly) > 1 else 0.0
    week_cv = week_std / max(abs(week_mean), 1.0)
    worst_week = min(weekly) if weekly else 0.0
    positive_week_ratio = (
        sum(1 for x in weekly if x > 0) / len(weekly)
        if weekly else 0.0
    )
    apr = by_month.get("2026-04", 0.0)
    may = by_month.get("2026-05", 0.0)
    jun = by_month.get("2026-06", 0.0)

    pnl = float(m.total_pnl)
    dd = float(m.max_drawdown)
    trades_n = int(m.total_trades)
    pf = float(m.profit_factor or 0.0)
    calmar = float(m.calmar_ratio or 0.0)

    # Conservative stability score.  This intentionally punishes one-month
    # wonders and one-week blowups even when total PnL is positive.
    month_balance = min(may, jun) / max(abs(pnl), 1.0)
    dd_term = pnl / max(dd, 250.0)
    trade_term = min(trades_n / 80.0, 1.5)
    pf_term = max(0.0, min((pf - 1.0) * 2.0, 2.0))
    week_term = max(0.0, positive_week_ratio - 0.45) * 2.0
    cv_penalty = min(max(week_cv - 1.0, 0.0), 3.0)
    worst_penalty = max(0.0, abs(min(worst_week, 0.0)) / 1000.0)
    month_penalty = 0.0 if may > 0 and jun > 0 else 2.5
    score = dd_term + trade_term + pf_term + week_term + month_balance - cv_penalty - worst_penalty - month_penalty

    if trades_n < 25:
        verdict = "REJECT_TOO_FEW_TRADES"
    elif pnl <= 0 or pf <= 1.02:
        verdict = "REJECT_NO_EDGE"
    elif may <= 0 or jun <= 0:
        verdict = "REJECT_MONTH_UNSTABLE"
    elif dd > 2500:
        verdict = "REJECT_DD"
    elif positive_week_ratio < 0.50:
        verdict = "REJECT_WEEKLY_UNSTABLE"
    else:
        verdict = "CANDIDATE"

    return Row(
        strategy=strategy,
        name_hint=name_hint,
        params=params,
        trades=trades_n,
        win_rate=float(m.win_rate),
        pnl=round(pnl, 2),
        max_dd=round(dd, 2),
        pf=round(pf, 4),
        calmar=round(calmar, 4),
        worst_week=round(worst_week, 2),
        week_cv=round(week_cv, 4),
        positive_week_ratio=round(positive_week_ratio, 4),
        may_pnl=round(may, 2),
        jun_pnl=round(jun, 2),
        apr_pnl=round(apr, 2),
        score=round(score, 4),
        verdict=verdict,
    )


def build_trend_timeline(candles: Sequence[Candle], area_tf: str, tick: float) -> List[dict]:
    det = build_zone_detector(area_timeframe=area_tf, value_area_pct=0.80, tick_size=tick, max_recent=10)
    out: List[dict] = []
    for candle in candles:
        det.update(candle)
        active = det.get_active_zone()
        recent = det.get_recent_zones()
        out.append({
            "active": active,
            "mature": bool(det.is_zone_mature),
            "recent": recent or ([active] if active else []),
        })
    return out


def run_trend(candles: List[Candle]) -> List[Row]:
    log("Trend sweep starting")
    tick = get_tick_size(CONTRACT_ID)
    bt_cfg = BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        symbol=SYMBOL,
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
    )
    rows: List[Row] = []
    area_tfs = ["5m", "10m", "15m", "30m", "1h"]
    timelines = {}
    for tf in area_tfs:
        log(f"  Trend timeline {tf}")
        timelines[tf] = build_trend_timeline(candles, tf, tick)

    combos = []
    for tf in area_tfs:
        for rr in [1, 2, 3, 4]:
            for confirm in [1, 2, 3]:
                for sl_ticks in [20, 40, 80]:
                    for trail_enabled, trig, trail_ticks in [
                        (False, 0.0, 0),
                        (True, 0.30, 5),
                        (True, 0.50, 5),
                        (True, 0.50, 10),
                    ]:
                        combos.append((tf, rr, confirm, sl_ticks, trail_enabled, trig, trail_ticks))

    for idx, (tf, rr, confirm, sl_ticks, trail_enabled, trig, trail_ticks) in enumerate(combos, 1):
        p = StrategyParams(
            strategy="trend",
            contract_id=CONTRACT_ID,
            contract_size=SIZE,
            area_timeframe=tf,
            value_area_pct=0.80,
            rr_ratio=rr,
            tr_sl_ticks=sl_ticks,
            sl_ticks=sl_ticks,
            tr_trail_enabled=trail_enabled,
            trail_enabled=trail_enabled,
            tr_trail_trigger_pct=trig,
            trail_trigger_pct=trig,
            tr_trail_sl_ticks=trail_ticks,
            trail_sl_ticks=trail_ticks,
            breakout_confirm_bars=confirm,
            tr_one_trade_per_session=True,
            one_trade_per_session_direction=True,
            full_tp_lock=0,
            tr_full_tp_lock=0,
        )
        engine = BacktestEngine(bt_cfg, strategy_params=p, zone_timeline=timelines[tf], record_equity=False)
        result = engine.run(candles)
        params = {
            "area_timeframe": tf,
            "rr_ratio": rr,
            "breakout_confirm_bars": confirm,
            "tr_sl_ticks": sl_ticks,
            "trail": "off" if not trail_enabled else f"{int(trig*100)}%/{trail_ticks}t",
        }
        rows.append(_score_result("trend", f"TF{tf} RR{rr} C{confirm} SL{sl_ticks}", params, result))
        if idx % 100 == 0:
            log(f"  Trend {idx}/{len(combos)}")
    return rows


def run_mlc2(candles: List[Candle]) -> List[Row]:
    log("ML Consolidation V2 sweep starting")
    tick = get_tick_size(CONTRACT_ID)
    bt_cfg = BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        symbol=SYMBOL,
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
    )
    rows: List[Row] = []
    lookbacks = [30, 60, 120, 240]
    timelines = {}
    for lb in lookbacks:
        log(f"  MLC2 VP timeline LB{lb}")
        timelines[lb] = precompute_vp_timeline(candles, lb, tick_size=tick, recalc_interval=5)

    combos = []
    for lb in lookbacks:
        for band in [1, 2, 4]:
            for sl_buf in [2, 4, 8]:
                for sl_mode in ["va", "range"]:
                    for max_risk in [12, 20, 40]:
                        for tp_mode, rr in [("poc", 1.0), ("rr", 1.0), ("rr", 1.5)]:
                            for sessions in [("ASIA",), ("PRE",), ("ASIA", "PRE")]:
                                for session_limit in [True, False]:
                                    combos.append((lb, band, sl_buf, sl_mode, max_risk, tp_mode, rr, sessions, session_limit))

    for idx, (lb, band, sl_buf, sl_mode, max_risk, tp_mode, rr, sessions, session_limit) in enumerate(combos, 1):
        sig_cfg = MLTrendConfig(
            lookback=lb,
            band_ticks=band,
            sl_buffer_ticks=sl_buf,
            sl_mode=sl_mode,
            tp_mode=tp_mode,
            rr=rr,
            max_risk_ticks=max_risk,
            min_risk_ticks=4,
        )
        run_cfg = MLTrendBacktestConfig(
            trail_trigger_pct=0.50,
            trail_lock_pct=0.05,
            one_trade_per_session=session_limit,
            allowed_sessions=sessions,
            min_score=0.0,
        )
        bt = MLTrendBacktester(sig_cfg, run_cfg, contract_id=CONTRACT_ID, contract_size=SIZE, bt_config=bt_cfg, scorer=None)
        result = bt.run(candles, vp_timeline=timelines[lb])
        params = {
            "lookback": lb,
            "band_ticks": band,
            "sl_buffer_ticks": sl_buf,
            "sl_mode": sl_mode,
            "max_risk_ticks": max_risk,
            "tp_mode": tp_mode,
            "rr": rr,
            "sessions": "+".join(sessions),
            "session_limit": session_limit,
            "trail": "50%/5%",
        }
        rows.append(_score_result("ml_consolidation_v2", f"LB{lb} B{band} R{max_risk}", params, result))
        if idx % 300 == 0:
            log(f"  MLC2 {idx}/{len(combos)}")
    return rows


def run_confluence_small(candles: List[Candle]) -> List[Row]:
    """Small corrected-market sweep.

    Confluence is much slower than the other two because it recomputes
    multi-timeframe clusters and scorer features.  This pass is intentionally
    narrow: it validates whether the repaired market-entry path still has a
    basic edge.  If this narrow pass fails, the strategy class is not live-ready.
    """
    log("Confluence corrected-market small sweep starting")
    tick = get_tick_size(CONTRACT_ID)
    timeframes = timeframes_for_base(1)
    scorer = resolve_scorer(True, None)
    bt_cfg = BacktestConfig(
        initial_capital=INITIAL_CAPITAL,
        symbol=SYMBOL,
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
    )
    log("  Confluence zone timeline")
    timeline = build_zone_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
    rows: List[Row] = []
    combos = []
    for rr in [1.0, 1.5, 2.0, 2.5]:
        for max_risk in [20, 40, 80]:
            for min_prob in [0.0, 0.65, 0.75]:
                for band, min_tf in [(4, 2), (8, 3)]:
                    combos.append((rr, max_risk, min_prob, band, min_tf))
    for idx, (rr, max_risk, min_prob, band, min_tf) in enumerate(combos, 1):
        min_score = math.log(min_prob / (1 - min_prob)) if 0.0 < min_prob < 1.0 else 0.0
        sig_cfg = ConfluenceConfig(band_ticks=band, min_distinct_tf=min_tf, rr=rr)
        sig_cfg.direction_mode = "auto"
        sig_cfg.tick_size = tick
        sig_cfg.ev_floor = None
        sig_cfg.rr_grid = None
        sig_cfg.enable_breakout = False
        sig_cfg.max_risk_ticks = max_risk
        run_cfg = ConfluenceBacktestConfig(
            wait_minutes=1,
            min_score=min_score,
            base_minutes=1,
            timeframes=timeframes,
            one_trade_per_session_direction=True,
            trail_trigger_pct=0.50,
            trail_lock_pct=0.05,
            full_tp_lock=0,
            allowed_sessions=("ASIA",),
        )
        bt = ConfluenceBacktester(sig_cfg, run_cfg, contract_id=CONTRACT_ID, contract_size=SIZE, bt_config=bt_cfg, scorer=scorer)
        result = bt.run(candles, zones_timeline=timeline)
        params = {
            "conf_rr": rr,
            "conf_max_risk_ticks": max_risk,
            "conf_min_prob": min_prob,
            "conf_band_ticks": band,
            "conf_min_distinct_tf": min_tf,
            "conf_allowed_sessions": ["ASIA"],
            "conf_trail": "50%/5%",
            "entry_type": "corrected_market",
        }
        rows.append(_score_result("confluence", f"RR{rr} R{max_risk} P{min_prob}", params, result))
        log(f"  Confluence {idx}/{len(combos)}")
    return rows


def top_by_strategy(rows: List[Row], strategy: str, n: int = 10) -> List[Row]:
    items = [r for r in rows if r.strategy == strategy]
    # candidate first, then score.  Keep rejects too so the report can show
    # whether a class is structurally broken.
    return sorted(items, key=lambda r: (r.verdict == "CANDIDATE", r.score, r.pnl), reverse=True)[:n]


def write_outputs(rows: List[Row]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data = [asdict(r) for r in rows]
    JSON_OUT.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    with CSV_OUT.open("w", newline="", encoding="utf-8") as f:
        fieldnames = list(data[0].keys()) if data else []
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(data)

    lines = [
        "# Stability Preset Study — 2026-06-26",
        "",
        "Scoring target: 1x MNQ, many trades, positive May and June, weekly variation controlled.",
        "",
    ]
    for strategy in ["trend", "confluence", "ml_consolidation_v2"]:
        lines.append(f"## {strategy}")
        lines.append("")
        lines.append("| Rank | Verdict | Hint | Trades | Win | PnL | MaxDD | PF | WorstW | WeekCV | May | Jun | Score | Params |")
        lines.append("|---:|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---|")
        for rank, r in enumerate(top_by_strategy(rows, strategy, 12), 1):
            lines.append(
                f"| {rank} | {r.verdict} | {r.name_hint} | {r.trades} | {r.win_rate*100:.1f}% | "
                f"${r.pnl:,.0f} | ${r.max_dd:,.0f} | {r.pf:.2f} | ${r.worst_week:,.0f} | "
                f"{r.week_cv:.2f} | ${r.may_pnl:,.0f} | ${r.jun_pnl:,.0f} | {r.score:.2f} | "
                f"`{json.dumps(r.params, ensure_ascii=False)}` |"
            )
        lines.append("")
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")
    log(f"Wrote {CSV_OUT}")
    log(f"Wrote {MD_OUT}")


def main() -> None:
    candles = load_candles()
    all_rows: List[Row] = []
    t0 = time.perf_counter()
    all_rows.extend(run_trend(candles))
    write_outputs(all_rows)
    all_rows.extend(run_mlc2(candles))
    write_outputs(all_rows)
    all_rows.extend(run_confluence_small(candles))
    write_outputs(all_rows)
    log(f"Done in {(time.perf_counter() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
