"""Research PI continuation across the daily 15:45–18:00 ET close window.

This runner compares the current production BacktestEngine path with one
ATR-blend re-entry during the first five minutes after the same-day 18:00 ET
reopen, limited to a Level-2 bullish bubble (深蓝圈). The continuation only
arms when that bot-owned trade survives into the 15:45 ET flatten. An original
stop/target touch while flat cancels the ticket. Optional gap caps are expressed
in the original trade's R and are diagnostic candidates.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.db.models import (  # noqa: E402
    Candle,
    Direction,
    ExitReason,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.exit_policy import ExitPolicy, ExitTrailMode  # noqa: E402
from backend.strategy.session_filter import (  # noqa: E402
    MARKET_PHASE_FLATTEN,
    as_new_york,
    is_market_reopen,
    market_close_phase,
)
from backend.timebase import as_utc  # noqa: E402
from scripts.pi_exhaustive_exit_study import (  # noqa: E402
    CURRENT_EXIT,
    DEFAULT_VALIDATED_STORE,
    SignalSpec,
    _configure_params,
    _load_run_candles,
)
from scripts.pi_exit_engine_grid_study import (  # noqa: E402
    GridPiSignalStrategy,
    _load_base_params,
    _robustness,
    trade_rows,
)


PT = ZoneInfo("America/Los_Angeles")
GAP_CAPS: tuple[float | None, ...] = (None, 0.5, 1.0, 2.0)
LV2_LONG = SignalSpec(
    key="long_level2_only",
    label="LONG Level 2 bullish bubble only",
    long_kinds=("深蓝圈",),
    short_kinds=(),
    long_only=True,
)


def _utc_iso(value: datetime) -> str:
    return as_utc(value).astimezone(timezone.utc).isoformat()


class ReopenCarryEngine(BacktestEngine):
    """Backtest adapter that arms one PI signal for the next same-day reopen."""

    def __init__(self, *args, max_gap_r: float | None, **kwargs):
        self.max_gap_r = max_gap_r
        self.carry_ticket: dict[str, Any] | None = None
        self.carry_events: list[dict[str, Any]] = []
        super().__init__(*args, **kwargs)

    def _reset(self):
        super()._reset()
        self.carry_ticket = None
        self.carry_events = []

    @staticmethod
    def _is_lv2_bull(position) -> bool:
        meta = getattr(position, "meta", None) or {}
        pi = meta.get("pi") if isinstance(meta, dict) else None
        return (
            getattr(position, "direction", None) == Direction.BUY
            and isinstance(pi, dict)
            and pi.get("kind") == "深蓝圈"
            and not meta.get("pi_continuation")
        )

    def _process_candle(self, candle: Candle):
        ticket = self.carry_ticket
        if ticket is not None and candle.timestamp > ticket["flat_time"]:
            direction = ticket["direction"]
            stop_hit = (
                candle.low <= ticket["original_sl"]
                if direction == Direction.BUY
                else candle.high >= ticket["original_sl"]
            )
            target_hit = (
                candle.high >= ticket["tp"]
                if direction == Direction.BUY
                else candle.low <= ticket["tp"]
            )
            if stop_hit or target_hit:
                self.carry_events.append({
                    "event": "invalidated",
                    "reason": "original stop touched while flat" if stop_hit else "original target reached while flat",
                    "time_utc": _utc_iso(candle.timestamp),
                })
                self.carry_ticket = None
        position = self._open_position
        should_warm_atr = (
            self.carry_ticket is not None and not is_market_reopen(candle.timestamp)
        ) or (
            position is not None and self._is_lv2_bull(position)
        )
        if self.strategy_mode == "pi" and should_warm_atr:
            # The shared live path also observes closed bars while a continuation
            # ticket is active; blocked bars still update ATR, never enter.
            self.trend_follow.observe(candle, [], True)
        super()._process_candle(candle)

    def _force_exit(self, candle: Candle, reason: ExitReason):
        position = self._open_position
        if (
            position is not None
            and reason == ExitReason.FLATTEN
            and market_close_phase(candle.timestamp) == MARKET_PHASE_FLATTEN
            and self._is_lv2_bull(position)
            and self.carry_ticket is None
        ):
            entry = float(position.entry_price)
            original_sl = float(
                getattr(position, "original_sl_price", None) or position.sl_price
            )
            risk = abs(entry - original_sl)
            meta = copy.deepcopy(getattr(position, "meta", None) or {})
            self.carry_ticket = {
                "direction": position.direction,
                "entry": entry,
                "risk": risk,
                "original_sl": original_sl,
                "tp": float(position.original_tp_price or position.tp_price),
                "flat_price": float(candle.close),
                "flat_time": candle.timestamp,
                "pi": copy.deepcopy(meta.get("pi") or {}),
                "signal_reason": str(meta.get("signal_reason") or "PI Level 2 bullish bubble"),
            }
            self.carry_events.append({
                "event": "armed",
                "flat_time_utc": _utc_iso(candle.timestamp),
                "flat_time_pt": as_utc(candle.timestamp).astimezone(PT).isoformat(),
                "entry": entry,
                "flat_price": float(candle.close),
                "risk": risk,
            })
        super()._force_exit(candle, reason)

    def take_reopen_ticket(self, candle: Candle) -> dict[str, Any] | None:
        ticket = self.carry_ticket
        if ticket is None:
            return None
        current_ny = as_new_york(candle.timestamp)
        source_ny = as_new_york(ticket["flat_time"])
        if current_ny.date() > source_ny.date():
            self.carry_events.append({
                "event": "expired",
                "reason": "no same-day reopen bar; weekend/holiday carry disabled",
                "flat_time_utc": _utc_iso(ticket["flat_time"]),
            })
            self.carry_ticket = None
            return None
        if current_ny.date() != source_ny.date() or not is_market_reopen(candle.timestamp):
            return None
        self.carry_ticket = None
        return ticket


class ReopenGridPiSignalStrategy(GridPiSignalStrategy):
    def __init__(self, params, variant, owner: ReopenCarryEngine):
        self.owner = owner
        super().__init__(params, variant)

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        ticket = self.owner.take_reopen_ticket(candle)
        if ticket is None:
            return super().evaluate(candle, zones, is_mature)

        self._roll(candle)
        entry = self._round(float(candle.close))
        gap_r = (
            abs(entry - ticket["flat_price"]) / ticket["risk"]
            if ticket["risk"] > 0 else float("inf")
        )
        if self.owner.max_gap_r is not None and gap_r > self.owner.max_gap_r:
            self.owner.carry_events.append({
                "event": "skipped",
                "reason": "reopen gap exceeded R cap",
                "reopen_time_utc": _utc_iso(candle.timestamp),
                "reopen_time_pt": as_utc(candle.timestamp).astimezone(PT).isoformat(),
                "gap_r": round(gap_r, 5),
                "max_gap_r": self.owner.max_gap_r,
            })
            return None

        width = self._atr_blend()
        if width is None or width <= 0:
            self.owner.carry_events.append({
                "event": "skipped",
                "reason": "ATR blend unavailable at reopen",
                "reopen_time_utc": _utc_iso(candle.timestamp),
                "gap_r": round(gap_r, 5),
            })
            return None
        if ticket["direction"] == Direction.SELL and self.sl_atr > 0:
            width *= float(getattr(self.params, "pi_short_sl_value", 2.5)) / self.sl_atr
        signal = self._make(
            candle,
            ticket["direction"],
            f"{ticket['signal_reason']} | REOPEN CONTINUATION",
            width=width,
        )
        if signal is None:
            self.owner.carry_events.append({
                "event": "skipped",
                "reason": "PI strategy rejected continuation bracket",
                "reopen_time_utc": _utc_iso(candle.timestamp),
                "gap_r": round(gap_r, 5),
            })
            return None

        signal.meta["pi"] = copy.deepcopy(ticket["pi"])
        signal.meta["pi_continuation"] = {
            "source_flat_time": _utc_iso(ticket["flat_time"]),
            "reopen_time": _utc_iso(candle.timestamp),
            "source_entry": ticket["entry"],
            "source_flat_price": ticket["flat_price"],
            "reopen_gap_r": round(gap_r, 5),
            "atr_blend": round(float(width), 6),
            "reentries_for_source": 1,
        }
        hold = (
            self._grid_variant.long_hold
            if signal.direction == Direction.BUY
            else self._grid_variant.short_hold
        )
        signal.exit_policy = ExitPolicy(
            model="pi",
            max_hold_minutes=max(0, int(hold)),
            hard_tp_enabled=bool(self._grid_variant.hard_tp),
            trail_mode=ExitTrailMode.NONE,
        )
        self.owner.carry_events.append({
            "event": "reentered",
            "reopen_time_utc": _utc_iso(candle.timestamp),
            "reopen_time_pt": as_utc(candle.timestamp).astimezone(PT).isoformat(),
            "entry": entry,
            "gap_r": round(gap_r, 5),
            "atr_blend": round(float(width), 6),
        })
        return signal


def _make_config(params, symbol: str) -> BacktestConfig:
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=50_000.0,
        symbol=symbol,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )


def _run_variant(params, symbol: str, candles: list[Candle], cap: float | None):
    variant_params = copy.deepcopy(params)
    variant_params.pi_continue_long_kinds = ["深蓝圈"]
    variant_params.pi_continue_short_kinds = []
    variant_params.pi_continue_short_levels = []
    variant_params.pi_reopen_max_gap_r = 0.0 if cap is None else float(cap)
    engine = BacktestEngine(
        config=_make_config(variant_params, symbol),
        strategy_params=variant_params,
        record_equity=False,
    )
    result = engine.run(candles)
    rows = trade_rows(result.trades, symbol)
    continuations = [
        trade for trade in result.trades
        if (getattr(trade, "meta", None) or {}).get("pi_continuation")
    ]
    cont_rows = trade_rows(continuations, symbol)
    return {
        "all": _robustness(rows),
        "continuations": _robustness(cont_rows),
        "trades": len(result.trades),
        "continuation_trades": len(continuations),
        "events": engine.pi_reopen_events,
    }


def _run_baseline(params, symbol: str, candles: list[Candle]):
    engine = BacktestEngine(
        config=_make_config(params, symbol),
        strategy_params=params,
        record_equity=False,
    )
    result = engine.run(candles)
    return _robustness(trade_rows(result.trades, symbol)), len(result.trades)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store-dir", default=str(DEFAULT_VALIDATED_STORE))
    parser.add_argument("--symbols", nargs="+", choices=("MNQ", "MES"), default=("MNQ", "MES"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    candle_store.STORE_DIR = Path(args.store_dir)
    rows = __import__("backend.data.pi_history", fromlist=["load_rows"]).load_rows()
    report: dict[str, Any] = {
        "study": "PI Level-2 bullish bubble daily reopen continuation",
        "generated_at": datetime.now().astimezone().isoformat(),
        "store_dir": str(Path(args.store_dir)),
        "signal": "long 深蓝圈 (Level 2)",
        "continuation_rule": (
            "one re-entry in the first five minutes after the same-day 18:00 ET reopen, "
            "only when the original bot-owned position survives to 15:45 ET flatten; "
            "a stop/target touch while flat, weekend carry, or a late reopen cancels the ticket"
        ),
        "bracket_rule": "recalculate SL/TP from the ATR blend observed through the close/reopen window",
        "gap_caps_r": list(GAP_CAPS),
        "symbols": {},
    }

    for symbol in args.symbols:
        candles, coverage = _load_run_candles(symbol, rows)
        base = _load_base_params(symbol)
        params = _configure_params(base, CURRENT_EXIT, LV2_LONG)
        baseline, baseline_n = _run_baseline(params, symbol, candles)
        variants = []
        for cap in GAP_CAPS:
            result = _run_variant(params, symbol, candles, cap)
            variants.append({"max_gap_r": cap, **result})
        report["symbols"][symbol] = {
            "coverage": coverage,
            "baseline": baseline,
            "baseline_trades": baseline_n,
            "variants": variants,
        }

    output_dir = market_data.derived_path("research")
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"pi_reopen_continuation_lv2_{stamp}.json"
    csv_path = output_dir / f"pi_reopen_continuation_lv2_{stamp}.csv"
    md_path = output_dir / f"pi_reopen_continuation_lv2_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    csv_rows = []
    md = [
        "# PI Level-2 bullish bubble reopen-continuation study",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Store: `{report['store_dir']}`",
        f"Rule: {report['continuation_rule']}.",
        f"Bracket: {report['bracket_rule']}.",
        "",
        "| Symbol | Variant | trades | PnL | PF | 14t PF | continue n | continue PnL | continue PF |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for symbol, data in report["symbols"].items():
        base = data["baseline"]["baseline"]
        base_stress = data["baseline"]["stress_14t"]
        md.append(
            f"| {symbol} | baseline | {base['n']} | ${base['pnl']:.2f} | "
            f"{base['pf']:.3f} | {base_stress['pf']:.3f} | 0 | $0.00 | n/a |"
        )
        csv_rows.append({
            "symbol": symbol, "variant": "baseline", "max_gap_r": "",
            "trades": data["baseline_trades"], "pnl": base["pnl"],
            "pf": base["pf"], "stress_14t_pf": base_stress["pf"],
            "continuation_trades": 0, "continuation_pnl": 0.0, "continuation_pf": None,
        })
        for item in data["variants"]:
            all_base = item["all"]["baseline"]
            all_stress = item["all"]["stress_14t"]
            c_base = item["continuations"]["baseline"]
            c_stress = item["continuations"]["stress_14t"]
            label = "unlimited" if item["max_gap_r"] is None else f"{item['max_gap_r']}R cap"
            md.append(
                f"| {symbol} | {label} | {all_base['n']} | ${all_base['pnl']:.2f} | "
                f"{all_base['pf']:.3f} | {all_stress['pf']:.3f} | "
                f"{c_base['n']} | ${c_base['pnl']:.2f} | {c_base['pf']:.3f} |"
            )
            csv_rows.append({
                "symbol": symbol, "variant": "continuation", "max_gap_r": item["max_gap_r"],
                "trades": item["trades"], "pnl": all_base["pnl"], "pf": all_base["pf"],
                "stress_14t_pf": all_stress["pf"], "continuation_trades": item["continuation_trades"],
                "continuation_pnl": c_base["pnl"], "continuation_pf": c_base["pf"],
            })
    md.extend(["", "## Event details", ""])
    for symbol, data in report["symbols"].items():
        md.append(f"### {symbol}")
        md.append("")
        md.append(f"Coverage: `{data['coverage']['first_signal']}` → `{data['coverage']['last_signal']}`; {data['coverage']['bars']:,} candles.")
        for item in data["variants"]:
            label = "unlimited" if item["max_gap_r"] is None else f"{item['max_gap_r']}R"
            md.append(f"- {label}: " + "; ".join(
                f"{event['event']}" + (f" ({event.get('reason')})" if event.get("reason") else "")
                + (f" gap={event.get('gap_r')}R" if event.get("gap_r") is not None else "")
                for event in item["events"]
            ))
        md.append("")
    md.append("Sample covers fewer than 12 months; all variant results remain exploratory.")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)

    print(f"Saved {json_path}")
    print(f"Saved {csv_path}")
    print(f"Saved {md_path}")
    for symbol, data in report["symbols"].items():
        base = data["baseline"]["baseline"]
        print(f"{symbol} baseline n={base['n']} PnL=${base['pnl']:.2f} PF={base['pf']:.3f}")
        for item in data["variants"]:
            stats = item["continuations"]["baseline"]
            label = "unlimited" if item["max_gap_r"] is None else f"{item['max_gap_r']}R"
            print(f"  {label}: continuation n={stats['n']} PnL=${stats['pnl']:.2f} PF={stats['pf']:.3f}")


if __name__ == "__main__":
    main()
