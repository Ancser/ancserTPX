"""Run the trend sweep outside the frontend.

Writes the same payload shape consumed by the SWEEP results tab:
  data/sweep_results.json

Run:
  python scripts/run_trend_sweep_backend.py
"""
from __future__ import annotations

import dataclasses
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.sweep import run_trend_sweep
from backend.data import candle_store
from backend.db.models import StrategyParams


PRESET_FILE = ROOT / "data" / "presets.json"
OUT_FILE = ROOT / "data" / "sweep_results.json"
LOG_FILE = ROOT / "data" / "machinelearning" / "trend_sweep_backend.log"


def load_base_params() -> tuple[StrategyParams, str]:
    raw = {}
    name = "StrategyParams default"
    if PRESET_FILE.exists():
        data = json.load(open(PRESET_FILE, encoding="utf-8"))
        presets = data.get("presets") or {}
        wanted = data.get("last_used_bt") or data.get("last_used_live")
        if wanted in presets:
            name = wanted
            raw = dict(presets[wanted])
        elif presets:
            name, raw = next(iter(presets.items()))
            raw = dict(raw)

    fields = {f.name for f in dataclasses.fields(StrategyParams)}
    return StrategyParams(**{k: v for k, v in raw.items() if k in fields}), name


def main() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    params, preset_name = load_base_params()
    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit("No MNQ 1m candles in local store")

    log(f"START preset={preset_name}")
    log(f"CANDLES {len(candles)} {candles[0].timestamp.isoformat()} -> {candles[-1].timestamp.isoformat()}")

    def progress(done: int, total: int, detail: str) -> None:
        log(f"PROGRESS {done}/{total} {detail}")

    results = run_trend_sweep(candles, params, progress)
    results.sort(key=lambda r: -r.get("score", 0.0))
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candles": len(candles),
        "range": [candles[0].timestamp.isoformat(), candles[-1].timestamp.isoformat()],
        "base_preset": preset_name,
        "results": results,
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    log(f"DONE results={len(results)} out={OUT_FILE}")
    if results:
        best = results[0]
        log(
            "BEST "
            f"{best.get('label')} pnl={best.get('pnl')} maxDD={best.get('max_dd')} "
            f"trades={best.get('trades')} pf={best.get('pf')} score={best.get('score')}"
        )


if __name__ == "__main__":
    main()
