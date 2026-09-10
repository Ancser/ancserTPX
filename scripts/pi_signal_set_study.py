"""Offline PI signal-set and exit-configuration study.

This is a research report generator, not the removed frontend Sweep feature.
It reuses the same PI history, candle store and exit simulator as the existing
PI studies, then compares signal-set definitions without changing presets or
live behaviour.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend.data import market_data  # noqa: E402
from backend.data.pi_history import load_rows  # noqa: E402
from pi_exit_study import (  # noqa: E402
    DIRECTION,
    POINT_VALUE,
    RT_COST,
    SYMBOL_MAP,
    _utc,
    at_or_after,
    build,
    simulate,
)


NY = ZoneInfo("America/New_York")


def _trade_rows() -> list[dict]:
    data = {symbol: build(symbol) for symbol in ("MNQ", "MES")}
    rows: list[dict] = []
    for source in load_rows():
        symbol = str(source.get("symbol") or "").upper()
        if symbol not in SYMBOL_MAP:
            continue
        future = SYMBOL_MAP[symbol]
        try:
            ts = _utc(datetime.fromisoformat(str(source["ts"]).replace("Z", "+00:00")))
        except (KeyError, TypeError, ValueError):
            continue
        times, bars, blend = data[future]
        index = at_or_after(times, ts)
        if index is None or (times[index] - ts).total_seconds() > 10 * 60:
            continue
        if blend[index] is None:
            continue
        for mark in source.get("marks") or []:
            kind = mark.get("kind")
            direction = DIRECTION.get(kind, 0)
            if not direction:
                continue
            rows.append({
                "message_id": str(source.get("id") or ""),
                "future": future,
                "symbol": symbol,
                "ts": ts.isoformat(),
                "ny_date": ts.astimezone(NY).date().isoformat(),
                "kind": kind,
                "size": mark.get("size") or "unknown",
                "pos": mark.get("pos"),
                "direction": direction,
                "index": index,
                "width": blend[index],
                "times": times,
                "bars": bars,
            })
    return rows


def _configs() -> dict[str, dict]:
    """A compact, predeclared grid; no post-hoc best parameter search."""
    long_exits = {
        "L_sltp_3.5x_3R": ("sltp", 3.5, 3.0, 0),
        "L_sltp_5x_2R": ("sltp", 5.0, 2.0, 0),
        "L_time_60m": ("time", 0, 0, 60),
        "L_time_120m": ("time", 0, 0, 120),
        "L_time_240m": ("time", 0, 0, 240),
        "L_sltime_3.5x_120m": ("sl_time", 3.5, 0, 120),
        "L_sltime_5x_240m": ("sl_time", 5.0, 0, 240),
    }
    short_exits = {
        "S_sltime_2.5x_60m": ("sl_time", 2.5, 0, 60),
        "S_sltime_3.5x_60m": ("sl_time", 3.5, 0, 60),
        "S_sltime_5x_120m": ("sl_time", 5.0, 0, 120),
        "S_time_60m": ("time", 0, 0, 60),
        "S_time_120m": ("time", 0, 0, 120),
        "S_time_240m": ("time", 0, 0, 240),
        "S_sltp_3.5x_2R": ("sltp", 3.5, 2.0, 0),
        "S_sltp_5x_2R": ("sltp", 5.0, 2.0, 0),
    }
    result = {}
    for long_name, long_exit in long_exits.items():
        for short_name, short_exit in short_exits.items():
            result[f"{long_name}+{short_name}"] = {
                "long": long_exit,
                "short": short_exit,
            }
    return result


SIGNAL_SETS = {
    # This is the currently active PI-only mapping: long cyan/deep-blue,
    # short pink.  The live preset still owns the actual execution settings.
    "current_pi_only": lambda row: row["kind"] in {"青π", "深蓝圈", "粉π"},
    "all_supported": lambda row: True,
    "level_1": lambda row: row["size"] == "Level 1",
    "level_2": lambda row: row["size"] == "Level 2",
    "level_3": lambda row: row["size"] == "Level 3",
    "pi_kinds_only": lambda row: row["kind"] in {"青π", "粉π"},
}


def _metrics(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "pnl": 0.0, "pf": 0.0, "win_rate": 0.0, "max_dd": 0.0}
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = -sum(value for value in values if value <= 0)
    equity = peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": len(values),
        "pnl": round(sum(values), 2),
        "pf": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "win_rate": round(sum(value > 0 for value in values) / len(values), 3),
        "max_dd": round(max_dd, 2),
    }


def _run(rows: list[dict], predicate, config: dict) -> dict:
    selected = [row for row in rows if predicate(row)]
    values: list[float] = []
    by_direction: dict[str, list[float]] = defaultdict(list)
    by_month: dict[str, list[float]] = defaultdict(list)
    exit_reasons: dict[str, int] = defaultdict(int)
    for row in selected:
        side = "long" if row["direction"] > 0 else "short"
        mode, sl, rr, hold = config[side]
        points, why = simulate(
            row["index"], row["direction"], row["bars"], row["times"],
            row["width"], mode, sl, rr, hold,
        )
        usd = points * POINT_VALUE[row["future"]] - RT_COST[row["future"]]
        values.append(usd)
        by_direction[side].append(usd)
        by_month[row["ny_date"][:7]].append(usd)
        exit_reasons[why] += 1
    return {
        "overall": _metrics(values),
        "long": _metrics(by_direction["long"]),
        "short": _metrics(by_direction["short"]),
        "monthly": {key: _metrics(by_month[key]) for key in sorted(by_month)},
        "exit_reasons": dict(sorted(exit_reasons.items())),
    }


def run() -> dict:
    rows = _trade_rows()
    configs = _configs()
    results: dict[str, dict] = {}
    for set_name, predicate in SIGNAL_SETS.items():
        results[set_name] = {}
        for config_name, config in configs.items():
            results[set_name][config_name] = _run(rows, predicate, config)

    def ranked(set_name: str, key: str, limit: int = 10) -> list[dict]:
        ranked_rows = []
        for config_name, result in results[set_name].items():
            overall = result["overall"]
            ranked_rows.append({"config": config_name, **overall})
        return sorted(
            ranked_rows,
            key=lambda row: (row[key] if row[key] is not None else -10**9, row["pnl"]),
            reverse=True,
        )[:limit]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": {
            "entry": "first 1m candle at or after the PI event timestamp",
            "exit": "existing pi_exit_study simulator; costs included",
            "signal_set_note": "Level is a source annotation; no Level is promoted by this report",
            "drawdown": "chronological per-signal USD equity, no portfolio overlap/netting assumption",
        },
        "coverage": {
            "trades": len(rows),
            "dates": sorted({row["ny_date"] for row in rows}),
            "kinds": dict(sorted({kind: sum(row["kind"] == kind for row in rows) for kind in {row["kind"] for row in rows}}.items())),
            "levels": dict(sorted({level: sum(row["size"] == level for row in rows) for level in {row["size"] for row in rows}}.items())),
        },
        "configs": configs,
        "results": results,
        "ranked_by_pnl": {name: ranked(name, "pnl") for name in SIGNAL_SETS},
        "ranked_by_pf": {name: ranked(name, "pf") for name in SIGNAL_SETS},
    }


def _markdown(result: dict) -> str:
    lines = [
        "# PI signal-set × exit study",
        "",
        "Offline research only; it does not modify the live preset or engine.",
        "",
        f"- Generated: `{result['generated_at']}`",
        f"- Matched PI trades: `{result['coverage']['trades']}`",
        f"- Levels: `{result['coverage']['levels']}`",
        "",
        "## Top by PnL",
        "",
    ]
    for name, rows in result["ranked_by_pnl"].items():
        lines.append(f"### {name}")
        lines.append("")
        lines.append("| Config | n | PnL | PF | Win | Max DD |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for row in rows[:5]:
            lines.append(
                f"| {row['config']} | {row['n']} | ${row['pnl']:,.0f} | "
                f"{row['pf'] if row['pf'] is not None else '—'} | {row['win_rate']:.1%} | "
                f"${row['max_dd']:,.0f} |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    result = run()
    output_dir = market_data.derived_path("research")
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "pi_signal_set_study"
    (output_dir / f"{stem}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (output_dir / f"{stem}.md").write_text(_markdown(result), encoding="utf-8")
    print(output_dir / f"{stem}.md")
    print(json.dumps(result["coverage"], ensure_ascii=False))
    for name, rows in result["ranked_by_pnl"].items():
        print(name, json.dumps(rows[:3], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
