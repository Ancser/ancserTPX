"""Read-only bid/ask replay for the saved QQQ PI 2026-03-30 pilot."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.data import market_data  # noqa: E402
from backend.data.pi_history import load_rows  # noqa: E402
from scripts.pi_underlying_path_study_20260921 import (  # noqa: E402
    canonical_buy_signals,
    expected_entry,
    following_sessions,
)


DEFAULT_DATA_ROOT = Path(r"F:\ancserQuant\ancserMarketData\source\options\thetadata")
EVENT_DATE = "2026-03-30"
EXIT_DATE = "2026-03-31"
EXPIRATION = "2026-03-31"
ET = ZoneInfo("America/New_York")
ENTRY_TIME = datetime(2026, 3, 30, 15, 2, tzinfo=ET)
EXIT_TIMES = {
    "next_day_10_00_et": datetime(2026, 3, 31, 10, 0, tzinfo=ET),
    "next_day_15_30_et": datetime(2026, 3, 31, 15, 30, tzinfo=ET),
}
CONTRACT_MULTIPLIER_ASSUMPTION = 100
ROUND_TRIP_COMMISSION_SENSITIVITY = 1.30


@dataclass(frozen=True)
class PilotSpec:
    """One saved QQQ event and its next-session quote replay windows."""

    event_date: str = EVENT_DATE
    exit_date: str = EXIT_DATE
    expiration: str = EXPIRATION
    event_time_et: str = "15:01"
    entry_time_et: str = "15:02"

    def __post_init__(self) -> None:
        date.fromisoformat(self.event_date)
        date.fromisoformat(self.exit_date)
        date.fromisoformat(self.expiration)
        time.fromisoformat(self.event_time_et)
        time.fromisoformat(self.entry_time_et)
        if self.event_date > self.exit_date:
            raise ValueError("exit_date must be on or after event_date")

    @property
    def entry_datetime(self) -> datetime:
        return datetime.combine(
            date.fromisoformat(self.event_date),
            time.fromisoformat(self.entry_time_et),
            tzinfo=ET,
        )

    @property
    def exit_datetimes(self) -> dict[str, datetime]:
        exit_day = date.fromisoformat(self.exit_date)
        return {
            "next_day_10_00_et": datetime.combine(exit_day, time(10, 0), tzinfo=ET),
            "next_day_15_30_et": datetime.combine(exit_day, time(15, 30), tzinfo=ET),
        }


DEFAULT_SPEC = PilotSpec()


def pilot_files(spec: PilotSpec = DEFAULT_SPEC) -> tuple[str, ...]:
    return (
        f"raw/QQQ/eod_dte_0_60/date={spec.event_date}/expiration=ALL.csv.gz",
        f"raw/QQQ/open_interest_dte_0_60/date={spec.event_date}/expiration=ALL.csv.gz",
        f"raw/QQQ/quote_1m/date={spec.event_date}/expiration={spec.expiration}.csv.gz",
        f"raw/QQQ/greeks_first_order_1m/date={spec.event_date}/expiration={spec.expiration}.csv.gz",
        f"raw/QQQ/quote_1m/date={spec.exit_date}/expiration={spec.expiration}.csv.gz",
        f"raw/QQQ/greeks_first_order_1m/date={spec.exit_date}/expiration={spec.expiration}.csv.gz",
    )


PILOT_FILES = pilot_files()


def default_pi_path() -> Path:
    root = market_data.configured_market_data_root()
    return market_data.pi_source_root(root) / "pi_signals.json"


def canonical_episode_specs(pi_path: Path | None = None) -> list[tuple[PilotSpec, dict[str, str]]]:
    """Derive every QQQ level-2 deep-blue episode from canonical PI source time."""
    rows = load_rows(path=pi_path or default_pi_path())
    signals, _ = canonical_buy_signals(rows)
    selected = [
        signal for signal in signals
        if signal.symbol == "QQQ" and signal.kind == "深蓝圈" and signal.level == 2
    ]
    episodes: list[tuple[PilotSpec, dict[str, str]]] = []
    for signal in selected:
        entry_day, entry_ts = expected_entry(signal.ts)
        if entry_day is None or entry_ts is None:
            raise ValueError(f"canonical PI signal has no next eligible minute: {signal.ts.isoformat()}")
        exit_day = following_sessions(entry_day, 1)[0]
        source_et = signal.ts.astimezone(ET)
        entry_et = entry_ts.astimezone(ET)
        spec = PilotSpec(
            event_date=entry_day.isoformat(),
            exit_date=exit_day.isoformat(),
            expiration=exit_day.isoformat(),
            event_time_et=source_et.strftime("%H:%M"),
            entry_time_et=entry_et.strftime("%H:%M"),
        )
        episodes.append((spec, {
            "signal_id": signal.signal_id,
            "source_ts_utc": signal.ts.isoformat(),
            "source_ts_et": source_et.isoformat(),
            "derived_entry_ts_utc": entry_ts.isoformat(),
            "derived_entry_ts_et": entry_et.isoformat(),
            "derivation": "backend.data.pi_history.load_rows -> canonical_buy_signals -> expected_entry (next eligible one-minute session timestamp)",
        }))
    return episodes


def canonical_episode_for_date(event_date: str, pi_path: Path | None = None) -> tuple[PilotSpec, dict[str, str]]:
    matches = [item for item in canonical_episode_specs(pi_path) if item[0].event_date == event_date]
    if len(matches) != 1:
        raise ValueError(f"expected one canonical QQQ level-2 deep-blue episode for {event_date}, found {len(matches)}")
    return matches[0]


def _read_csv(path: Path) -> Iterator[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def _count_rows(path: Path) -> int:
    return sum(1 for _ in _read_csv(path))


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    if len(normalized) >= 5 and normalized[-5] in "+-" and normalized[-3] != ":":
        normalized = normalized[:-2] + ":" + normalized[-2:]
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp lacks timezone offset: {value}")
    return parsed.astimezone(ET)


def _manifest_records(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "coverage_manifest.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"Theta coverage manifest missing: {path}")
    records: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            rel = str(row.get("response_path", "")).replace("\\", "/").lstrip("./")
            if rel:
                records[rel] = row
    return records


def _verify_inventory(
    root: Path,
    records: dict[str, dict[str, Any]],
    relative_files: tuple[str, ...] = PILOT_FILES,
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for relative in relative_files:
        path = root / Path(relative)
        if not path.is_file():
            raise FileNotFoundError(f"pilot data file missing: {path}")
        manifest = records.get(relative)
        if manifest is None or manifest.get("status") != "downloaded":
            raise ValueError(f"pilot manifest row missing or incomplete: {relative}")
        rows = _count_rows(path)
        expected_rows = int(manifest.get("rows", -1))
        if rows != expected_rows:
            raise ValueError(f"manifest/data row-count mismatch for {relative}: {expected_rows} vs {rows}")
        inventory.append({
            "path": str(path),
            "dataset": manifest.get("dataset"),
            "request_date": manifest.get("request_date"),
            "status": manifest.get("status"),
            "rows": rows,
            "contracts_manifest": manifest.get("contracts"),
            "compressed_bytes": path.stat().st_size,
        })
    return inventory


def _entry_selection(
    path: Path,
    *,
    entry_time: datetime = ENTRY_TIME,
    expiration: str = EXPIRATION,
) -> tuple[float, dict[str, float], dict[str, Any], dict[float, dict[str, float | None]]]:
    rows_at_entry: list[dict[str, str]] = []
    all_rights_at_entry = 0
    row_count = 0
    for row in _read_csv(path):
        row_count += 1
        try:
            at_entry = _timestamp(row["timestamp"]) == entry_time
        except (KeyError, ValueError):
            at_entry = False
        if at_entry:
            all_rights_at_entry += 1
            if row.get("right", "").upper() == "CALL" and row.get("expiration") == expiration:
                rows_at_entry.append(row)
    synchronized: list[tuple[float, float]] = []
    unsynchronized = 0
    for row in rows_at_entry:
        strike = _number(row.get("strike"))
        spot = _number(row.get("underlying_price"))
        try:
            underlying_time = _timestamp(row["underlying_timestamp"])
        except (KeyError, ValueError):
            underlying_time = None
        if strike is None or spot is None or underlying_time != entry_time:
            unsynchronized += 1
            continue
        synchronized.append((strike, spot))
    spots = {spot for _, spot in synchronized}
    if not synchronized or len(spots) != 1:
        raise ValueError(f"expected one synchronized entry spot, observed {sorted(spots)}")
    spot = next(iter(spots))
    strikes = sorted({strike for strike, _ in synchronized})
    selected = {
        "atm": min(strikes, key=lambda strike: (abs(strike - spot), strike)),
        "otm_3": min((strike for strike in strikes if strike >= spot + 3.0), default=math.nan),
        "otm_10": min((strike for strike in strikes if strike >= spot + 10.0), default=math.nan),
    }
    if any(not math.isfinite(strike) for strike in selected.values()):
        raise ValueError(f"entry chain lacks one or more target strikes: {selected}")
    entry_greeks = {
        strike: {
            "implied_vol": _number(row.get("implied_vol")),
            "iv_error": _number(row.get("iv_error")),
        }
        for row in rows_at_entry
        if (strike := _number(row.get("strike"))) is not None and strike in selected.values()
    }
    details = {
        "first_order_rows_at_entry_all_rights": all_rights_at_entry,
        "first_order_call_rows_at_entry": len(rows_at_entry),
        "synchronized_call_rows": len(synchronized),
        "unsynchronized_call_rows": unsynchronized,
        "first_order_file_rows": row_count,
        "underlying_timestamp": entry_time.isoformat(),
        "underlying_spot": spot,
        "strike_rule": "ATM minimizes absolute strike distance (ties choose lower strike); OTM selects the lowest listed call strike at or above spot + $3 / spot + $10.",
        "actual_strikes": selected,
    }
    return spot, selected, details, entry_greeks


def _quote_quality(row: dict[str, str]) -> tuple[bool, list[str], dict[str, float | None]]:
    values = {name: _number(row.get(name)) for name in ("bid", "ask", "bid_size", "ask_size")}
    reasons: list[str] = []
    if any(value is None for value in values.values()):
        reasons.append("nonfinite_or_missing_quote_field")
    bid, ask = values["bid"], values["ask"]
    bid_size, ask_size = values["bid_size"], values["ask_size"]
    if bid_size is not None and bid_size <= 0 or ask_size is not None and ask_size <= 0:
        reasons.append("nonpositive_quote_size")
    if bid is not None and bid < 0 or ask is not None and ask < 0:
        reasons.append("negative_price")
    if ask is not None and ask <= 0:
        reasons.append("zero_or_negative_ask")
    if bid is not None and ask is not None and bid > ask:
        reasons.append("crossed_market")
    return not reasons, reasons, values


def _bid_mark_quality(
    row: dict[str, str],
) -> tuple[bool, bool, bool, bool, dict[str, float | None]]:
    """Return mark-quality, executable-bid, economic-zero, and no-execution flags."""
    _, reasons, values = _quote_quality(row)
    bid, ask = values["bid"], values["ask"]
    mark_eligible = (
        bid is not None
        and bid >= 0
        and ask is not None
        and ask > 0
        and values["ask_size"] is not None
        and values["ask_size"] > 0
        and "nonfinite_or_missing_quote_field" not in reasons
        and "negative_price" not in reasons
        and "crossed_market" not in reasons
    )
    executable_bid = mark_eligible and values["bid_size"] is not None and values["bid_size"] > 0
    economic_zero = mark_eligible and bid == 0.0
    no_executable_bid = mark_eligible and not executable_bid
    return mark_eligible, executable_bid, economic_zero, no_executable_bid, values


def _quote_file(path: Path, selected_strikes: set[float]) -> tuple[dict[str, Any], dict[tuple[float, str], dict[datetime, dict[str, str]]]]:
    total = 0
    contracts: set[tuple[float, str]] = set()
    zero_ask_times: Counter[str] = Counter()
    crossed = zero_ask = zero_bid = no_executable_bid = zero_bid_positive_size = 0
    nonfinite = nonpositive_size = valid = 0
    selected: dict[tuple[float, str], dict[datetime, dict[str, str]]] = {}
    for row in _read_csv(path):
        total += 1
        strike = _number(row.get("strike"))
        right = row.get("right", "").upper()
        if strike is not None:
            contracts.add((strike, right))
        quality, reasons, values = _quote_quality(row)
        try:
            stamp_text = _timestamp(row["timestamp"]).isoformat(timespec="seconds")
        except (KeyError, ValueError):
            stamp_text = "unparsed"
        crossed += "crossed_market" in reasons
        if values["ask"] == 0:
            zero_ask += 1
            zero_ask_times[stamp_text] += 1
        if values["bid"] == 0:
            zero_bid += 1
            if values["bid_size"] is not None and values["bid_size"] > 0:
                zero_bid_positive_size += 1
            else:
                no_executable_bid += 1
        nonfinite += "nonfinite_or_missing_quote_field" in reasons
        nonpositive_size += "nonpositive_quote_size" in reasons
        valid += quality
        if strike not in selected_strikes or right != "CALL":
            continue
        try:
            stamp = _timestamp(row["timestamp"])
        except (KeyError, ValueError):
            continue
        key = (strike, right)
        selected.setdefault(key, {})[stamp] = row
    return ({
        "rows": total,
        "contracts": len(contracts),
        "valid_rows": valid,
        "crossed_rows": crossed,
        "zero_ask_rows": zero_ask,
        "zero_ask_timestamp_counts": dict(zero_ask_times),
        "zero_bid_rows": zero_bid,
        "zero_bid_positive_size_rows": zero_bid_positive_size,
        "no_executable_bid_rows": no_executable_bid,
        "nonfinite_or_missing_quote_field_rows": nonfinite,
        "nonpositive_size_rows": nonpositive_size,
    }, selected)


def _stamp_text(stamp: datetime | None) -> str | None:
    return stamp.isoformat(timespec="seconds") if stamp is not None else None


def _single_quote(
    by_time: dict[datetime, dict[str, str]], stamp: datetime, *, entry: bool,
) -> tuple[dict[str, str] | None, list[str]]:
    row = by_time.get(stamp)
    if row is None:
        return None, []
    valid, reasons, _ = _quote_quality(row)
    if entry and (_number(row.get("ask")) or 0.0) <= 0 and "zero_or_negative_ask" not in reasons:
        reasons.append("zero_or_negative_ask")
        valid = False
    return row, ([] if valid else reasons)


def _path_excursions(
    entry_ask: float,
    entry_time: datetime,
    exit_time: datetime,
    series: list[dict[datetime, dict[str, str]]],
    entry_bid: float | None = None,
) -> dict[str, Any]:
    bids: list[tuple[datetime, float]] = []
    zero_bid_observations = 0
    no_executable_bid_observations = 0
    if entry_bid is not None and math.isfinite(entry_bid) and entry_bid >= 0:
        bids.append((entry_time, entry_bid))
        zero_bid_observations += entry_bid == 0
    for by_time in series:
        for stamp, row in by_time.items():
            if entry_time < stamp <= exit_time:
                valid, executable_bid, economic_zero, no_executable, values = _bid_mark_quality(row)
                if valid and values["bid"] is not None:
                    bids.append((stamp, values["bid"]))
                    zero_bid_observations += economic_zero
                    no_executable_bid_observations += no_executable
    if not bids:
        return {
            "valid_bid_observations": 0,
            "immediate_entry_bid": entry_bid,
            "immediate_entry_bid_included": entry_bid is not None,
            "zero_bid_observations": zero_bid_observations,
            "no_executable_bid_observations": no_executable_bid_observations,
            "mark_only_bid_observations": no_executable_bid_observations,
            "mfe_gross_usd": None,
            "mae_gross_usd": None,
        }
    high_time, high_bid = max(bids, key=lambda item: item[1])
    low_time, low_bid = min(bids, key=lambda item: item[1])
    multiplier = CONTRACT_MULTIPLIER_ASSUMPTION
    return {
        "valid_bid_observations": len(bids),
        "immediate_entry_bid": entry_bid,
        "immediate_entry_bid_included": entry_bid is not None,
        "zero_bid_observations": zero_bid_observations,
        "no_executable_bid_observations": no_executable_bid_observations,
        "mark_only_bid_observations": no_executable_bid_observations,
        "first_observation_et": _stamp_text(min(stamp for stamp, _ in bids)),
        "last_observation_et": _stamp_text(max(stamp for stamp, _ in bids)),
        "mfe_best_bid": high_bid,
        "mfe_time_et": _stamp_text(high_time),
        "mfe_gross_usd": round((high_bid - entry_ask) * multiplier, 2),
        "mfe_premium_return_pct": round((high_bid / entry_ask - 1) * 100, 4),
        "mae_worst_bid": low_bid,
        "mae_time_et": _stamp_text(low_time),
        "mae_gross_usd": round((low_bid - entry_ask) * multiplier, 2),
        "mae_premium_return_pct": round((low_bid / entry_ask - 1) * 100, 4),
    }


def build_pilot(
    data_root: Path = DEFAULT_DATA_ROOT,
    commission_round_trip: float = 0.0,
    spec: PilotSpec | None = None,
    pi_path: Path | None = None,
) -> dict[str, Any]:
    """Compute one-contract ask-to-bid outcomes for one canonical PI episode."""
    if not math.isfinite(commission_round_trip) or commission_round_trip < 0:
        raise ValueError("commission_round_trip must be finite and nonnegative")
    root = Path(data_root)
    requested_spec = spec or DEFAULT_SPEC
    resolved_spec, pi_provenance = canonical_episode_for_date(requested_spec.event_date, pi_path)
    relative_files = pilot_files(resolved_spec)
    manifest = _manifest_records(root)
    inventory = _verify_inventory(root, manifest, relative_files)
    event_greeks = root / relative_files[3]
    entry_time = resolved_spec.entry_datetime
    exit_times = resolved_spec.exit_datetimes
    spot, selected, selection_details, entry_greeks = _entry_selection(
        event_greeks,
        entry_time=entry_time,
        expiration=resolved_spec.expiration,
    )
    strikes = set(selected.values())
    quote_diagnostics: dict[str, dict[str, Any]] = {}
    quote_series: dict[str, dict[tuple[float, str], dict[datetime, dict[str, str]]]] = {}
    for label, relative in (
        (resolved_spec.event_date, relative_files[2]),
        (resolved_spec.exit_date, relative_files[4]),
    ):
        quote_diagnostics[label], quote_series[label] = _quote_file(root / relative, strikes)

    outcomes: list[dict[str, Any]] = []
    for label, strike in selected.items():
        key = (strike, "CALL")
        event_quotes = quote_series[resolved_spec.event_date].get(key, {})
        next_quotes = quote_series[resolved_spec.exit_date].get(key, {})
        entry_row, entry_reasons = _single_quote(event_quotes, entry_time, entry=True)
        entry_present = entry_row is not None
        entry_valid = entry_present and not entry_reasons
        entry_values = _quote_quality(entry_row)[2] if entry_valid and entry_row else {}
        entry_ask = entry_values.get("ask") if entry_values else None
        outcome: dict[str, Any] = {
            "selection": label,
            "right": "CALL",
            "strike": strike,
            "spot_at_entry": spot,
            "moneyness_dollars_at_entry": round(strike - spot, 4),
            "entry_time_et": _stamp_text(entry_time),
            "entry_quote_missing": not entry_present,
            "entry_quote_invalid": bool(entry_present and entry_reasons),
            "entry_invalid_reasons": entry_reasons,
            "entry_bid": entry_values.get("bid") if entry_values else None,
            "entry_ask": entry_ask,
            "entry_bid_size": entry_values.get("bid_size") if entry_values else None,
            "entry_ask_size": entry_values.get("ask_size") if entry_values else None,
            "entry_iv": entry_greeks.get(strike, {}).get("implied_vol"),
            "entry_iv_error": entry_greeks.get(strike, {}).get("iv_error"),
            "exits": {},
        }
        for exit_name, exit_time in exit_times.items():
            exit_row, exit_reasons = _single_quote(next_quotes, exit_time, entry=False)
            exit_present = exit_row is not None
            exit_values = _quote_quality(exit_row)[2] if exit_present and exit_row else {}
            exit_bid = exit_values.get("bid") if exit_values else None
            mark_eligible = False
            executable_bid = False
            economic_zero_bid = False
            no_executable_bid = False
            if exit_present and exit_row:
                mark_eligible, executable_bid, economic_zero_bid, no_executable_bid, _ = _bid_mark_quality(exit_row)
            pnl_gross = ((exit_bid - entry_ask) * CONTRACT_MULTIPLIER_ASSUMPTION
                         if entry_ask is not None and exit_bid is not None and mark_eligible else None)
            premium_return = ((exit_bid / entry_ask - 1) * 100
                              if entry_ask is not None and entry_ask > 0 and exit_bid is not None and mark_eligible else None)
            outcome["exits"][exit_name] = {
                "scheduled_time_et": _stamp_text(exit_time),
                "exit_quote_missing": not exit_present,
                "exit_quote_invalid": bool(exit_present and exit_reasons),
                "exit_invalid_reasons": exit_reasons,
                "exit_bid": exit_bid,
                "exit_ask": exit_values.get("ask") if exit_values else None,
                "exit_bid_size": exit_values.get("bid_size") if exit_values else None,
                "exit_ask_size": exit_values.get("ask_size") if exit_values else None,
                "exit_mark_eligible": mark_eligible,
                "exit_bid_executable": executable_bid,
                "exit_mark_only": bool(mark_eligible and not executable_bid),
                "economic_zero_bid_observed": economic_zero_bid,
                "no_executable_bid": no_executable_bid,
                "zero_exit_bid_retained": bool(mark_eligible and economic_zero_bid),
                "gross_pnl_per_contract_usd": round(pnl_gross, 2) if pnl_gross is not None else None,
                "premium_return_pct_gross": round(premium_return, 4) if premium_return is not None else None,
                "pnl_after_configured_commission_usd": round(pnl_gross - commission_round_trip, 2)
                if pnl_gross is not None else None,
                "configured_round_trip_commission_usd": commission_round_trip,
                "pnl_after_1_30_commission_sensitivity_usd": round(pnl_gross - ROUND_TRIP_COMMISSION_SENSITIVITY, 2)
                if pnl_gross is not None else None,
                "premium_return_pct_after_1_30_sensitivity": round(
                    ((pnl_gross - ROUND_TRIP_COMMISSION_SENSITIVITY)
                     / (entry_ask * CONTRACT_MULTIPLIER_ASSUMPTION) * 100), 4,
                ) if pnl_gross is not None and entry_ask else None,
                "excursions_from_entry_ask_to_scheduled_exit": _path_excursions(
                    float(entry_ask), entry_time, exit_time,
                    [event_quotes, next_quotes],
                    entry_bid=float(outcome["entry_bid"]) if outcome["entry_bid"] is not None else None,
                ) if entry_ask is not None else {"valid_bid_observations": 0},
            }
        outcomes.append(outcome)

    total_compressed = sum(item["compressed_bytes"] for item in inventory)
    total_rows = sum(item["rows"] for item in inventory)
    return {
        "study": "single-event QQQ PI level-2 deep-blue long-call bid/ask pilot",
        "event_date": resolved_spec.event_date,
        "event_time_et": resolved_spec.event_time_et,
        "entry_time_et": resolved_spec.entry_time_et,
        "exit_date": resolved_spec.exit_date,
        "expiration": resolved_spec.expiration,
        "pi_provenance": pi_provenance,
        "data_root": str(root),
        "manifest_files_verified_downloaded": len(inventory),
        "inventory_rows": total_rows,
        "compressed_bytes": total_compressed,
        "inventory": inventory,
        "selection": selection_details,
        "quote_diagnostics_by_date": quote_diagnostics,
        "quote_age_status": "unknown; 1-minute snapshot timestamps do not expose last underlying NBBO update time",
        "contract_multiplier_assumption": CONTRACT_MULTIPLIER_ASSUMPTION,
        "contract_multiplier_definition_verified": False,
        "configured_round_trip_commission_usd_per_contract": commission_round_trip,
        "gross_is_default": commission_round_trip == 0.0,
        "round_trip_commission_sensitivity_usd_per_contract": ROUND_TRIP_COMMISSION_SENSITIVITY,
        "results": outcomes,
        "interpretation": "Retrospective single-event source-time pilot. One-contract long-call ask-to-bid marks; no event-study win-rate or best-contract claim.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--event-date", default=EVENT_DATE,
                        help="canonical QQQ episode date; source and next-minute timestamps are derived from PI history")
    parser.add_argument("--pi-path", type=Path, default=None,
                        help="optional canonical PI archive path")
    parser.add_argument("--commission-round-trip", type=float, default=0.0,
                        help="commission dollars per contract per round trip (default 0.00 gross P&L)")
    parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    args = parser.parse_args()
    resolved_spec, _ = canonical_episode_for_date(args.event_date, args.pi_path)
    result = build_pilot(
        args.data_root,
        args.commission_round_trip,
        spec=resolved_spec,
        pi_path=args.pi_path,
    )
    print(json.dumps(result, indent=None if args.compact else 2, sort_keys=True))


if __name__ == "__main__":
    main()
