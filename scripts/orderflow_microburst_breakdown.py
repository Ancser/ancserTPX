"""Break down the already-generated raw-MBO microburst event file.

This script does not reread the large DBN files.  It provides the clean
comparison that is easy to corrupt by mixing overlapping windows: the main
pattern table uses one five-second window for every fixed split, plus the
single ``1x150`` event.  It also reports the wider 1/5/10/30-second window
diagnostics and conditional labels such as possible absorption and
profit-taking reversal.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.data import market_data


UTC = timezone.utc
EVENT_PATH = market_data.derived_path("research", "orderflow_microburst_study.events.jsonl.gz")
JSON_PATH = market_data.derived_path("research", "orderflow_microburst_breakdown.json")
MARKDOWN_PATH = market_data.derived_path("research", "orderflow_microburst_breakdown.md")
TICK_VALUE_MNQ = 0.50


def _events() -> Iterable[dict[str, Any]]:
    with gzip.open(EVENT_PATH, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            pattern = str(row.get("pattern") or "")
            if pattern == "1x150" or pattern.endswith("@5s") or pattern.endswith("@1s") or pattern.endswith("@10s") or pattern.endswith("@30s"):
                yield row


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _stat(rows: list[dict[str, Any]]) -> dict[str, Any]:
    follow: dict[int, list[float]] = {5: [], 30: [], 60: []}
    reverse: dict[int, list[float]] = {5: [], 30: [], 60: []}
    wins: dict[int, list[bool]] = {5: [], 30: [], 60: []}
    reverse_wins: dict[int, list[bool]] = {5: [], 30: [], 60: []}
    for row in rows:
        for horizon in (5, 30, 60):
            outcome = row.get(f"outcome_{horizon}s") or {}
            if "return_ticks" not in outcome:
                continue
            # The raw-MBO producer already stores direction * price change.
            # Do not multiply by direction a second time here.
            value = float(outcome["return_ticks"])
            follow[horizon].append(value)
            reverse[horizon].append(-value)
            wins[horizon].append(value > 0)
            reverse_wins[horizon].append(value < 0)
    out: dict[str, Any] = {
        "n": len(rows),
        "days": len({row.get("trade_date") for row in rows}),
        "buy": sum(row.get("side") == "buy" for row in rows),
        "sell": sum(row.get("side") == "sell" for row in rows),
        "median_actual_qty": _median([float(row["actual_qty"]) for row in rows]),
        "median_trade_count": _median([float(row["trade_count"]) for row in rows]),
        "median_duration_ms": _median([float(row["duration_ms"]) for row in rows]),
        "median_event_impact_ticks": _median([float(row["event_impact_ticks"]) for row in rows]),
        "pi_matches": sum(bool(row.get("pi_match")) for row in rows),
    }
    out["pi_match_rate"] = round(out["pi_matches"] / len(rows), 4) if rows else 0.0
    for horizon in (5, 30, 60):
        out[f"follow_{horizon}s_n"] = len(follow[horizon])
        out[f"follow_{horizon}s_mean_ticks"] = _mean(follow[horizon])
        out[f"follow_{horizon}s_median_ticks"] = _median(follow[horizon])
        out[f"follow_{horizon}s_win_rate"] = round(sum(wins[horizon]) / len(wins[horizon]), 4) if wins[horizon] else None
        out[f"reverse_{horizon}s_mean_ticks"] = _mean(reverse[horizon])
        out[f"reverse_{horizon}s_median_ticks"] = _median(reverse[horizon])
        out[f"reverse_{horizon}s_win_rate"] = round(sum(reverse_wins[horizon]) / len(reverse_wins[horizon]), 4) if reverse_wins[horizon] else None
    return out


def _base_pattern(name: str) -> str | None:
    if name == "1x150":
        return name
    match = re.match(r"^(\d+x\d+)@5s$", name)
    return match.group(1) if match else None


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _table_row(name: str, stat: dict[str, Any]) -> str:
    return (
        f"| `{name}` | {stat['n']} | {stat['buy']}/{stat['sell']} | "
        f"{_fmt(stat['median_actual_qty'])} | {_fmt(stat['median_duration_ms'])} | "
        f"{_fmt(stat['follow_30s_median_ticks'])} | {_fmt(stat['follow_30s_win_rate'])} | "
        f"{_fmt(stat['reverse_30s_median_ticks'])} | {_fmt(stat['reverse_30s_win_rate'])} | "
        f"{stat['pi_matches']} |"
    )


def main() -> int:
    grouped: dict[str, list[dict[str, Any]]] = {}
    windows: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, list[dict[str, Any]]] = {}
    pi_aligned: dict[str, list[dict[str, Any]]] = {}
    for row in _events():
        pattern = str(row.get("pattern") or "")
        base = _base_pattern(pattern)
        if base:
            grouped.setdefault(base, []).append(row)
        if "@" in pattern:
            windows.setdefault(pattern, []).append(row)
        for label in row.get("labels") or []:
            labels.setdefault(label, []).append(row)
        if row.get("pi_match"):
            pi_aligned.setdefault(pattern, []).append(row)

    clean = {name: _stat(rows) for name, rows in sorted(grouped.items())}
    window_stats = {name: _stat(rows) for name, rows in sorted(windows.items())}
    label_stats = {name: _stat(rows) for name, rows in sorted(labels.items())}
    pi_stats = {name: _stat(rows) for name, rows in sorted(pi_aligned.items())}
    report = {
        "source_events": str(EVENT_PATH),
        "generated_at": datetime.now(UTC).isoformat(),
        "tick_value_mnq": TICK_VALUE_MNQ,
        "clean_fixed_patterns_5s": clean,
        "window_stats": window_stats,
        "label_stats": label_stats,
        "pi_aligned_stats": pi_stats,
        "notes": [
            "Follow return is direction times price return; positive means the aggressive side continued.",
            "Reverse return is the same event traded against the aggressive side; this is diagnostic, not a live rule.",
            "The clean table uses one @5s window per fixed split so overlapping windows do not multiply the comparison.",
            "Rows can overlap across patterns and are not a PnL backtest with ATR exits, slippage, or position management.",
            "active_continuation, exhaustion_or_reversal, and potential_profit_taking_reversal use the 30-second future outcome by definition; they are post-event descriptive labels and must not be used as causal entry filters.",
        ],
    }
    JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    JSON_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# MBO micro-burst breakdown",
        "",
        "This is a raw-price diagnostic from the completed MBO event file; it is not a live rule or a PnL backtest.",
        "",
        "## Clean comparison: one five-second window",
        "",
        "`Follow` is positive when price continues in the aggressive direction. `Reverse` is positive when fading that event would have worked. Values are ticks; one MNQ tick is $0.50 per contract.",
        "",
        "| Pattern | n | buy/sell | median qty | median duration ms | follow median 30s | follow win 30s | reverse median 30s | reverse win 30s | PI matches |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, stat in clean.items():
        lines.append(_table_row(name, stat))
    lines.extend([
        "",
        "## Window sensitivity",
        "",
        "| Pattern | n | follow median 30s | follow win 30s | median duration ms |",
        "|---|---:|---:|---:|---:|",
    ])
    for name, stat in window_stats.items():
        lines.append(
            f"| `{name}` | {stat['n']} | {_fmt(stat['follow_30s_median_ticks'])} | "
            f"{_fmt(stat['follow_30s_win_rate'])} | {_fmt(stat['median_duration_ms'])} |"
        )
    lines.extend([
        "",
        "## Conditional labels",
        "",
        "Labels are not mutually exclusive. `possible_absorption` means limited event price progress plus opposing passive activity; `potential_profit_taking_reversal` means a same-direction 30-second pre-trend followed by a directional 30-second reversal. The latter, along with active/exhaustion labels, uses future information and is descriptive only; none identifies the trader.",
        "",
        "| Label | n | follow median 30s | follow win 30s | reverse median 30s | reverse win 30s |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for name, stat in label_stats.items():
        lines.append(
            f"| `{name}` | {stat['n']} | {_fmt(stat['follow_30s_median_ticks'])} | "
            f"{_fmt(stat['follow_30s_win_rate'])} | {_fmt(stat['reverse_30s_median_ticks'])} | "
            f"{_fmt(stat['reverse_30s_win_rate'])} |"
        )
    lines.extend([
        "",
        "## PI-lead events",
        "",
        "The source event must precede a same-direction QQQ PI mark by 0–5 minutes. This is a lead/lag association, not evidence that the event generated PI.",
        "",
        "| Pattern | PI-aligned n | follow median 30s | follow win 30s | reverse median 30s | reverse win 30s |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for name, stat in pi_stats.items():
        lines.append(
            f"| `{name}` | {stat['n']} | {_fmt(stat['follow_30s_median_ticks'])} | "
            f"{_fmt(stat['follow_30s_win_rate'])} | {_fmt(stat['reverse_30s_median_ticks'])} | "
            f"{_fmt(stat['reverse_30s_win_rate'])} |"
        )
    lines.extend([
        "",
        "## Guardrails",
        "",
        "Strict `3x50` and `2x75` are rare in this sample. Their positive medians must not be promoted without more months. The high-frequency `30x5` and cumulative-150 families have much larger samples but are overlapping event diagnostics, not independent trades.",
        "",
    ])
    MARKDOWN_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(JSON_PATH)
    print(MARKDOWN_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
