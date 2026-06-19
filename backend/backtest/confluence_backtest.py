# ============================================================
# 文件: backend/backtest/confluence_backtest.py
# 狀態: v0.18.0 (multi-timeframe weighted confluence — research backtester)
# 關聯文件:
#   ← backend/strategy/confluence.py     (signal engine)
#   ← backend/strategy/consolidation.py  (per-TF ClockBucketZoneDetector)
#   ← backend/backtest/metrics.py        (MetricsCalculator — shared metrics)
#   ← backend/db/models.py               (Trade, Direction, BacktestConfig)
# ============================================================
"""Dedicated backtester for the confluence engine.

Runs one ClockBucketZoneDetector per timeframe over the 1m stream, evaluates
weighted level confluence every bar, and emits Trade objects which are scored by
the SHARED MetricsCalculator (so calmar / PF / drawdown / win-rate match the
rest of the app).

Key difference vs the trend BacktestEngine — the FILL MODEL is live-accurate:
a confluence limit order is ONE-SHOT. It is given `wait_minutes` 1m candles to
fill; if price never touches it, the order is cancelled and NOT re-posted. This
removes the optimistic "re-post at a fresh level every candle" behaviour that
made the trend backtest over-estimate live fills.
"""

from __future__ import annotations

import logging
import time as _time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

from backend.db.models import (
    Candle, Trade, Direction, ExitReason, StrategyType, BacktestConfig,
    Metrics, get_point_value, get_tick_size,
)
from backend.strategy.consolidation import (
    ClockBucketZoneDetector, AREA_TIMEFRAME_MINUTES,
)
from backend.strategy.confluence import (
    ConfluenceConfig, evaluate_confluence, evaluate_confluence_scored, gate_signals,
    ConfluenceSignal, snapshot_zones_by_tf,
)
from backend.strategy.confluence_features import CONTEXT_WINDOW
from backend.backtest.metrics import MetricsCalculator
from backend.backtest.intrabar import resolve_same_bar_exit


# wait-timeout values the optimizer sweeps (minutes == 1m candles)
WAIT_MINUTES_CHOICES = (1, 5, 15, 30, 60)


def build_zone_timeline(
    candles_1m: List[Candle],
    timeframes,
    tick_size: float,
    max_recency_depth: int,
    progress_callback=None,
) -> List[Dict[str, list]]:
    """Feed the 1m stream through one detector per TF ONCE and return a
    per-candle ``zones_by_tf`` timeline (index-aligned to the SORTED candles).

    Completed-zone value profiles are frozen at finalization, so the snapshot
    only changes when a new zone completes in some TF — between change-points
    the same dict object is shared (cheap memory, cheap replay). The optimizer
    reuses this across every signal-param combo instead of re-running the slow
    detector pass each time.
    """
    candles = sorted(candles_1m, key=lambda c: c.timestamp)
    detectors: Dict[str, ClockBucketZoneDetector] = {
        tf: ClockBucketZoneDetector(
            area_timeframe=tf,
            value_area_pct=0.80,
            tick_size=tick_size,
            max_recent=max_recency_depth + 2,
            recalc_active_each_bar=False,
        )
        for tf in timeframes
    }
    depth = max_recency_depth + 1
    timeline: List[Dict[str, list]] = []
    last_counts = None
    snapshot: Dict[str, list] = {}
    total = len(candles)
    _t0 = _time.perf_counter()
    _step = max(5000, total // 10)  # ~10 heartbeats over the run
    logger.info(
        f"[ZoneTimeline] start: {total} candles × {len(detectors)} TF "
        f"({','.join(detectors.keys())})"
    )
    if progress_callback:
        progress_callback("building zone timeline", 0, total, "starting detectors")
    for i, candle in enumerate(candles):
        for det in detectors.values():
            det.update(candle)
        counts = tuple(det.completed_zone_count for det in detectors.values())
        if counts != last_counts:
            snapshot = snapshot_zones_by_tf(detectors, depth)
            last_counts = counts
        timeline.append(snapshot)
        if (i + 1) % _step == 0 or i + 1 == total:
            el = _time.perf_counter() - _t0
            rate = (i + 1) / el if el > 0 else 0
            eta = (total - (i + 1)) / rate if rate > 0 else 0
            logger.info(
                f"[ZoneTimeline] {i + 1}/{total} ({100 * (i + 1) // total}%) "
                f"· {el:.0f}s elapsed · {rate:.0f} bars/s · ETA {eta:.0f}s"
            )
            if progress_callback:
                progress_callback(
                    "building zone timeline", i + 1, total,
                    f"{rate:.0f} bars/s, ETA {eta:.0f}s",
                )
    logger.info(
        f"[ZoneTimeline] done: {total} candles in {_time.perf_counter() - _t0:.1f}s"
    )
    return timeline


@dataclass
class ConfluenceBacktestConfig:
    """Run-level knobs distinct from the signal-level ConfluenceConfig."""
    wait_minutes: int = 15                 # one-shot limit-order timeout (searched)
    one_trade_per_session_direction: bool = True
    timeframes: tuple = tuple(AREA_TIMEFRAME_MINUTES.keys())
    min_score: float = 0.0                 # scorer gate: skip signals below this
    base_minutes: int = 1                  # minutes per input candle (1m or 5m base)
    # ── STYLE (v0.24 exit-policy): all default to a no-op so an all-OFF Style
    # reproduces the original behaviour. See backend/strategy/exit_policy.py. ──
    trail_trigger_pct: float = 0.0         # 0 = trailing OFF
    trail_lock_pct: float = 0.0            # locked SL as fraction of TP distance on trigger
    full_tp_lock: int = 0                  # 0 = OFF; stop new entries after N full-TP exits/session

    @property
    def wait_bars(self) -> int:
        """One-shot timeout expressed in INPUT candles (minute-accurate across
        base resolutions): wait_minutes=60 with a 5m base == 12 bars."""
        return max(1, round(self.wait_minutes / max(1, self.base_minutes)))


class ConfluenceBacktester:
    def __init__(
        self,
        signal_cfg: ConfluenceConfig,
        run_cfg: Optional[ConfluenceBacktestConfig] = None,
        contract_id: str = "CON.F.US.MNQ.M26",
        contract_size: int = 1,
        bt_config: Optional[BacktestConfig] = None,
        scorer=None,
    ):
        self.signal_cfg = signal_cfg
        self.run_cfg = run_cfg or ConfluenceBacktestConfig()
        # explainable scorer: when set, both modes are evaluated per bar and the
        # highest-scoring action above min_score is taken (per-step auto-select).
        self.scorer = scorer
        if signal_cfg.direction_mode == "auto":
            self.modes = signal_cfg.auto_modes()
        else:
            self.modes = (signal_cfg.direction_mode,)
        self.contract_id = contract_id
        self.contract_size = max(1, int(contract_size or 1))
        self.bt = bt_config or BacktestConfig()
        self.POINT_VALUE = get_point_value(contract_id)
        self.TICK_SIZE = get_tick_size(contract_id)
        self.signal_cfg.tick_size = self.TICK_SIZE

        # one detector per timeframe
        self.detectors: Dict[str, ClockBucketZoneDetector] = {
            tf: ClockBucketZoneDetector(
                area_timeframe=tf,
                value_area_pct=0.80,   # vah_80/val_80 used for SL spans; bands computed separately
                tick_size=self.TICK_SIZE,
                max_recent=self.signal_cfg.max_recency_depth + 2,
                recalc_active_each_bar=False,  # confluence reads completed zones only → big speedup
            )
            for tf in self.run_cfg.timeframes
        }

        # STYLE exit-policy (v0.24): shared helper so live == backtest.
        from backend.strategy.exit_policy import ConfluenceExitStyle
        self.style = ConfluenceExitStyle(
            trail_trigger_pct=float(getattr(self.run_cfg, "trail_trigger_pct", 0.0) or 0.0),
            trail_lock_pct=float(getattr(self.run_cfg, "trail_lock_pct", 0.0) or 0.0),
            full_tp_lock=int(getattr(self.run_cfg, "full_tp_lock", 0) or 0),
            session_limit=bool(self.run_cfg.one_trade_per_session_direction),
        )

        # state
        self._capital = self.bt.initial_capital
        self._trades: List[Trade] = []
        self._open: Optional[Trade] = None
        self._pending: Optional[ConfluenceSignal] = None
        self._pending_age: int = 0
        self._session_dir_used: set = set()
        self._trail_triggered: bool = False          # per-open-position latch
        self._session_tp_count: Dict[str, int] = {}  # full_tp_lock: TP exits per session

    # ── helpers ──

    def _zones_by_tf(self) -> Dict[str, list]:
        return snapshot_zones_by_tf(self.detectors, self.signal_cfg.max_recency_depth + 1)

    @staticmethod
    def _session_key(ts) -> str:
        return ts.strftime("%Y-%m-%d")

    # ── main loop ──

    def run(
        self,
        candles_1m: List[Candle],
        zones_timeline: Optional[List[Dict[str, list]]] = None,
        progress_callback=None,
    ) -> "ConfluenceBacktestResult":
        """Run the backtest.

        If ``zones_timeline`` is supplied (one zones_by_tf snapshot per candle,
        index-aligned to the SORTED candle list), the per-TF detectors are NOT
        fed — the optimizer precomputes the timeline once and replays it cheaply
        across every parameter combo. ``candles_1m`` must already be sorted when
        a timeline is passed.
        """
        candles = candles_1m if zones_timeline is not None else sorted(
            candles_1m, key=lambda c: c.timestamp
        )
        total = len(candles)
        wait = self.run_cfg.wait_bars  # minute-accurate timeout in input candles
        edge_guard = wait + 2  # no new entries this close to data end
        progress_step = max(5000, total // 20) if total else 1
        if progress_callback:
            progress_callback("replaying strategy", 0, total, "starting simulation")

        for i, candle in enumerate(candles):
            if progress_callback and ((i + 1) % progress_step == 0 or i + 1 == total):
                progress_callback(
                    "replaying strategy", i + 1, total,
                    f"{len(self._trades)} closed trades",
                )
            # 1) feed every timeframe detector (skipped when a timeline is given)
            if zones_timeline is None:
                for det in self.detectors.values():
                    det.update(candle)

            # 2) manage an open position (exit on SL/TP)
            if self._open is not None:
                self._check_exit(candle)
                if self._open is not None:
                    continue  # still open — no new orders

            # 3) manage a pending one-shot limit order
            if self._pending is not None:
                if self._try_fill(candle):
                    continue
                self._pending_age += 1
                if self._pending_age >= wait:
                    self._pending = None          # one-shot: cancel, do NOT re-post
                    self._pending_age = 0
                continue

            # 4) flat & idle — evaluate confluence for a new signal
            if i >= total - edge_guard:
                continue
            snap = zones_timeline[i] if zones_timeline is not None else None
            recent = candles[max(0, i - CONTEXT_WINDOW + 1):i + 1]
            self._maybe_open(candle, snap, recent)

        return self._finalize()

    def _maybe_open(self, candle: Candle, zones_by_tf: Optional[Dict[str, list]] = None,
                    recent_candles: Optional[List[Candle]] = None):
        if zones_by_tf is None:
            zones_by_tf = self._zones_by_tf()
        if len(zones_by_tf) < self.signal_cfg.min_distinct_tf:
            return
        # full_tp_lock: stop opening new trades once this session hit N full TPs
        if self.style.full_tp_lock > 0:
            if self._session_tp_count.get(self._session_key(candle.timestamp), 0) >= self.style.full_tp_lock:
                return
        if self.scorer is not None:
            # explainable path: score both modes, gate (EV-priority if cfg.ev_floor
            # is set, else win-prob/score), take best. Same helper as live engine.
            signals = evaluate_confluence_scored(
                zones_by_tf, candle.close, self.signal_cfg, self.scorer, modes=self.modes,
                recent_candles=recent_candles,
            )
            signals = gate_signals(signals, self.signal_cfg, self.run_cfg.min_score)
        else:
            signals = evaluate_confluence(zones_by_tf, candle.close, self.signal_cfg)
            # best = strongest confluence by summed weight
            signals.sort(key=lambda s: s.cluster.total_weight, reverse=True)
        if not signals:
            return
        for sig in signals:
            if self.run_cfg.one_trade_per_session_direction:
                key = (self._session_key(candle.timestamp), sig.direction.value)
                if key in self._session_dir_used:
                    continue
            self._pending = sig
            self._pending_age = 0
            return

    def _try_fill(self, candle: Candle) -> bool:
        sig = self._pending
        entry = sig.entry_price
        filled = (candle.low <= entry) if sig.direction == Direction.BUY else (candle.high >= entry)
        if not filled:
            return False
        self._open_trade(sig, candle)
        self._pending = None
        self._pending_age = 0
        if self.run_cfg.one_trade_per_session_direction:
            self._session_dir_used.add((self._session_key(candle.timestamp), sig.direction.value))
        # entry candle: only an immediate SL can trigger (TP needs a later bar)
        self._check_sl_only(candle)
        return True

    def _open_trade(self, sig: ConfluenceSignal, candle: Candle):
        self._trail_triggered = False   # reset STYLE trail latch for the new position
        cl = sig.cluster
        self._open = Trade(
            trade_id=f"C{uuid.uuid4().hex[:8]}",
            strategy=StrategyType.TREND_FOLLOW,
            direction=sig.direction,
            entry_price=sig.entry_price,
            entry_time=candle.timestamp,
            sl_price=sig.sl_price,
            tp_price=sig.tp_price,
            original_sl_price=sig.sl_price,
            original_tp_price=sig.tp_price,
            zone_id=cl.largest_tf,
            zone_source="confluence",
            contracts=self.contract_size,
            point_value=self.POINT_VALUE,
            contract_id=self.contract_id,
            meta={
                "mode": sig.direction_mode,
                "side": cl.side,
                "weight": round(cl.total_weight, 2),
                "tfs": cl.distinct_tfs,
                "largest_tf": cl.largest_tf,
                "labels": cl.labels,
                "wait_min": self.run_cfg.wait_minutes,
                "score": round(sig.score, 4),
                "prob": round(sig.prob, 4),
                "features": sig.features,
            },
        )

    # ── exits ──

    def _check_sl_only(self, candle: Candle):
        pos = self._open
        if not pos:
            return
        if pos.direction == Direction.BUY and candle.low <= pos.sl_price:
            self._exit(candle, pos.sl_price, ExitReason.SL)
        elif pos.direction == Direction.SELL and candle.high >= pos.sl_price:
            self._exit(candle, pos.sl_price, ExitReason.SL)

    def _check_exit(self, candle: Candle):
        pos = self._open
        if not pos:
            return
        if pos.direction == Direction.BUY:
            hit_sl = candle.low <= pos.sl_price
            hit_tp = candle.high >= pos.tp_price
        else:
            hit_sl = candle.high >= pos.sl_price
            hit_tp = candle.low <= pos.tp_price

        # once trail has latched, an SL hit is a profit-locked TRAIL exit
        sl_reason = ExitReason.TRAIL_SL if self._trail_triggered else ExitReason.SL
        if hit_sl and hit_tp:
            if resolve_same_bar_exit(candle.open, pos.sl_price, pos.tp_price) == "sl":
                self._exit(candle, pos.sl_price, sl_reason)
            else:
                self._exit(candle, pos.tp_price, ExitReason.TP)
        elif hit_sl:
            self._exit(candle, pos.sl_price, sl_reason)
        elif hit_tp:
            self._exit(candle, pos.tp_price, ExitReason.TP)
        elif self.style.trail_enabled:
            # A close-based trail can only be applied after this bar survives
            # the pre-existing SL/TP. Applying it before exit checks lets the
            # close rewrite the same bar's earlier path.
            from backend.strategy.exit_policy import maybe_trail_sl
            new_sl, self._trail_triggered = maybe_trail_sl(
                pos.direction, pos.entry_price, pos.tp_price, pos.sl_price,
                self._trail_triggered, candle.close, self.style)
            pos.sl_price = new_sl

    def _exit(self, candle: Candle, exit_price: float, reason: ExitReason):
        pos = self._open
        pt = pos.point_value or self.POINT_VALUE
        if pos.direction == Direction.BUY:
            gross = (exit_price - pos.entry_price) * pt * pos.contracts
        else:
            gross = (pos.entry_price - exit_price) * pt * pos.contracts
        commission = self.bt.commission_rt * pos.contracts
        fees = self.bt.fees_rt * pos.contracts
        pos.pnl = gross - commission - fees
        pos.commission = commission
        pos.fees = fees
        pos.exit_price = exit_price
        pos.exit_time = candle.timestamp
        pos.exit_reason = reason
        self._capital += pos.pnl
        self._trades.append(pos)
        self._open = None
        # full_tp_lock: tally FULL-TP exits in this session (trail exits don't count)
        if reason == ExitReason.TP and self.style.full_tp_lock > 0:
            k = self._session_key(candle.timestamp)
            self._session_tp_count[k] = self._session_tp_count.get(k, 0) + 1

    def _finalize(self) -> "ConfluenceBacktestResult":
        metrics = MetricsCalculator().calculate_all(self._trades, self.bt.initial_capital)
        return ConfluenceBacktestResult(
            metrics=metrics,
            trades=self._trades,
            final_capital=self._capital,
            signal_cfg=self.signal_cfg,
            run_cfg=self.run_cfg,
        )


@dataclass
class ConfluenceBacktestResult:
    metrics: Metrics
    trades: List[Trade]
    final_capital: float
    signal_cfg: ConfluenceConfig
    run_cfg: ConfluenceBacktestConfig
