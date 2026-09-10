"""Offline event hypotheses using the production PI strategy/exit engine.

No broker calls. Existing MBO minute caches support minute-resolution state
transitions; raw-derived burst events supply subminute execution sequences.
Previously examined dates are retrospective validation, never fresh holdout.
"""
from __future__ import annotations

import gzip
import json
import logging
import sys
from bisect import bisect_left
from collections import defaultdict
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestEngine
from backend.backtest.robustness import segment_index
from backend.data import candle_store, market_data
from backend.db.models import StrategyParams
from backend.live.pi_listener import PiSignal
from backend.strategy.pi_signal import PiSignalStrategy
from backend.timebase import UTC
from scripts.orderflow_context_combination_study import _prepare_days
from scripts.orderflow_entry_competition import _metrics


class EventStrategy(PiSignalStrategy):
    """Replace only the entry feed; inherit PI geometry and engine controls."""
    def __init__(self, params, events):
        super().__init__(params)
        self._hist = []
        self.events = iter(sorted(events))
        self.next_event = next(self.events, None)

    def evaluate(self, candle, zones=None, is_mature=True):
        now = candle.timestamp.timestamp()
        while self.next_event and self.next_event[0] <= now:
            stamp, direction = self.next_event
            # Events blocked by an open position do not become stale entries.
            if now - stamp < 60:
                self.push(PiSignal(
                    message_id=f"research:{stamp}:{direction}",
                    ts=datetime.fromtimestamp(stamp, UTC), equity="QQQ",
                    future="MNQ", direction=direction,
                    kind="青π" if direction > 0 else "粉π", size="", pos=None,
                ))
            self.next_event = next(self.events, None)
        return super().evaluate(candle, zones, is_mature)


def hypotheses(days):
    """Fixed hypotheses, online regression trained only on preceding bars.

    Absorption residual uses OFI / previous depth; confirmations consume
    subsequent completed bars. No full-day extreme or outcome label is read.
    """
    events = defaultdict(set)
    for family in ("absorption", "defense_break", "false_break", "split_pressure"):
        for strength in (1.0, 2.0):
            events[f"{family}:{strength}"]
    for day in days:
        bars = day["bars"]
        xx, xy, err2, n = 1e-9, 0.0, 0.0, 0
        for i in range(1, len(bars)):
            p, b = bars[i-1], bars[i]
            if any(r.get(k) is None for r in (p, b) for k in ("open", "high", "low", "close")):
                continue
            depth = max(1, float(p.get("close_bid_depth", 0)) + float(p.get("close_ask_depth", 0)))
            x = float(b.get("ofi", 0)) / depth
            change = float(b["close"]) - float(p["close"])
            residual = change - xy / xx * x
            sigma = max(0.25, (err2 / max(n, 1)) ** 0.5)
            available = int(b["epoch"]) + 60
            if i >= 30 and n >= 20 and int(b["epoch"]) == int(p["epoch"]) + 60:
                recent = bars[i-10:i]
                high = max(float(r["high"]) for r in recent if r.get("high") is not None)
                low = min(float(r["low"]) for r in recent if r.get("low") is not None)
                for strength in (1.0, 2.0):
                    for direction in (-1, 1):
                        side = "bid" if direction > 0 else "ask"
                        # Prior minute opposing aggression fails to push price;
                        # current minute confirms reversal and positive pressure.
                        opp = float(p.get("sell" if direction > 0 else "buy", 0))
                        own = float(p.get("buy" if direction > 0 else "sell", 0))
                        stalled = direction * (float(p["close"]) - float(p["open"])) >= -0.25
                        refill = float(p.get(side + "_refill_qty", 0))
                        if opp > strength * max(1, own) and stalled and refill > 0 and direction * residual > strength * sigma and direction * x > 0:
                            events[f"absorption:{strength}"].add((available, direction))
                        opposite = "ask" if direction > 0 else "bid"
                        previous_depth = float(p.get("close_" + opposite + "_depth", 0))
                        current_depth = float(b.get("close_" + opposite + "_depth", 0))
                        crossed = float(b["close"]) > high if direction > 0 else float(b["close"]) < low
                        if crossed and previous_depth > 0 and current_depth < previous_depth / (1 + strength) and direction * x > strength:
                            events[f"defense_break:{strength}"].add((available, direction))
                        reclaimed = (float(b["low"]) < low and float(b["close"]) > low) if direction > 0 else (float(b["high"]) > high and float(b["close"]) < high)
                        if reclaimed and direction * residual > strength * sigma and direction * x > 0:
                            events[f"false_break:{strength}"].add((available, direction))
            # Update strictly after using the prediction/residual.
            xx = 0.98 * xx + x*x
            xy = 0.98 * xy + x*change
            err2 += residual * residual
            n += 1
    path = market_data.derived_path("research", "orderflow_microburst_study.events.jsonl.gz")
    counts = defaultdict(int)
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            e = json.loads(line)
            counts["raw_event_rows"] += 1
            # Existing post-event fields include whole seconds t+1..t+3;
            # all three seconds become available at the START of t+4.
            if int(e["event_sec"]) % 60 >= 57:
                counts["post3s_crosses_old_next_open"] += 1
            if int(e.get("trade_count", 0)) < 3:
                continue
            for strength in (1.0, 2.0):
                if int(e.get("actual_qty", 0)) >= 150 * strength and int(e.get("event_impact_ticks", 0)) >= 2 * strength:
                    available = int(e["event_sec"]) + 1
                    events[f"split_pressure:{strength}"].add((available, int(e["direction"])))
    return events, dict(counts)


def replay(params, candles, events=None):
    engine = BacktestEngine(strategy_params=params, record_equity=False)
    if events is not None:
        engine.trend_follow = EventStrategy(params, events)
    result = engine.run(candles)
    return [{"date": t.entry_time.date().isoformat(),
             "entry_epoch": int(t.entry_time.timestamp()),
             "exit_epoch": int(t.exit_time.timestamp()),
             "direction": 1 if t.direction.value == "buy" else -1,
             "entry": t.entry_price, "exit": t.exit_price,
             "sl": t.sl_price, "tp": t.tp_price,
             "net_pnl": t.pnl, "exit_reason": str(t.exit_reason)}
            for t in result.trades]


def map_events(signals, epochs):
    """Abstain when both directions collapse onto the same decision candle."""
    grouped = defaultdict(set)
    for stamp, direction in signals:
        i = bisect_left(epochs, stamp)
        if i < len(epochs) and epochs[i] - stamp < 60:
            grouped[epochs[i]].add(direction)
    return {(stamp, next(iter(sides))) for stamp, sides in grouped.items()
            if len(sides) == 1}, sum(len(sides) > 1 for sides in grouped.values())


def main():
    logging.basicConfig(level=logging.ERROR)
    preset = json.loads((ROOT / "data/presets.json").read_text(encoding="utf-8"))
    # Preset storage may wrap its public fields in a versioned object.
    def find(obj):
        if isinstance(obj, dict):
            if "PI 2MNQ BOTH BEST" in obj:
                return obj["PI 2MNQ BOTH BEST"]
            for value in obj.values():
                found = find(value)
                if found is not None:
                    return found
        return None
    chosen = find(preset)
    if chosen is None:
        raise RuntimeError("PI baseline preset missing")
    keys = {f.name for f in fields(StrategyParams)}
    params = StrategyParams(**{k: v for k, v in chosen.items() if k in keys})
    print("Loading cached contexts", flush=True)
    days = _prepare_days()
    dates = sorted(d["date"] for d in days)
    if not dates:
        raise RuntimeError("No MBO coverage")
    start = datetime.fromisoformat(dates[0]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(dates[-1]).replace(tzinfo=UTC) + timedelta(days=1)
    print("Loading canonical candles with seven-day warmup", flush=True)
    snapshot = candle_store.load_snapshot("MNQ")
    candles = candle_store.select_range(snapshot, start - timedelta(days=7), end)
    del snapshot
    candle_store.invalidate_cache("MNQ")
    print(f"Baseline: {len(candles)} candles", flush=True)
    baseline = replay(params, candles)
    # Exact deterministic trade fingerprint before evaluating hypotheses.
    if baseline != replay(params, candles) or not baseline:
        raise RuntimeError("Baseline reproduction failed or produced no trades")
    events, audit = hypotheses(days)
    epochs = [int(c.timestamp.timestamp()) for c in candles]
    results = {}
    allowed_dates = set(dates)
    cutoff = max(d for i, d in enumerate(dates) if segment_index(i, len(dates)) < 2)
    def summarize(trades):
        trades = [t for t in trades if t["date"] in allowed_dates]
        return {name: _metrics([t for t in trades if predicate(t)],
                              sum(date_predicate(d) for d in dates))
                for name, (predicate, date_predicate) in {
                    "all": (lambda t: True, lambda d: True),
                    "early": (lambda t: t["date"] <= cutoff, lambda d: d <= cutoff),
                    "later_retrospective": (lambda t: t["date"] > cutoff, lambda d: d > cutoff),
                    "long": (lambda t: t["direction"] > 0, lambda d: True),
                    "short": (lambda t: t["direction"] < 0, lambda d: True),
                }.items()}
    for name, signals in events.items():
        # Materialize at the first candle timestamp at/after availability.
        # Engine execution remains its ordinary market-entry behavior.
        mapped, conflicts = map_events(signals, epochs)
        print(f"Replay {name}: {len(mapped)} events", flush=True)
        trades = replay(params, candles, mapped)
        results[name] = {"events": len(mapped), "conflicting_candles_skipped": conflicts,
                         "metrics": summarize(trades), "trades": trades}
    report = {"status": "retrospective_research_only", "dates": dates,
              "cutoff": cutoff, "preset": chosen, "audit": audit,
              "baseline": summarize(baseline), "baseline_trades": baseline,
              "results": results,
              "limitations": [
                  "All dates were used in previous research; no untouched holdout exists here.",
                  "Three state models use completed one-minute MBO caches, not tick-level reconstruction.",
                  "Burst candidates reuse raw-derived event archive; no participant identity is inferred.",
                  "Production PI has a short TP as well as 60-minute exit; old research disabled that TP.",
                  "Determinism on identical input is checked; this is not a live-fill parity claim.",
                  "No additional slippage model; production engine costs and fills are inherited.",
              ]}
    path = market_data.derived_path("research", "orderflow_event_engine_study.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# Order-flow entry hypotheses — production engine replay", "",
             f"Coverage: {dates[0]} to {dates[-1]}, {len(dates)} MBO dates; 2 MNQ.",
             "", "| Model | Trades | Net USD | Closed-trade maxDD | PF |",
             "|---|---:|---:|---:|---:|"]
    for name, metrics in [("PI preset", report["baseline"])] + [(k,v["metrics"]) for k,v in results.items()]:
        m = metrics["all"]
        lines.append(f"| {name} | {m['trades']} | {m['net_pnl']} | {m['max_drawdown_abs']} | {m['pf']} |")
    lines += ["", "## Limits", ""] + ["- " + note for note in report["limitations"]]
    path.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"report": str(path), "baseline": report["baseline"],
                      "results": {k: v["metrics"] for k,v in results.items()}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
