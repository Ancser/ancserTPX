from __future__ import annotations

import copy
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import BacktestConfig, Direction, ExitReason, StrategyParams, TradeSignal
from backend.db.models import _extract_symbol, get_commission_rt, get_fees_rt
from backend.terminal_live import _build_strategy_params
from backend.strategy.session_filter import (
    MARKET_CLOCK_VERSION,
    MARKET_PHASE_FLATTEN,
    MARKET_PHASE_PRE_FLATTEN,
    is_market_reopen,
    market_close_phase,
)


PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def _load_best_params() -> StrategyParams:
    data = json.loads((ROOT / "data" / "presets.json").read_text(encoding="utf-8"))
    preset = data["presets"]["BEST"]
    return _build_strategy_params(preset, str(preset.get("contract_id") or ""))


def _make_config(params: StrategyParams) -> BacktestConfig:
    cid = params.contract_id
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=50_000.0,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )


def _round_tick(price: float, tick: float) -> float:
    return round(float(price) / tick) * tick


def _pt(ts: datetime | None) -> str:
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(PT).strftime("%Y-%m-%d %H:%M")


def _reason_name(reason: Any) -> str:
    value = getattr(reason, "value", reason)
    return str(value or "unknown").lower()


class NoFlattenBestEngine(BacktestEngine):
    """Upper-bound check: let BEST ignore the 12:45-15:00 close window."""

    CLOSE_WINDOW_ENABLED = False


class CarryLateBestEngine(BacktestEngine):
    """BEST only: detect close-window signals and enter at the next 18:00 ET open."""

    MAX_REOPEN_GAP_R: float | None = None

    def _reset(self):
        super()._reset()
        self._carry_signal: TradeSignal | None = None
        self._carry_source_ts: datetime | None = None
        self._carry_source_entry: float | None = None
        self._carry_late_candidates: list[dict[str, Any]] = []
        self._carry_reopen_entries: list[dict[str, Any]] = []
        self._carry_reopen_skips: list[dict[str, Any]] = []

    @staticmethod
    def _in_flatten_window(ts: datetime) -> bool:
        return market_close_phase(ts) == MARKET_PHASE_FLATTEN

    @staticmethod
    def _in_late_window(ts: datetime) -> bool:
        return market_close_phase(ts) in {
            MARKET_PHASE_PRE_FLATTEN,
            MARKET_PHASE_FLATTEN,
        }

    def _arm_carry(self, signal: TradeSignal, candle) -> None:
        if self._carry_signal is None:
            self._carry_signal = copy.deepcopy(signal)
            self._carry_source_ts = candle.timestamp
            self._carry_source_entry = float(signal.entry_price)
        self._carry_late_candidates.append({
            "signal_time_utc": candle.timestamp.isoformat(),
            "signal_time_pt": _pt(candle.timestamp),
            "direction": signal.direction.value,
            "entry": float(signal.entry_price),
            "sl": float(signal.sl_price),
            "tp": float(signal.tp_price),
            "zone_id": signal.zone_id,
        })
        self.trend_follow.notify_order_cancelled()

    def _enter_carried_signal(self, candle) -> bool:
        if self._carry_signal is None:
            return False
        signal = copy.deepcopy(self._carry_signal)
        old_entry = float(signal.entry_price)
        sl_dist = abs(old_entry - float(signal.sl_price))
        tp_dist = abs(float(signal.tp_price) - old_entry)
        entry = _round_tick(float(candle.open), self.TICK_SIZE)
        gap_ticks = (
            round((entry - float(self._carry_source_entry)) / self.TICK_SIZE, 2)
            if self._carry_source_entry is not None
            else None
        )
        gap_r = abs(entry - old_entry) / sl_dist if sl_dist > 0 else 999.0
        if self.MAX_REOPEN_GAP_R is not None and gap_r > float(self.MAX_REOPEN_GAP_R):
            self._carry_reopen_skips.append({
                "signal_time_pt": _pt(self._carry_source_ts),
                "reopen_time_pt": _pt(candle.timestamp),
                "direction": signal.direction.value,
                "original_entry": self._carry_source_entry,
                "reopen_entry": entry,
                "gap_ticks": gap_ticks,
                "gap_r": round(gap_r, 4),
                "max_gap_r": float(self.MAX_REOPEN_GAP_R),
            })
            self._carry_signal = None
            self._carry_source_ts = None
            self._carry_source_entry = None
            self.trend_follow.notify_order_cancelled()
            return False
        if signal.direction == Direction.BUY:
            signal.sl_price = _round_tick(entry - sl_dist, self.TICK_SIZE)
            signal.tp_price = _round_tick(entry + tp_dist, self.TICK_SIZE)
        else:
            signal.sl_price = _round_tick(entry + sl_dist, self.TICK_SIZE)
            signal.tp_price = _round_tick(entry - tp_dist, self.TICK_SIZE)
        signal.entry_price = entry
        signal.timestamp = candle.timestamp
        signal.reason = f"{signal.reason} | CARRY_REOPEN_18ET"
        signal.meta = dict(signal.meta or {})
        signal.meta.update({
            "carry_reopen": True,
            "carry_original_signal_time": self._carry_source_ts.isoformat() if self._carry_source_ts else None,
            "carry_original_entry": self._carry_source_entry,
            "carry_reopen_entry": entry,
            "carry_gap_ticks": gap_ticks,
            "carry_gap_r": round(gap_r, 4),
        })
        self._carry_reopen_entries.append({
            "signal_time_pt": _pt(self._carry_source_ts),
            "entry_time_pt": _pt(candle.timestamp),
            "direction": signal.direction.value,
            "original_entry": self._carry_source_entry,
            "reopen_entry": entry,
            "gap_ticks": signal.meta["carry_gap_ticks"],
            "gap_r": signal.meta["carry_gap_r"],
        })
        self._carry_signal = None
        self._carry_source_ts = None
        self._carry_source_entry = None
        self._execute_entry(signal, candle)
        if self._open_position:
            self._check_sl_only(candle)
        return True

    def _process_candle(self, candle):
        if self._record_equity:
            self._equity_curve.append((candle.timestamp, self._capital))

        trade_date = _topstep_trade_date(candle.timestamp)
        if trade_date != self._loss_count_date:
            self._loss_count_date = trade_date
            self._daily_loss_count = 0
            self._daily_win_count = 0
        self._reset_full_tp_counts_for_session(candle.timestamp)

        if self._breakout_trackers:
            self._update_breakout_trackers(candle)

        close_phase = market_close_phase(candle.timestamp)
        in_flatten = close_phase == MARKET_PHASE_FLATTEN
        in_late = close_phase in {MARKET_PHASE_PRE_FLATTEN, MARKET_PHASE_FLATTEN}

        # Keep Topstep-compatible handling for already-open trades.
        if in_flatten:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            if not self._open_position and not self._pending_order:
                self._try_late_signal(candle)
            return

        if in_late and self._pending_order:
            self._cancel_pending_order()

        if self._open_position:
            self._check_exit(candle)
            if self._open_position:
                if self._pmo_max_hold_minutes > 0 and self.strategy_mode in ("pmo", "factor"):
                    held = (candle.timestamp - self._open_position.entry_time).total_seconds() / 60.0
                    if held >= self._pmo_max_hold_minutes:
                        self._force_exit(candle, ExitReason.FLATTEN)
                        return
                self._check_trailing_sl(candle)
                return

        if self._pending_order and not self._open_position:
            if not self._trend_session_allowed(candle.timestamp):
                self._cancel_pending_order(release_lock=True)
                self._reset_trend_session_state()
                return
            if self._check_pending_fill(candle):
                return
            self._cancel_pending_order(release_lock=True)

        if self._open_position or self._pending_order:
            return
        if self._near_data_end:
            return
        if not self._trend_session_allowed(candle.timestamp):
            self._reset_trend_session_state()
            return

        if self._carry_signal is not None and is_market_reopen(candle.timestamp):
            if self._enter_carried_signal(candle):
                return

        if in_late:
            self._try_late_signal(candle)
            return

        signal = self.trend_follow.evaluate(candle, [], True)
        if signal:
            signal.zone_source = "factor"
            if not self._passes_entry_gates(signal, candle):
                return
            if getattr(signal, "order_type", "limit") == "market":
                self._execute_entry(signal, candle)
                if self._open_position:
                    self._check_sl_only(candle)
            else:
                self._place_pending_order(signal, candle)

    def _try_late_signal(self, candle) -> None:
        if self._carry_signal is not None:
            # Still ingest state; ignore additional late candidates for entry.
            self.trend_follow.evaluate(candle, [], True)
            return
        signal = self.trend_follow.evaluate(candle, [], True)
        if signal:
            signal.zone_source = "factor"
            if self._passes_entry_gates(signal, candle):
                self._arm_carry(signal, candle)

    def _passes_entry_gates(self, signal: TradeSignal, candle) -> bool:
        if self._tr_daily_loss_stop and self._daily_loss_count >= self._tr_daily_loss_stop:
            self.trend_follow.notify_order_cancelled()
            return False
        if self._tr_daily_win_stop and self._daily_win_count >= self._tr_daily_win_stop:
            self.trend_follow.notify_order_cancelled()
            return False
        if self._prev_rv_gate and self._gate_block_today:
            self.trend_follow.notify_order_cancelled()
            return False
        if self._signal_full_tp_locked(signal, candle):
            self.trend_follow.notify_order_cancelled()
            return False
        if self._session_direction_is_used(signal, candle):
            self.trend_follow.notify_order_cancelled()
            return False
        self._mark_session_direction_used(signal, candle)
        return True


class CarryLateGapGuardBestEngine(CarryLateBestEngine):
    MAX_REOPEN_GAP_R = 0.25


def _run(engine_cls, params: StrategyParams, candles):
    engine = engine_cls(
        config=_make_config(params),
        strategy_params=copy.deepcopy(params),
        zone_timeline=None,
        record_equity=False,
    )
    result = engine.run(candles)
    return engine, result


def _summarize(label: str, result) -> dict[str, Any]:
    trades = list(result.trades)
    pnls = [float(t.pnl or 0.0) for t in trades]
    exits = Counter(_reason_name(t.exit_reason) for t in trades)
    carry = [t for t in trades if (t.meta or {}).get("carry_reopen")]
    return {
        "label": label,
        "trades": int(result.metrics.total_trades),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p < 0),
        "pnl": round(float(result.metrics.total_pnl), 2),
        "pf": round(float(result.metrics.profit_factor), 4),
        "max_dd": round(float(result.metrics.max_drawdown), 2),
        "win_rate": round(float(result.metrics.win_rate), 4),
        "exit_counts": dict(sorted(exits.items())),
        "carry_trades": len(carry),
    }


def _trade_rows(result) -> list[dict[str, Any]]:
    rows = []
    for t in result.trades:
        meta = dict(t.meta or {})
        rows.append({
            "entry_pt": _pt(t.entry_time),
            "exit_pt": _pt(t.exit_time),
            "direction": t.direction.value,
            "entry": round(float(t.entry_price), 2),
            "exit": round(float(t.exit_price or 0.0), 2),
            "pnl": round(float(t.pnl or 0.0), 2),
            "reason": _reason_name(t.exit_reason),
            "carry": bool(meta.get("carry_reopen")),
            "carry_signal_pt": (
                _pt(datetime.fromisoformat(meta["carry_original_signal_time"]))
                if meta.get("carry_original_signal_time")
                else ""
            ),
            "carry_gap_ticks": meta.get("carry_gap_ticks"),
        })
    return rows


def main() -> int:
    logging.disable(logging.CRITICAL)
    params = _load_best_params()
    candles = sorted(candle_store.load("MNQ", 1), key=lambda candle: candle.timestamp)
    candles = [c for c in candles if c.timestamp <= datetime(2026, 7, 15, 20, 59, tzinfo=UTC)]
    if not candles:
        raise SystemExit("No MNQ candles loaded.")

    _baseline_engine, baseline = _run(BacktestEngine, params, candles)
    carry_engine, carry = _run(CarryLateBestEngine, params, candles)
    carry_guard_engine, carry_guard = _run(CarryLateGapGuardBestEngine, params, candles)
    _no_flat_engine, no_flat = _run(NoFlattenBestEngine, params, candles)

    payload = {
        "created_at": datetime.now(UTC).isoformat(),
        "preset": "BEST",
        "data": {
            "bars": len(candles),
            "start": candles[0].timestamp.isoformat(),
            "end": candles[-1].timestamp.isoformat(),
        },
        "rule_tested": {
            "market_clock_version": MARKET_CLOCK_VERSION,
            "baseline": "production BEST behavior: cancel pending 15:30 ET, flatten/block new entries 15:45-18:00 ET",
            "carry_reopen": "BEST only: if a BEST factor signal appears 15:30-18:00 ET while flat, defer it and enter at the first 18:00 ET ASIA candle; SL/TP distances from the original signal are re-anchored",
            "carry_reopen_gap_guard": "same as carry_reopen, but skip if absolute reopen gap is greater than 0.25R from the original signal entry",
            "no_flatten_upper_bound": "BEST only upper-bound: remove close-window flatten/block entirely",
        },
        "summary": [
            _summarize("BEST baseline", baseline),
            _summarize("BEST carry late signal to 18:00 ET reopen", carry),
            _summarize("BEST carry reopen with gap <= 0.25R", carry_guard),
            _summarize("BEST no close-window flatten upper-bound", no_flat),
        ],
        "carry_late_candidates": carry_engine._carry_late_candidates,
        "carry_reopen_entries": carry_engine._carry_reopen_entries,
        "carry_guard_reopen_entries": carry_guard_engine._carry_reopen_entries,
        "carry_guard_reopen_skips": carry_guard_engine._carry_reopen_skips,
        "trades": {
            "baseline": _trade_rows(baseline),
            "carry_reopen": _trade_rows(carry),
            "carry_reopen_gap_guard": _trade_rows(carry_guard),
            "no_flatten_upper_bound": _trade_rows(no_flat),
        },
    }

    out_dir = ROOT / "data" / "machinelearning"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"best_reopen_carry_study_{datetime.now(UTC).strftime('%Y%m%d_%H%M%SZ')}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "output": str(out),
        "summary": payload["summary"],
        "carry_late_candidates": payload["carry_late_candidates"],
        "carry_reopen_entries": payload["carry_reopen_entries"],
        "carry_guard_reopen_entries": payload["carry_guard_reopen_entries"],
        "carry_guard_reopen_skips": payload["carry_guard_reopen_skips"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
