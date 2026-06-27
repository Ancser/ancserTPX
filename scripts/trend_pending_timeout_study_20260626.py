"""Trend #1 pending-order timeout / fill-quality study.

Read-only local research script.  It does not call the broker, does not start
FastAPI, and does not touch live state.

Question:
    Does Trend #1 work because a pending limit is refreshed every 1 minute, or
    would keeping the same limit order alive for 2..60 minutes improve live
    realism/performance?

Also tests fill realism:
    penetration_ticks=0: OHLC touch is filled (optimistic)
    penetration_ticks=1: price must trade 1 tick through the limit
    penetration_ticks=2: price must trade 2 ticks through the limit

Outputs:
    data/machinelearning/trend_pending_timeout_study_20260626.csv
    data/machinelearning/trend_pending_timeout_study_20260626.md
"""

from __future__ import annotations

import csv
import json
import pickle
import statistics
import sys
import time as time_mod
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestEngine  # noqa: E402
from backend.db.models import (  # noqa: E402
    BacktestConfig,
    Candle,
    Direction,
    ExitReason,
    StrategyParams,
    TradeSignal,
    get_commission_rt,
    get_fees_rt,
    get_tick_size,
)
from backend.strategy.consolidation import build_zone_detector  # noqa: E402


CONTRACT_ID = "CON.F.US.MNQ.U26"
SYMBOL = "MNQ"
STORE = ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl"
OUT_DIR = ROOT / "data" / "machinelearning"
CSV_OUT = OUT_DIR / "trend_pending_timeout_study_20260626.csv"
MD_OUT = OUT_DIR / "trend_pending_timeout_study_20260626.md"


@dataclass
class StudyRow:
    wait_minutes: int
    penetration_ticks: int
    attempts: int
    fills: int
    cancels: int
    fill_rate: float
    avg_fill_age_bars: float
    trades: int
    win_rate: float
    pnl: float
    max_dd: float
    pf: float
    calmar: float
    worst_week: float
    week_cv: float
    may_pnl: float
    jun_pnl: float


class TrendPendingBacktester(BacktestEngine):
    def __init__(self, *args, pending_wait_bars: int, penetration_ticks: int, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending_wait_bars = max(1, int(pending_wait_bars))
        self.penetration_ticks = max(0, int(penetration_ticks))
        self._pending_max_age = self.pending_wait_bars
        self.pending_attempts = 0
        self.pending_fills = 0
        self.pending_cancels = 0
        self.fill_ages: List[int] = []

    def _place_pending_order(self, signal: TradeSignal, candle: Candle):
        self.pending_attempts += 1
        super()._place_pending_order(signal, candle)

    def _cancel_pending_order(self):
        if self._pending_order:
            self.pending_cancels += 1
        super()._cancel_pending_order()

    def _check_pending_fill(self, candle: Candle) -> bool:
        order = self._pending_order
        if not order:
            return False

        penetration = self.penetration_ticks * self.TICK_SIZE
        if order.direction == Direction.BUY:
            filled = candle.low <= order.entry_price - penetration
        else:
            filled = candle.high >= order.entry_price + penetration

        if filled:
            self.pending_fills += 1
            self.fill_ages.append(self._pending_age + 1)
            self._execute_entry(order, candle)
            self._pending_order = None
            self._pending_age = 0
            if self._open_position:
                self._check_sl_only(candle)
            return True
        return False

    def _process_candle(self, candle: Candle):
        """Same as BacktestEngine, except pending order can live N bars."""
        if self._record_equity:
            self._equity_curve.append((candle.timestamp, self._capital))

        if self._breakout_trackers:
            self._update_breakout_trackers(candle)

        _recent_zones = []
        if self._zone_timeline is not None:
            _zt = self._zone_timeline[self._zi] if self._zi < len(self._zone_timeline) else {}
            self._zi += 1
            _active_zone = _zt.get("active")
            _is_mature = _zt.get("mature", False)
            _recent_zones = _zt.get("recent") or ([_active_zone] if _active_zone else [])
        else:
            self.detector.update(candle)

        date_str = candle.timestamp.strftime("%Y-%m-%d")
        daily = self._daily_pnl.get(date_str, 0)
        if daily <= -self.config.max_daily_loss:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            return

        self._reset_full_tp_counts_for_session(candle.timestamp)

        candle_time = candle.timestamp.time()
        session_start = time(22, 0)
        in_flatten_window = candle_time >= self.FLATTEN_TIME_UTC and candle_time < session_start
        if in_flatten_window:
            if self._open_position:
                self._force_exit(candle, ExitReason.FLATTEN)
            if self._pending_order:
                self._cancel_pending_order()
            return

        in_pre_flatten = candle_time >= self.PRE_FLATTEN_UTC and candle_time < session_start
        if in_pre_flatten and self._pending_order:
            self._cancel_pending_order()

        if self._open_position:
            self._check_exit(candle)
            if self._open_position:
                self._check_trailing_sl(candle)
                return

        if self._pending_order and not self._open_position:
            if self._check_pending_fill(candle):
                return
            self._pending_age += 1
            if self._pending_age >= self.pending_wait_bars:
                self._cancel_pending_order()
            else:
                return

        if not self._open_position and not self._pending_order:
            if self._near_data_end:
                return

            if self._zone_timeline is not None:
                eval_zones = _recent_zones
                eval_mature = _is_mature
                zone_source = "current"
            else:
                eval_zones = self.detector.get_recent_zones()
                eval_mature = self.detector.is_zone_mature
                zone_source = "current"

            signal = self.trend_follow.evaluate(candle, eval_zones, eval_mature)
            if signal:
                signal.zone_source = zone_source
                if self._signal_full_tp_locked(signal, candle):
                    self.trend_follow.notify_order_cancelled()
                    return
                if self._session_direction_is_used(signal, candle):
                    self.trend_follow.notify_order_cancelled()
                    return
                self._mark_session_direction_used(signal, candle)
                if getattr(signal, "order_type", "limit") == "market":
                    self._execute_entry(signal, candle)
                    if self._open_position:
                        self._check_sl_only(candle)
                else:
                    self._place_pending_order(signal, candle)


def load_candles() -> List[Candle]:
    return sorted(pickle.loads(STORE.read_bytes()), key=lambda c: c.timestamp)


def build_trend_timeline(candles: List[Candle], area_tf: str, tick: float) -> List[dict]:
    det = build_zone_detector(area_timeframe=area_tf, value_area_pct=0.80, tick_size=tick, max_recent=10)
    out = []
    for candle in candles:
        det.update(candle)
        active = det.get_active_zone()
        recent = det.get_recent_zones()
        out.append({
            "active": active,
            "mature": bool(det.is_zone_mature),
            "recent": recent or ([active] if active else []),
        })
    return out


def weekly_monthly(trades) -> tuple[float, float, float, float]:
    by_week: Dict[str, float] = {}
    by_month: Dict[str, float] = {}
    for t in trades:
        ts = t.entry_time
        pnl = float(t.pnl or 0.0)
        iso = ts.isocalendar()
        wk = f"{iso.year}-W{iso.week:02d}"
        mo = ts.strftime("%Y-%m")
        by_week[wk] = by_week.get(wk, 0.0) + pnl
        by_month[mo] = by_month.get(mo, 0.0) + pnl
    vals = list(by_week.values())
    mean = statistics.mean(vals) if vals else 0.0
    cv = (statistics.pstdev(vals) / max(abs(mean), 1.0)) if len(vals) > 1 else 0.0
    return (
        min(vals) if vals else 0.0,
        cv,
        by_month.get("2026-05", 0.0),
        by_month.get("2026-06", 0.0),
    )


def make_params() -> StrategyParams:
    return StrategyParams(
        strategy="trend",
        contract_id=CONTRACT_ID,
        contract_size=1,
        area_timeframe="1h",
        value_area_pct=0.80,
        method="single",
        tf_combo=[],
        rr_ratio=4,
        breakout_confirm_bars=3,
        sl_ticks=40,
        tr_sl_ticks=40,
        trail_enabled=True,
        tr_trail_enabled=True,
        trail_trigger_pct=0.50,
        tr_trail_trigger_pct=0.50,
        trail_sl_ticks=10,
        tr_trail_sl_ticks=10,
        full_tp_lock=0,
        tr_full_tp_lock=0,
        one_trade_per_session_direction=True,
        tr_one_trade_per_session=True,
    )


def run_one(candles, timeline, wait_minutes: int, penetration_ticks: int) -> StudyRow:
    bt_cfg = BacktestConfig(
        initial_capital=50_000.0,
        symbol=SYMBOL,
        commission_rt=get_commission_rt(CONTRACT_ID),
        fees_rt=get_fees_rt(CONTRACT_ID),
    )
    engine = TrendPendingBacktester(
        bt_cfg,
        strategy_params=make_params(),
        zone_timeline=timeline,
        record_equity=False,
        pending_wait_bars=wait_minutes,
        penetration_ticks=penetration_ticks,
    )
    result = engine.run(candles)
    m = result.metrics
    worst_week, week_cv, may, jun = weekly_monthly(result.trades)
    attempts = engine.pending_attempts
    fills = engine.pending_fills
    cancels = engine.pending_cancels
    return StudyRow(
        wait_minutes=wait_minutes,
        penetration_ticks=penetration_ticks,
        attempts=attempts,
        fills=fills,
        cancels=cancels,
        fill_rate=fills / attempts if attempts else 0.0,
        avg_fill_age_bars=statistics.mean(engine.fill_ages) if engine.fill_ages else 0.0,
        trades=m.total_trades,
        win_rate=m.win_rate,
        pnl=m.total_pnl,
        max_dd=m.max_drawdown,
        pf=m.profit_factor,
        calmar=m.calmar_ratio,
        worst_week=worst_week,
        week_cv=week_cv,
        may_pnl=may,
        jun_pnl=jun,
    )


def write_outputs(rows: List[StudyRow]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with CSV_OUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(StudyRow.__dataclass_fields__.keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r.__dict__)

    def fmt_money(x: float) -> str:
        return f"${x:,.0f}"

    lines = [
        "# Trend #1 pending timeout study — 2026-06-26",
        "",
        "Preset: `06.26 CODEX #1 Trend穩定 MNQx1 TF1h RR1:4 C3 SL40 Trail50L10`",
        "",
        "| Wait | Penetrate | Attempts | Fill% | Avg fill age | Trades | Win | PnL | MaxDD | PF | WorstW | May | Jun |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in sorted(rows, key=lambda x: (x.penetration_ticks, x.wait_minutes)):
        lines.append(
            f"| {r.wait_minutes}m | {r.penetration_ticks}t | {r.attempts} | "
            f"{r.fill_rate*100:.1f}% | {r.avg_fill_age_bars:.1f} | {r.trades} | "
            f"{r.win_rate*100:.1f}% | {fmt_money(r.pnl)} | {fmt_money(r.max_dd)} | "
            f"{r.pf:.2f} | {fmt_money(r.worst_week)} | {fmt_money(r.may_pnl)} | {fmt_money(r.jun_pnl)} |"
        )
    MD_OUT.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    candles = load_candles()
    tick = get_tick_size(CONTRACT_ID)
    print(f"loaded {len(candles):,} candles {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)
    print("building 1h zone timeline", flush=True)
    t0 = time_mod.perf_counter()
    timeline = build_trend_timeline(candles, "1h", tick)
    print(f"timeline built in {time_mod.perf_counter() - t0:.1f}s", flush=True)

    rows: List[StudyRow] = []
    waits = [1, 2, 3, 5, 10, 15, 30, 60]
    penetrations = [0, 1, 2]
    for pen in penetrations:
        for wait in waits:
            t1 = time_mod.perf_counter()
            row = run_one(candles, timeline, wait, pen)
            rows.append(row)
            write_outputs(rows)
            print(
                json.dumps({
                    "wait": wait,
                    "penetration": pen,
                    "attempts": row.attempts,
                    "fill_rate": round(row.fill_rate, 3),
                    "trades": row.trades,
                    "pnl": round(row.pnl, 2),
                    "dd": round(row.max_dd, 2),
                    "pf": round(row.pf, 3),
                    "sec": round(time_mod.perf_counter() - t1, 1),
                }),
                flush=True,
            )
    print(f"wrote {CSV_OUT}", flush=True)
    print(f"wrote {MD_OUT}", flush=True)


if __name__ == "__main__":
    main()
