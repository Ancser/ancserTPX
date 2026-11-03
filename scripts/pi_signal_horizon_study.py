"""PI signal family x holding horizon on the latest data (production engine).

Question (2026-10-02): for each PI signal family -- LV1 / LV2 / LV3, long and
short -- which holding horizon suits it: 30 minutes, 60 minutes, hold to the
15:45 ET close, continue from the 18:00 ET (15:00 PT) reopen, or overnight?

Method
  * production ``BacktestEngine`` and PI entry (one position at a time, the
    5-minute signal age gate, ATR-blend stops); only the exit policy differs;
  * the shared PI history plus the new-channel audit marks, through the same
    run-scoped overlay the Backtest button uses (``load_replay_rows``);
  * every horizon replays the same signals: the cutoff is the last trading day
    whose next session is complete in the candle store, so an overnight hold
    never ends on a truncated bar;
  * gates that are not about the signal are off in the matrix (daily loss
    stop, LV2 replacement, continuation unless the horizon is continuation);
    the presets themselves are re-run separately with their own gates;
  * reproduction first: the harness reproduces the published 2026-09-15 strict
    rows trade-for-trade on their snapshot, and the production engine on the
    latest data, before any matrix row is computed.

Horizons
  m30 / m60  time exit after 30 / 60 minutes (attached stop, no target)
  close      stop only, flattened by the production 15:45 ET session flatten
  reopen     close, then one re-entry at the 18:00 ET reopen (15:00 PT) through
             the production continuation path, held to the next 15:45 ET
  overnight  counterfactual: the entry day's flatten is skipped and the trade
             is held to the next session's 15:45 ET flatten (Topstep requires
             flat by 16:10 ET, so this measures the signal, not a legal setup)
  current    production exit (3R target, long to the close, short 60 minutes)
Short families also run with a 4.0x stop (the long stop) so a long horizon
cannot look bad only because of the 1.5x short stop.

Controls and candidates
  control    one synthetic LV3 mark per trading day, at times of day cycled
             from the real family, through the same engine and exits; it
             measures what the 2026 up-trend alone pays a held position
  candidate  production parameters (PI STRICT MNQ + overrides) built from the
             findings, run by the plain production engine with the preset's
             own gates, i.e. exactly what the app would trade

No preset, live setting, store or production strategy is changed; output goes
to the external research tree.  Run: ``python scripts/pi_signal_horizon_study.py``
(about 10 minutes, three workers).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestConfig, BacktestEngine  # noqa: E402
from backend.backtest.robustness import series_stats  # noqa: E402
from backend.data import candle_store, market_data  # noqa: E402
from backend.data.pi_history import load_rows  # noqa: E402
from backend.data.pi_live_audit import load_replay_rows  # noqa: E402
from backend.db.models import Candle, get_commission_rt, get_fees_rt  # noqa: E402
from backend.strategy.pi_signal import PiSignalStrategy  # noqa: E402
from backend.strategy.session_filter import as_new_york  # noqa: E402
from backend.timebase import NEW_YORK, UTC, as_utc  # noqa: E402
from scripts.pi_exhaustive_exit_study import (  # noqa: E402
    CURRENT_EXIT,
    DEFAULT_VALIDATED_STORE,
    ExitSpec,
    SignalSpec,
    _configure_params,
    _path_diagnostics,
)
from scripts.pi_exit_engine_grid_study import (  # noqa: E402
    GridPiSignalStrategy,
    _exit_counts,
    _finite,
    _load_base_params,
    _load_run_candles,
    _monthly_stats,
    _robustness,
    _status,
    trade_rows,
)
from scripts.strategy_long_term_audit import _make_params  # noqa: E402

STUDY_VERSION = "2026-10-02-pi-signal-horizon-v1"
INITIAL_CAPITAL = 50_000.0
PI_SYMBOLS = ("MNQ", "MES")
SESSION_FLATTEN = time(15, 45)
SESSION_END = time(17, 0)
REOPEN = time(18, 0)
WARMUP_DAYS = 7
REPLAY_LIMIT = 20_000

# The 2026-09-15 exhaustive study rows the harness must reproduce exactly
# (same snapshot, history only, the old parameter configuration).
PUBLISHED_STRICT = {
    "MNQ": {"n": 74, "pnl": 3037.24, "pf": 1.6862},
    "MES": {"n": 53, "pnl": 485.53, "pf": 1.3426},
}
STRICT_SIGNALS = SignalSpec("strict", "STRICT · 青π / 粉π", ("青π",), ("粉π",))

FAMILIES: tuple[SignalSpec, ...] = (
    SignalSpec("lv1_long", "LV1 long 淡蓝圈", ("淡蓝圈",), (), None, True),
    SignalSpec("lv2_long", "LV2 long 深蓝圈", ("深蓝圈",), (), None, True),
    SignalSpec("lv3_long", "LV3 long 青π", ("青π",), (), None, True),
    SignalSpec("lv1_short", "LV1 short 紫圈 L1", (), ("紫圈",), (1,)),
    SignalSpec("lv2_short", "LV2 short 紫圈 L2", (), ("紫圈",), (2,)),
    SignalSpec("lv3_short", "LV3 short 粉π", (), ("粉π",)),
)


@dataclass(frozen=True)
class Horizon:
    key: str
    label: str
    exit: ExitSpec
    reopen: bool = False
    overnight: bool = False


def _horizon(key: str, label: str, long_hold: int, short_hold: int, *,
             hard_tp: bool = False, reopen: bool = False, overnight: bool = False,
             short_sl: float = 1.5) -> Horizon:
    return Horizon(
        key=key,
        label=label,
        exit=ExitSpec(key=key, label=label, mode="atr", long_sl=4.0, rr=3.0,
                      short_sl=short_sl, long_hold=long_hold, short_hold=short_hold,
                      hard_tp=hard_tp),
        reopen=reopen,
        overnight=overnight,
    )


HORIZONS: tuple[Horizon, ...] = (
    _horizon("m30", "30 min", 30, 30),
    _horizon("m60", "60 min", 60, 60),
    _horizon("close", "to 15:45 ET close", 0, 0),
    _horizon("reopen", "close + 18:00 ET reopen", 0, 0, reopen=True),
    _horizon("overnight", "overnight to next 15:45 ET", 0, 0, overnight=True),
    _horizon("current", "production (TP 3R, short 60m)", 0, 60, hard_tp=True),
)
SHORT_WIDE: tuple[Horizon, ...] = tuple(
    replace(h, key=f"{h.key}_s4", label=f"{h.label} · short SL 4x",
            exit=replace(h.exit, key=f"{h.exit.key}_s4", short_sl=4.0))
    for h in HORIZONS if h.key != "current"
)
PRESETS: tuple[tuple[str, str], ...] = (
    ("PI STRICT MNQ", "MNQ"),
    ("PI 2MNQ BOTH BEST", "MNQ"),
    ("PI 2MES BOTH BEST", "MES"),
)

# Market-drift control: one synthetic mark per trading day, at times of day
# cycled from the real family, through the same engine and exits.  March to
# September 2026 was a strong up-trend, so any long held overnight earned the
# drift; a family's overnight edge is only its own if it beats this control.
CONTROL_SIDES: dict[str, tuple[str, str]] = {"long": ("lv3_long", "青π"), "short": ("lv3_short", "粉π")}

# Production-parameter candidates built from this study's findings (PI STRICT
# MNQ plus overrides; the preset's own gates).  Production sets one hold per
# direction, so every short kind shares the short hold.
CANDIDATES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("STRICT (as is)", {}),
    ("STRICT + 青π reopen", {"pi_continue_long_kinds": ["青π"]}),
    ("STRICT + 青π reopen · shorts to close",
     {"pi_continue_long_kinds": ["青π"], "pi_short_hold_min": 0}),
    ("STRICT + 青π reopen + 紫圈 L1 short",
     {"pi_continue_long_kinds": ["青π"], "pi_short_kinds": ["粉π", "紫圈"], "pi_short_levels": [1]}),
    ("STRICT + 青π reopen + 紫圈 L1 short · shorts to close",
     {"pi_continue_long_kinds": ["青π"], "pi_short_kinds": ["粉π", "紫圈"], "pi_short_levels": [1],
      "pi_short_hold_min": 0}),
    ("青π long only + reopen",
     {"pi_long_only": True, "pi_short_kinds": [], "pi_continue_long_kinds": ["青π"]}),
)


def horizons_for(family: SignalSpec) -> tuple[Horizon, ...]:
    return HORIZONS + (SHORT_WIDE if family.short_kinds else ())


# ── research harness (production engine; exit policy and replay window only) ──

class HorizonPiStrategy(GridPiSignalStrategy):
    """Production PI entry with the horizon's exit, latest audit marks and a cutoff."""

    def __init__(self, params: Any, variant: ExitSpec, *,
                 replay_rows: Optional[list[dict]] = None,
                 cutoff: Optional[datetime] = None):
        self._grid_variant = variant
        PiSignalStrategy.__init__(self, params, replay_rows=replay_rows)
        if cutoff is not None:
            # A new list: the unfiltered history is a shared module cache.
            self._hist = [(ts, sig) for ts, sig in self._hist if ts <= cutoff]

    def build_reopen_continuation(self, ticket: dict, candle: Candle, max_gap_r: float = 1.0):
        signal = super().build_reopen_continuation(ticket, candle, max_gap_r)
        if signal is not None:
            # The continuation leg keeps the horizon's exit contract.
            signal.exit_policy = self.research_exit_policy(signal.direction)
        return signal


def holds_through_close(entry_time: datetime, now: datetime) -> bool:
    """Overnight counterfactual: skip the session flatten on the entry's NY date only."""
    return as_new_york(now).date() == as_new_york(entry_time).date()


class HorizonEngine(BacktestEngine):
    """Production engine; the overnight horizon skips the entry day's flatten."""

    def __init__(self, *args: Any, overnight: bool = False, **kwargs: Any):
        self.overnight = overnight
        super().__init__(*args, **kwargs)

    def _process_candle(self, candle: Candle):
        if self.overnight:
            position = self._open_position
            # Instance attribute shadows the class switch for this bar only.
            self.CLOSE_WINDOW_ENABLED = not (
                position is not None
                and holds_through_close(position.entry_time, candle.timestamp)
            )
        super()._process_candle(candle)


def _config(params: Any, symbol: str) -> BacktestConfig:
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=symbol,
        commission_rt=get_commission_rt(params.contract_id),
        fees_rt=get_fees_rt(params.contract_id),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80)),
    )


def build_engine(params: Any, horizon: Horizon, symbol: str, *,
                 replay_rows: Optional[list[dict]], cutoff: Optional[datetime]) -> HorizonEngine:
    engine = HorizonEngine(
        config=_config(params, symbol),
        strategy_params=params,
        record_equity=False,
        pi_replay_rows=replay_rows,
        overnight=horizon.overnight,
    )
    engine.trend_follow = HorizonPiStrategy(
        params, horizon.exit, replay_rows=replay_rows, cutoff=cutoff
    )
    engine._pending_max_age = engine.trend_follow.PENDING_TIMEOUT_CANDLES
    return engine


def configure(base: Any, horizon: Horizon, family: SignalSpec) -> Any:
    params = _configure_params(base, horizon.exit, family)
    params.tr_daily_loss_stop = 0
    params.pi_lv2_replace_pi = False
    if horizon.reopen:
        params.pi_continue_long_kinds = list(family.long_kinds)
        params.pi_continue_short_kinds = list(family.short_kinds)
        params.pi_continue_short_levels = (
            list(family.short_levels) if family.short_levels is not None else None
        )
    else:
        params.pi_continue_long_kinds = []
        params.pi_continue_short_kinds = []
        params.pi_continue_short_levels = None
    return params


# ── data window ─────────────────────────────────────────────────────────────

def ny_close(day: date) -> datetime:
    return datetime.combine(day, SESSION_FLATTEN, tzinfo=NEW_YORK)


def latest_window(candles: list[Candle]) -> dict[str, Any]:
    """Last complete session S (a bar at/after 15:45 ET) and the signal cutoff,
    the trading day before S, so every horizon can finish inside the data."""
    sessions = sorted({
        as_new_york(c.timestamp).date()
        for c in candles
        if as_new_york(c.timestamp).time() >= SESSION_FLATTEN
        and as_new_york(c.timestamp).time() < SESSION_END
        and as_new_york(c.timestamp).weekday() < 5
    })
    if len(sessions) < 2:
        raise RuntimeError("fewer than two complete sessions in the candle window")
    last_session = sessions[-1]
    cutoff_day = sessions[-2]
    return {
        "last_session": last_session,
        "cutoff_day": cutoff_day,
        "cutoff_utc": datetime.combine(cutoff_day, time(23, 59, 59), tzinfo=NEW_YORK).astimezone(UTC),
        "candles_end_utc": datetime.combine(last_session, SESSION_END, tzinfo=NEW_YORK).astimezone(UTC),
    }


def _first_signal(rows: list[dict], replay: list[dict], symbol: str) -> datetime:
    source = {"MNQ": "QQQ", "MES": "SPY"}[symbol]
    stamps = [
        datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).astimezone(UTC)
        for r in list(rows) + list(replay)
        if str(r.get("symbol") or "").upper() == source
    ]
    if not stamps:
        raise RuntimeError(f"no PI rows for {symbol}")
    return min(stamps)


def load_latest(symbol: str) -> dict[str, Any]:
    rows = load_rows()
    snapshot = candle_store.load_snapshot(symbol, 1)
    probe_start = snapshot.last_time - timedelta(days=10)
    window = latest_window(candle_store.select_range(snapshot, start=probe_start))
    replay = load_replay_rows(
        datetime(2026, 1, 1, tzinfo=UTC), window["candles_end_utc"],
        future=symbol, limit=REPLAY_LIMIT,
    )
    start = _first_signal(rows, replay, symbol) - timedelta(days=WARMUP_DAYS)
    candles = candle_store.select_range(snapshot, start=start, end=window["candles_end_utc"])
    return {
        "symbol": symbol,
        "candles": candles,
        "replay": replay,
        "window": window,
        "coverage": {
            "store": str(candle_store.STORE_DIR),
            "bars": len(candles),
            "first_bar": as_utc(candles[0].timestamp).isoformat(),
            "last_bar": as_utc(candles[-1].timestamp).isoformat(),
            "store_last_bar": snapshot.last_time.isoformat(),
            "history_rows": len(rows),
            "audit_replay_rows": len(replay),
            "audit_first": replay[0]["ts"] if replay else None,
            "audit_last": replay[-1]["ts"] if replay else None,
            "last_complete_session": window["last_session"].isoformat(),
            "signal_cutoff_day": window["cutoff_day"].isoformat(),
        },
    }


# ── summaries ───────────────────────────────────────────────────────────────

def _trade_r(trade: Any, point_value: float) -> Optional[float]:
    entry = float(trade.entry_price)
    stop = float(getattr(trade, "original_sl_price", None) or trade.sl_price)
    risk = abs(entry - stop) * point_value * max(1, int(getattr(trade, "contracts", 1) or 1))
    return _finite(trade.pnl) / risk if risk > 0 else None


def _crossed_close(trade: Any) -> bool:
    entry = as_new_york(trade.entry_time)
    exit_ = as_new_york(trade.exit_time) if trade.exit_time else None
    return bool(exit_ and (exit_.date() > entry.date() or exit_.time() > SESSION_FLATTEN))


def _is_continuation(trade: Any) -> bool:
    return bool((getattr(trade, "meta", None) or {}).get("pi_continuation"))


def window_problems(trades: Iterable[Any], horizon: Horizon) -> list[str]:
    """Hard checks that the horizon was really applied."""
    problems: list[str] = []
    for trade in trades:
        reason = getattr(getattr(trade, "exit_reason", None), "value", "")
        stamp = as_utc(trade.entry_time).isoformat()
        if not horizon.exit.hard_tp and reason == "tp":
            problems.append(f"{stamp}: target exit while the horizon has no target")
        if _is_continuation(trade):
            entry = as_new_york(trade.entry_time).time()
            if not (REOPEN <= entry <= time(18, 6)):
                problems.append(f"{stamp}: continuation entry {entry} outside the reopen window")
    return problems


def summarize(trades: list[Any], *, symbol: str, point_value: float, horizon: Horizon,
              candles: list[Candle], signals_available: int,
              reopen_events: list[dict]) -> dict[str, Any]:
    rows = trade_rows(trades, symbol)
    robustness = _robustness(rows)
    status, reasons = _status(robustness)
    holds = [
        (as_utc(t.exit_time) - as_utc(t.entry_time)).total_seconds() / 60.0
        for t in trades if t.exit_time
    ]
    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        by_day[as_new_york(t.entry_time).date().isoformat()] += _finite(t.pnl)
    best_two = sum(sorted(by_day.values(), reverse=True)[:2])
    r_values = [r for r in (_trade_r(t, point_value) for t in trades) if r is not None]
    continuation = [t for t in trades if _is_continuation(t)]
    base_legs = [t for t in trades if not _is_continuation(t)]
    crossed = [t for t in base_legs if _crossed_close(t)]
    events = Counter(str(e.get("event")) for e in reopen_events)
    return {
        "robustness": robustness,
        "status": status,
        "status_reasons": reasons,
        "signals_available": signals_available,
        "hold_minutes": {
            "median": round(sorted(holds)[len(holds) // 2], 1) if holds else None,
            "max": round(max(holds), 1) if holds else None,
        },
        "without_best_2_days": round(robustness["baseline"]["pnl"] - best_two, 2),
        "r": {
            "n": len(r_values),
            "total": round(sum(r_values), 3),
            "mean": round(sum(r_values) / len(r_values), 4) if r_values else None,
            "values": [round(v, 4) for v in r_values],
        },
        "continuation": {
            "legs": len(continuation),
            "pnl": round(sum(_finite(t.pnl) for t in continuation), 2),
            "events": dict(events),
        },
        "crossed_close": {
            "count": len(crossed),
            "pnl": round(sum(_finite(t.pnl) for t in crossed), 2),
            "weekend": sum(1 for t in crossed
                           if as_new_york(t.exit_time).date() - as_new_york(t.entry_time).date()
                           > timedelta(days=1)),
        },
        "exit_counts": _exit_counts(rows),
        "monthly": _monthly_stats(rows),
        "diagnostics": _path_diagnostics(trades, candles, horizon.exit),
        "fingerprint": [
            (as_utc(t.entry_time).isoformat(), as_utc(t.exit_time).isoformat() if t.exit_time else None,
             round(_finite(t.pnl), 6))
            for t in sorted(trades, key=lambda item: as_utc(item.entry_time))
        ],
    }


def signals_available(strategy: HorizonPiStrategy, symbol: str, family: SignalSpec) -> int:
    count = 0
    for _ts, sig in strategy._hist:
        if getattr(sig, "future", None) != symbol:
            continue
        kind = getattr(sig, "kind", "")
        if getattr(sig, "direction", 0) > 0:
            count += kind in family.long_kinds
            continue
        if kind not in family.short_kinds:
            continue
        if family.short_levels is not None and kind == "紫圈":
            count += getattr(sig, "level", None) in family.short_levels
        else:
            count += 1
    return count


# ── workers ─────────────────────────────────────────────────────────────────

_WORKER: dict[str, Any] = {}


def _worker_init(symbol: str, store_dir: str) -> None:
    global _WORKER
    logging.disable(logging.CRITICAL)
    candle_store.STORE_DIR = Path(store_dir)
    data = load_latest(symbol)
    data["base"] = _load_base_params(symbol)
    _WORKER = data


def _run_matrix_job(job: tuple[str, str]) -> dict[str, Any]:
    family_key, horizon_key = job
    family = next(f for f in FAMILIES if f.key == family_key)
    horizon = next(h for h in horizons_for(family) if h.key == horizon_key)
    symbol = _WORKER["symbol"]
    params = configure(_WORKER["base"], horizon, family)
    engine = build_engine(params, horizon, symbol, replay_rows=_WORKER["replay"],
                          cutoff=_WORKER["window"]["cutoff_utc"])
    trades = engine.run(_WORKER["candles"]).trades
    problems = window_problems(trades, horizon)
    if problems:
        raise RuntimeError(f"{symbol} {family_key}/{horizon_key}: " + "; ".join(problems[:5]))
    summary = summarize(
        trades, symbol=symbol, point_value=float(engine.POINT_VALUE), horizon=horizon,
        candles=_WORKER["candles"],
        signals_available=signals_available(engine.trend_follow, symbol, family),
        reopen_events=list(engine.pi_reopen_events),
    )
    return {"study": "matrix", "symbol": symbol, "family": family_key,
            "family_label": family.label, "horizon": horizon_key,
            "horizon_label": horizon.label, **summary}


def _run_preset_job(preset: str) -> dict[str, Any]:
    """The preset as the app runs it (its own gates), latest data, one contract."""
    symbol = _WORKER["symbol"]
    presets = json.loads(market_data.repository_data_path("presets.json").read_text(encoding="utf-8"))
    params = _make_params({"strategy": "pi", "preset": preset, "overrides": {}}, symbol,
                          presets.get("presets", {}))
    engine = BacktestEngine(config=_config(params, symbol), strategy_params=params,
                            record_equity=False, pi_replay_rows=_WORKER["replay"])
    trades = engine.run(_WORKER["candles"]).trades
    rows = trade_rows(trades, symbol)
    robustness = _robustness(rows)
    status, reasons = _status(robustness)
    by_kind: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_kind[f"{row['direction']} {row['kind']}"].append(row["pnl"])
    return {
        "study": "preset", "symbol": symbol, "preset": preset,
        "robustness": robustness, "status": status, "status_reasons": reasons,
        "exit_counts": _exit_counts(rows), "monthly": _monthly_stats(rows),
        "by_kind": {k: {"n": len(v), "pnl": round(sum(v), 2),
                        "pf": round(series_stats(v)["pf"], 4)} for k, v in sorted(by_kind.items())},
        "continuation_legs": sum(1 for t in trades if _is_continuation(t)),
    }


def control_rows(real: list, candles: list[Candle], symbol: str, kind: str,
                 cutoff_day: date) -> list[dict]:
    """One synthetic mark per trading day at times of day cycled from the real
    family (history-shaped rows, converted by the production row parser)."""
    source = {"MNQ": "QQQ", "MES": "SPY"}[symbol]
    marks = [ts for ts, sig in real if sig.future == symbol and sig.kind == kind]
    if not marks:
        return []
    times = [as_new_york(ts).time().replace(second=0, microsecond=0) for ts in marks]
    first = min(as_new_york(ts).date() for ts in marks)
    days = sorted({
        as_new_york(c.timestamp).date() for c in candles
        if time(10, 0) <= as_new_york(c.timestamp).time() < time(15, 30)
        and as_new_york(c.timestamp).weekday() < 5
    })
    rows = []
    for index, day in enumerate(d for d in days if first <= d <= cutoff_day):
        ts = datetime.combine(day, times[index % len(times)], tzinfo=NEW_YORK).astimezone(UTC)
        rows.append({"id": f"control-{symbol}-{day.isoformat()}", "ts": ts.isoformat(),
                     "symbol": source, "content": "",
                     "marks": [{"kind": kind, "size": "Level 3", "level": 3, "count": 1}]})
    return rows


def _run_control_job(job: tuple[str, str]) -> dict[str, Any]:
    from backend.strategy.pi_signal import _rows_to_signals

    side, horizon_key = job
    family_key, kind = CONTROL_SIDES[side]
    family = next(f for f in FAMILIES if f.key == family_key)
    horizon = next(h for h in horizons_for(family) if h.key == horizon_key)
    symbol = _WORKER["symbol"]
    params = configure(_WORKER["base"], horizon, family)
    engine = build_engine(params, horizon, symbol, replay_rows=_WORKER["replay"],
                          cutoff=_WORKER["window"]["cutoff_utc"])
    rows = control_rows(engine.trend_follow._hist, _WORKER["candles"], symbol, kind,
                        _WORKER["window"]["cutoff_day"])
    engine.trend_follow._hist = _rows_to_signals(rows)
    engine.trend_follow._hist_i = 0
    trades = engine.run(_WORKER["candles"]).trades
    problems = window_problems(trades, horizon)
    if problems:
        raise RuntimeError(f"{symbol} control {side}/{horizon_key}: " + "; ".join(problems[:5]))
    summary = summarize(
        trades, symbol=symbol, point_value=float(engine.POINT_VALUE), horizon=horizon,
        candles=_WORKER["candles"], signals_available=len(rows),
        reopen_events=list(engine.pi_reopen_events),
    )
    return {"study": "control", "symbol": symbol, "family": f"control_{side}",
            "family_label": f"CONTROL {side} (daily, {kind} time profile)",
            "horizon": horizon_key, "horizon_label": horizon.label, **summary}


def _run_candidate_job(name: str) -> dict[str, Any]:
    symbol = _WORKER["symbol"]
    overrides = dict(next(o for n, o in CANDIDATES if n == name))
    presets = json.loads(market_data.repository_data_path("presets.json").read_text(encoding="utf-8"))
    params = _make_params({"strategy": "pi", "preset": "PI STRICT MNQ", "overrides": overrides},
                          symbol, presets.get("presets", {}))
    engine = BacktestEngine(config=_config(params, symbol), strategy_params=params,
                            record_equity=False, pi_replay_rows=_WORKER["replay"])
    trades = engine.run(_WORKER["candles"]).trades
    rows = trade_rows(trades, symbol)
    robustness = _robustness(rows)
    status, reasons = _status(robustness)
    by_kind: dict[str, list[float]] = defaultdict(list)
    for trade, row in zip(sorted(trades, key=lambda t: as_utc(t.entry_time)), rows):
        leg = " (reopen)" if _is_continuation(trade) else ""
        by_kind[f"{row['direction']} {row['kind']}{leg}"].append(row["pnl"])
    return {
        "study": "candidate", "symbol": symbol, "candidate": name, "overrides": overrides,
        "robustness": robustness, "status": status, "status_reasons": reasons,
        "exit_counts": _exit_counts(rows), "monthly": _monthly_stats(rows),
        "by_kind": {k: {"n": len(v), "pnl": round(sum(v), 2),
                        "pf": round(series_stats(v)["pf"], 4)} for k, v in sorted(by_kind.items())},
        "continuation_legs": sum(1 for t in trades if _is_continuation(t)),
    }


def _run_job(job: tuple[str, ...]) -> dict[str, Any]:
    if job[0] == "preset":
        return _run_preset_job(job[1])
    if job[0] == "candidate":
        return _run_candidate_job(job[1])
    if job[0] == "control":
        return _run_control_job((job[1], job[2]))
    return _run_matrix_job((job[1], job[2]))


def _fingerprint(trades: Iterable[Any]) -> list[tuple]:
    return [
        (as_utc(t.entry_time).isoformat(), as_utc(t.exit_time).isoformat() if t.exit_time else None,
         round(float(t.entry_price), 8), round(_finite(t.pnl), 8),
         getattr(getattr(t, "exit_reason", None), "value", None))
        for t in sorted(trades, key=lambda item: as_utc(item.entry_time))
    ]


def _archive_parity(symbol: str, store_dir: str) -> dict[str, Any]:
    """Published 2026-09-15 strict row, trade-for-trade, on its own snapshot."""
    logging.disable(logging.CRITICAL)
    # Reading a snapshot must not drop a derived columns file into the archive.
    candle_store._write_sidecar = lambda *args, **kwargs: None
    candle_store.STORE_DIR = Path(store_dir)
    rows = load_rows()
    candles, coverage = _load_run_candles(symbol, rows)
    params = _configure_params(_load_base_params(symbol), CURRENT_EXIT, STRICT_SIGNALS)
    harness = HorizonEngine(config=_config(params, symbol), strategy_params=params,
                            record_equity=False)
    harness.trend_follow = HorizonPiStrategy(params, CURRENT_EXIT)
    harness._pending_max_age = harness.trend_follow.PENDING_TIMEOUT_CANDLES
    stats = _robustness(trade_rows(harness.run(candles).trades, symbol))["baseline"]
    expected = PUBLISHED_STRICT[symbol]
    match = (stats["n"] == expected["n"] and abs(stats["pnl"] - expected["pnl"]) < 0.01
             and abs(stats["pf"] - expected["pf"]) < 0.0001)
    return {"symbol": symbol, "store": store_dir, "match": match,
            "got": {k: stats[k] for k in ("n", "pnl", "pf")}, "expected": expected,
            "bars": coverage["bars"]}


def _latest_parity(symbol: str, store_dir: str) -> dict[str, Any]:
    """Production engine vs the harness on the latest data and audit marks."""
    logging.disable(logging.CRITICAL)
    candle_store.STORE_DIR = Path(store_dir)
    data = load_latest(symbol)
    params = _configure_params(_load_base_params(symbol), CURRENT_EXIT, STRICT_SIGNALS)
    production = BacktestEngine(config=_config(params, symbol), strategy_params=params,
                                record_equity=False, pi_replay_rows=data["replay"])
    prod_trades = production.run(data["candles"]).trades
    harness = build_engine(params, Horizon("parity", "parity", CURRENT_EXIT), symbol,
                           replay_rows=data["replay"], cutoff=None)
    harness_trades = harness.run(data["candles"]).trades
    match = _fingerprint(prod_trades) == _fingerprint(harness_trades)
    replay_used = sum(
        1 for t in prod_trades
        if str(((getattr(t, "meta", None) or {}).get("pi") or {}).get("signal_ts") or "") >= "2026-09-15"
    )
    return {"symbol": symbol, "match": match, "production_trades": len(prod_trades),
            "harness_trades": len(harness_trades), "trades_after_0915": replay_used,
            "coverage": data["coverage"]}


def _isolated(fn, *args) -> Any:
    """One fresh process per parity check: separate candle caches and stores."""
    with ProcessPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn, *args).result()


def dimension_checks(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each horizon must change something; identical rows are reported."""
    out = []
    grouped: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for r in results:
        if r["study"] == "matrix":
            grouped[(r["symbol"], r["family"])][r["horizon"]] = r
    for (symbol, family), rows in sorted(grouped.items()):
        keys = sorted(rows)
        identical = [
            (a, b) for i, a in enumerate(keys) for b in keys[i + 1:]
            if rows[a]["fingerprint"] == rows[b]["fingerprint"] and rows[a]["fingerprint"]
        ]
        out.append({
            "symbol": symbol, "family": family, "identical_pairs": identical,
            "reopen_legs": rows.get("reopen", {}).get("continuation", {}).get("legs"),
            "reopen_events": rows.get("reopen", {}).get("continuation", {}).get("events"),
            "overnight_crossed_close": rows.get("overnight", {}).get("crossed_close", {}).get("count"),
        })
    return out


def per_signal(row: dict[str, Any]) -> Optional[float]:
    """Net PnL per traded source signal (a reopen leg belongs to its source)."""
    base = row["robustness"]["baseline"]["n"] - row["continuation"]["legs"]
    return round(row["robustness"]["baseline"]["pnl"] / base, 2) if base > 0 else None


def drift_comparison(matrix: list[dict[str, Any]], controls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Real LV3 family vs the daily control, per traded signal, and each
    horizon's change against holding to the close."""
    out = []
    for control in controls:
        side = control["family"].removeprefix("control_")
        family_key = CONTROL_SIDES[side][0]
        real = next((r for r in matrix if r["symbol"] == control["symbol"]
                     and r["family"] == family_key and r["horizon"] == control["horizon"]), None)
        real_close = next((r for r in matrix if r["symbol"] == control["symbol"]
                           and r["family"] == family_key and r["horizon"] == "close"), None)
        ctrl_close = next((c for c in controls if c["symbol"] == control["symbol"]
                           and c["family"] == control["family"] and c["horizon"] == "close"), None)
        if real is None or real_close is None or ctrl_close is None:
            continue
        r_sig, c_sig = per_signal(real), per_signal(control)
        r_close, c_close = per_signal(real_close), per_signal(ctrl_close)
        out.append({
            "symbol": control["symbol"], "side": side, "horizon": control["horizon"],
            "real_per_signal": r_sig, "control_per_signal": c_sig,
            "real_vs_close": None if r_sig is None or r_close is None else round(r_sig - r_close, 2),
            "control_vs_close": None if c_sig is None or c_close is None else round(c_sig - c_close, 2),
            "real_signals": real["robustness"]["baseline"]["n"] - real["continuation"]["legs"],
            "control_signals": control["robustness"]["baseline"]["n"] - control["continuation"]["legs"],
            "control_pf": control["robustness"]["baseline"]["pf"],
        })
    return out


def pooled_r(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """MNQ + MES per family/horizon in R (net PnL / initial risk), one row each."""
    pooled: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in results:
        if r["study"] == "matrix":
            pooled[(r["family"], r["horizon"])].extend(r["r"]["values"])
    out = []
    for (family, horizon), values in sorted(pooled.items()):
        stats = series_stats(values)
        out.append({"family": family, "horizon": horizon, "n": len(values),
                    "total_r": round(sum(values), 2),
                    "mean_r": round(sum(values) / len(values), 3) if values else None,
                    "pf_r": round(stats["pf"], 3), "win": round(stats["win"], 3)})
    return out


# ── report ──────────────────────────────────────────────────────────────────

def _cell(r: dict[str, Any]) -> str:
    b = r["robustness"]["baseline"]
    if not b["n"]:
        return "0"
    return f"{b['n']} / ${b['pnl']:,.0f} / {b['pf']:.2f}"


def write_report(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# PI signal family × holding horizon (latest data)",
        "",
        f"- Version `{report['study_version']}`, generated `{report['generated_at']}`.",
        "- Production `BacktestEngine` and PI entry; one position at a time; one contract;",
        "  canonical commission + fees; 14-tick round-trip stress; matrix gates off (see docstring).",
        "",
        "## Reproduction",
        "",
    ]
    for item in report["parity"]["archive"]:
        lines.append(f"- Published 2026-09-15 strict `{item['symbol']}`: match={item['match']} "
                     f"got {item['got']} expected {item['expected']}.")
    for item in report["parity"]["latest"]:
        lines.append(f"- Production vs harness on latest data `{item['symbol']}`: match={item['match']} "
                     f"({item['production_trades']} trades, {item['trades_after_0915']} after 2026-09-15).")
    lines += ["", "## Data", ""]
    for symbol, cov in report["coverage"].items():
        lines.append(f"- `{symbol}`: bars {cov['first_bar']} → {cov['last_bar']} (store ends "
                     f"{cov['store_last_bar']}); history {cov['history_rows']} rows + audit "
                     f"{cov['audit_replay_rows']} rows ({cov['audit_first']} → {cov['audit_last']}); "
                     f"signals through {cov['signal_cutoff_day']}, exits through {cov['last_complete_session']}.")
    lines += ["", "## Presets on the latest data (their own gates, one contract)", "",
              "| Preset | Symbol | n | Net | PF | 14t PF | MaxDD | WF PF | Status |",
              "|---|---|---:|---:|---:|---:|---:|---|---|"]
    for r in report["presets"]:
        b, s = r["robustness"]["baseline"], r["robustness"]["stress_14t"]
        wf = " / ".join(f"{seg['pf']:.2f}" for seg in r["robustness"]["walk_forward"]["segments"])
        lines.append(f"| {r['preset']} | {r['symbol']} | {b['n']} | ${b['pnl']:,.0f} | {b['pf']:.2f} | "
                     f"{s['pf']:.2f} | ${b['max_dd']:,.0f} | {wf} | {r['status']} |")
    for symbol in PI_SYMBOLS:
        rows = [r for r in report["matrix"] if r["symbol"] == symbol]
        if not rows:
            continue
        keys = [h.key for h in HORIZONS] + [h.key for h in SHORT_WIDE]
        lines += ["", f"## {symbol}: n / net / PF per horizon", "",
                  "| Family | " + " | ".join(keys) + " |",
                  "|---|" + "---|" * len(keys)]
        for family in FAMILIES:
            cells = []
            for key in keys:
                match = [r for r in rows if r["family"] == family.key and r["horizon"] == key]
                cells.append(_cell(match[0]) if match else "")
            lines.append(f"| {family.label} | " + " | ".join(cells) + " |")
        lines += ["", f"### {symbol} detail", "",
                  "| Family | Horizon | signals | n | Net | PF | 14t PF | MaxDD | w/o best 2d | WF PF | hold med (m) | crossed close | reopen legs | Status |",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|"]
        for r in rows:
            b, s = r["robustness"]["baseline"], r["robustness"]["stress_14t"]
            wf = " / ".join(f"{seg['pf']:.2f}" for seg in r["robustness"]["walk_forward"]["segments"])
            lines.append(
                f"| {r['family_label']} | {r['horizon_label']} | {r['signals_available']} | {b['n']} | "
                f"${b['pnl']:,.0f} | {b['pf']:.2f} | {s['pf']:.2f} | ${b['max_dd']:,.0f} | "
                f"${r['without_best_2_days']:,.0f} | {wf} | {r['hold_minutes']['median']} | "
                f"{r['crossed_close']['count']} | {r['continuation']['legs']} | {r['status']} |")
    lines += ["", "## Market-drift control (daily synthetic LV3 marks, same engine and exits)", "",
              "Per traded source signal; `vs close` = this horizon minus holding to the 15:45 ET close.",
              "",
              "| Symbol | Side | Horizon | real $/signal | control $/signal | real vs close | control vs close | real n | control n | control PF |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for d in report["drift"]:
        lines.append(f"| {d['symbol']} | {d['side']} | {d['horizon']} | {d['real_per_signal']} | "
                     f"{d['control_per_signal']} | {d['real_vs_close']} | {d['control_vs_close']} | "
                     f"{d['real_signals']} | {d['control_signals']} | {d['control_pf']:.2f} |")
    lines += ["", "## Production-parameter candidates (PI STRICT MNQ + overrides, its own gates)", "",
              "| Candidate | Symbol | n | reopen legs | Net | PF | 14t PF | MaxDD | WF PF | Status |",
              "|---|---|---:|---:|---:|---:|---:|---:|---|---|"]
    for r in report["candidates"]:
        b, s = r["robustness"]["baseline"], r["robustness"]["stress_14t"]
        wf = " / ".join(f"{seg['pf']:.2f}" for seg in r["robustness"]["walk_forward"]["segments"])
        lines.append(f"| {r['candidate']} | {r['symbol']} | {b['n']} | {r['continuation_legs']} | "
                     f"${b['pnl']:,.0f} | {b['pf']:.2f} | {s['pf']:.2f} | ${b['max_dd']:,.0f} | {wf} | "
                     f"{r['status']} |")
    lines += ["", "## MNQ + MES pooled in R (net PnL ÷ initial risk)", "",
              "| Family | Horizon | n | total R | mean R | PF (R) | win |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for r in report["pooled_r"]:
        lines.append(f"| {r['family']} | {r['horizon']} | {r['n']} | {r['total_r']} | {r['mean_r']} | "
                     f"{r['pf_r']} | {r['win']:.0%} |")
    lines += ["", "## Dimension checks", ""]
    for d in report["dimension_checks"]:
        lines.append(f"- {d['symbol']} {d['family']}: identical {d['identical_pairs'] or 'none'}; "
                     f"reopen legs {d['reopen_legs']} {d['reopen_events']}; overnight crossed close "
                     f"{d['overnight_crossed_close']}.")
    lines += ["", "PI history covers under 12 months; same-sample winners are exploratory, "
              "and families with n < 40 cannot be ranked.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(report: dict[str, Any], path: Path) -> None:
    fields = ["symbol", "family", "horizon", "signals", "n", "pnl", "pf", "stress_14t_pf", "max_dd",
              "without_best_2_days", "wf_pass", "hold_median_min", "crossed_close", "reopen_legs",
              "mfe_r_median", "mae_r_median", "status"]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for r in report["matrix"]:
            b = r["robustness"]["baseline"]
            writer.writerow({
                "symbol": r["symbol"], "family": r["family"], "horizon": r["horizon"],
                "signals": r["signals_available"], "n": b["n"], "pnl": b["pnl"], "pf": b["pf"],
                "stress_14t_pf": r["robustness"]["stress_14t"]["pf"], "max_dd": b["max_dd"],
                "without_best_2_days": r["without_best_2_days"],
                "wf_pass": r["robustness"]["walk_forward"]["pass"],
                "hold_median_min": r["hold_minutes"]["median"],
                "crossed_close": r["crossed_close"]["count"],
                "reopen_legs": r["continuation"]["legs"],
                "mfe_r_median": r["diagnostics"].get("mfe_r_median"),
                "mae_r_median": r["diagnostics"].get("mae_r_median"),
                "status": r["status"],
            })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default="MNQ,MES")
    parser.add_argument("--store-dir", default=str(candle_store.STORE_DIR),
                        help="latest working store (default: the canonical store)")
    parser.add_argument("--archive-dir", default=str(DEFAULT_VALIDATED_STORE),
                        help="snapshot of the published 2026-09-15 study")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--out", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.disable(logging.CRITICAL)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if not symbols or set(symbols) - set(PI_SYMBOLS):
        raise SystemExit(f"symbols must be a subset of {PI_SYMBOLS}")

    report: dict[str, Any] = {
        "study_version": STUDY_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "parity": {"archive": [], "latest": []},
        "coverage": {}, "presets": [], "matrix": [], "controls": [], "candidates": [],
    }
    for symbol in symbols:
        print(f"[{symbol}] reproducing the published 2026-09-15 strict row...", flush=True)
        archive = _isolated(_archive_parity, symbol, args.archive_dir)
        report["parity"]["archive"].append(archive)
        print(f"  {archive}", flush=True)
        if not archive["match"]:
            raise SystemExit(f"{symbol}: harness does not reproduce the published row; stop.")
        print(f"[{symbol}] production vs harness on the latest data...", flush=True)
        latest = _isolated(_latest_parity, symbol, args.store_dir)
        report["parity"]["latest"].append({k: v for k, v in latest.items() if k != "coverage"})
        report["coverage"][symbol] = latest["coverage"]
        print(f"  match={latest['match']} production={latest['production_trades']} "
              f"after 09-15={latest['trades_after_0915']} {latest['coverage']}", flush=True)
        if not latest["match"]:
            raise SystemExit(f"{symbol}: harness differs from production on the latest data; stop.")

    buckets = {"preset": "presets", "matrix": "matrix", "control": "controls", "candidate": "candidates"}
    for symbol in symbols:
        jobs: list[tuple[str, ...]] = [("preset", name) for name, sym in PRESETS if sym == symbol]
        jobs += [("candidate", name) for name, _ in CANDIDATES]
        jobs += [("matrix", f.key, h.key) for f in FAMILIES for h in horizons_for(f)]
        jobs += [("control", side, h.key) for side, (family_key, _) in CONTROL_SIDES.items()
                 for h in horizons_for(next(f for f in FAMILIES if f.key == family_key))]
        with ProcessPoolExecutor(max_workers=max(1, args.workers), initializer=_worker_init,
                                 initargs=(symbol, args.store_dir)) as pool:
            futures = [pool.submit(_run_job, job) for job in jobs]
            for index, future in enumerate(as_completed(futures), 1):
                result = future.result()
                report[buckets[result["study"]]].append(result)
                b = result["robustness"]["baseline"]
                label = (result.get("preset") or result.get("candidate")
                         or f"{result['family']}/{result['horizon']}")
                print(f"[{symbol} {index}/{len(jobs)}] {label}: n={b['n']} pnl=${b['pnl']:,.0f} "
                      f"PF={b['pf']:.2f}", flush=True)

    order = {h.key: i for i, h in enumerate(HORIZONS + SHORT_WIDE)}
    family_order = {f.key: i for i, f in enumerate(FAMILIES)}
    report["matrix"].sort(key=lambda r: (r["symbol"], family_order[r["family"]], order[r["horizon"]]))
    report["controls"].sort(key=lambda r: (r["symbol"], r["family"], order[r["horizon"]]))
    candidate_order = {name: i for i, (name, _) in enumerate(CANDIDATES)}
    report["candidates"].sort(key=lambda r: (r["symbol"], candidate_order[r["candidate"]]))
    report["drift"] = drift_comparison(report["matrix"], report["controls"])
    report["dimension_checks"] = dimension_checks(report["matrix"])
    report["pooled_r"] = pooled_r(report["matrix"])
    if not any((d["overnight_crossed_close"] or 0) > 0 for d in report["dimension_checks"]):
        raise SystemExit("no overnight trade crossed a close: the overnight horizon was not applied")

    stamp = datetime.now().strftime("%Y%m%d")
    output = Path(args.out) if args.out else market_data.derived_path(
        "research", f"pi_signal_horizon_study_{stamp}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_csv(report, output.with_suffix(".csv"))
    write_report(report, output.with_suffix(".md"))
    print(f"JSON: {output}\nCSV: {output.with_suffix('.csv')}\nReport: {output.with_suffix('.md')}", flush=True)


if __name__ == "__main__":
    main()
