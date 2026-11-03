"""Premium stop/target rule of scripts/pi_option_exit_grid_study.py."""
from datetime import datetime, timedelta

from scripts.pi_option_exit_grid_study import simulate_exit
from scripts.pi_option_limit_entry_study import ET

T0 = datetime(2026, 7, 31, 14, 0, tzinfo=ET)


def _q(bid, bid_size=10, ask=None):
    ask = bid + 0.05 if ask is None else ask
    return {"bid": str(bid), "ask": str(ask), "bid_size": str(bid_size), "ask_size": "10"}


def _path(bids):
    return {T0 + timedelta(minutes=i): row for i, row in enumerate(bids, 1)}


def test_stop_sells_at_the_observed_bid_even_through_a_gap():
    path = _path([_q(1.10), _q(0.90), _q(0.40), _q(3.00)])
    cap = T0 + timedelta(minutes=4)
    price, when, reason = simulate_exit(path, sorted(path), 1.00, 0.40, 1.00, cap)
    assert (reason, price, when) == ("sl", 0.40, T0 + timedelta(minutes=3))   # gap below 0.60


def test_target_fills_at_the_target_and_time_exit_uses_the_cap_bid():
    path = _path([_q(1.20), _q(2.60), _q(0.10)])
    cap = T0 + timedelta(minutes=3)
    assert simulate_exit(path, sorted(path), 1.00, 0.50, 1.00, cap) == (2.00, T0 + timedelta(minutes=2), "tp")
    assert simulate_exit(path, sorted(path), 1.00, None, None, cap) == (0.10, cap, "time")


def test_invalid_rows_are_skipped_and_a_missing_cap_bid_is_no_trade():
    path = _path([{"bid": "0", "ask": "0", "bid_size": "0", "ask_size": "0"}, _q(1.05)])
    assert simulate_exit(path, sorted(path), 1.00, 0.20, None, T0 + timedelta(minutes=9)) is None
    assert simulate_exit(path, sorted(path), 1.00, 0.20, None,
                         T0 + timedelta(minutes=2)) == (1.05, T0 + timedelta(minutes=2), "time")
