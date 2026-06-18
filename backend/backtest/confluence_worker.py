# ============================================================
# 文件: backend/backtest/confluence_worker.py
# 狀態: v0.25 (single-backtest off-loaded to a dedicated child process)
# 關聯文件:
#   ← backend/api/routes.py                (submits jobs via ProcessPoolExecutor)
#   ← backend/backtest/confluence_backtest.py (ConfluenceBacktester + timeline)
# ============================================================
"""Runs ONE confluence backtest inside a dedicated child process.

Why a separate process and not a thread?  A web backtest is CPU-bound pure
Python.  Under the GIL a worker *thread* still holds the interpreter lock for
long stretches, starving the FastAPI event loop — so fetching data / live
trading / drawing the chart all freeze until the backtest finishes (the
"stuck then suddenly moved" symptom).  A child *process* has its own GIL, so
the web server stays fully responsive while it computes.

The process is kept alive between calls (a max_workers=1 pool in routes.py),
and this module caches the candle set + the slow zone timeline in module-level
globals, so repeated runs on the SAME data (different model / RR / band / trail)
skip the detector pass entirely — identical to the in-process cache it replaces.
"""

from __future__ import annotations

import logging
import math
import sys
import time as _time
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── per-process cache (one candle set + its zone timeline) ──
_W: dict = {"ckey": None, "candles": None, "tkey": None, "timeline": None}


def _ensure_logging() -> None:
    """Child processes start with no logging handlers, so the [ZoneTimeline]
    heartbeat would vanish.  Attach a stderr handler that shares the parent's
    console (Windows spawn inherits the console), so progress stays visible."""
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s [bt-proc] %(name)s: %(message)s",
                                          "%H:%M:%S"))
        root.addHandler(h)
        root.setLevel(logging.INFO)


def _get_timeline(candles, timeframes, tick, depth):
    """Reuse the cached zone timeline when the candle set is unchanged."""
    from backend.backtest.confluence_backtest import build_zone_timeline
    key = (len(candles),
           candles[0].timestamp if candles else None,
           candles[-1].timestamp if candles else None,
           tuple(timeframes), float(tick), int(depth))
    if _W["tkey"] == key and _W["timeline"] is not None:
        logger.info(f"[BTWorker] reusing cached zone timeline ({len(candles)} candles) — skipped rebuild")
        return _W["timeline"]
    timeline = build_zone_timeline(candles, timeframes, tick, depth)
    _W["tkey"] = key
    _W["timeline"] = timeline
    return timeline


def run_job(ckey, candles_or_none, params: dict) -> dict:
    """Entry point invoked in the child process.

    ``candles_or_none`` is the full sorted candle list ONLY when the caller
    detected the data changed (keyed by ``ckey``); otherwise None and we reuse
    the cached set — so we don't re-pickle ~60k candles on every run.
    Returns a plain (picklable, FastAPI-free) dict the parent turns into a
    BacktestResponse."""
    _ensure_logging()
    _t0 = _time.perf_counter()

    if candles_or_none is not None:
        _W["ckey"] = ckey
        _W["candles"] = candles_or_none
        _W["tkey"] = None          # candle set changed → invalidate timeline
        _W["timeline"] = None
        logger.info(f"[BTWorker] received {len(candles_or_none)} candles (key={ckey})")
    candles = _W["candles"]
    if candles is None:
        raise RuntimeError("backtest worker has no candles cached; resend candles")

    from backend.db.models import (
        get_tick_size, get_commission_rt, get_fees_rt, _extract_symbol, BacktestConfig,
    )
    from backend.strategy.consolidation import timeframes_for_base
    from backend.strategy.confluence import ConfluenceConfig, MAX_RECENCY_DEPTH
    from backend.strategy.confluence_scorer import resolve_scorer
    from backend.backtest.confluence_backtest import (
        ConfluenceBacktester, ConfluenceBacktestConfig,
    )

    p = params                       # plain dict of request fields + precomputed bits
    contract_id = p["contract_id"]
    contract_size = int(p["contract_size"])
    rr_grid = p.get("rr_grid") or None
    tick = get_tick_size(contract_id)
    base = max(1, int(p.get("conf_base_minutes") or 1))
    timeframes = timeframes_for_base(base)

    scorer = resolve_scorer(bool(p.get("conf_use_scorer", True)), rr_grid)

    min_score = 0.0
    cmp = p.get("conf_min_prob")
    if cmp and 0.0 < cmp < 1.0:
        min_score = math.log(cmp / (1.0 - cmp))

    sig_cfg = ConfluenceConfig(
        band_ticks=p["conf_band_ticks"],
        min_distinct_tf=p["conf_min_distinct_tf"],
        rr=p["conf_rr"],
    )
    sig_cfg.direction_mode = "auto"
    sig_cfg.tick_size = tick
    sig_cfg.ev_floor = p.get("conf_ev_floor")
    sig_cfg.rr_grid = tuple(rr_grid) if rr_grid else None
    sig_cfg.enable_breakout = bool(p.get("conf_enable_breakout", True))

    run_cfg = ConfluenceBacktestConfig(
        wait_minutes=p["conf_wait_minutes"], min_score=min_score,
        base_minutes=base, timeframes=timeframes,
        one_trade_per_session_direction=bool(p.get("conf_session_limit", True)),
        trail_trigger_pct=float(p.get("conf_trail_trigger_pct", 0.0) or 0.0),
        trail_lock_pct=float(p.get("conf_trail_lock_pct", 0.0) or 0.0),
        full_tp_lock=int(p.get("conf_full_tp_lock", 0) or 0),
    )
    bt_cfg = BacktestConfig(
        initial_capital=p["initial_capital"],
        symbol=_extract_symbol(contract_id),
        commission_rt=get_commission_rt(contract_id),
        fees_rt=get_fees_rt(contract_id),
    )

    timeline = _get_timeline(candles, timeframes, tick, MAX_RECENCY_DEPTH)
    bt = ConfluenceBacktester(
        signal_cfg=sig_cfg, run_cfg=run_cfg, contract_id=contract_id,
        contract_size=contract_size, bt_config=bt_cfg, scorer=scorer,
    )
    result = bt.run(candles, zones_timeline=timeline)

    symbol_label = "/" + bt_cfg.symbol
    trades = []
    for t in result.trades:
        trades.append({
            "trade_id": t.trade_id,
            "strategy": "confluence",
            "symbol": symbol_label,
            "size": t.contracts,
            "direction": t.direction.value if t.direction else "",
            "entry_price": t.entry_price,
            "entry_time": t.entry_time.isoformat() if t.entry_time else "",
            "exit_price": t.exit_price,
            "exit_time": t.exit_time.isoformat() if t.exit_time else None,
            "sl_price": t.sl_price,
            "tp_price": t.tp_price,
            "original_sl_price": getattr(t, "original_sl_price", None) or t.sl_price,
            "original_tp_price": getattr(t, "original_tp_price", None) or t.tp_price,
            "pnl": t.pnl,
            "commission": t.commission,
            "fees": t.fees,
            "exit_reason": t.exit_reason.value if t.exit_reason else None,
            "zone_id": t.zone_id,
            "zone_source": getattr(t, "zone_source", "confluence"),
            "vol_ratio": getattr(t, "vol_ratio", 0.0),
            "is_big_trend": getattr(t, "is_big_trend", False),
        })

    m = result.metrics
    metrics = {
        "total_trades": m.total_trades, "wins": m.wins, "losses": m.losses,
        "win_rate": m.win_rate, "avg_win": m.avg_win, "avg_loss": m.avg_loss,
        "avg_rr_ratio": m.avg_rr_ratio, "expectancy": m.expectancy,
        "max_drawdown": m.max_drawdown, "max_drawdown_pct": m.max_drawdown_pct,
        "calmar_ratio": m.calmar_ratio, "profit_factor": m.profit_factor,
        "max_consecutive_losses": m.max_consecutive_losses, "total_pnl": m.total_pnl,
        "total_gain": getattr(m, "total_gain", 0.0), "total_loss": getattr(m, "total_loss", 0.0),
        "daily_pnl": m.daily_pnl or {},
    }

    equity = []
    cap = p["initial_capital"]
    if candles:
        equity.append([candles[0].timestamp.timestamp() * 1000, cap])
    for t in sorted(result.trades, key=lambda x: (x.exit_time or x.entry_time)):
        cap += (t.pnl or 0.0)
        ts = (t.exit_time or t.entry_time)
        if ts:
            equity.append([ts.timestamp() * 1000, round(cap, 2)])

    logger.info(f"[BTWorker] backtest done in {_time.perf_counter() - _t0:.1f}s "
                f"({len(trades)} trades)")
    return {"metrics": metrics, "trades": trades, "equity": equity}
