"""Read-only study: 2026-06-23 live drift and pending-order timeout.

This script never calls the live broker/API and never starts/stops the live
engine.  It replays the current production CODEX #2 confluence preset from the
local 1m store and compares different one-shot limit-order lifetimes:

- fixed wait: 1/5/10/15/30/60 minutes
- adaptive_min_tf: wait = shortest contributing TF in the confluence cluster
- adaptive_largest_tf: wait = largest/primary TF in the confluence cluster
- adaptive_largest_cap15: largest TF, capped at 15 minutes

It also audits the local live ledgers for PT 2026-06-23, including the known
case where Topstep trade_history shows a fill after the bot ledger recorded the
signal as cancelled.
"""

from __future__ import annotations

import json
import math
import pickle
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.confluence_backtest import (  # noqa: E402
    ConfluenceBacktestConfig,
    ConfluenceBacktester,
    build_zone_timeline,
)
from backend.backtest.metrics import MetricsCalculator  # noqa: E402
from backend.db.models import (  # noqa: E402
    BacktestConfig,
    Direction,
    ExitReason,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.confluence import (  # noqa: E402
    ConfluenceConfig,
    MAX_RECENCY_DEPTH,
)
from backend.strategy.confluence_scorer import ConfluenceScorer  # noqa: E402
from backend.strategy.consolidation import (  # noqa: E402
    AREA_TIMEFRAME_MINUTES,
    timeframes_for_base,
)
from backend.strategy.confluence_features import CONTEXT_WINDOW  # noqa: E402
from backend.strategy.session_filter import market_session_code  # noqa: E402


DATA = ROOT / "data"
OUT = DATA / "machinelearning"
STORE = DATA / "store" / "MNQ_accumulated_1m.pkl"
TIMELINE_CACHE = OUT / "pending_timeout_zone_timeline_20260624.pkl"
MODEL_ID = "20260618_codex_rr3-band4-mintf2-production-baseline-02"
MODEL_PATH = DATA / "models" / "registry" / f"{MODEL_ID}.json"
RESULTS_JSON = OUT / "pending_timeout_study_20260624.json"
REPORT_MD = OUT / "pending_timeout_study_20260624.md"

CONTRACT_ID = "CON.F.US.MNQ.U26"
TICK_SIZE = 0.25
POINT_VALUE = 2.0
CONTRACTS = 3
PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
TRAIN_END = datetime(2026, 6, 18, 20, 59, tzinfo=UTC)
TARGET_PT_DATE = "2026-06-23"


def _safe_iso(s) -> Optional[datetime]:
    if not s:
        return None
    text = str(s)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Topstep occasionally emits too many fractional digits.
    m = re.match(r"(.+\.)(\d{6})\d+([+-]\d\d:\d\d)$", text)
    if m:
        text = m.group(1) + m.group(2) + m.group(3)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def pt_date(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(PT).date().isoformat()


def pt_hour(dt: datetime) -> str:
    local = dt.astimezone(PT)
    return local.strftime("%Y-%m-%d %H:00")


def topstep_session_key(ts: datetime) -> str:
    # Same CT 17:00 trade-date convention used by ConfluenceBacktester.
    ct = ts.astimezone(ZoneInfo("America/Chicago"))
    if ct.hour >= 17:
        ct = ct + timedelta(days=1)
    return ct.strftime("%Y-%m-%d")


def market_segment_key(ts: datetime) -> str:
    ts = ts.astimezone(UTC)
    code = market_session_code(ts)
    if code == "ASIA" and ts.hour < 7:
        day = (ts - timedelta(days=1)).date()
    else:
        day = ts.date()
    return f"{day.isoformat()}-{code}"


def max_drawdown(pnls):
    equity = peak = dd = 0.0
    for pnl in pnls:
        equity += float(pnl or 0.0)
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def summarize_trades(trades):
    pnls = [float(getattr(t, "pnl", 0.0) or 0.0) for t in trades]
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


def timeframe_minutes(tf: str) -> int:
    return int(AREA_TIMEFRAME_MINUTES.get(str(tf), 1) or 1)


@dataclass(frozen=True)
class WaitSpec:
    name: str
    selector: str
    fixed_minutes: int = 1
    cap_minutes: Optional[int] = None


class AdaptiveWaitBacktester(ConfluenceBacktester):
    def __init__(self, *args, wait_spec: WaitSpec, **kwargs):
        super().__init__(*args, **kwargs)
        self.wait_spec = wait_spec
        self._pending_wait_bars = 1
        self._pending_wait_minutes = 1
        self.pending_attempts: list[dict] = []

    def _wait_minutes_for_signal(self, sig) -> int:
        spec = self.wait_spec
        if spec.selector == "fixed":
            minutes = int(spec.fixed_minutes)
        else:
            tfs = list(getattr(sig.cluster, "distinct_tfs", None) or [])
            if not tfs:
                largest = getattr(sig.cluster, "largest_tf", None)
                if largest:
                    tfs = [largest]
            values = [timeframe_minutes(tf) for tf in tfs if timeframe_minutes(tf) > 0]
            if not values:
                minutes = int(spec.fixed_minutes or 1)
            elif spec.selector == "min_tf":
                minutes = min(values)
            elif spec.selector == "largest_tf":
                largest = getattr(sig.cluster, "largest_tf", None)
                minutes = timeframe_minutes(largest) if largest else max(values)
            else:
                minutes = int(spec.fixed_minutes or 1)
        if spec.cap_minutes:
            minutes = min(minutes, int(spec.cap_minutes))
        return max(1, int(minutes))

    def _new_pending_record(self, candle, sig) -> dict:
        cl = sig.cluster
        return {
            "placed_time": candle.timestamp.isoformat(),
            "placed_pt": candle.timestamp.astimezone(PT).isoformat(),
            "pt_date": pt_date(candle.timestamp),
            "pt_hour": pt_hour(candle.timestamp),
            "segment": market_session_code(candle.timestamp),
            "market_segment_key": market_segment_key(candle.timestamp),
            "topstep_session": topstep_session_key(candle.timestamp),
            "direction": getattr(sig.direction, "value", str(sig.direction)),
            "entry": round(float(sig.entry_price), 2),
            "sl": round(float(sig.sl_price), 2),
            "tp": round(float(sig.tp_price), 2),
            "risk_ticks": round(abs(sig.entry_price - sig.sl_price) / TICK_SIZE, 1),
            "wait_minutes": self._pending_wait_minutes,
            "wait_bars": self._pending_wait_bars,
            "mode": sig.direction_mode,
            "side": cl.side,
            "largest_tf": cl.largest_tf,
            "min_tf": min((timeframe_minutes(tf), tf) for tf in cl.distinct_tfs)[1]
            if cl.distinct_tfs else None,
            "wall_id": getattr(cl, "wall_id", None),
            "labels": list(cl.labels),
            "tfs": list(cl.distinct_tfs),
            "score": round(float(sig.score), 4),
            "prob": round(float(sig.prob), 4),
            "result": "pending",
        }

    def _maybe_open(self, candle, zones_by_tf=None, recent_candles=None):
        before = self._pending
        super()._maybe_open(candle, zones_by_tf, recent_candles)
        if self._pending is not None and self._pending is not before:
            self._pending_wait_minutes = self._wait_minutes_for_signal(self._pending)
            self._pending_wait_bars = max(
                1,
                round(self._pending_wait_minutes / max(1, self.run_cfg.base_minutes)),
            )
            self.pending_attempts.append(self._new_pending_record(candle, self._pending))

    def _mark_pending(self, result: str, candle, fill_trade=None):
        if not self.pending_attempts:
            return
        rec = self.pending_attempts[-1]
        if rec.get("result") != "pending":
            return
        rec["result"] = result
        rec["resolved_time"] = candle.timestamp.isoformat()
        rec["resolved_pt"] = candle.timestamp.astimezone(PT).isoformat()
        rec["age_bars"] = self._pending_age
        if fill_trade is not None:
            rec["trade_id"] = getattr(fill_trade, "trade_id", None)
            rec["fill_price"] = getattr(fill_trade, "entry_price", None)

    def _try_fill(self, candle) -> bool:
        before_open = self._open
        filled = super()._try_fill(candle)
        if filled:
            self._mark_pending("filled", candle, self._open or before_open)
        return filled

    def _open_trade(self, sig, candle):
        super()._open_trade(sig, candle)
        if self._open is not None:
            self._open.meta = dict(self._open.meta or {})
            self._open.meta["wait_min"] = self._pending_wait_minutes
            self._open.meta["wait_spec"] = self.wait_spec.name

    def run(self, candles_1m, zones_timeline=None, progress_callback=None):
        candles = candles_1m if zones_timeline is not None else sorted(
            candles_1m, key=lambda c: c.timestamp
        )
        total = len(candles)
        edge_guard = 65
        for i, candle in enumerate(candles):
            if zones_timeline is None:
                for det in self.detectors.values():
                    det.update(candle)

            if self._open is not None:
                self._check_exit(candle)
                if self._open is not None:
                    continue

            if self._pending is not None:
                if not self._session_entry_allowed(candle.timestamp):
                    self._release_pending_lock(candle.timestamp, self._pending)
                    self._mark_pending("cancel_session", candle)
                    self._pending = None
                    self._pending_age = 0
                    continue
                if self._try_fill(candle):
                    continue
                self._pending_age += 1
                if self._pending_age >= self._pending_wait_bars:
                    self._release_pending_lock(candle.timestamp, self._pending)
                    self._mark_pending("cancel_timeout", candle)
                    self._pending = None
                    self._pending_age = 0
                continue

            if i >= total - edge_guard:
                continue
            snap = zones_timeline[i] if zones_timeline is not None else None
            recent = candles[max(0, i - CONTEXT_WINDOW + 1): i + 1]
            self._maybe_open(candle, snap, recent)

        return self._finalize()


def load_inputs():
    with STORE.open("rb") as handle:
        bars = sorted(pickle.load(handle), key=lambda c: c.timestamp)
    timeline = None
    if TIMELINE_CACHE.exists():
        with TIMELINE_CACHE.open("rb") as handle:
            cache = pickle.load(handle)
        candidate = cache.get("timeline") if isinstance(cache, dict) else None
        if candidate is not None and len(candidate) == len(bars):
            timeline = candidate
            print(f"[pending-study] using timeline cache {TIMELINE_CACHE}", flush=True)
        else:
            print("[pending-study] stale/missing timeline cache -> rebuild", flush=True)
    if timeline is None:
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
        print(f"[pending-study] wrote timeline cache {TIMELINE_CACHE}", flush=True)
    return bars, timeline


def make_configs():
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
        allowed_sessions=("ASIA",),
    )
    bt_cfg = BacktestConfig(
        initial_capital=50000.0,
        symbol="MNQ",
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
    )
    return signal_cfg, run_cfg, bt_cfg


def run_variant(spec: WaitSpec, bars, timeline, scorer):
    signal_cfg, run_cfg, bt_cfg = make_configs()
    bt = AdaptiveWaitBacktester(
        signal_cfg=signal_cfg,
        run_cfg=run_cfg,
        contract_id=CONTRACT_ID,
        contract_size=CONTRACTS,
        bt_config=bt_cfg,
        scorer=scorer,
        wait_spec=spec,
    )
    print(f"[pending-study] replay {spec.name}", flush=True)
    result = bt.run(bars, zones_timeline=timeline)
    trades = result.trades
    attempts = bt.pending_attempts

    by_day = defaultdict(list)
    by_hour = defaultdict(list)
    by_segment = defaultdict(list)
    by_tf = defaultdict(list)
    for trade in trades:
        by_day[pt_date(trade.entry_time)].append(trade)
        by_hour[pt_hour(trade.entry_time)].append(trade)
        by_segment[market_session_code(trade.entry_time)].append(trade)
        by_tf[str((trade.meta or {}).get("largest_tf") or "unknown")].append(trade)

    attempts_by_day = defaultdict(list)
    attempts_by_hour = defaultdict(list)
    attempts_by_result = Counter()
    attempts_by_tf_result = defaultdict(Counter)
    wait_dist = Counter()
    for rec in attempts:
        attempts_by_day[rec["pt_date"]].append(rec)
        attempts_by_hour[rec["pt_hour"]].append(rec)
        attempts_by_result[rec["result"]] += 1
        attempts_by_tf_result[str(rec.get("largest_tf") or "unknown")][rec["result"]] += 1
        wait_dist[str(rec["wait_minutes"])] += 1

    target_trades = by_day.get(TARGET_PT_DATE, [])
    target_attempts = attempts_by_day.get(TARGET_PT_DATE, [])
    return {
        "name": spec.name,
        "wait_spec": spec.__dict__,
        "full": summarize_trades(trades),
        "oos_after_train_end": summarize_trades([t for t in trades if t.entry_time > TRAIN_END]),
        "target_pt_date": summarize_trades(target_trades),
        "target_pt_date_trade_rows": [
            {
                "entry_pt": t.entry_time.astimezone(PT).isoformat(),
                "exit_pt": t.exit_time.astimezone(PT).isoformat() if t.exit_time else None,
                "segment": market_session_code(t.entry_time),
                "direction": getattr(t.direction, "value", str(t.direction)),
                "entry": t.entry_price,
                "exit": t.exit_price,
                "pnl": t.pnl,
                "exit_reason": getattr(t.exit_reason, "value", str(t.exit_reason)),
                "largest_tf": (t.meta or {}).get("largest_tf"),
                "wall_id": (t.meta or {}).get("wall_id"),
                "labels": (t.meta or {}).get("labels"),
                "wait_min": (t.meta or {}).get("wait_min"),
                "prob": (t.meta or {}).get("prob"),
                "score": (t.meta or {}).get("score"),
            }
            for t in target_trades
        ],
        "target_attempts": {
            "orders": len(target_attempts),
            "results": dict(Counter(rec["result"] for rec in target_attempts)),
            "by_hour": {
                k: {
                    "orders": len(v),
                    "filled": sum(1 for r in v if r["result"] == "filled"),
                    "timeout": sum(1 for r in v if r["result"] == "cancel_timeout"),
                    "pnl": summarize_trades(by_hour.get(k, [])).get("pnl", 0.0),
                    "trades": len(by_hour.get(k, [])),
                }
                for k, v in sorted(attempts_by_hour.items())
                if k.startswith(TARGET_PT_DATE)
            },
            "first20": target_attempts[:20],
            "last20": target_attempts[-20:],
        },
        "attempts_all": {
            "orders": len(attempts),
            "results": dict(attempts_by_result),
            "wait_dist": dict(wait_dist),
            "by_tf_result": {k: dict(v) for k, v in sorted(attempts_by_tf_result.items())},
        },
        "by_segment": {k: summarize_trades(v) for k, v in sorted(by_segment.items())},
        "by_tf": {k: summarize_trades(v) for k, v in sorted(by_tf.items())},
        "by_day": {k: summarize_trades(v) for k, v in sorted(by_day.items())},
    }


def live_trade_pnl(row) -> float:
    if row.get("pnl") is not None:
        return float(row.get("pnl") or 0.0)
    if row.get("entry_price") is None or row.get("exit_price") is None:
        return 0.0
    size = float(row.get("size") or row.get("contracts") or CONTRACTS)
    if str(row.get("direction")).lower() in ("buy", "long"):
        gross = (float(row["exit_price"]) - float(row["entry_price"])) * POINT_VALUE * size
    else:
        gross = (float(row["entry_price"]) - float(row["exit_price"])) * POINT_VALUE * size
    return gross - float(row.get("commission") or 0.0) - float(row.get("fees") or 0.0)


def audit_live_ledgers():
    out = {}
    for rel in ("data/trades.json", "data/live_exits.json", "data/trade_history.json"):
        path = ROOT / rel
        if not path.exists():
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))
        picked = []
        for row in rows:
            dt = _safe_iso(row.get("entry_time") or row.get("exit_time"))
            if not dt or pt_date(dt) != TARGET_PT_DATE:
                continue
            pnl = live_trade_pnl(row)
            conf = row.get("confluence") or {}
            picked.append({
                "entry_pt": dt.astimezone(PT).isoformat(),
                "exit_pt": (_safe_iso(row.get("exit_time")).astimezone(PT).isoformat()
                            if _safe_iso(row.get("exit_time")) else None),
                "account_id": row.get("account_id"),
                "status": row.get("status"),
                "direction": row.get("direction"),
                "size": row.get("size"),
                "entry": row.get("entry_price"),
                "exit": row.get("exit_price"),
                "pnl": pnl,
                "exit_reason": row.get("exit_reason"),
                "segment": market_session_code(dt),
                "largest_tf": row.get("largest_tf") or conf.get("largest_tf"),
                "wall_id": row.get("wall_id") or conf.get("wall_id"),
                "labels": row.get("labels") or conf.get("labels"),
            })
        closed = [
            r for r in picked
            if r.get("exit") is not None or r.get("status") == "closed"
        ]
        cancelled = [r for r in picked if r.get("status") == "cancelled" or r.get("exit_reason") == "cancelled"]
        by_account = defaultdict(list)
        by_segment = defaultdict(list)
        for r in closed:
            by_account[str(r.get("account_id"))].append(r)
            by_segment[r.get("segment")].append(r)
        out[rel] = {
            "rows": len(picked),
            "closed": len(closed),
            "cancelled": len(cancelled),
            "pnl": sum(r["pnl"] for r in closed),
            "by_account": {
                k: {
                    "trades": len(v),
                    "pnl": sum(r["pnl"] for r in v),
                    "wins": sum(1 for r in v if r["pnl"] > 0),
                    "losses": sum(1 for r in v if r["pnl"] < 0),
                }
                for k, v in sorted(by_account.items())
            },
            "by_segment": {
                k: {
                    "trades": len(v),
                    "pnl": sum(r["pnl"] for r in v),
                    "wins": sum(1 for r in v if r["pnl"] > 0),
                    "losses": sum(1 for r in v if r["pnl"] < 0),
                }
                for k, v in sorted(by_segment.items())
            },
            "rows_detail": picked,
        }
    return out


def fmt_money(v):
    return f"${v:,.0f}"


def fmt_pct(v):
    return f"{v * 100:.1f}%"


def metric_row(name, metrics):
    pf = metrics["profit_factor"]
    return [
        name,
        metrics["trades"],
        fmt_pct(metrics["win_rate"]),
        fmt_money(metrics["pnl"]),
        fmt_money(metrics["max_dd"]),
        "∞" if math.isinf(pf) else f"{pf:.2f}",
        f"${metrics['expectancy']:.2f}",
    ]


def table(headers, rows):
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *["| " + " | ".join(map(str, row)) + " |" for row in rows],
    ])


def write_report(results):
    variants = results["variants"]
    target_rows = [metric_row(v["name"], v["target_pt_date"]) for v in variants]
    oos_rows = [metric_row(v["name"], v["oos_after_train_end"]) for v in variants]

    best_target = max(variants, key=lambda v: (v["target_pt_date"]["pnl"], -v["target_pt_date"]["max_dd"]))
    best_oos = max(variants, key=lambda v: (v["oos_after_train_end"]["pnl"], -v["oos_after_train_end"]["max_dd"]))

    current = next(v for v in variants if v["name"] == "fixed_1m_current")
    current_hours = current["target_attempts"]["by_hour"]
    hour_rows = [
        [h, x["orders"], x["filled"], x["timeout"], x["trades"], fmt_money(x["pnl"])]
        for h, x in current_hours.items()
    ]

    live = results["live_audit"]
    trade_hist = live.get("data/trade_history.json", {})
    live_ledger = live.get("data/trades.json", {})

    report = f"""# Pending timeout / 6-23 drift study — 2026-06-24

Model/preset tested: `{MODEL_ID}`, CODEX #2 style — RR5, POff, MaxRisk80,
Trail50/Lock5, SessionLimit ON, allowed session = ASIA, MNQx3.

Data: `{results['range'][0]}` to `{results['range'][1]}`.

## Main result

On PT `{TARGET_PT_DATE}`, the current 1-minute pending timeout produced many
cancelled one-shot orders and a small number of fills.  The live ledger and
Topstep trade history disagree on one important late fill: the bot-side
`trades.json` recorded a signal as cancelled, while Topstep `trade_history.json`
shows the same price later filled and stopped out.  That is live/backtest drift:
the backtest assumes cancel succeeds; live can still get filled if broker cancel
fails or local state is stale.

Best PT-day PnL variant in this test: `{best_target['name']}`  
Best OOS-after-training variant: `{best_oos['name']}`

## PT 2026-06-23 only

{table(["Variant", "Trades", "Win", "P&L", "MaxDD", "PF", "Exp"], target_rows)}

## OOS after training cutoff

Training cutoff: `{TRAIN_END.isoformat()}`

{table(["Variant", "Trades", "Win", "P&L", "MaxDD", "PF", "Exp"], oos_rows)}

## Current 1m timeout concentration on PT 2026-06-23

{table(["PT hour", "Orders", "Filled", "Timed out", "Trades", "P&L"], hour_rows)}

## Live ledger audit, PT 2026-06-23

- `data/trades.json`: rows={live_ledger.get('rows')}, closed={live_ledger.get('closed')}, cancelled={live_ledger.get('cancelled')}, closed P&L={fmt_money(live_ledger.get('pnl', 0.0))}
- `data/trade_history.json`: rows={trade_hist.get('rows')}, closed={trade_hist.get('closed')}, P&L across copied accounts={fmt_money(trade_hist.get('pnl', 0.0))}

`trade_history.json` includes copied-account executions.  Per-account P&L:

{table(["Account", "Trades", "Wins", "Losses", "P&L"], [
    [acct, row["trades"], row["wins"], row["losses"], fmt_money(row["pnl"])]
    for acct, row in sorted((trade_hist.get("by_account") or {}).items())
])}

## Interpretation

1. ASIA-only explains why RTH/PRE open had no trades.  The preset is explicitly
   not allowed to enter during regular US open.
2. The evening burst is mostly repeated one-shot pending orders around the same
   cluster/wall.  With 1m timeout, an unfilled limit cancels quickly, the same
   confluence still exists, and the next bar reposts.
3. Longer pending lifetimes change both fill timing and session locks.  They can
   catch missed pullbacks, but they also leave stale orders alive after the
   market context has moved.  The adaptive variants quantify that trade-off.
4. The dangerous live-only difference is cancel failure/stale exchange state.
   If cancel fails, the bot currently keeps retrying and keeps local pending
   state, but Topstep can still fill/close that order in a way the backtest does
   not model.
"""
    REPORT_MD.write_text(report, encoding="utf-8")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    bars, timeline = load_inputs()
    scorer = ConfluenceScorer.load(MODEL_PATH)
    specs = [
        WaitSpec("fixed_1m_current", "fixed", 1),
        WaitSpec("fixed_5m", "fixed", 5),
        WaitSpec("fixed_10m", "fixed", 10),
        WaitSpec("fixed_15m", "fixed", 15),
        WaitSpec("fixed_30m", "fixed", 30),
        WaitSpec("fixed_60m", "fixed", 60),
        WaitSpec("adaptive_min_tf", "min_tf", 1),
        WaitSpec("adaptive_largest_tf", "largest_tf", 1),
        WaitSpec("adaptive_largest_cap15", "largest_tf", 1, cap_minutes=15),
    ]
    variants = [run_variant(spec, bars, timeline, scorer) for spec in specs]
    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model": MODEL_ID,
        "contract": CONTRACT_ID,
        "contracts": CONTRACTS,
        "range": [bars[0].timestamp.isoformat(), bars[-1].timestamp.isoformat()],
        "target_pt_date": TARGET_PT_DATE,
        "variants": variants,
        "live_audit": audit_live_ledgers(),
    }
    RESULTS_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(results)
    print(f"[pending-study] wrote {RESULTS_JSON}", flush=True)
    print(f"[pending-study] wrote {REPORT_MD}", flush=True)


if __name__ == "__main__":
    main()
