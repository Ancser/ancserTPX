"""Run the three proposed prop-firm intraday strategies on the candle store.

Examples:

    python scripts/prop_intraday_research.py
    python scripts/prop_intraday_research.py --symbols MNQ MES --start 2020-01-01
    python scripts/prop_intraday_research.py --news-csv data/research/news_events.csv

The optional news CSV needs a ``timestamp_et`` (preferred) or ``timestamp``
column.  A missing news file is reported as an uncovered filter; the script
never invents recurring "news times" and calls that a historical calendar.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.backtest.prop_intraday_research import (  # noqa: E402
    load_news_events,
    load_symbol_sessions,
    recommended_configs,
    result_to_dict,
    run_config,
)


def _json_default(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serialisable: {type(value)!r}")


def _parse_day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _store_meta(symbol: str) -> dict:
    path = ROOT / "data" / "store" / f"{symbol}_accumulated_1m.meta.json"
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}
    body["path"] = str(path)
    body["exists"] = True
    return body


def _short_row(result) -> dict:
    stats = result.summary["stats"]
    slip14 = result.summary["slip_14t"]
    walk = result.summary.get("walk_forward") or {}
    return {
        "symbol": result.symbol,
        "strategy": result.strategy,
        "variant": result.variant,
        "trades": stats["n"],
        "pnl": stats["pnl"],
        "pf": stats["pf"],
        "win": stats["win"],
        "max_dd": stats["max_dd"],
        "walk_forward": bool(walk.get("pass")),
        "monte_carlo": bool(result.summary.get("monte_carlo_pass")),
        "slip14_pnl": slip14["pnl"],
        "slip14_pf": slip14["pf"],
        "verdict": result.summary["verdict"],
        "news_filter": result.diagnostics["news_filter_applied"],
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["symbol"])
        writer.writeheader()
        writer.writerows(rows)


def _fmt(value: float, decimals: int = 2) -> str:
    return f"{float(value):.{decimals}f}"


def _write_markdown(path: Path, payload: dict, rows: list[dict]) -> None:
    news = payload["news_filter"]
    lines = [
        "# Prop Intraday Strategy Research",
        "",
        f"Generated: {payload['created_at']}",
        "",
        "This is a research-only, causal 1-minute OHLCV simulation. It does not register a live strategy.",
        "",
        "## Coverage and assumptions",
        "",
        f"- Symbols: {', '.join(payload['symbols'])}",
        f"- Requested date range: {payload['requested_range']['start'] or 'store start'} to "
        f"{payload['requested_range']['end'] or 'store end'}",
        f"- Risk budget: ${payload['risk_dollars']:.2f} per trade; maximum "
        f"{payload['max_trades_per_day']} trades/day",
        "- Signal fill: next available 1m open; one position at a time",
        "- Baseline includes contract commission and fees; 1/2/4/8/14-tick round-trip slippage is injected separately",
        "- Same-bar SL/TP uses the shared nearest-to-open rule; post-TP1 ambiguity is stop-first",
        "- RTH clock: America/New_York (DST-aware), entries before 15:00 ET, forced flat at 15:50 ET",
        f"- Historical news filter: {'APPLIED' if news['applied'] else 'NOT APPLIED — no audited calendar supplied'}",
        "",
        "## Results",
        "",
        "| Symbol | Strategy | Variant | n | PnL | PF | Max DD | WF | MC | 14t PF | Verdict |",
        "|---|---|---|---:|---:|---:|---:|:---:|:---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['symbol']} | {row['strategy']} | {row['variant']} | {row['trades']} | "
            f"${_fmt(row['pnl'], 0)} | {_fmt(row['pf'])} | ${_fmt(row['max_dd'], 0)} | "
            f"{'Y' if row['walk_forward'] else 'N'} | {'Y' if row['monte_carlo'] else 'N'} | "
            f"{_fmt(row['slip14_pf'])} | {row['verdict']} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation guardrails",
            "",
            "`RESEARCH_CANDIDATE` means only that the predeclared mechanical gates passed. It is not live-trading approval.",
            "A variant with `FAIL_14T_SLIPPAGE` has positive baseline results but insufficient margin for the repository's measured stress level.",
            "The JSON companion contains yearly, long/short, walk-forward, Monte Carlo, sizing, consistency, and data-seam details.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES"])
    parser.add_argument("--start", help="inclusive ET date, YYYY-MM-DD")
    parser.add_argument("--end", help="inclusive ET date, YYYY-MM-DD")
    parser.add_argument("--news-csv", help="audited historical news calendar")
    parser.add_argument("--risk-dollars", type=float, default=200.0)
    parser.add_argument("--max-trades-per-day", type=int, default=2)
    parser.add_argument("--mc-iters", type=int, default=1000)
    parser.add_argument(
        "--output",
        default="data/research/prop_intraday_research_current.json",
        help="JSON report path; .csv and .md companions are written beside it",
    )
    parser.add_argument("--include-trades", action="store_true")
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        help="run only the named variant; repeatable",
    )
    args = parser.parse_args()

    symbols = [str(symbol).upper() for symbol in args.symbols]
    bad = [symbol for symbol in symbols if symbol not in ("MNQ", "MES")]
    if bad:
        parser.error(f"unsupported symbols: {', '.join(bad)}")
    if args.risk_dollars <= 0:
        parser.error("--risk-dollars must be positive")
    if args.max_trades_per_day <= 0:
        parser.error("--max-trades-per-day must be positive")
    if args.mc_iters <= 0:
        parser.error("--mc-iters must be positive")

    start_day = _parse_day(args.start)
    end_day = _parse_day(args.end)
    if start_day and end_day and start_day > end_day:
        parser.error("--start must be <= --end")
    news_events = load_news_events(args.news_csv)

    started = time.time()
    results = []
    dataset_rows = []
    print(
        f"Prop intraday research | symbols={','.join(symbols)} | "
        f"risk=${args.risk_dollars:g} | news={'yes' if news_events else 'NO'}"
    )
    for symbol in symbols:
        load_started = time.time()
        sessions, info = load_symbol_sessions(
            symbol, start_date=start_day, end_date=end_day
        )
        meta = _store_meta(symbol)
        dataset_rows.append(
            {
                "actual": asdict(info),
                "metadata_file": meta,
                "known_seams": meta.get("known_seams", []),
            }
        )
        print(
            f"[{symbol}] {info.total_bars:,} store bars | {info.rth_sessions:,} full RTH sessions | "
            f"{info.first_timestamp} -> {info.last_timestamp} | load {time.time()-load_started:.1f}s"
        )
        configs = recommended_configs(
            symbol,
            risk_dollars=args.risk_dollars,
            max_trades_per_day=args.max_trades_per_day,
        )
        if args.variant:
            wanted = set(args.variant)
            configs = tuple(config for config in configs if config.name in wanted)
            missing = wanted - {config.name for config in configs}
            if missing:
                parser.error(f"unknown {symbol} variants: {', '.join(sorted(missing))}")
        for index, config in enumerate(configs, 1):
            run_started = time.time()
            result = run_config(
                sessions,
                config,
                symbol,
                news_events=news_events,
                monte_carlo_iters=args.mc_iters,
            )
            results.append(result)
            row = _short_row(result)
            print(
                f"  [{index:02d}/{len(configs):02d}] {config.name:<39} "
                f"n={row['trades']:4d} PF={row['pf']:6.2f} PnL={row['pnl']:+9.0f} "
                f"DD={row['max_dd']:7.0f} 14tPF={row['slip14_pf']:5.2f} "
                f"{row['verdict']} ({time.time()-run_started:.1f}s)"
            )

    summary_rows = [_short_row(result) for result in results]
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": time.time() - started,
        "symbols": symbols,
        "requested_range": {"start": args.start, "end": args.end},
        "risk_dollars": args.risk_dollars,
        "max_trades_per_day": args.max_trades_per_day,
        "monte_carlo_iters": args.mc_iters,
        "news_filter": {
            "applied": bool(news_events),
            "source": args.news_csv,
            "events": len(news_events),
            "uncovered_reason": None if news_events else "no audited historical calendar supplied",
        },
        "execution_assumptions": {
            "timezone": "America/New_York",
            "signal_fill": "next_available_1m_open",
            "entry_cutoff_et": "15:00",
            "force_flat_et": "15:50",
            "one_position": True,
            "fees_and_commission": True,
            "baseline_slippage_ticks_rt": 0,
            "stress_slippage_ticks_rt": [1, 2, 4, 8, 14],
            "partial_same_bar_tie": "stop_first",
        },
        "datasets": dataset_rows,
        "results": [
            result_to_dict(result, include_trades=args.include_trades)
            for result in results
        ],
    }

    output = (ROOT / args.output).resolve() if not Path(args.output).is_absolute() else Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    csv_path = output.with_suffix(".csv")
    markdown_path = output.with_suffix(".md")
    _write_csv(csv_path, summary_rows)
    _write_markdown(markdown_path, payload, summary_rows)
    print(f"\nJSON: {output}")
    print(f"CSV : {csv_path}")
    print(f"MD  : {markdown_path}")
    print(f"Elapsed: {time.time()-started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

