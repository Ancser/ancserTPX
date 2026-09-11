"""Completed-bar delta-change experiment; production PI exits, offline only."""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sys
from collections import Counter
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.robustness import segment_index
from backend.data import candle_store, market_data
from backend.db.models import StrategyParams
from backend.timebase import UTC
from scripts.orderflow_event_engine_study import replay, map_events
from scripts.orderflow_entry_competition import _metrics
from scripts.orderflow_pi_relationship_study import _load_days, _profile, TICK_SIZE

WINDOWS = (1, 3, 5, 10)
GATES = ("raw", "location", "reclaim", "reclaim_vwap")
NY = ZoneInfo("America/New_York")


def partition_delta(bar, profile):
    """VA boundary ticks belong inside; conserve signed executed delta."""
    out = dict.fromkeys(("below", "inside", "above"), 0.0)
    for cell in bar.get("cells", []):
        price = cell[0] * TICK_SIZE
        region = "below" if price < profile["val"] else "above" if price > profile["vah"] else "inside"
        out[region] += cell[1] - cell[2]
    return out


def changes(values, index, window):
    """Two adjacent windows of positive and negative magnitudes; no division by zero."""
    prior = values[index-2*window+1:index-window+1]
    recent = values[index-window+1:index+1]
    return tuple(mean(max(sign*x, 0) for x in block)
                 for sign in (1, -1) for block in (prior, recent))


def features(day, profile):
    bars = sorted(day["bars"], key=lambda b: int(b["epoch"]))
    rows = []
    volume = weighted = 0.0
    for b in bars:
        for c in b.get("cells", []):
            qty = c[1] + c[2]
            volume += qty
            weighted += c[0] * TICK_SIZE * qty
        rows.append({**b, "d": float(b.get("buy", 0))-float(b.get("sell", 0)),
                     "regions": partition_delta(b, profile) if profile else {},
                     "vwap": weighted/volume if volume else None})
    return rows


def generate(days, profiles):
    events = {f"{family}/w{w}/{source}/{gate}": set()
              for family in ("exhaustion", "absorption") for w in WINDOWS
              for source in ("whole", "outside") for gate in GATES}
    diagnostics = []
    for day in days:
        profile = profiles.get(day["date"])
        bars = features(day, profile)
        whole = [b["d"] for b in bars]
        for w in WINDOWS:
            for i in range(max(30, 2*w), len(bars)):
                history = bars[i-max(30, 2*w):i+1]
                if any(any(b.get(k) is None for k in ("open", "high", "low", "close")) for b in history):
                    continue
                if any(int(b["epoch"])-int(a["epoch"]) != 60 for a, b in zip(history, history[1:])):
                    continue
                b, prev = bars[i], bars[i-1]
                scale = max(1, median(abs(x) for x in whole[i-30:i]))
                location = "unknown" if not profile else (
                    "below" if b["close"] < profile["val"] else
                    "above" if b["close"] > profile["vah"] else "inside")
                for source in ("whole", "outside"):
                    if source == "outside" and not profile:
                        continue
                    for direction in (1, -1):
                        region = "below" if direction == 1 else "above"
                        values = whole if source == "whole" else [r["regions"][region] for r in bars]
                        p0, p1, n0, n1 = changes(values, i, w)
                        opp0, opp1 = (n0, n1) if direction == 1 else (p0, p1)
                        # Fixed 30% weakening versus persistent strong opposing aggression.
                        exhaustion = opp0 >= scale and opp1 <= 0.7*opp0
                        absorption = opp1 >= scale and opp1 >= 0.7*opp0
                        recent = bars[i-w+1:i+1]
                        old = bars[i-2*w+1:i-w+1]
                        if direction == 1:
                            stalled = min(r["low"] for r in recent) >= min(r["low"] for r in old)-TICK_SIZE
                            touched = bool(profile and min(r["low"] for r in history[-2*w:]) <= profile["val"])
                            reclaimed = bool(touched and b["close"] > profile["val"])
                        else:
                            stalled = max(r["high"] for r in recent) <= max(r["high"] for r in old)+TICK_SIZE
                            touched = bool(profile and max(r["high"] for r in history[-2*w:]) >= profile["vah"])
                            reclaimed = bool(touched and b["close"] < profile["vah"])
                        confirming = direction*(b["close"]-prev["close"]) > 0
                        aligned = b["vwap"] is not None and direction*(b["close"]-b["vwap"]) > 0
                        for family, qualifies in (("exhaustion", exhaustion), ("absorption", absorption and stalled)):
                            if not qualifies:
                                continue
                            for gate, accepted in (("raw", True), ("location", touched),
                                                   ("reclaim", reclaimed and stalled and confirming),
                                                   ("reclaim_vwap", reclaimed and stalled and confirming and aligned)):
                                if accepted:
                                    key = f"{family}/w{w}/{source}/{gate}"
                                    stamp = int(b["epoch"])+60
                                    events[key].add((stamp, direction))
                                    diagnostics.append({"rule": key, "available": stamp, "date": day["date"],
                                        "direction": direction, "location": location,
                                        "positive_previous": p0, "positive_recent": p1,
                                        "negative_previous": n0, "negative_recent": n1,
                                        "positive_change_scaled": (p1-p0)/scale,
                                        "negative_change_scaled": (n1-n0)/scale})
    return events, diagnostics


def summarize(trades, dates, cutoff):
    dates = set(dates)
    trades = [t for t in trades if t["date"] in dates]
    result = {}
    for split in ("all", "early", "later"):
        selected = dates if split == "all" else {d for d in dates if (d <= cutoff) == (split == "early")}
        for side, direction in (("both", 0), ("long", 1), ("short", -1)):
            rows = [t for t in trades if t["date"] in selected and (not direction or t["direction"] == direction)]
            result[f"{split}/{side}"] = _metrics(rows, len(selected))
    return result


def find_preset(obj):
    if isinstance(obj, dict):
        if "PI 2MNQ BOTH BEST" in obj:
            return obj["PI 2MNQ BOTH BEST"]
        for value in obj.values():
            found = find_preset(value)
            if found is not None:
                return found
    return None


def minute_grid(candles):
    """Reject off-grid records from the research copy; never rewrite the store."""
    valid, rejected = [], []
    for c in candles:
        if c.timestamp.second or c.timestamp.microsecond:
            rejected.append({"timestamp":c.timestamp.isoformat(),"open":c.open,
                             "high":c.high,"low":c.low,"close":c.close})
        else:
            valid.append(c)
    return valid,rejected


def main():
    logging.basicConfig(level=logging.ERROR)
    target = market_data.derived_path("research", "delta_value_change_study")
    target.mkdir(parents=True, exist_ok=True)
    chosen = find_preset(json.loads((ROOT/"data/presets.json").read_text(encoding="utf-8")))
    if not chosen:
        raise RuntimeError("PI preset missing")
    params = StrategyParams(**{k:v for k,v in chosen.items() if k in {f.name for f in fields(StrategyParams)}})
    days = _load_days(market_data.derived_path("orderflow", "mnq"))
    dates = sorted(d["date"] for d in days)
    start = datetime.fromisoformat(dates[0]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(dates[-1]).replace(tzinfo=UTC)+timedelta(days=1)
    snapshot = candle_store.load_snapshot("MNQ")
    candles = candle_store.select_range(snapshot, start-timedelta(days=7), end)
    del snapshot
    candle_store.invalidate_cache("MNQ")
    print(f"Coverage {len(days)} dates; {len(candles)} execution candles", flush=True)
    # Canonical observed RTH session dates prevent treating a missing weekday
    # as yesterday. A prior footprint must have all 390 consecutive RTH bars.
    sessions = sorted({c.timestamp.astimezone(NY).date().isoformat() for c in candles
                       if 570 <= c.timestamp.astimezone(NY).hour*60+c.timestamp.astimezone(NY).minute < 960})
    by_date = {d["date"]:d for d in days}
    profiles, profile_audit = {}, {}
    for date in dates:
        previous = max((d for d in sessions if d < date), default=None)
        prior = by_date.get(previous)
        bars = sorted(prior["bars"], key=lambda b:int(b["epoch"])) if prior else []
        complete = len(bars) == 390 and all(int(b["epoch"])-int(a["epoch"]) == 60 for a,b in zip(bars,bars[1:]))
        profiles[date] = _profile(bars) if complete else None
        profile_audit[date] = {"previous_session": previous, "complete_prior_profile": complete}
    cutoff = max(d for i,d in enumerate(dates) if segment_index(i,len(dates)) < 2)
    baseline = replay(params, candles)
    if not baseline or baseline != replay(params, candles):
        raise RuntimeError("PI baseline not reproducible")
    old_path = market_data.derived_path("research", "orderflow_event_engine_study.json")
    previous_check = "no comparable archived baseline"
    if old_path.exists():
        old = json.loads(old_path.read_text(encoding="utf-8"))
        old_settings = {k:v for k,v in old.get("preset",{}).items() if k != "contract_id"}
        current_settings = {k:v for k,v in chosen.items() if k != "contract_id"}
        if old.get("dates") == dates and old_settings == current_settings:
            if baseline != old["baseline_trades"]:
                raise RuntimeError("Saved PI baseline differs: investigate before comparing candidates")
            previous_check = "exact archived trade-for-trade match"
    print(f"Baseline verified: {previous_check}", flush=True)
    raw_baseline = baseline
    candles,rejected = minute_grid(candles)
    if rejected:
        print(f"Exclude {len(rejected)} off-minute-grid records from research copy",flush=True)
        baseline = replay(params,candles)
        if baseline != replay(params,candles):
            raise RuntimeError("Filtered baseline is not reproducible")
    signals, diagnostics = generate(days, profiles)
    epochs = [int(c.timestamp.timestamp()) for c in candles]
    results, memo = {}, {}
    for index,(name,events) in enumerate(signals.items(),1):
        mapped, conflicts = map_events(events, epochs)
        fingerprint = tuple(sorted(mapped))
        if fingerprint not in memo:
            memo[fingerprint] = replay(params,candles,mapped) if mapped else []
        trades = memo[fingerprint]
        results[name] = {"events":len(events), "mapped_events":len(mapped), "conflicts":conflicts,
                         "metrics":summarize(trades,dates,cutoff), "trades":trades,
                         "signal_hash":hashlib.sha256(repr(fingerprint).encode()).hexdigest()}
        (target/"progress.json").write_text(json.dumps({"completed":index,"total":len(signals),"latest":name}),encoding="utf-8")
        print(f"{index}/{len(signals)} {name}: {results[name]['metrics']['all/both']}",flush=True)
    # Select exclusively on earlier net profit, minimum 10 early trades;
    # later results are revealed only after this fixed ranking.
    eligible = [k for k,v in results.items() if v["metrics"]["early/both"]["trades"] >= 10]
    selected = max(eligible,key=lambda k:results[k]["metrics"]["early/both"]["net_pnl"],default=None)
    loc = Counter((r["rule"],r["location"]) for r in diagnostics)
    report = {"dates":dates,"cutoff":cutoff,"preset":chosen,"baseline_check":previous_check,
        "baseline":summarize(baseline,dates,cutoff),"baseline_trades":baseline,
        "raw_baseline_trades":raw_baseline,"excluded_off_grid_candles":rejected,
        "cleaning_changed_pi_trades":baseline != raw_baseline,
        "profile_audit":profile_audit,"selected_on_early_net":selected,"results":results,
        "location_event_counts":{f"{k[0]}/{k[1]}":v for k,v in loc.items()},
        "limitations":["Retrospective dates, no untouched holdout; 64 fixed comparisons incur selection bias.",
        "Price-cell delta is net aggressive flow, not trader identity or proof of absorption.",
        "Candidates are available at minute end and use the next stamped candle's production close execution, not next open.",
        "Production same-entry-candle stop heuristic, costs and single-position gates inherited; no extra slippage.",
        "MaxDD is closed-trade drawdown, not intratrade equity drawdown.",
        "Off-minute-grid records are excluded from the research copy; canonical store remains untouched.",
        "Directional metrics are contributions to a shared one-position portfolio, not independent long/short replays.",
        "Incomplete/missing prior RTH profiles, including shortened sessions, disable VA rules.",
        "Whole/raw includes all coverage; outside and gated models require valid prior profiles. Compare common-profile subset too."]}
    valid_dates = {d for d,p in profiles.items() if p}
    report["common_profile_baseline"] = summarize(baseline,valid_dates,cutoff)
    for result in results.values():
        result["common_profile_metrics"] = summarize(result["trades"],valid_dates,cutoff)
    (target/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    with gzip.open(target/"events.jsonl.gz","wt",encoding="utf-8") as out:
        for row in diagnostics:
            out.write(json.dumps(row)+"\n")
    lines = ["# Delta changes × prior RTH value — production PI exit replay", "",
        f"{dates[0]}–{dates[-1]}; {len(dates)} dates; early cutoff {cutoff}; {params.contract_size} MNQ.",
        f"Baseline: {previous_check}. Early-selected rule: {selected}.","",
        "| Rule | N | Net USD | PF | Closed DD | Later N | Later net | Later PF |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for key,metrics in [("PI",report["baseline"])]+[(k,v["metrics"]) for k,v in results.items()]:
        a,b = metrics["all/both"],metrics["later/both"]
        lines.append(f"| {key} | {a['trades']} | {a['net_pnl']} | {a['pf']} | {a['max_drawdown_abs']} | {b['trades']} | {b['net_pnl']} | {b['pf']} |")
    lines += ["", "## Limits", ""]+["- "+x for x in report["limitations"]]
    (target/"report.md").write_text("\n".join(lines),encoding="utf-8")
    analyze_saved(target)
    print(f"REPORT {target/'report.md'}",flush=True)


def analyze_saved(target):
    """Audit completed trades against entry-time regions; no new model selection."""
    report = json.loads((target/"report.json").read_text(encoding="utf-8"))
    lookup = {}
    with gzip.open(target/"events.jsonl.gz","rt",encoding="utf-8") as stream:
        for line in stream:
            r = json.loads(line)
            lookup[r["rule"],r["available"],r["direction"]] = r["location"]
    dates, cutoff = report["dates"],report["cutoff"]
    for name,result in report["results"].items():
        buckets = {location:[] for location in ("below","inside","above","unknown","unmatched")}
        for trade in result["trades"]:
            region = lookup.get((name,trade["entry_epoch"],trade["direction"]),"unmatched")
            buckets[region].append(trade)
        result["entry_region_metrics"] = {k:summarize(v,dates,cutoff) for k,v in buckets.items()}
        if buckets["unmatched"]:
            raise RuntimeError(f"Entry diagnostic join failed: {name}")
    old_path = market_data.derived_path("research","orderflow_event_engine_study.json")
    if old_path.exists():
        old = json.loads(old_path.read_text(encoding="utf-8"))
        report["archive_trade_fingerprint_matches"] = report["baseline_trades"] == old["baseline_trades"]
    report["distinct_signal_streams"] = len({v["signal_hash"] for v in report["results"].values()})
    selected = report["selected_on_early_net"]
    best = report["results"][selected]
    all_base = report["baseline"]["all/both"]
    def dominates(m,b):
        return isinstance(m["pf"],(int,float)) and isinstance(b["pf"],(int,float)) and m["pf"]>b["pf"] and m["net_pnl"]>b["net_pnl"] and m["max_drawdown_abs"]<=b["max_drawdown_abs"]
    report["dominating_pi_all"] = [k for k,v in report["results"].items() if dominates(v["metrics"]["all/both"],all_base)]
    report["dominating_pi_common_profile"] = [k for k,v in report["results"].items()
        if dominates(v["common_profile_metrics"]["all/both"],report["common_profile_baseline"]["all/both"])]
    lines = ["# Delta change study — interpretation", "",
        f"{len(report['results'])} fixed rules, {report['distinct_signal_streams']} distinct event streams.",
        f"Early selection (net PnL, N>=10): `{selected}`; cutoff {cutoff}.",
        f"Exact archived PI trade fingerprint: {report.get('archive_trade_fingerprint_matches')}.",
        "", "## PI and early-selected candidate", "",
        "| Model / split | N | Net USD | PF | Win rate | Closed DD |",
        "|---|---:|---:|---:|---:|---:|"]
    for name,metrics in (("PI",report["baseline"]),(selected,best["metrics"])):
        for split in ("all/both","all/long","all/short","early/both","later/both","later/long","later/short"):
            m=metrics[split]
            lines.append(f"| {name} {split} | {m['trades']} | {m['net_pnl']} | {m['pf']} | {m['win_rate']} | {m['max_drawdown_abs']} |")
    lines += ["", "## Controlled comparisons (common valid prior-profile dates)","",
        "| Rule | N | Net USD | PF | Closed DD |", "|---|---:|---:|---:|---:|"]
    for k in ["PI"]+[f"absorption/w5/{s}/{g}" for s in ("whole","outside") for g in GATES]:
        m=report["common_profile_baseline"]["all/both"] if k=="PI" else report["results"][k]["common_profile_metrics"]["all/both"]
        lines.append(f"| {k} | {m['trades']} | {m['net_pnl']} | {m['pf']} | {m['max_drawdown_abs']} |")
    lines += ["", "## Entry location contributions", "",
        "These are conditional contributions, not independent regional strategies or a matched causal effect.","",
        "| Rule / region | N | Net USD | PF |", "|---|---:|---:|---:|"]
    for k in ("absorption/w5/whole/raw",selected):
        for region,metrics in report["results"][k]["entry_region_metrics"].items():
            m=metrics["all/both"]
            lines.append(f"| {k} {region} | {m['trades']} | {m['net_pnl']} | {m['pf']} |")
    lines += ["",f"Rules exceeding PI net and PF with no worse closed DD: {report['dominating_pi_all']}",
        f"Same comparison on valid prior-profile dates: {report['dominating_pi_common_profile']}","",
        "## Rule definition", "",
        "Long candidate: average negative-delta magnitude in the latest 5 completed bars is at least the preceding 30-bar median absolute delta and at least 70% of its previous 5-bar average. Latest 5-bar low is no more than one tick below previous 5-bar low. At least one of the last 10 bars touches or crosses yesterday's VAL. Short mirrors positive delta and VAH. This location rule does not require reclaim or VWAP alignment.","",
        "Outside source first nets buys minus sells within the relevant price region, then splits the resulting regional delta into positive and negative magnitudes. It does not identify institutions.","",
        "## Limits",""]+["- "+x for x in report["limitations"]]
    (target/"analysis.md").write_text("\n".join(lines),encoding="utf-8")
    (target/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")


if __name__ == "__main__":
    main()
