"""Shared OHLC-only tie-break rules for ambiguous intrabar exits."""

from __future__ import annotations


def resolve_same_bar_exit(open_price: float, sl_price: float, tp_price: float) -> str:
    """Return ``"sl"`` or ``"tp"`` when one bar touched both levels.

    OHLC data does not reveal the true path inside the bar. Use the level
    nearest to the bar open as the best deterministic approximation. Exact
    ties resolve to SL so missing tick data cannot add optimistic bias.
    """
    dist_sl = abs(float(open_price) - float(sl_price))
    dist_tp = abs(float(open_price) - float(tp_price))
    return "sl" if dist_sl <= dist_tp else "tp"
