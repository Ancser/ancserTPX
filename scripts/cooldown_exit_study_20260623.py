"""Read-only study of post-trade cooldown / lockout rules.

This script does not call the live API, does not start/stop the live engine,
and does not mutate scorer/preset/live ledgers.  It reuses the cached zone
timeline and compares the current production-style confluence preset with a few
extra post-exit throttles:

- 1h cooldown after a losing trade
- 1h gap after every closed trade
- skip the rest of the market segment after a winning trade
- skip the rest of the Topstep trade day after a winning trade
- stop after 3 consecutive losses for the market segment
- stop after 3 consecutive losses for the Topstep trade day
- a conservative combo of loss-cooldown + 3-loss market stop
"""

from __future__ import annotations

import json
import math
import pickle
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.confluence_backtest import (
    build_zone_timeline,
    ConfluenceBacktestConfig,
    ConfluenceBacktester,
)
from backend.db.models import BacktestConfig, ExitReason, get_commission_rt, get_fees_rt
from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
from backend.strategy.confluence_scorer import ConfluenceScorer
from backend.strategy.consolidation import timeframes_for_base
from backend.strategy.session_filter import (
    DEFAULT_ALLOWED_SESSIONS,
    allowed_sessions_label,
    market_session_code,
)


DATA = ROOT / "data"
OUT = DATA / "machinelearning"
STORE = DATA / "store" / "MNQ_accumulated_1m.pkl"
TIMELINE_CACHE = OUT / "cooldown_zone_timeline_20260623.pkl"
FALLBACK_TIMELINE_CACHE = OUT / "audit_zone_timeline_20260623.pkl"
MODEL_ID = "20260618_codex_rr3-band4-mintf2-production-baseline-02"
MODEL_PATH = DATA / "models" / "registry" / f"{MODEL_ID}.json"
RESULTS_JSON = OUT / "cooldown_exit_study_20260623.json"
REPORT_MD = OUT / "cooldown_exit_study_20260623.md"

CONTRACT_ID = "CON.F.US.MNQ.U26"
TICK_SIZE = 0.25
UTC = timezone.utc
PT = ZoneInfo("America/Los_Angeles")
TRAIN_END = datetime(2026, 6, 18, 20, 59, tzinfo=UTC)


@dataclass(frozen=True)
class RuleSpec:
    name: str
    loss_cooldown_minutes: int = 0
    any_trade_gap_minutes: int = 0
    win_skip_scope: Optional[str] = None        # "market_segment" | "topstep_day"
    three_loss_stop_scope: Optional[str] = None # "market_segment" | "topstep_day"


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


def market_segment_key(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    ts = ts.astimezone(UTC)
    code = market_session_code(ts)
    if code == "ASIA" and ts.hour < 7:
        day = (ts - timedelta(days=1)).date()
    else:
        day = ts.date()
    return f"{day.isoformat()}-{code}"


class RuleBacktester(ConfluenceBacktester):
    def __init__(self, *args, rule: RuleSpec, **kwargs):
        super().__init__(*args, **kwargs)
        self.rule = rule
        self._cooldown_until: Optional[datetime] = None
        self._trade_gap_until: Optional[datetime] = None
        self._blocked_market_segments: set[str] = set()
        self._blocked_topstep_days: set[str] = set()
        self._loss_streak = 0

    def _entry_blocked_by_rule(self, ts: datetime) -> bool:
        if self._cooldown_until and ts < self._cooldown_until:
            return True
        if self._trade_gap_until and ts < self._trade_gap_until:
            return True
        if market_segment_key(ts) in self._blocked_market_segments:
            return True
        if self._session_key(ts) in self._blocked_topstep_days:
            return True
        return False

    def _maybe_open(self, candle, zones_by_tf=None, recent_candles=None):
        if self._entry_blocked_by_rule(candle.timestamp):
            return
        return super()._maybe_open(candle, zones_by_tf, recent_candles)

    def _try_fill(self, candle) -> bool:
        # If the one-shot limit reaches the next disabled window before fill,
        # cancel it and release the session lock.  Mirrors live pending behavior.
        if self._entry_blocked_by_rule(candle.timestamp):
            self._release_pending_lock(candle.timestamp, self._pending)
            self._pending = None
            self._pending_age = 0
            return False
        return super()._try_fill(candle)

    def _exit(self, candle, exit_price: float, reason: ExitReason):
        super()._exit(candle, exit_price, reason)
        if not self._trades:
            return
        trade = self._trades[-1]
        pnl = float(trade.pnl or 0.0)
        exit_ts = trade.exit_time or candle.timestamp

        if self.rule.any_trade_gap_minutes > 0:
            self._trade_gap_until = exit_ts + timedelta(minutes=self.rule.any_trade_gap_minutes)

        if pnl < 0:
            self._loss_streak += 1
            if self.rule.loss_cooldown_minutes > 0:
                self._cooldown_until = exit_ts + timedelta(minutes=self.rule.loss_cooldown_minutes)
            if self.rule.three_loss_stop_scope and self._loss_streak >= 3:
                if self.rule.three_loss_stop_scope == "market_segment":
                    self._blocked_market_segments.add(market_segment_key(exit_ts))
                elif self.rule.three_loss_stop_scope == "topstep_day":
                    self._blocked_topstep_days.add(self._session_key(exit_ts))
        elif pnl > 0:
            self._loss_streak = 0
            if self.rule.win_skip_scope == "market_segment":
                self._blocked_market_segments.add(market_segment_key(exit_ts))
            elif self.rule.win_skip_scope == "topstep_day":
                self._blocked_topstep_days.add(self._session_key(exit_ts))


def load_inputs():
    with STORE.open("rb") as handle:
        bars = sorted(pickle.load(handle), key=lambda c: c.timestamp)
    timeline = None
    for cache_path in (TIMELINE_CACHE, FALLBACK_TIMELINE_CACHE):
        if not cache_path.exists():
            continue
        with cache_path.open("rb") as handle:
            cache = pickle.load(handle)
        candidate = cache.get("timeline") if isinstance(cache, dict) else None
        if candidate is not None and len(candidate) == len(bars):
            timeline = candidate
            print(f"[cooldown] using timeline cache {cache_path}", flush=True)
            break
        if candidate is not None:
            print(
                f"[cooldown] ignoring stale timeline {cache_path} "
                f"({len(candidate)} != {len(bars)})",
                flush=True,
            )
    if timeline is None:
        print("[cooldown] rebuilding dedicated zone timeline cache", flush=True)
        timeline = build_zone_timeline(
            bars,
            timeframes_for_base(1),
            TICK_SIZE,
            MAX_RECENCY_DEPTH,
        )
        TIMELINE_CACHE.write_bytes(pickle.dumps({
            "generated_at": datetime.now(UTC).isoformat(),
            "bars": len(bars),
            "timeline": timeline,
        }))
        print(f"[cooldown] wrote timeline cache {TIMELINE_CACHE}", flush=True)
    return bars, timeline


def run_variant(rule: RuleSpec, bars, timeline):
    signal_cfg = ConfluenceConfig(band_ticks=4.0, min_distinct_tf=2, rr=5.0)
    signal_cfg.direction_mode = "auto"
    signal_cfg.tick_size = TICK_SIZE
    signal_cfg.enable_breakout = False
    signal_cfg.max_risk_ticks = 80
    signal_cfg.ev_floor = None
    signal_cfg.rr_grid = None

    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=1,
        one_trade_per_session_direction=True,
        timeframes=timeframes_for_base(1),
        min_score=0.0,
        base_minutes=1,
        trail_trigger_pct=0.50,
        trail_lock_pct=0.05,
        full_tp_lock=0,
        allowed_sessions=tuple(DEFAULT_ALLOWED_SESSIONS),
    )
    bt_cfg = BacktestConfig(
        initial_capital=50000.0,
        symbol="MNQ",
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
    )
    bt = RuleBacktester(
        signal_cfg=signal_cfg,
        run_cfg=run_cfg,
        contract_id=CONTRACT_ID,
        contract_size=3,
        bt_config=bt_cfg,
        scorer=ConfluenceScorer.load(MODEL_PATH),
        rule=rule,
    )
    print(f"[cooldown] {rule.name}", flush=True)
    result = bt.run(bars, zones_timeline=timeline)
    trades = result.trades

    by_segment = defaultdict(list)
    by_tf = defaultdict(list)
    by_day = defaultdict(list)
    for trade in trades:
        by_segment[market_session_code(trade.entry_time)].append(trade)
        by_tf[str((trade.meta or {}).get("largest_tf") or "unknown")].append(trade)
        by_day[trade.entry_time.astimezone(PT).date().isoformat()].append(trade)

    return {
        "name": rule.name,
        "rule": {
            "loss_cooldown_minutes": rule.loss_cooldown_minutes,
            "any_trade_gap_minutes": rule.any_trade_gap_minutes,
            "win_skip_scope": rule.win_skip_scope,
            "three_loss_stop_scope": rule.three_loss_stop_scope,
        },
        "contracts": 3,
        "allowed_sessions": list(DEFAULT_ALLOWED_SESSIONS),
        "full": summary(trades),
        "in_sample": summary([t for t in trades if t.entry_time <= TRAIN_END]),
        "oos_after_train_end": summary([t for t in trades if t.entry_time > TRAIN_END]),
        "by_segment": {key: summary(vals) for key, vals in sorted(by_segment.items())},
        "by_tf": {key: summary(vals) for key, vals in sorted(by_tf.items())},
        "by_day": {key: summary(vals) for key, vals in sorted(by_day.items())},
    }


def one_contract(row):
    out = json.loads(json.dumps(row))
    out["name"] += "_MNQx1"
    out["contracts"] = 1
    for section in ("full", "in_sample", "oos_after_train_end"):
        for field in ("pnl", "max_dd", "expectancy"):
            out[section][field] /= 3.0
    for groups in ("by_segment", "by_tf", "by_day"):
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
    rules = [
        RuleSpec("baseline_ASIA_PRE"),
        RuleSpec("loss_cooldown_1h", loss_cooldown_minutes=60),
        RuleSpec("trade_gap_1h_after_exit", any_trade_gap_minutes=60),
        RuleSpec("win_skip_market_segment", win_skip_scope="market_segment"),
        RuleSpec("win_skip_topstep_day", win_skip_scope="topstep_day"),
        RuleSpec("three_loss_stop_market_segment", three_loss_stop_scope="market_segment"),
        RuleSpec("three_loss_stop_topstep_day", three_loss_stop_scope="topstep_day"),
        RuleSpec(
            "combo_loss1h_threeLossSegment",
            loss_cooldown_minutes=60,
            three_loss_stop_scope="market_segment",
        ),
    ]
    rows = [run_variant(rule, bars, timeline) for rule in rules]
    rows_all = rows + [one_contract(row) for row in rows]

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": MODEL_ID,
        "range": [bars[0].timestamp.isoformat(), bars[-1].timestamp.isoformat()],
        "train_end": TRAIN_END.isoformat(),
        "common": {
            "rr": 5,
            "min_prob": "OFF",
            "max_risk_ticks": 80,
            "trail_trigger_pct": 0.50,
            "trail_lock_pct": 0.05,
            "session_limit": "conservative TF+direction Topstep-session lock ON",
            "market_sessions": allowed_sessions_label(DEFAULT_ALLOWED_SESSIONS),
        },
        "variants": rows_all,
    }
    RESULTS_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    full_rows = [
        [r["name"], r["contracts"], *fmt_metric(r["full"])]
        for r in rows_all
    ]
    oos_rows = [
        [r["name"], r["contracts"], *fmt_metric(r["oos_after_train_end"])]
        for r in rows_all
    ]
    segment_rows = []
    for r in rows:
        for seg in ("ASIA", "PRE"):
            m = r["by_segment"].get(seg) or summary([])
            segment_rows.append([r["name"], seg, *fmt_metric(m)])

    report = f"""# Cooldown / lockout study — 2026-06-23

Model: `{MODEL_ID}`  
Data: `{bars[0].timestamp.isoformat()}` to `{bars[-1].timestamp.isoformat()}`  
Common: RR5, probability gate OFF, MaxRisk80, Trail50/Lock5, band4, minTF2,
market `{allowed_sessions_label(DEFAULT_ALLOWED_SESSIONS)}`, conservative
session lock ON.

## Full range

{table(["Variant", "MNQ", "N", "Win", "P&L", "MaxDD", "PF", "Exp"], full_rows)}

## OOS after scorer training end

Training cutoff: `{TRAIN_END.isoformat()}`

{table(["Variant", "MNQ", "N", "Win", "P&L", "MaxDD", "PF", "Exp"], oos_rows)}

## By market segment (MNQx3)

{table(["Variant", "Segment", "N", "Win", "P&L", "MaxDD", "PF", "Exp"], segment_rows)}
"""
    REPORT_MD.write_text(report, encoding="utf-8")
    print(f"[cooldown] wrote {RESULTS_JSON}", flush=True)
    print(f"[cooldown] wrote {REPORT_MD}", flush=True)


if __name__ == "__main__":
    main()
