"""Build the PI option path cache used by the Backtest page model ``pi_options``.

For every PI long signal of kind 深蓝圈 (LV2) / 青π (LV3), first per symbol
and day (same selection as the research studies), store the next-session
expiry calls at ATM / +$3 / +$5 / +$7 / +$10 and their minute quotes from the
entry minute through the exit session's 16:00, already judged by the Theta
quality rules (backend.options.pi_options.Point).  Reading the saved Theta
files takes a few minutes; afterwards every backtest setting runs in
milliseconds.  Output: derived/research/pi_option_paths_v1.json.gz.

Run:  python scripts/pi_option_path_cache.py
"""
from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime, time, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.options.pi_options import cache_path  # noqa: E402
from scripts.pi_option_limit_entry_study import (  # noqa: E402
    ET, load_signal_paths, load_signals, quote_point)
from scripts.pi_theta_option_pilot import DEFAULT_DATA_ROOT  # noqa: E402

OFFSETS = (3.0, 5.0, 7.0, 10.0)
SESSION_END = time(16, 0)


def build(root: Path = DEFAULT_DATA_ROOT) -> dict:
    signals, skipped = load_signals(root)
    rows = []
    for index, signal in enumerate(signals, 1):
        cohort, entry = signal["cohort"], signal["entry"]
        try:
            spot, strikes, paths = load_signal_paths(root, signal, offsets=OFFSETS)
        except ValueError as exc:
            skipped[f"strike_selection:{exc}"[:60]] = skipped.get(f"strike_selection:{exc}"[:60], 0) + 1
            continue
        until = datetime.combine(cohort["exit_date"], SESSION_END, tzinfo=ET)
        cached_paths = {}
        for label, strike in strikes.items():
            path = paths.get(float(strike), {})
            cached_paths[label] = [
                list(quote_point(path[t], t))
                for t in sorted(path) if entry <= t <= until
            ]
        rows.append({
            "symbol": cohort["symbol"], "date": cohort["event_date"].isoformat(),
            "tier": signal["tier"], "kind": signal["mark"]["kind"],
            "source_timestamp_utc": signal["mark"]["source_timestamp_utc"],
            "entry_epoch": int(entry.timestamp()), "entry_et": entry.isoformat(),
            "exit_date": cohort["exit_date"].isoformat(),
            "expiration": cohort["expiration"].isoformat(),
            "spot": spot, "strikes": {k: float(v) for k, v in strikes.items()},
            "paths": cached_paths,
        })
        print(f"{index}/{len(signals)} {cohort['symbol']} {entry:%Y-%m-%d %H:%M}", flush=True)
    dates = sorted(r["date"] for r in rows)
    return {
        "schema": "ancsertpx.pi-option-paths.v1",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "selection": "PI long 深蓝圈/青π, first per symbol and day, 09:30-16:00 quote window",
        "window": {"start": dates[0] if dates else None, "end": dates[-1] if dates else None},
        "skipped": skipped,
        "signals": rows,
    }


def main() -> None:
    payload = build()
    out = cache_path()
    temp = out.with_name(out.name + ".tmp")
    with gzip.open(temp, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    temp.replace(out)
    print(f"written: {out} ({len(payload['signals'])} signals)")


if __name__ == "__main__":
    main()
