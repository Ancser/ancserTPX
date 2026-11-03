"""Limit-fill rule of scripts/pi_option_limit_entry_study.py (minute quotes)."""
from datetime import datetime, timedelta

from scripts.pi_option_limit_entry_study import ET, limit_fill_time, limit_price

T0 = datetime(2026, 7, 31, 14, 0, tzinfo=ET)


def _q(ask, ask_size=10, bid=None, bid_size=10):
    bid = ask - 0.05 if bid is None else bid
    return {"bid": str(bid), "ask": str(ask), "bid_size": str(bid_size), "ask_size": str(ask_size)}


def _path(asks):
    return {T0 + timedelta(minutes=i): row for i, row in enumerate(asks, 1)}


def test_limit_price_rounds_down_to_a_cent():
    assert limit_price(2.47, 0.40) == 1.48
    assert limit_price(0.05, 0.60) == 0.02


def test_fills_on_the_first_valid_minute_at_or_below_the_limit():
    path = _path([_q(2.00), _q(1.60), _q(1.20), _q(1.10)])
    minutes = sorted(path)
    hit = limit_fill_time(path, minutes, 1.20, T0 + timedelta(hours=20))
    assert hit == T0 + timedelta(minutes=3)            # positive: it does fill


def test_invalid_quotes_and_the_exit_boundary_never_fill():
    path = _path([_q(1.00, ask_size=0), _q(0.0), _q(1.50), _q(1.00)])
    minutes = sorted(path)
    # zero ask size / zero ask rows are not fills; the valid 1.00 is at minute 4
    assert limit_fill_time(path, minutes, 1.10, T0 + timedelta(hours=1)) == T0 + timedelta(minutes=4)
    # ... but not when the order stops working at minute 4 (the exit time)
    assert limit_fill_time(path, minutes, 1.10, T0 + timedelta(minutes=4)) is None
    assert limit_fill_time(path, minutes, 0.0, T0 + timedelta(hours=1)) is None
