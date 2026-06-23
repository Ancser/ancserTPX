"""Read-only 60d study of session-lock identity and MaxRisk.

Compares:
  1) legacy lock keyed only by largest timeframe,
  2) fixed lock keyed by the physical contributing wall,
  3) session limit disabled.

Runs only one process and reuses the audit zone-timeline cache. It never calls
the live API or mutates the active scorer/preset/live ledgers.
"""

from __future__ import annotations

import json
import math
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from backend.backtest.confluence_backtest import (
    ConfluenceBacktestConfig,
    ConfluenceBacktester,
)
from backend.db.models import BacktestConfig, get_commission_rt, get_fees_rt
from backend.strategy.confluence import ConfluenceConfig, cluster_wall_id
from backend.strategy.confluence_scorer import ConfluenceScorer
from backend.strategy.consolidation import timeframes_for_base


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = DATA / "machinelearning"
STORE = DATA / "store" / "MNQ_accumulated_1m.pkl"
TIMELINE_CACHE = OUT / "audit_zone_timeline_20260623.pkl"
MODEL_ID = "20260618_codex_rr3-band4-mintf2-production-baseline-02"
MODEL_PATH = DATA / "models" / "registry" / f"{MODEL_ID}.json"
RESULTS_JSON = OUT / "session_lock_risk_study_20260623.json"
REPORT_MD = OUT / "session_lock_risk_study_20260623.md"

CONTRACT_ID = "CON.F.US.MNQ.U26"
TICK_SIZE = 0.25
PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
TRAIN_END = datetime(2026, 6, 18, 20, 59, tzinfo=UTC)


def max_drawdown(pnls):
    equity = peak = dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def summary(trades):
    pnls = [float(t.pnl or 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "pnl": sum(pnls),
        "max_dd": max_drawdown(pnls),
        "profit_factor": sum(wins) / -sum(losses) if losses else (math.inf if wins else 0.0),
        "expectancy": sum(pnls) / len(trades) if trades else 0.0,
    }


def physical_wall_id(sig) -> Optional[str]:
    """Use the exact production telemetry identity in the research variant."""
    return cluster_wall_id(sig.cluster) or None


class WallLockBacktester(ConfluenceBacktester):
    def _session_lock_key(self, ts, sig):
        wall_id = physical_wall_id(sig)
        direction = self._breakout_direction_from_trade_direction(sig.direction)
        if not wall_id or not direction:
            return None
        return (self._session_key(ts), wall_id, direction)


def load_inputs():
    with STORE.open("rb") as handle:
        bars = sorted(pickle.load(handle), key=lambda c: c.timestamp)
    with TIMELINE_CACHE.open("rb") as handle:
        cache = pickle.load(handle)
    timeline = cache["timeline"]
    if len(timeline) != len(bars):
        raise RuntimeError(
            f"timeline cache {len(timeline)} != store bars {len(bars)}; "
            "run scripts.audit_live_vs_backtest_20260618_22 first"
        )
    return bars, timeline


def run_variant(name, lock_mode, max_risk, bars, timeline):
    signal_cfg = ConfluenceConfig(band_ticks=4.0, min_distinct_tf=2, rr=5.0)
    signal_cfg.direction_mode = "auto"
    signal_cfg.tick_size = TICK_SIZE
    signal_cfg.enable_breakout = False
    signal_cfg.max_risk_ticks = max_risk
    signal_cfg.ev_floor = None
    signal_cfg.rr_grid = None

    session_limit = lock_mode != "off"
    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=1,
        one_trade_per_session_direction=session_limit,
        timeframes=timeframes_for_base(1),
        min_score=0.0,
        base_minutes=1,
        trail_trigger_pct=0.50,
        trail_lock_pct=0.05,
        full_tp_lock=0,
    )
    bt_cfg = BacktestConfig(
        initial_capital=50000.0,
        symbol="MNQ",
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
    )
    cls = WallLockBacktester if lock_mode == "wall" else ConfluenceBacktester
    bt = cls(
        signal_cfg=signal_cfg,
        run_cfg=run_cfg,
        contract_id=CONTRACT_ID,
        contract_size=3,
        bt_config=bt_cfg,
        scorer=ConfluenceScorer.load(MODEL_PATH),
    )
    print(f"[study] {name}", flush=True)
    result = bt.run(bars, zones_timeline=timeline)
    trades = result.trades
    full = summary(trades)
    insample = summary([t for t in trades if t.entry_time <= TRAIN_END])
    oos = summary([t for t in trades if t.entry_time > TRAIN_END])

    by_tf = defaultdict(list)
    by_day = defaultdict(list)
    for trade in trades:
        by_tf[str((trade.meta or {}).get("largest_tf") or "unknown")].append(trade)
        by_day[trade.entry_time.astimezone(PT).date().isoformat()].append(trade)
    tf_rows = {
        key: summary(values)
        for key, values in sorted(by_tf.items())
    }
    day_rows = {
        key: summary(values)
        for key, values in sorted(by_day.items())
    }
    return {
        "name": name,
        "lock_mode": lock_mode,
        "max_risk": max_risk,
        "contracts": 3,
        "full": full,
        "in_sample": insample,
        "oos_after_train_end": oos,
        "by_tf": tf_rows,
        "by_day": day_rows,
    }


def one_contract(row):
    """Exact scale-down: trade path is size-independent in this engine."""
    out = json.loads(json.dumps(row))
    out["name"] += "_MNQx1"
    out["contracts"] = 1
    for section in ("full", "in_sample", "oos_after_train_end"):
        for field in ("pnl", "max_dd", "expectancy"):
            out[section][field] /= 3.0
    for groups in ("by_tf", "by_day"):
        for metrics in out[groups].values():
            for field in ("pnl", "max_dd", "expectancy"):
                metrics[field] /= 3.0
    return out


def fmt_metric(metrics):
    pf = metrics["profit_factor"]
    return [
        metrics["trades"],
        f"{metrics['win_rate'] * 100:.1f}%",
        f"${metrics['pnl']:.0f}",
        f"${metrics['max_dd']:.0f}",
        "∞" if math.isinf(pf) else f"{pf:.2f}",
        f"${metrics['expectancy']:.2f}",
    ]


def table(headers, rows):
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *["| " + " | ".join(map(str, row)) + " |" for row in rows],
    ])


def main():
    bars, timeline = load_inputs()
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "full"
    specs = {
        "r60": [("original_TFlock_R60", "tf", 60)],
        "off80": [("session_OFF_R80", "off", 80)],
        "full": [
            ("original_TFlock_R80", "tf", 80),
            ("fixed_WALLlock_R80", "wall", 80),
            ("fixed_WALLlock_R60", "wall", 60),
            ("session_OFF_R60", "off", 60),
        ],
    }.get(mode)
    if specs is None:
        raise SystemExit("usage: session_lock_risk_study_20260623.py [full|r60|off80]")
    rows = [run_variant(*spec, bars, timeline) for spec in specs]
    rows_all = rows + [one_contract(row) for row in rows]
    suffixes = {
        "r60": "session_lock_risk_R60_extra_20260623",
        "off80": "session_lock_risk_OFF_R80_extra_20260623",
    }
    stem = suffixes.get(mode)
    results_path = OUT / f"{stem}.json" if stem else RESULTS_JSON
    report_path = OUT / f"{stem}.md" if stem else REPORT_MD
    results_path.write_text(
        json.dumps({
            "generated_at": datetime.now(UTC).isoformat(),
            "model": MODEL_ID,
            "train_end": TRAIN_END.isoformat(),
            "bars": len(bars),
            "range": [bars[0].timestamp.isoformat(), bars[-1].timestamp.isoformat()],
            "variants": rows_all,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    full_rows = [
        [r["name"], r["contracts"], r["lock_mode"], r["max_risk"], *fmt_metric(r["full"])]
        for r in rows_all
    ]
    oos_rows = [
        [r["name"], r["contracts"], *fmt_metric(r["oos_after_train_end"])]
        for r in rows_all
    ]
    report = f"""# Session-lock / MaxRisk study — 2026-06-23

Model: `{MODEL_ID}`  
Data: `{bars[0].timestamp.isoformat()}` to `{bars[-1].timestamp.isoformat()}`  
Common: RR5, probability gate OFF, Trail50/Lock5, band4, minTF2.

## Full range

{table(["Variant", "MNQ", "Lock", "Risk", "N", "Win", "P&L", "MaxDD", "PF", "Exp"], full_rows)}

## OOS after scorer training end

Training cutoff: `{TRAIN_END.isoformat()}`

{table(["Variant", "MNQ", "N", "Win", "P&L", "MaxDD", "PF", "Exp"], oos_rows)}

## Lock definitions

- `tf`: legacy lock `(trade_date, largest_tf, direction)`.
- `wall`: fixed lock `(trade_date, tf:real_zone_id:VA-side+band, direction)`.
- `off`: no one-trade-per-wall session lock.

The MNQx1 rows are exact linear scale-downs of MNQx3; contract quantity does
not affect signal selection, fills, or locks in this backtester.
"""
    report_path.write_text(report, encoding="utf-8")
    print(f"[study] wrote {results_path}", flush=True)
    print(f"[study] wrote {report_path}", flush=True)


if __name__ == "__main__":
    main()
