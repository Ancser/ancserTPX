"""Research-only comparison of the selected MBO strategy by market session.

This runner does not call Databento and does not change production settings. It
replays the current ``DELTA ABSORPTION`` strategy through the normal backtest
engine, but supplies a session-local context provider backed by the compact
ALL-session archives. The same entry, exit, cost, and one-position rules are
used for ASIA, EURO, PRE, RTH, and AH.

The raw MBO download is intentionally a separate step. This script refuses to
pretend a missing or incomplete session is a zero-result session.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, time as wall_time, timedelta
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api.routes import BacktestRequest, _build_strategy_params_from_request  # noqa: E402
from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.backtest.robustness import series_stats, slip_injection  # noqa: E402
from backend.data import candle_store, market_data, orderflow  # noqa: E402
from backend.db.models import (  # noqa: E402
    current_quarterly_contract_id,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.delta_absorption import CachedDeltaContextProvider  # noqa: E402
from backend.strategy.delta_absorption import DeltaAbsorptionStrategy  # noqa: E402
from backend.strategy.session_filter import (  # noqa: E402
    SESSION_CODES,
    market_session_id,
)
from backend.timebase import UTC, as_utc  # noqa: E402


EXPECTED_MINUTES = {
    "ASIA": 540,
    "EURO": 240,
    "PRE": 150,
    "RTH": 390,
    "AH": 120,
}
SESSION_WALL_BOUNDS = {
    "ASIA": (wall_time(18, 0), wall_time(3, 0)),
    "EURO": (wall_time(3, 0), wall_time(7, 0)),
    "PRE": (wall_time(7, 0), wall_time(9, 30)),
    "RTH": (wall_time(9, 30), wall_time(16, 0)),
    "AH": (wall_time(16, 0), wall_time(18, 0)),
}
MARKET_ZONE = ZoneInfo("America/New_York")
DEFAULT_START = date(2026, 8, 15)
DEFAULT_END = date(2026, 9, 14)


def _session_date(session_id: str) -> date | None:
    try:
        return date.fromisoformat(str(session_id)[:10])
    except ValueError:
        return None


def _session_code(session_id: str, bars: list[Mapping[str, Any]]) -> str:
    for row in bars:
        code = str(row.get("session") or "").upper()
        if code in SESSION_CODES:
            return code
    return str(session_id).rsplit("-", 1)[-1].upper()


def _read_all_sessions(
    symbol: str,
    start: date,
    end: date,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Load and merge UTC-day ALL archives into New-York session buckets."""
    base = str(symbol).lower()
    root = market_data.derived_path("orderflow", base)
    sessions: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    source_dates: list[str] = []
    invalid: list[dict[str, str]] = []
    if root.is_dir():
        paths = sorted(root.glob(f"all_sessions_footprint_{base}_*.json.gz"))
    else:
        paths = []
    for path in paths:
        try:
            prefix = f"all_sessions_footprint_{base}_"
            calendar_day = date.fromisoformat(
                path.name[len(prefix):len(prefix) + 10]
            )
        except ValueError:
            continue
        if not (start - timedelta(days=2) <= calendar_day < end + timedelta(days=2)):
            continue
        try:
            payload = orderflow.read_footprint_cache(path)
            meta = payload.get("meta") or {}
            if (
                int(meta.get("schema_version") or 0) != orderflow.CACHE_SCHEMA_VERSION
                or str(meta.get("session") or "").upper() != "ALL"
            ):
                invalid.append({"path": str(path), "reason": "not_schema_v5_all"})
                continue
        except (OSError, ValueError, EOFError, TypeError) as exc:
            invalid.append({"path": str(path), "reason": f"{type(exc).__name__}: {exc}"})
            continue
        source_dates.append(calendar_day.isoformat())
        for raw in payload.get("bars") or []:
            if not isinstance(raw, Mapping):
                continue
            session_id = str(raw.get("session_id") or "")
            try:
                epoch = int(raw.get("epoch") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if not session_id or epoch <= 0:
                continue
            # A duplicate minute must not be counted twice if archives overlap.
            sessions[session_id][epoch] = dict(raw)

    merged = {
        key: [row for _, row in sorted(rows.items())]
        for key, rows in sessions.items()
    }
    return merged, {
        "cache_root": str(root),
        "source_dates": sorted(set(source_dates)),
        "invalid_archives": invalid,
    }


def _complete_session(session_id: str, bars: list[Mapping[str, Any]]) -> bool:
    code = _session_code(session_id, list(bars))
    expected = EXPECTED_MINUTES.get(code)
    bounds = SESSION_WALL_BOUNDS.get(code)
    session_day = _session_date(session_id)
    if expected is None or bounds is None or session_day is None:
        return False
    epochs = [int(row.get("epoch") or 0) for row in bars]
    if not epochs:
        return False
    start_wall, end_wall = bounds
    start_local = datetime.combine(session_day, start_wall, tzinfo=MARKET_ZONE)
    end_day = session_day + timedelta(days=1) if end_wall <= start_wall else session_day
    end_local = datetime.combine(end_day, end_wall, tzinfo=MARKET_ZONE)
    start_epoch = int(start_local.astimezone(UTC).timestamp())
    end_epoch = int(end_local.astimezone(UTC).timestamp())
    # MBO archives only create a minute when an order-book event occurs.  A
    # session is therefore complete when it spans the intended wall-clock
    # interval and has enough observed event minutes; requiring one row for
    # every minute would incorrectly discard valid low-activity AH sessions.
    observed_span = max(epochs) - min(epochs) + 60
    expected_span = end_epoch - start_epoch
    minimum_observed = max(20, int(expected * 0.40))
    return (
        min(epochs) <= start_epoch + 60
        and max(epochs) + 60 >= end_epoch - 60
        and observed_span >= expected_span - 120
        and len(epochs) >= minimum_observed
    )


def _session_inventory(
    sessions: Mapping[str, list[dict[str, Any]]],
    start: date,
    end: date,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    complete: dict[str, list[dict[str, Any]]] = {}
    counts: Counter[str] = Counter()
    incomplete: list[dict[str, Any]] = []
    for session_id, bars in sessions.items():
        session_day = _session_date(session_id)
        code = _session_code(session_id, bars)
        if session_day is None or not (start <= session_day < end):
            continue
        counts[code] += 1
        if _complete_session(session_id, bars):
            complete[session_id] = bars
        else:
            incomplete.append({
                "session_id": session_id,
                "session": code,
                "bars": len(bars),
                "expected": EXPECTED_MINUTES.get(code),
            })
    by_code: dict[str, list[str]] = defaultdict(list)
    for session_id in complete:
        by_code[_session_code(session_id, complete[session_id])].append(session_id)
    for code in by_code:
        by_code[code].sort(key=lambda item: int(complete[item][0]["epoch"]))
    return complete, {
        "all_session_ids": len(sessions),
        "candidate_sessions": dict(sorted(counts.items())),
        "complete_sessions": {
            code: len(ids) for code, ids in sorted(by_code.items())
        },
        "incomplete_sessions": incomplete,
        "complete_by_code": dict(by_code),
    }


class AllSessionDeltaContextProvider(CachedDeltaContextProvider):
    """Use one complete same-code session as the prior value-area profile."""

    provider_name = "all_session_research"

    def __init__(
        self,
        *,
        symbol: str,
        tick_size: float,
        session_code: str,
        sessions: Mapping[str, list[dict[str, Any]]],
        previous_profiles: Mapping[str, tuple[str | None, dict[str, float] | None]],
    ) -> None:
        super().__init__(
            symbol=symbol,
            tick_size=tick_size,
            require_complete_profile=False,
        )
        self.session_code = session_code
        self.sessions = sessions
        self.previous_profiles = previous_profiles

    def snapshot(self, timestamp: datetime) -> dict[str, Any] | None:
        now = as_utc(timestamp)
        session_id = market_session_id(now)
        bars = self.sessions.get(session_id)
        if not bars or _session_code(session_id, bars) != self.session_code:
            return None
        decision_epoch = int(now.timestamp() // 60 * 60)
        eligible = [
            row for row in bars
            if int(row.get("epoch") or 0) > 0
            and int(row.get("epoch") or 0) + 60 <= decision_epoch
        ]
        if not eligible:
            return None
        previous_id, profile = self.previous_profiles.get(session_id, (None, None))
        return {
            "date": session_id,
            "session": self.session_code,
            "bars": eligible,
            "bar": eligible[-1],
            "profile": profile,
            "previous_profile_date": previous_id,
            "vwap": self._vwap(eligible),
            "provider": self.provider_name,
        }

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "state": "all_session_research",
            "connected": True,
            "provider": self.provider_name,
            "symbol": self.symbol,
            "session": self.session_code,
        }


def _profiles_by_session(
    complete: Mapping[str, list[dict[str, Any]]],
    inventory: Mapping[str, Any],
    tick_size: float,
) -> dict[str, tuple[str | None, dict[str, float] | None]]:
    result: dict[str, tuple[str | None, dict[str, float] | None]] = {}
    by_code = inventory.get("complete_by_code") or {}
    for code, session_ids in by_code.items():
        ordered = list(session_ids)
        for index, session_id in enumerate(ordered):
            prior_id = ordered[index - 1] if index else None
            prior = (
                orderflow.footprint_profile(
                    complete[prior_id], value_area_pct=0.70, tick_size=tick_size,
                )
                if prior_id else {}
            )
            result[session_id] = (prior_id, prior or None)
    return result


def _make_params(symbol: str, session_code: str):
    request = BacktestRequest(
        strategy="delta_absorption",
        contract_id=current_quarterly_contract_id(symbol),
        contract_size=1,
        delta_window=5,
        delta_baseline_window=30,
        delta_strength_multiplier=1.0,
        delta_weakening_ratio=0.70,
        delta_stall_ticks=1,
        delta_source="whole",
        delta_gate="location",
        delta_pattern="absorption",
        delta_side_mode="all",
        delta_require_profile=True,
        factor_sl_rule="atr_blend",
        factor_tp_rule="atr_blend",
        factor_sl_value=4.0,
        factor_tp_value=12.0,
        rr_ratio=3,
        pi_long_only=False,
        pi_long_hold_min=0,
        pi_short_hold_min=60,
        tr_allowed_sessions=[session_code],
        tr_one_trade_per_session=False,
    )
    params = _build_strategy_params_from_request(request, 1)
    params.contract_id = current_quarterly_contract_id(symbol)
    params.contract_size = 1
    params.tr_allowed_sessions = [session_code]
    params.tr_one_trade_per_session = False
    return params


def _trade_row(trade: Any, symbol: str) -> dict[str, Any]:
    return {
        "entry_time": as_utc(trade.entry_time).isoformat(),
        "exit_time": as_utc(trade.exit_time).isoformat() if trade.exit_time else None,
        "pnl": round(float(trade.pnl or 0.0), 4),
        "direction": "long" if getattr(trade.direction, "value", "") == "buy" else "short",
        "size": max(1, int(getattr(trade, "contracts", 1) or 1)),
        "entry": float(trade.entry_price),
        "exit": float(trade.exit_price) if trade.exit_price is not None else None,
        "exit_reason": getattr(getattr(trade, "exit_reason", None), "value", str(trade.exit_reason)),
        "symbol": symbol,
    }


def _stats(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    pnls = [float(row["pnl"]) for row in rows]
    base = series_stats(pnls)
    stress = slip_injection(rows, levels=(14,), symbol=symbol)["levels"][0]["stats"]
    entry_days = sorted({str(row["entry_time"])[:10] for row in rows})
    return {
        "trades": int(base["n"]),
        "pnl": round(float(base["pnl"]), 2),
        "pf": round(float(base["pf"]), 4),
        "win_rate": round(float(base["win"]), 4),
        "max_dd": round(float(base["max_dd"]), 2),
        "expectancy": round(float(base["pnl"]) / len(rows), 2) if rows else 0.0,
        "stress_14t_pnl": round(float(stress["pnl"]), 2),
        "stress_14t_pf": round(float(stress["pf"]), 4),
        "entry_days": len(entry_days),
        "exit_reasons": dict(Counter(str(row["exit_reason"]) for row in rows)),
        "by_direction": {
            side: {
                "trades": len(side_rows),
                "pnl": round(sum(float(row["pnl"]) for row in side_rows), 2),
                "pf": round(float(series_stats([float(row["pnl"]) for row in side_rows])["pf"]), 4),
            }
            for side in ("long", "short")
            for side_rows in [[row for row in rows if row["direction"] == side]]
        },
    }


def _run_symbol(
    symbol: str,
    start: date,
    end: date,
    requested_sessions: list[str],
) -> dict[str, Any]:
    started = time.perf_counter()
    sessions, cache_info = _read_all_sessions(symbol, start, end)
    complete, inventory = _session_inventory(sessions, start, end)
    profiles = _profiles_by_session(complete, inventory, 0.25)
    available_codes = [
        code for code in requested_sessions
        if (inventory.get("complete_by_code") or {}).get(code)
    ]
    result: dict[str, Any] = {
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cache": cache_info,
        "inventory": {
            key: value for key, value in inventory.items()
            if key != "complete_by_code"
        },
        "session_results": {},
        "status": "no_complete_sessions" if not available_codes else "complete",
    }
    if not available_codes:
        return result

    contract_id = current_quarterly_contract_id(symbol)
    candle_snapshot = candle_store.load_snapshot(symbol)
    earliest_epoch = min(
        int(complete[session_id][0]["epoch"])
        for code in available_codes
        for session_id in (inventory["complete_by_code"].get(code) or [])
    )
    latest_epoch = max(
        int(complete[session_id][-1]["epoch"])
        for code in available_codes
        for session_id in (inventory["complete_by_code"].get(code) or [])
    )
    candle_start = datetime.fromtimestamp(earliest_epoch, UTC) - timedelta(days=7)
    candle_end = datetime.fromtimestamp(latest_epoch, UTC) + timedelta(days=3)
    candles = candle_store.select_range(candle_snapshot, candle_start, candle_end)
    candle_store.invalidate_cache(symbol)
    result["candle_input"] = {
        "bars": len(candles),
        "start": candles[0].timestamp.isoformat() if candles else None,
        "end": candles[-1].timestamp.isoformat() if candles else None,
    }
    if not candles:
        result["status"] = "no_execution_candles"
        return result

    config = BacktestConfig(
        strategies=["trend"],
        symbol=symbol,
        initial_capital=50_000.0,
        commission_rt=get_commission_rt(contract_id),
        fees_rt=get_fees_rt(contract_id),
        value_area_pct=0.80,
    )
    for code in available_codes:
        provider = AllSessionDeltaContextProvider(
            symbol=symbol,
            tick_size=0.25,
            session_code=code,
            sessions=complete,
            previous_profiles=profiles,
        )
        params = _make_params(symbol, code)
        engine = BacktestEngine(
            config=config,
            strategy_params=params,
            record_equity=False,
        )
        # Keep the production engine and exits; replace only the context source
        # with a session-local research provider.
        engine.trend_follow = DeltaAbsorptionStrategy(params, context_provider=provider)
        engine._pending_max_age = engine.trend_follow.PENDING_TIMEOUT_CANDLES
        backtest = engine.run(candles)
        rows = [_trade_row(trade, symbol) for trade in backtest.trades]
        result["session_results"][code] = {
            "complete_sessions": len(inventory["complete_by_code"].get(code) or []),
            "stats": _stats(rows, symbol),
            "trades": rows,
        }
    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


def _write_outputs(report: dict[str, Any]) -> tuple[Path, Path]:
    target = market_data.derived_path(
        "research", "mbo_session_comparison_2026-08-15_2026-09-14.json",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = target.with_suffix(".md")
    lines = [
        "# MBO session comparison — DELTA ABSORPTION",
        "",
        f"Window: `{report['start']}` to `{report['end']}` (end exclusive).",
        "Same current entry/exit/cost rules; session-local prior same-code profile.",
        "",
        "| Symbol | Session | Complete sessions | Trades | PnL | PF | 14t PF | Max DD |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for symbol, data in report["symbols"].items():
        for code, item in (data.get("session_results") or {}).items():
            stats = item["stats"]
            lines.append(
                f"| {symbol} | {code} | {item['complete_sessions']} | "
                f"{stats['trades']} | {stats['pnl']} | {stats['pf']} | "
                f"{stats['stress_14t_pf']} | {stats['max_dd']} |"
            )
    lines.extend([
        "",
        "## Interpretation guardrails",
        "",
        "- A session with fewer than roughly 20 trades is descriptive, not a production ranking.",
        "- Results are entry-session attribution; a position may exit later in another session.",
        "- No per-session parameter tuning was performed.",
        "- Missing or incomplete session archives are reported and never converted to zero trades.",
    ])
    markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target, markdown


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--symbols", nargs="+", default=["MNQ", "MES"])
    parser.add_argument("--sessions", nargs="+", default=list(SESSION_CODES))
    args = parser.parse_args()
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    sessions = [str(value).upper() for value in args.sessions]
    invalid = [value for value in sessions if value not in SESSION_CODES]
    if end <= start or invalid:
        parser.error("invalid date range or session code")
    report = {
        "status": "retrospective_research_only",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sessions": sessions,
        "symbols": {},
    }
    for raw_symbol in args.symbols:
        symbol = str(raw_symbol).upper()
        print(f"[{symbol}] loading ALL-session caches...", flush=True)
        data = _run_symbol(symbol, start, end, sessions)
        report["symbols"][symbol] = data
        print(
            f"[{symbol}] {data['status']} | "
            + ", ".join(
                f"{code} n={item['stats']['trades']} PF={item['stats']['pf']}"
                for code, item in (data.get("session_results") or {}).items()
            ),
            flush=True,
        )
    json_path, md_path = _write_outputs(report)
    print(f"JSON: {json_path}")
    print(f"Report: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
