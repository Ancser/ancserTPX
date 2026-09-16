"""Exhaustive PI signal/exit research using the production backtest engine.

This is a research runner only.  It does not change the selected preset,
live configuration, or canonical market-data files.

The study has two complementary parts:

* all non-empty subsets of the three long-side PI marks crossed with all
  declared short-side choices, including purple Level 1 and Level 2;
* a full exit grid for the isolated signal families and the current set.

The first part answers whether a combination is hiding a weak mark.  The
second part answers whether a mark is being entered correctly but paid badly
by the current exit geometry.  Every run uses the production
``BacktestEngine`` and the shared PI history loader.  Fixed-distance exits
are deliberately counterfactual and are never promoted automatically.

The default store is the 2026-09-14 reconciliation snapshot.  The current
working store was found to contain a later synthetic MNQ MNQ->NQ seam at
2026-08-31 00:00Z; using the validated snapshot keeps that defect out of the
comparison.  Pass ``--store-dir`` explicitly to audit another snapshot.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
from bisect import bisect_left, bisect_right
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.data.pi_history import load_rows  # noqa: E402
from backend.db.models import (  # noqa: E402
    Candle,
    Direction,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
    get_tick_size,
)
from backend.timebase import UTC, as_utc  # noqa: E402
from scripts.pi_exit_engine_grid_study import (  # noqa: E402
    GridPiSignalStrategy,
    _configure_params as _legacy_configure_params,
    _exit_counts,
    _finite,
    _group_stats,
    _kind,
    _load_base_params,
    _load_run_candles,
    _monthly_stats,
    _robustness,
    _status,
    _direction,
    trade_rows,
)


STUDY_VERSION = "2026-09-15-pi-exhaustive-exit-v1"
INITIAL_CAPITAL = 50_000.0
PI_SYMBOLS = ("MNQ", "MES")
PI_SOURCE_SYMBOL = {"MNQ": "QQQ", "MES": "SPY"}
DEFAULT_VALIDATED_STORE = Path(
    r"F:\ancserQuant\ancserMarketData\archive\futures_reconciliation_20260914T174740Z"
)
DEFAULT_WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))


@dataclass(frozen=True)
class ExitSpec:
    """One pre-declared exit counterfactual.

    ATR variants use ``long_sl``/``short_sl`` as ATR-blend multipliers and a
    shared ``rr``.  Fixed variants use tick distances and still use the same
    production engine for fills, costs, session flattening, and overlap
    handling.
    """

    key: str
    label: str
    mode: str = "atr"
    long_sl: float = 4.0
    rr: float = 3.0
    short_sl: float = 1.5
    long_hold: int = 0
    short_hold: int = 60
    hard_tp: bool = True
    fixed_sl_ticks: int = 0
    fixed_tp_ticks: int = 0


@dataclass(frozen=True)
class SignalSpec:
    key: str
    label: str
    long_kinds: tuple[str, ...]
    short_kinds: tuple[str, ...]
    short_levels: Optional[tuple[int, ...]] = None
    long_only: bool = False


CURRENT_EXIT = ExitSpec(
    key="current",
    label="CURRENT · L4 / TP3R / S1.5 / short60",
)


def _atr(
    key: str,
    label: str,
    long_sl: float,
    rr: float,
    short_sl: float,
    long_hold: int = 0,
    short_hold: int = 60,
    hard_tp: bool = True,
) -> ExitSpec:
    return ExitSpec(
        key=key,
        label=label,
        mode="atr",
        long_sl=long_sl,
        rr=rr,
        short_sl=short_sl,
        long_hold=long_hold,
        short_hold=short_hold,
        hard_tp=hard_tp,
    )


def _fixed(
    key: str,
    label: str,
    sl_ticks: int,
    tp_ticks: int,
    long_hold: int = 0,
    short_hold: int = 60,
) -> ExitSpec:
    return ExitSpec(
        key=key,
        label=label,
        mode="fixed",
        long_sl=4.0,
        rr=tp_ticks / sl_ticks if sl_ticks else 0.0,
        short_sl=4.0,
        long_hold=long_hold,
        short_hold=short_hold,
        fixed_sl_ticks=sl_ticks,
        fixed_tp_ticks=tp_ticks,
    )


# Keep the older grid so this report remains comparable with the earlier
# production-engine report, then add the exact cases requested by the user.
EXIT_VARIANTS: tuple[ExitSpec, ...] = (
    CURRENT_EXIT,
    _atr("rr1", "RR1 · L4 / S1.5", 4.0, 1.0, 1.5),
    _atr("rr1_5", "RR1.5 · L4 / S1.5", 4.0, 1.5, 1.5),
    _atr("rr2", "RR2 · L4 / S1.5", 4.0, 2.0, 1.5),
    _atr("long_sl3_rr3", "L3 / RR3 / S1.5", 3.0, 3.0, 1.5),
    _atr("long_sl3_5_rr3", "L3.5 / RR3 / S1.5", 3.5, 3.0, 1.5),
    _atr("long_sl5_rr3", "L5 / RR3 / S1.5", 5.0, 3.0, 1.5),
    _atr("short_sl2_rr3", "L4 / RR3 / S2", 4.0, 3.0, 2.0),
    _atr("short_sl2_5_rr3", "L4 / RR3 / S2.5", 4.0, 3.0, 2.5),
    _atr("short_no_time", "L4 / RR3 / short OFF", 4.0, 3.0, 1.5, 0, 0),
    _atr("short_time30", "L4 / RR3 / short30", 4.0, 3.0, 1.5, 0, 30),
    _atr("short_time120", "L4 / RR3 / short120", 4.0, 3.0, 1.5, 0, 120),
    _atr("long_time60", "L4 / RR3 / long60", 4.0, 3.0, 1.5, 60, 60),
    _atr("long_time120", "L4 / RR3 / long120", 4.0, 3.0, 1.5, 120, 60),
    _atr("long_time240", "L4 / RR3 / long240", 4.0, 3.0, 1.5, 240, 60),
    _atr(
        "no_hard_tp_time",
        "SL only + long120 / short60",
        4.0,
        3.0,
        1.5,
        120,
        60,
        False,
    ),
    _atr(
        "no_hard_tp_session",
        "SL only + session flatten",
        4.0,
        3.0,
        1.5,
        0,
        0,
        False,
    ),
    _atr("atr05_rr1", "ATR0.5 / RR1 / S0.5", 0.5, 1.0, 0.5),
    _atr("atr05_rr2", "ATR0.5 / RR2 / S0.5", 0.5, 2.0, 0.5),
    _atr("atr1_rr1", "ATR1 / RR1 / S1", 1.0, 1.0, 1.0),
    _atr("atr1_rr2", "ATR1 / RR2 / S1", 1.0, 2.0, 1.0),
    _atr("atr15_rr2", "ATR1.5 / RR2 / S1.5", 1.5, 2.0, 1.5),
    _atr("atr15_both15", "ATR1.5 / RR2 / both15", 1.5, 2.0, 1.5, 15, 15),
    _atr("current_both15", "CURRENT risk / both15", 4.0, 3.0, 1.5, 15, 15),
    _atr("current_both30", "CURRENT risk / both30", 4.0, 3.0, 1.5, 30, 30),
    _fixed("fixed40_80", "FIXED 40t / 80t / short60", 40, 80),
    _fixed("fixed80_160", "FIXED 80t / 160t / short60", 80, 160),
    _fixed("fixed80_240", "FIXED 80t / 240t / short60", 80, 240),
    _fixed("fixed120_240", "FIXED 120t / 240t / short60", 120, 240),
    _fixed("fixed80_160_both30", "FIXED 80t / 160t / both30", 80, 160, 30, 30),
)


LONG_KINDS = ("青π", "深蓝圈", "淡蓝圈")
SHORT_CHOICES: tuple[SignalSpec, ...] = (
    SignalSpec("short_none", "short OFF", (), ()),
    SignalSpec("short_pink", "粉π", (), ("粉π",)),
    SignalSpec("short_purple", "紫圈 both levels", (), ("紫圈",)),
    SignalSpec("short_purple_l1", "紫圈 Level 1", (), ("紫圈",), (1,)),
    SignalSpec("short_purple_l2", "紫圈 Level 2", (), ("紫圈",), (2,)),
    SignalSpec("short_pink_purple", "粉π + 紫圈 both", (), ("粉π", "紫圈")),
    SignalSpec(
        "short_pink_purple_l1",
        "粉π + 紫圈 Level 1",
        (),
        ("粉π", "紫圈"),
        (1,),
    ),
    SignalSpec(
        "short_pink_purple_l2",
        "粉π + 紫圈 Level 2",
        (),
        ("粉π", "紫圈"),
        (2,),
    ),
)


def _subset_kinds(mask: int) -> tuple[str, ...]:
    return tuple(kind for bit, kind in enumerate(LONG_KINDS) if mask & (1 << bit))


def _short_key(spec: SignalSpec) -> str:
    levels = "" if spec.short_levels is None else "_lv" + "".join(map(str, spec.short_levels))
    return spec.key + levels


def _kind_label(kinds: tuple[str, ...]) -> str:
    return "+".join(kinds) if kinds else "OFF"


def _build_signal_combos() -> tuple[SignalSpec, ...]:
    out: list[SignalSpec] = []
    for mask in range(1, 1 << len(LONG_KINDS)):
        longs = _subset_kinds(mask)
        for short in SHORT_CHOICES:
            if not short.short_kinds and not short.long_kinds:
                # short_none is still meaningful for a long subset.
                pass
            out.append(
                SignalSpec(
                    key=f"L{mask}_{_short_key(short)}",
                    label=f"L {_kind_label(longs)} / {_kind_label(short.short_kinds)}"
                    + (" / " + ",".join(f"Level {v}" for v in short.short_levels)
                       if short.short_levels else ""),
                    long_kinds=longs,
                    short_kinds=short.short_kinds,
                    short_levels=short.short_levels,
                    long_only=False,
                )
            )
    # Add short-only families so the matrix can prove whether an apparent
    # edge is purely long-side or purely short-side.
    for short in SHORT_CHOICES[1:]:
        out.append(
            SignalSpec(
                key=f"Loff_{_short_key(short)}",
                label=f"L OFF / {_kind_label(short.short_kinds)}"
                + (" / " + ",".join(f"Level {v}" for v in short.short_levels)
                   if short.short_levels else ""),
                long_kinds=(),
                short_kinds=short.short_kinds,
                short_levels=short.short_levels,
                long_only=False,
            )
        )
    return tuple(out)


SIGNAL_COMBOS: tuple[SignalSpec, ...] = _build_signal_combos()


FEATURE_SETS: tuple[SignalSpec, ...] = (
    SignalSpec("current", "CURRENT · 青π+深蓝 / 粉π", ("青π", "深蓝圈"), ("粉π",)),
    SignalSpec("strict", "STRICT · 青π / 粉π", ("青π",), ("粉π",)),
    SignalSpec("long_pi", "LONG PI · 青π+深蓝", ("青π", "深蓝圈"), (), long_only=True),
    SignalSpec("blue_all", "BLUE ALL · 青π+深蓝+淡蓝", LONG_KINDS, (), long_only=True),
    SignalSpec("cyan", "青π only", ("青π",), (), long_only=True),
    SignalSpec("deep_blue", "深蓝圈 only", ("深蓝圈",), (), long_only=True),
    SignalSpec("light_blue", "淡蓝圈 only", ("淡蓝圈",), (), long_only=True),
    SignalSpec("pink", "粉π only", (), ("粉π",)),
    SignalSpec("purple", "紫圈 both levels only", (), ("紫圈",)),
    SignalSpec("purple_l1", "紫圈 Level 1 only", (), ("紫圈",), (1,)),
    SignalSpec("purple_l2", "紫圈 Level 2 only", (), ("紫圈",), (2,)),
    SignalSpec("all", "ALL · blue + pink + purple", LONG_KINDS, ("粉π", "紫圈")),
)


class FixedPiSignalStrategy(GridPiSignalStrategy):
    """PI production entry with a research-only fixed bracket."""

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True):
        signal = super().evaluate(candle, zones, is_mature)
        if signal is None or self._grid_variant.mode != "fixed":
            return signal
        tick = get_tick_size(getattr(self.params, "contract_id", ""))
        sl = max(1, int(self._grid_variant.fixed_sl_ticks)) * tick
        tp = max(1, int(self._grid_variant.fixed_tp_ticks)) * tick
        entry = float(signal.entry_price)
        if signal.direction == Direction.BUY:
            signal.sl_price = self._round(entry - sl)
            signal.tp_price = self._round(entry + tp)
        else:
            signal.sl_price = self._round(entry + sl)
            signal.tp_price = self._round(entry - tp)
        signal.meta.setdefault("research_exit", {}).update(
            {
                "mode": "fixed",
                "sl_ticks": self._grid_variant.fixed_sl_ticks,
                "tp_ticks": self._grid_variant.fixed_tp_ticks,
            }
        )
        return signal


def _configure_params(base: Any, variant: ExitSpec, signals: SignalSpec) -> Any:
    # The existing helper owns all current preset defaults and keeps this
    # study aligned with the earlier reproduction.
    params = _legacy_configure_params(base, variant, signals)
    if signals.short_levels is None:
        params.pi_short_levels = None
    else:
        params.pi_short_levels = list(signals.short_levels)
    return params


def _build_engine(params: Any, variant: ExitSpec) -> BacktestEngine:
    symbol = _extract_symbol(params.contract_id)
    config = BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=symbol,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    engine = BacktestEngine(config=config, strategy_params=params, record_equity=False)
    strategy_cls = FixedPiSignalStrategy if variant.mode == "fixed" else GridPiSignalStrategy
    engine.trend_follow = strategy_cls(params, variant)
    engine._pending_max_age = engine.trend_follow.PENDING_TIMEOUT_CANDLES
    return engine


def _run(params: Any, variant: ExitSpec, candles: list[Candle]) -> list[Any]:
    return _build_engine(params, variant).run(candles).trades


def _path_diagnostics(
    trades: Iterable[Any], candles: list[Candle], variant: ExitSpec
) -> dict[str, Any]:
    """Summarize MFE/MAE and whether losses reached the original TP first.

    This is a 1-minute OHLC path diagnostic, not tick-level truth.  It is
    still useful for distinguishing an entry that never moved from an entry
    that reached a good excursion but was paid badly by the exit.
    """

    ordered = sorted(trades, key=lambda item: as_utc(item.entry_time))
    if not ordered or not candles:
        return {
            "trades_with_path": 0,
            "mfe_r_mean": 0.0,
            "mfe_r_median": 0.0,
            "mae_r_mean": 0.0,
            "mae_r_median": 0.0,
            "tp_reached": 0,
            "loss_before_tp": 0,
            "loss_after_tp": 0,
            "loss_within_5m": 0,
            "time_exit_count": 0,
        }

    stamps = [as_utc(c.timestamp) for c in candles]
    mfe_r: list[float] = []
    mae_r: list[float] = []
    tp_reached = 0
    loss_before_tp = 0
    loss_after_tp = 0
    loss_within_5m = 0
    time_exit_count = 0
    time_exit_losses = 0
    losses = 0

    for trade in ordered:
        if not trade.exit_time:
            continue
        entry_time = as_utc(trade.entry_time)
        exit_time = as_utc(trade.exit_time)
        left = bisect_left(stamps, entry_time)
        right = bisect_right(stamps, exit_time)
        if right <= left:
            continue
        path = candles[left:right]
        entry = float(trade.entry_price)
        original_sl = float(getattr(trade, "original_sl_price", None) or trade.sl_price)
        original_tp = float(getattr(trade, "original_tp_price", None) or trade.tp_price)
        risk = abs(entry - original_sl)
        if risk <= 0:
            continue
        long = _direction(getattr(trade, "direction", "")) == "long"
        if long:
            favourable = max(float(c.high) - entry for c in path)
            adverse = max(entry - float(c.low) for c in path)
            reached_tp = any(float(c.high) >= original_tp for c in path)
        else:
            favourable = max(entry - float(c.low) for c in path)
            adverse = max(float(c.high) - entry for c in path)
            reached_tp = any(float(c.low) <= original_tp for c in path)
        mfe_r.append(max(0.0, favourable) / risk)
        mae_r.append(max(0.0, adverse) / risk)
        if reached_tp:
            tp_reached += 1
        pnl = _finite(getattr(trade, "pnl", 0.0))
        if pnl < 0:
            losses += 1
            if reached_tp:
                loss_after_tp += 1
            else:
                loss_before_tp += 1
            if (exit_time - entry_time).total_seconds() <= 5 * 60:
                loss_within_5m += 1
        reason = getattr(getattr(trade, "exit_reason", None), "value", "")
        duration = (exit_time - entry_time).total_seconds() / 60.0
        hold = variant.long_hold if long else variant.short_hold
        if reason == "flatten" and hold > 0 and duration + 1e-6 >= hold:
            time_exit_count += 1
            if pnl < 0:
                time_exit_losses += 1

    n = len(mfe_r)
    return {
        "trades_with_path": n,
        "mfe_r_mean": round(sum(mfe_r) / n, 4) if n else 0.0,
        "mfe_r_median": round(median(mfe_r), 4) if n else 0.0,
        "mae_r_mean": round(sum(mae_r) / n, 4) if n else 0.0,
        "mae_r_median": round(median(mae_r), 4) if n else 0.0,
        "tp_reached": tp_reached,
        "tp_reached_rate": round(tp_reached / n, 4) if n else 0.0,
        "losses": losses,
        "loss_before_tp": loss_before_tp,
        "loss_before_tp_rate": round(loss_before_tp / losses, 4) if losses else 0.0,
        "loss_after_tp": loss_after_tp,
        "loss_within_5m": loss_within_5m,
        "time_exit_count": time_exit_count,
        "time_exit_losses": time_exit_losses,
    }


def _summary(
    trades: list[Any],
    candles: list[Candle],
    symbol: str,
    variant: ExitSpec,
    signals: SignalSpec,
    study: str,
) -> dict[str, Any]:
    rows = trade_rows(trades, symbol)
    robustness = _robustness(rows)
    status, reasons = _status(robustness)
    return {
        "study": study,
        "symbol": symbol,
        "key": f"{signals.key}__{variant.key}",
        "label": f"{signals.label} · {variant.label}",
        "status": status,
        "status_reasons": reasons,
        "exit": {
            "key": variant.key,
            "label": variant.label,
            "mode": variant.mode,
            "long_sl_atr": variant.long_sl,
            "rr": variant.rr,
            "short_sl_atr": variant.short_sl,
            "long_hold_min": variant.long_hold,
            "short_hold_min": variant.short_hold,
            "hard_tp": variant.hard_tp,
            "fixed_sl_ticks": variant.fixed_sl_ticks,
            "fixed_tp_ticks": variant.fixed_tp_ticks,
        },
        "signal_params": {
            "long_only": signals.long_only,
            "pi_long_kinds": list(signals.long_kinds),
            "pi_short_kinds": list(signals.short_kinds),
            "pi_short_levels": list(signals.short_levels) if signals.short_levels is not None else None,
        },
        "robustness": robustness,
        "diagnostics": _path_diagnostics(trades, candles, variant),
        "monthly": _monthly_stats(rows),
        "by_direction": _group_stats(rows, "direction"),
        "by_kind": _group_stats(rows, "kind"),
        "exit_counts": _exit_counts(rows),
    }


def _display(result: dict[str, Any]) -> str:
    base = result["robustness"]["baseline"]
    stress = result["robustness"]["stress_14t"]
    return (
        f"{result['symbol']} {result['study']} {result['label']}: {result['status']} "
        f"n={base['n']} PF={base['pf']:.2f} 14tPF={stress['pf']:.2f}"
    )


_WORKER: dict[str, Any] = {}


def _worker_init(symbol: str, store_dir: str) -> None:
    global _WORKER
    logging.disable(logging.CRITICAL)
    candle_store.STORE_DIR = Path(store_dir)
    rows = load_rows()
    candles, coverage = _load_run_candles(symbol, rows)
    _WORKER = {
        "symbol": symbol,
        "candles": candles,
        "coverage": coverage,
        "base": _load_base_params(symbol),
    }


def _worker_run(job: tuple[str, ExitSpec, SignalSpec]) -> dict[str, Any]:
    study, variant, signals = job
    params = _configure_params(_WORKER["base"], variant, signals)
    trades = _run(params, variant, _WORKER["candles"])
    return _summary(
        trades,
        _WORKER["candles"],
        _WORKER["symbol"],
        variant,
        signals,
        study,
    )


def _run_jobs(
    symbol: str,
    store_dir: Path,
    jobs: list[tuple[str, ExitSpec, SignalSpec]],
    workers: int,
) -> list[dict[str, Any]]:
    if not jobs:
        return []
    results: list[dict[str, Any]] = []
    if workers <= 1:
        _worker_init(symbol, str(store_dir))
        for index, job in enumerate(jobs, 1):
            result = _worker_run(job)
            results.append(result)
            print(f"[{symbol} {index}/{len(jobs)}] {_display(result)}", flush=True)
        return results

    with ProcessPoolExecutor(
        max_workers=min(workers, len(jobs)),
        initializer=_worker_init,
        initargs=(symbol, str(store_dir)),
    ) as pool:
        pending = [pool.submit(_worker_run, job) for job in jobs]
        for index, future in enumerate(as_completed(pending), 1):
            result = future.result()
            results.append(result)
            if index == 1 or index % 10 == 0 or index == len(jobs):
                print(f"[{symbol} {index}/{len(jobs)}] {_display(result)}", flush=True)
    return sorted(results, key=lambda item: item["key"])


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    base = result["robustness"]["baseline"]
    stress = result["robustness"]["stress_14t"]
    wf = result["robustness"]["walk_forward"]
    segments = wf.get("segments") or []
    diagnostics = result.get("diagnostics") or {}
    return {
        "study": result["study"],
        "symbol": result["symbol"],
        "key": result["key"],
        "label": result["label"],
        "status": result["status"],
        "n": base["n"],
        "pnl": base["pnl"],
        "pf": base["pf"],
        "stress_14t_pf": stress["pf"],
        "max_dd": base["max_dd"],
        "last_wf_pf": segments[-1].get("pf") if segments else None,
        "wf_pass": wf.get("pass"),
        "mfe_r_median": diagnostics.get("mfe_r_median"),
        "mae_r_median": diagnostics.get("mae_r_median"),
        "tp_reached_rate": diagnostics.get("tp_reached_rate"),
        "loss_before_tp_rate": diagnostics.get("loss_before_tp_rate"),
        "time_exit_count": diagnostics.get("time_exit_count"),
    }


def _write_csv(results: list[dict[str, Any]], path: Path) -> None:
    rows = [_compact_result(result) for result in results]
    fieldnames = list(rows[0]) if rows else ["study"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _table_lines(results: list[dict[str, Any]], limit: int = 30) -> list[str]:
    ranked = sorted(
        results,
        key=lambda item: (
            item["robustness"]["stress_14t"]["pf"],
            item["robustness"]["baseline"]["pf"],
            item["robustness"]["baseline"]["n"],
        ),
        reverse=True,
    )[:limit]
    lines = [
        "| Symbol | Study | Label | Status | n | PnL | PF | 14t PF | MFE(R) med | MAE(R) med | TP reached |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in ranked:
        base = item["robustness"]["baseline"]
        stress = item["robustness"]["stress_14t"]
        diag = item.get("diagnostics") or {}
        lines.append(
            f"| {item['symbol']} | {item['study']} | {item['label']} | {item['status']} | "
            f"{base['n']} | ${base['pnl']:,.0f} | {base['pf']:.2f} | {stress['pf']:.2f} | "
            f"{diag.get('mfe_r_median', 0):.2f} | {diag.get('mae_r_median', 0):.2f} | "
            f"{diag.get('tp_reached_rate', 0):.0%} |"
        )
    return lines


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# PI exhaustive signal and exit study",
        "",
        f"- Version: `{report['study_version']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Data snapshot: `{report['data_store']}`",
        "- Engine: production `BacktestEngine`; no production strategy configuration changed.",
        "- Costs: canonical commission + fees; 14-tick round-trip stress; no overlapping positions.",
        "- Diagnostics: 1-minute OHLC MFE/MAE path, not tick-level execution truth.",
        "- Fixed brackets are research counterfactuals, not a live recommendation.",
        "",
        "## Coverage and reproduction",
        "",
    ]
    for symbol, item in report["coverage"].items():
        lines.append(
            f"- `{symbol}`: {item['bars']:,} candles, PI `{item['first_signal']}` → `"
            f"{item['last_signal']}`; contract `{report['reproduction'][symbol]['contract_id']}`."
        )
    lines.append("")
    for symbol, item in report["reproduction"].items():
        base = item["baseline"]
        stress = item["stress_14t"]
        lines.append(
            f"- Reproduction `{symbol}`: match={item['match']} / n={base['n']} / "
            f"PnL=${base['pnl']:,.0f} / PF={base['pf']:.2f} / 14t PF={stress['pf']:.2f}."
        )
    lines.extend(
        [
            "",
            "## Best current-exit signal subsets",
            "",
            "The full 63-subset result is in JSON/CSV. The table below shows the top exploratory rows by stressed PF; selection on the same sample is not out-of-sample validation.",
            "",
        ]
    )
    lines.extend(_table_lines(report["signal_combo_results"], limit=40))
    lines.extend(
        [
            "",
            "## Exit grid for isolated signal families",
            "",
            "The full signal-family × exit grid is in JSON/CSV. This table is the top 50 rows by stressed PF.",
            "",
        ]
    )
    lines.extend(_table_lines(report["feature_exit_results"], limit=50))
    lines.extend(
        [
            "",
            "## Reading the diagnostics",
            "",
            "- High MFE with poor PF means the entry often moved correctly but the attached stop/target/time rule did not monetize the excursion.",
            "- Low MFE and high MAE means the mark itself did not produce a clean tradable move; changing SL/TP is unlikely to repair it without changing selection.",
            "- `loss_before_tp` counts losing trades whose 1-minute path never reached the original TP. `loss_after_tp` is the opposite and is especially relevant to no-hard-TP/time variants.",
            "- Purple Level 1 and Level 2 are separated by the structured source level. A visual `紫圈` label alone is not enough to infer that they are equivalent.",
            "- Status remains coverage-limited: the canonical PI history is under twelve months, and a same-sample winner is not automatically a durable model.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _run_reproduction(symbol: str, candles: list[Candle], base: Any) -> dict[str, Any]:
    """Reproduce the older report's current row before counterfactuals."""
    params = _configure_params(base, CURRENT_EXIT, FEATURE_SETS[0])
    config = BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=symbol,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )
    production = BacktestEngine(
        config=config,
        strategy_params=params,
        record_equity=False,
    ).run(candles).trades
    grid = _run(params, CURRENT_EXIT, candles)
    # The fixed/ATR subclass is identical to the old research subclass for
    # the current ATR case.  A mismatch is a hard stop, not a warning.
    old_fingerprint = [
        (
            as_utc(t.entry_time).isoformat(),
            as_utc(t.exit_time).isoformat() if t.exit_time else None,
            round(float(t.entry_price), 8),
            round(float(t.exit_price), 8) if t.exit_price is not None else None,
            round(_finite(t.pnl), 8),
            getattr(getattr(t, "exit_reason", None), "value", None),
            _kind(t),
        )
        for t in sorted(production, key=lambda item: as_utc(item.entry_time))
    ]
    grid_fingerprint = [
        (
            as_utc(t.entry_time).isoformat(),
            as_utc(t.exit_time).isoformat() if t.exit_time else None,
            round(float(t.entry_price), 8),
            round(float(t.exit_price), 8) if t.exit_price is not None else None,
            round(_finite(t.pnl), 8),
            getattr(getattr(t, "exit_reason", None), "value", None),
            _kind(t),
        )
        for t in sorted(grid, key=lambda item: as_utc(item.entry_time))
    ]
    if old_fingerprint != grid_fingerprint:
        raise RuntimeError(f"current PI reproduction mismatch for {symbol}")
    rows = trade_rows(production, symbol)
    rob = _robustness(rows)
    return {
        "match": True,
        "trades": len(production),
        "baseline": rob["baseline"],
        "stress_14t": rob["stress_14t"],
        "contract_id": params.contract_id,
        "contract_size": params.contract_size,
        "preset": "PI 2MNQ BOTH BEST",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="MNQ,MES", help="comma-separated symbols")
    parser.add_argument("--store-dir", default=str(DEFAULT_VALIDATED_STORE))
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--out", default="", help="external JSON output path")
    parser.add_argument(
        "--skip-combos",
        action="store_true",
        help="skip the 63 signal-subset runs and only run the feature exit grid",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.disable(logging.CRITICAL)
    symbols = tuple(
        value.strip().upper() for value in str(args.symbols).split(",") if value.strip()
    )
    invalid = set(symbols) - set(PI_SYMBOLS)
    if not symbols or invalid:
        raise SystemExit(f"symbols must be a subset of {PI_SYMBOLS}")
    store_dir = Path(args.store_dir).expanduser()
    if not store_dir.exists():
        raise SystemExit(f"store snapshot does not exist: {store_dir}")
    candle_store.STORE_DIR = store_dir
    rows = load_rows()
    if not rows:
        raise SystemExit("canonical PI history is empty")

    report: dict[str, Any] = {
        "study_version": STUDY_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "data_store": str(store_dir),
        "method": {
            "engine": "production BacktestEngine",
            "signal_combo_count_per_symbol": len(SIGNAL_COMBOS),
            "feature_set_count": len(FEATURE_SETS),
            "exit_variant_count": len(EXIT_VARIANTS),
            "stress_ticks_round_trip": 14,
            "current_preset": "PI 2MNQ BOTH BEST",
            "production_changed": False,
            "fixed_exit_note": "fixed brackets are counterfactuals; ATR variants match PI production geometry",
        },
        "coverage": {},
        "reproduction": {},
        "signal_combo_results": [],
        "feature_exit_results": [],
    }

    candles_by_symbol: dict[str, list[Candle]] = {}
    base_by_symbol: dict[str, Any] = {}
    for symbol in symbols:
        print(f"Loading {symbol} PI window...", flush=True)
        candles, coverage = _load_run_candles(symbol, rows)
        candles_by_symbol[symbol] = candles
        base_by_symbol[symbol] = _load_base_params(symbol)
        report["coverage"][symbol] = coverage
        print(
            f"  {len(candles):,} candles / {coverage['first_signal']} → {coverage['last_signal']}",
            flush=True,
        )
        started = datetime.now().timestamp()
        report["reproduction"][symbol] = _run_reproduction(
            symbol, candles, base_by_symbol[symbol]
        )
        print(
            f"  reproduction OK: n={report['reproduction'][symbol]['trades']} "
            f"PF={report['reproduction'][symbol]['baseline']['pf']:.2f} "
            f"({datetime.now().timestamp() - started:.1f}s)",
            flush=True,
        )

    for symbol in symbols:
        if not args.skip_combos:
            jobs = [("signal_combo", CURRENT_EXIT, signals) for signals in SIGNAL_COMBOS]
            report["signal_combo_results"].extend(
                _run_jobs(symbol, store_dir, jobs, max(1, int(args.workers)))
            )
        # Include current in this grid as well: it makes the signal-family
        # matrix self-contained and permits direct feature-vs-exit reading.
        jobs = [
            ("feature_exit", variant, signals)
            for signals in FEATURE_SETS
            for variant in EXIT_VARIANTS
        ]
        report["feature_exit_results"].extend(
            _run_jobs(symbol, store_dir, jobs, max(1, int(args.workers)))
        )

    output = Path(args.out).expanduser() if args.out else market_data.derived_path(
        "research", "pi_exhaustive_exit_study_20260915.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    report["signal_combo_results"] = sorted(
        report["signal_combo_results"], key=lambda item: item["key"]
    )
    report["feature_exit_results"] = sorted(
        report["feature_exit_results"], key=lambda item: item["key"]
    )
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(
        report["signal_combo_results"] + report["feature_exit_results"],
        output.with_suffix(".csv"),
    )
    _write_markdown(report, output.with_suffix(".md"))
    print(f"JSON: {output}", flush=True)
    print(f"CSV: {output.with_suffix('.csv')}", flush=True)
    print(f"Report: {output.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    main()
