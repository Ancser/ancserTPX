"""Research-only long-term comparison of every production FACTOR family.

The main long-term audit uses the named BEST preset, whose factor family is
EMAPMO.  This companion deliberately holds that preset's side, risk, session,
and exit settings fixed and changes only ``factor_signal_family`` across every
family accepted by ``FactorSignalStrategy``.  It exists to close the gap
between "FACTOR" as an engine mode and the three actual signal families:
EMAPMO, momentum-reversion, and icefishball.

No production code, presets, candles, or live state are changed.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import candle_store, market_data
from backend.db.models import current_quarterly_contract_id
from backend.timebase import as_utc
from scripts.strategy_long_term_audit import (
    _make_params,
    audit_model,
    build_rth_regimes,
    model_specs,
)


FAMILIES = ("emapmo", "momentum_reversion", "icefishball")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    return str(value)


def _run_symbol(symbol: str, presets: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    candles = sorted(candle_store.load(symbol, 1), key=lambda item: as_utc(item.timestamp))
    if not candles:
        raise SystemExit(f"no {symbol} candles in canonical store")
    regimes = build_rth_regimes(candles)
    base = next(spec for spec in model_specs() if spec["key"] == "factor")
    rows: list[dict[str, Any]] = []
    for family in FAMILIES:
        if family == "emapmo":
            # The named FACTOR/BEST long-term audit already ran this exact
            # configuration.  Reuse its serialized result instead of paying
            # for a second full-history replay.
            audit_path = (
                Path(r"F:\ancserQuant\ancserMarketData\derived\research")
                / f"all_model_long_term_audit_{symbol.lower()}_20260914.json"
            )
            payload = json.loads(audit_path.read_text(encoding="utf-8"))
            result = next(item for item in payload["results"] if item.get("key") == "factor")
            result = copy.deepcopy(result)
            result["symbol"] = symbol
            rows.append(result)
            base_stats = result["baseline"]["baseline"]
            stress = result["baseline"]["stress_14t"]
            print(
                f"{symbol} {family}: reused current audit; n={base_stats['n']} "
                f"PnL=${base_stats['pnl']:,.2f} PF={base_stats['pf']:.4f} "
                f"14tPF={stress['pf']:.4f} status={result['status']}",
                flush=True,
            )
            continue
        spec = copy.deepcopy(base)
        spec["key"] = f"factor_{family}"
        spec["label"] = f"FACTOR · {family}"
        spec["config_note"] = (
            f"BEST preset with factor_signal_family={family}; all other settings fixed"
        )
        # This study compares FACTOR entry families only.  The generic PI
        # directional exit overlay is a separate experiment and would double
        # the six full-history engine passes without changing the entry-family
        # question.
        spec["pi_overlay"] = False
        spec["overrides"] = dict(spec.get("overrides") or {})
        spec["overrides"]["factor_signal_family"] = family
        params = _make_params(spec, symbol, presets)
        result = audit_model(spec, params, candles, regimes, symbol)
        result["symbol"] = symbol
        rows.append(result)
        base_stats = result["baseline"]["baseline"]
        stress = result["baseline"]["stress_14t"]
        print(
            f"{symbol} {family}: n={base_stats['n']} PnL=${base_stats['pnl']:,.2f} "
            f"PF={base_stats['pf']:.4f} 14tPF={stress['pf']:.4f} "
            f"status={result['status']}",
            flush=True,
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="", help="JSON output path")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    market_root = Path(r"F:\ancserQuant\ancserMarketData")
    research_dir = market_root / "derived" / "research"
    out_path = (
        Path(args.out).resolve()
        if args.out
        else research_dir / "factor_family_long_term_audit_20260914.json"
    )
    preset_doc = json.loads(
        market_data.repository_data_path("presets.json").read_text(encoding="utf-8")
    )
    presets = preset_doc.get("presets", {})
    results: list[dict[str, Any]] = []
    for symbol in ("MNQ", "MES"):
        results.extend(_run_symbol(symbol, presets))
    payload = {
        "study_version": "2026-09-14-factor-family-long-term-v1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "families": list(FAMILIES),
        "symbols": ["MNQ", "MES"],
        "holding_constant": "BEST preset settings other than factor_signal_family; one contract; canonical costs; 14-tick stress; 500 MC; 3 equal-time WF segments",
        "production_changed": False,
        "results": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
