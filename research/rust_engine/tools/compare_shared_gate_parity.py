from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.backtest.robustness import monte_carlo, monte_carlo_passes, walk_forward

STRATEGIES = (
    "price_only_60_session_tsm",
    "60_session_tsm_with_lagged_tff_alignment",
    "price_only_paired_on_candidate_intervals",
    "vol20_scaled_price_tsm",
    "vol20_scaled_passive_long_paired_on_price_tsm",
    "vol20_scaled_tff_tsm",
    "vol20_scaled_passive_long_paired_on_tff_tsm",
)
NET_SEED = 20_261_010
STRESS_SEED = NET_SEED + 1
ITERATIONS = 1_000
DD_THRESHOLD = 2_000.0


def close(actual: float | None, expected: float | None) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-8)


def read_rows(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_manifest", type=Path)
    args = parser.parse_args()
    manifest_path = args.run_manifest.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output_root = Path(manifest["output_directory"])
    audit_rows = []
    for symbol_report in manifest["symbol_reports"]:
        symbol = symbol_report["symbol"]
        for strategy in STRATEGIES:
            rows = read_rows(output_root / f"trades_{symbol}_{strategy}.csv.gz")
            rust = symbol_report["strategy_results"][strategy]
            for pnl_field, rust_field, seed in (
                ("net_pnl", "net_shared_monte_carlo", NET_SEED),
                ("stress_14t_pnl", "stress_14t_shared_monte_carlo", STRESS_SEED),
            ):
                pnls = [float(row[pnl_field]) for row in rows]
                reference = monte_carlo(pnls, iters=ITERATIONS, seed=seed, dd_threshold=DD_THRESHOLD)
                actual = rust[rust_field]
                fields = (
                    "iters", "seed", "n", "pnl_p5", "pnl_p25", "pnl_p50", "pnl_p75", "pnl_p95",
                    "p_loss", "dd_p5", "dd_p25", "dd_p50", "dd_p75", "dd_p95", "p_dd_breach",
                    "dd_threshold", "pf_p5",
                )
                mismatch = [name for name in fields if not close(actual.get(name), reference.get(name))]
                gate = monte_carlo_passes(reference)
                if actual.get("shared_gate_pass") != gate:
                    mismatch.append("shared_gate_pass")
                audit_rows.append({
                    "symbol": symbol,
                    "strategy": strategy,
                    "gate": rust_field,
                    "exact": not mismatch,
                    "mismatches": mismatch,
                    "shared_gate_pass": gate,
                    "pnl_p5": reference["pnl_p5"],
                    "p_loss": reference["p_loss"],
                    "dd_p95": reference["dd_p95"],
                    "pf_p5": reference["pf_p5"],
                })
            for pnl_field, rust_field in (
                ("net_pnl", "net_walk_forward_shared"),
                ("stress_14t_pnl", "stress_14t_walk_forward_shared"),
            ):
                points = [{"entry_time": row["entry_time"], "pnl": float(row[pnl_field])} for row in rows]
                reference = walk_forward(points)
                actual = rust[rust_field]
                if reference is None:
                    exact = actual is None
                    mismatch = [] if exact else ["missing_or_unexpected_result"]
                else:
                    mismatch = []
                    if actual is None or actual.get("pass") != reference["pass"]:
                        mismatch.append("pass")
                    if actual is None or len(actual.get("segments", [])) != len(reference["segments"]):
                        mismatch.append("segments")
                    else:
                        for index, expected in enumerate(reference["segments"]):
                            for field in ("n", "pnl", "pf", "max_dd", "win"):
                                if not close(actual["segments"][index].get(field), expected[field]):
                                    mismatch.append(f"segment_{index}_{field}")
                    exact = not mismatch
                audit_rows.append({
                    "symbol": symbol,
                    "strategy": strategy,
                    "gate": rust_field,
                    "exact": exact,
                    "mismatches": mismatch,
                    "shared_gate_pass": reference["pass"] if reference else False,
                })
    audit = {
        "schema": "ancsertpx.shared-gate-python-parity.v1",
        "run_id": manifest["run_id"],
        "python_reference": "backend.backtest.robustness.monte_carlo / walk_forward",
        "checked_gate_rows": len(audit_rows),
        "exact_gate_rows": sum(bool(row["exact"]) for row in audit_rows),
        "all_exact": all(row["exact"] for row in audit_rows),
        "gate_results": audit_rows,
    }
    output_path = output_root / "shared_gate_parity_audit.json"
    output_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps({
        "audit_path": str(output_path),
        "all_exact": audit["all_exact"],
        "exact_gate_rows": audit["exact_gate_rows"],
        "checked_gate_rows": audit["checked_gate_rows"],
        "mismatches": [row for row in audit_rows if not row["exact"]],
    }, indent=2))


if __name__ == "__main__":
    main()
