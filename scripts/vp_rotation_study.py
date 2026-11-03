"""VP rotation: fade the edge, take profit at the opposite edge.

The user's idea (2026-09-27): once the first penetration of a value edge has
failed, treat the prior value area as a range -- short VAH, long VAL -- and
take profit at the opposite edge.  The production VolumeProfileStrategy
already expresses this:

  entry_mode="range"         fade a touch of VAH/VAL that closes back inside
  entry_mode="failed_break"  fade after price broke the edge and reclaimed it
  target_mode="opposite_edge" long -> TP at VAH, short -> TP at VAL

The 2026-09-15 five-year run tested these entries with POC and ATR targets
(range/POC PF 0.77, failed_break/POC PF 0.83, every year below 1) but never
with the opposite-edge target.  This script fills that gap and nothing else.

Pre-declared (before any result):
  * step 0 reproduces the frozen breakout/ATR reference ledger (trade count and
    net PnL); if it does not match, the comparison is not reported as valid;
  * variants: range+opposite_edge and failed_break+opposite_edge, stop 1.5 x
    ATR blend, everything else as the reference;
  * PASS only if, for BOTH MNQ and MES and each of 2022-23 / 2024-25 / 2026:
    n >= 20 and PF > 1 after costs; stress PF > 1 in 2024-25 and 2026
    (MNQ 14 ticks = $7, MES 4 ticks = $5); walk-forward passes.

Runs the production BacktestEngine: about 70 s per symbol per variant and
~1.3 GB RAM.  Run it yourself:

    python scripts/vp_rotation_study.py
"""
from __future__ import annotations

import gzip
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import volume_profile_research as vpr  # noqa: E402  (reuse the audited runner)
from backend.backtest.robustness import series_stats, tick_value  # noqa: E402

RESEARCH = Path(r"F:\ancserQuant\ancserMarketData\derived\research")
REFERENCE = RESEARCH / "vp_baseline_python_reference_2022_2026_20260925T0645Z"
REFERENCE_LEDGERS = {
    "MNQ": (REFERENCE / "mnq_vp_baseline_trades_2022-01-01_2026-09-23.csv.gz", date(2026, 9, 23)),
    "MES": (REFERENCE / "mes_vp_baseline_trades_2022-01-01_2026-09-14.csv.gz", date(2026, 9, 14)),
}
START = date(2022, 1, 1)
STRESS_TICKS = {"MNQ": 14, "MES": 4}
PERIODS = (("2022-23", 2022, 2023), ("2024-25", 2024, 2025), ("2026", 2026, 2026))
MIN_N = 20

BASE = {
    "side_mode": "all", "sl_atr": 1.5, "tp_atr": 2.0, "confirm_bars": 2,
    "breakout_buffer_ticks": 2, "touch_tolerance_ticks": 2,
    "reclaim_buffer_ticks": 1, "max_trades_per_day": 2,
    "min_source_candles": 60, "value_area_pct": 0.70,
}
REFERENCE_VARIANT = {**BASE, "entry_mode": "breakout", "target_mode": "atr"}
VARIANTS = {
    "range_to_opposite_edge": {**BASE, "entry_mode": "range", "target_mode": "opposite_edge"},
    "failed_break_to_opposite_edge": {**BASE, "entry_mode": "failed_break", "target_mode": "opposite_edge"},
}


def _reference_totals(symbol: str) -> tuple[int, float]:
    path, _end = REFERENCE_LEDGERS[symbol]
    count, net = 0, 0.0
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        header = handle.readline().rstrip("\n").split(",")
        column = header.index("net_pnl")
        for line in handle:
            count += 1
            net += float(line.rstrip("\n").split(",")[column])
    return count, round(net, 2)


def _cells(rows: list[dict], symbol: str) -> dict:
    charge = STRESS_TICKS[symbol] * tick_value(symbol)
    out = {}
    for label, first, last in PERIODS:
        pnls = [r["pnl"] for r in rows if first <= int(r["year"]) <= last]
        base = series_stats(pnls)
        stress = series_stats([p - charge for p in pnls])
        out[label] = {"n": base["n"], "pnl": round(base["pnl"], 2),
                      "pf": round(base["pf"], 4), "stress_pf": round(stress["pf"], 4)}
    return out


def _verdict(cells: dict, walk_forward_pass: bool) -> list[str]:
    reasons = []
    for label, _first, _last in PERIODS:
        cell = cells[label]
        if cell["n"] < MIN_N:
            reasons.append(f"{label}: n {cell['n']} < {MIN_N}")
        if cell["pf"] <= 1.0:
            reasons.append(f"{label}: PF {cell['pf']:.2f} <= 1")
        if label != "2022-23" and cell["stress_pf"] <= 1.0:
            reasons.append(f"{label}: stress PF {cell['stress_pf']:.2f} <= 1")
    if not walk_forward_pass:
        reasons.append("walk-forward failed")
    return reasons


def run() -> dict:
    report = {"generated_utc": datetime.now(timezone.utc).isoformat(), "symbols": {}}
    for symbol in ("MNQ", "MES"):
        _path, ref_end = REFERENCE_LEDGERS[symbol]
        end = date.today()
        candles, regimes = vpr._load_window(symbol, START, end)
        sym = {}
        ref = vpr._run_candidate(REFERENCE_VARIANT, candles, regimes, symbol, START, ref_end)
        expected = _reference_totals(symbol)
        got = (ref["baseline"]["baseline"]["n"], round(ref["baseline"]["baseline"]["pnl"], 2))
        sym["parity"] = {"expected": expected, "got": got, "match": got == expected}
        print(f"{symbol} parity: expected {expected} got {got}", flush=True)
        for name, values in VARIANTS.items():
            result = vpr._run_candidate(values, candles, regimes, symbol, START, end)
            rows = result["trade_rows"]
            cells = _cells(rows, symbol)
            wf = result["baseline"]["walk_forward"]
            sym[name] = {
                "all": result["baseline"]["baseline"],
                "periods": cells,
                "walk_forward": wf,
                "by_edge": {k: {"n": v.get("n"), "pf": v.get("pf")} for k, v in result["by_edge"].items()},
                "reasons": _verdict(cells, wf["pass"]),
            }
            print(f"{symbol} {name}: {json.dumps(cells)} WF {wf['pass']}", flush=True)
        report["symbols"][symbol] = sym
    report["verdicts"] = {
        name: ("PASS" if all(not report["symbols"][s][name]["reasons"] for s in ("MNQ", "MES"))
               and all(report["symbols"][s]["parity"]["match"] for s in ("MNQ", "MES"))
               else "REJECT")
        for name in VARIANTS
    }
    return report


def main() -> None:
    report = run()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    out = RESEARCH / f"vp_rotation_study_{stamp}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(report["verdicts"], indent=2))
    print(f"written: {out}")


if __name__ == "__main__":
    main()
