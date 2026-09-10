"""Offline study of pre-PI MBO filters applied to the existing PI entry.

The MBO cache covers only a subset of the PI history.  Results are therefore
reported against the MBO-covered baseline for each signal set; they are not a
full-period backtest and are not wired into live trading.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.data import market_data  # noqa: E402
from pi_exit_study import POINT_VALUE, RT_COST, simulate  # noqa: E402
from pi_signal_set_study import _metrics, _trade_rows  # noqa: E402


CURRENT_KINDS = {"青π", "深蓝圈", "粉π"}
CURRENT_EXIT = {
    "long": ("sltp", 3.5, 3.0, 0),
    "short": ("sl_time", 2.5, 0.0, 60),
}


def _load_diagnostics() -> dict[str, dict]:
    path = market_data.derived_path("research", "orderflow_pi_level_pre_signal.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["message_id"]: row for row in payload["pi_diagnostics"]}


def _covered_rows(diagnostics: dict[str, dict]) -> list[dict]:
    rows = []
    for row in _trade_rows():
        diag = diagnostics.get(row["message_id"])
        if diag is None:
            continue
        features = diag.get("precursor_features") or {}
        side = "long" if row["direction"] > 0 else "short"
        mode, sl, rr, hold = CURRENT_EXIT[side]
        points, why = simulate(
            row["index"], row["direction"], row["bars"], row["times"],
            row["width"], mode, sl, rr, hold,
        )
        row = {
            **row,
            "features": features,
            "level": row["size"],
            "side": side,
            "usd": points * POINT_VALUE[row["future"]] - RT_COST[row["future"]],
            "exit_reason": why,
        }
        rows.append(row)
    return rows


FILTERS = {
    "baseline": lambda f: True,
    "opposing_attack_ge_2": lambda f: f.get("opposing_attack_delta", 0) >= 2,
    "opposing_attack_ge_3": lambda f: f.get("opposing_attack_delta", 0) >= 3,
    "volume_burst_ge_1.5": lambda f: f.get("volume_burst", 0) >= 1.5,
    "volume_burst_ge_2": lambda f: f.get("volume_burst", 0) >= 2,
    "opposing_large_trade_ge_50": lambda f: f.get("opposing_large_trade", 0) >= 50,
    "passive_refill_ge_1.2": lambda f: f.get("passive_defense_refill", 0) >= 1.2,
    "attack2_and_volume1.5": lambda f: (
        f.get("opposing_attack_delta", 0) >= 2 and f.get("volume_burst", 0) >= 1.5
    ),
    "attack2_volume1.5_refill1.2": lambda f: (
        f.get("opposing_attack_delta", 0) >= 2
        and f.get("volume_burst", 0) >= 1.5
        and f.get("passive_defense_refill", 0) >= 1.2
    ),
}


def run() -> dict:
    diagnostics = _load_diagnostics()
    rows = _covered_rows(diagnostics)
    sets = {
        "current_pi_only": [row for row in rows if row["kind"] in CURRENT_KINDS],
        "all_supported": rows,
    }
    result = {}
    for set_name, group in sets.items():
        result[set_name] = {}
        for filter_name, predicate in FILTERS.items():
            selected = [row for row in group if predicate(row["features"])]
            result[set_name][filter_name] = {
                "metrics": _metrics([row["usd"] for row in selected]),
                "retained_fraction": round(len(selected) / len(group), 3) if group else 0,
                "sides": dict(Counter(row["side"] for row in selected)),
                "levels": dict(Counter(row["level"] for row in selected)),
                "exit_reasons": dict(Counter(row["exit_reason"] for row in selected)),
            }
    by_level = {}
    for level in ("Level 1", "Level 2", "Level 3"):
        group = [row for row in sets["all_supported"] if row["level"] == level]
        by_level[level] = {
            "metrics": _metrics([row["usd"] for row in group]),
            "n": len(group),
            "sides": dict(Counter(row["side"] for row in group)),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "features": "strictly the five complete MBO minutes before PI timestamp",
            "exit": "current PI exit: long SL3.5×blend+TP3R; short SL2.5×blend+60m",
            "warning": "only MBO-covered MNQ events; thresholds are exploratory, not live rules",
        },
        "coverage": {"mbo_pi_events": len(rows), "current_pi_only": len(sets["current_pi_only"])},
        "by_level": by_level,
        "filters": result,
    }


def _markdown(result: dict) -> str:
    lines = [
        "# PI × pre-signal MBO filter study",
        "",
        "Exploratory only; no filter is connected to live execution.",
        "",
        f"- MBO-covered PI events: `{result['coverage']['mbo_pi_events']}`",
        f"- MBO-covered current PI-only events: `{result['coverage']['current_pi_only']}`",
        "",
        "## Baseline by Level",
        "",
        "| Level | n | PnL | PF | Win | Max DD |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for level, value in result["by_level"].items():
        m = value["metrics"]
        lines.append(
            f"| {level} | {m['n']} | ${m['pnl']:,.0f} | {m['pf'] if m['pf'] is not None else '—'} | "
            f"{m['win_rate']:.1%} | ${m['max_dd']:,.0f} |"
        )
    for set_name, filters in result["filters"].items():
        lines.extend(["", f"## {set_name}", "", "| Filter | n | Retained | PnL | PF | Win | Max DD |", "|---|---:|---:|---:|---:|---:|---:|"])
        for name, value in filters.items():
            m = value["metrics"]
            lines.append(
                f"| {name} | {m['n']} | {value['retained_fraction']:.1%} | ${m['pnl']:,.0f} | "
                f"{m['pf'] if m['pf'] is not None else '—'} | {m['win_rate']:.1%} | ${m['max_dd']:,.0f} |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    result = run()
    output_dir = market_data.derived_path("research")
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "pi_orderflow_filter_study"
    (output_dir / f"{stem}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / f"{stem}.md").write_text(_markdown(result), encoding="utf-8")
    print(output_dir / f"{stem}.md")
    print(json.dumps(result["coverage"], ensure_ascii=False))
    for set_name, filters in result["filters"].items():
        print(set_name, json.dumps({name: value["metrics"] for name, value in filters.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
