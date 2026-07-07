"""Run the latest local sweep without touching the live server.

Writes:
- data/sweep_results.json
- data/sweep_runs/sweep_<timestamp>.json
- data/sweep_history.jsonl
- data/presets.json latest "SWEEP <MODEL> #..." presets
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api.routes import (
    BacktestRequest,
    _build_strategy_params_from_request,
    _normalize_contract_size,
    _sync_latest_sweep_presets,
)
from backend.backtest.sweep import run_factor_sweep, run_model_sweep
from backend.data import candle_store
from backend.db.models import current_quarterly_contract_id


def _qualified(results: list[dict]) -> dict[str, list[dict]]:
    out = {"TREND": [], "DAY ZONE": [], "DISTRIBUTION": [], "FACTOR": []}
    for row in results:
        if row.get("accept"):
            out.setdefault(str(row.get("model") or "TREND").upper(), []).append(row)
    return out


def _write_outputs(payload: dict) -> None:
    out_file = Path("data") / "sweep_results.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    runs_dir = Path("data") / "sweep_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"sweep_{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )

    results = list(payload.get("results") or [])
    qualified = payload.get("qualified_by_model") or {}
    top = results[0] if results else {}
    hist = {
        "created_at": payload["created_at"],
        "stamp": stamp,
        "candles": payload["candles"],
        "range": payload["range"],
        "variants": len(results),
        "accepted": sum(len(v) for v in qualified.values()),
        "by_model": {m: len(v) for m, v in qualified.items()},
        "top": {
            "model": top.get("model"),
            "label": top.get("label"),
            "pf": top.get("pf"),
            "pnl": top.get("pnl"),
            "max_dd": top.get("max_dd"),
            "trades": top.get("trades"),
        },
    }
    with (Path("data") / "sweep_history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(hist, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="MNQ")
    parser.add_argument("--contract-id", default="")
    parser.add_argument("--contract-size", type=int, default=1)
    parser.add_argument("--factor-only", action="store_true")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    contract_id = args.contract_id or current_quarterly_contract_id(symbol)
    contract_size = _normalize_contract_size(contract_id, args.contract_size)
    req = BacktestRequest(contract_id=contract_id, contract_size=contract_size)
    base = _build_strategy_params_from_request(req, contract_size)
    candles = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit(f"No {symbol} 1m candles in local candle_store.")

    print(
        f"SWEEP_START symbol={symbol} contract={contract_id} size={contract_size} "
        f"candles={len(candles)} range={candles[0].timestamp.isoformat()}->{candles[-1].timestamp.isoformat()}",
        flush=True,
    )

    def _progress(cur: int, total: int, detail: str) -> None:
        print(f"SWEEP_PROGRESS {cur}/{total} {detail}", flush=True)

    if args.factor_only:
        results = run_factor_sweep(candles, base, _progress)
    else:
        results = run_model_sweep(candles, base, _progress)
    results.sort(key=lambda r: -float(r.get("score", 0.0) or 0.0))
    qualified = _qualified(results)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candles": len(candles),
        "range": [candles[0].timestamp.isoformat(), candles[-1].timestamp.isoformat()],
        "results": results,
        "qualified_by_model": qualified,
    }
    _write_outputs(payload)
    payload["latest_sweep_presets"] = _sync_latest_sweep_presets(payload, req, contract_size)
    _write_outputs(payload)

    print(f"SWEEP_DONE variants={len(results)} accepted={sum(len(v) for v in qualified.values())}", flush=True)
    for row in results[:12]:
        print(
            "SWEEP_TOP "
            f"model={row.get('model')} label={row.get('label')} trades={row.get('trades')} "
            f"pf={row.get('pf')} pnl={row.get('pnl')} dd={row.get('max_dd')} "
            f"wf={row.get('wf_pass')} plateau={row.get('plateau_pass')} accept={row.get('accept')}",
            flush=True,
        )
    print("SWEEP_PRESETS " + json.dumps(payload["latest_sweep_presets"], ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
