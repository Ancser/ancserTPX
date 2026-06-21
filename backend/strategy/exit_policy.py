# ============================================================
# 文件: backend/strategy/exit_policy.py
# 狀態: v1.0.6 (confluence "Style" — break-even / trail / lock exit policy)
# 用途: SINGLE source of truth for the optional exit-policy ("保本/lock") that
#       the ML CONFLUENCE panel's STYLE section toggles. Backtest and live call
#       the SAME functions so the trailing math is identical (live == backtest).
# 關聯文件:
#   ← backend/backtest/confluence_backtest.py (per-candle exit management)
#   ← backend/live/engine.py                  (live SL-order modification)
#   ← backend/api/routes.py                    (conf_* request params)
# ============================================================
"""Confluence exit-policy ("Style") — break-even / trailing-SL / full-TP lock.

All knobs default to a no-op so an all-OFF Style == the original confluence
behaviour (structural SL/TP, one-shot, no trailing). Turning them on lets the
user A/B the effect in backtest and mirror it live.

The trailing rule mirrors the trend engine's one-time break-even trigger, but
expressed in FRACTIONS OF THE TP DISTANCE (scale-free) because confluence TP is
RR-based and varies per trade:
  * trail_trigger_pct: once price has moved this fraction of the entry→TP
    distance in our favour, the trail fires ONCE;
  * trail_lock_pct: on firing, SL jumps to entry ± (this fraction × TP distance)
    — a small locked profit (0.0 = pure break-even at entry).
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.db.models import Direction


@dataclass
class ConfluenceExitStyle:
    """Optional exit-policy knobs. Defaults = original behaviour (all OFF except
    session_limit, which is confluence's live-style one-shot-per-zone+direction
    rule within a Topstep session)."""
    trail_trigger_pct: float = 0.0   # 0 = trailing OFF
    trail_lock_pct: float = 0.0      # locked SL as fraction of TP distance on trigger
    full_tp_lock: int = 0            # 0 = OFF; stop new entries after N full-TP exits/session
    session_limit: bool = True       # one trade per zone+direction per Topstep session

    @property
    def trail_enabled(self) -> bool:
        return self.trail_trigger_pct > 0.0


def maybe_trail_sl(direction: Direction, entry: float, tp: float, current_sl: float,
                   triggered: bool, market_price: float,
                   style: ConfluenceExitStyle) -> "tuple[float, bool]":
    """One-time break-even/trail. Returns (new_sl, triggered).

    Idempotent once triggered. When price has advanced ``trail_trigger_pct`` of
    the entry→TP distance, move SL to entry ± ``trail_lock_pct`` × TP distance and
    latch ``triggered=True``. Identical in backtest and live → parity holds."""
    if triggered or not style.trail_enabled:
        return current_sl, triggered
    tp_dist = abs(tp - entry)
    if tp_dist <= 0:
        return current_sl, triggered
    moved = (market_price - entry) if direction == Direction.BUY else (entry - market_price)
    if moved >= style.trail_trigger_pct * tp_dist:
        lock = style.trail_lock_pct * tp_dist
        new_sl = entry + lock if direction == Direction.BUY else entry - lock
        return new_sl, True
    return current_sl, triggered
