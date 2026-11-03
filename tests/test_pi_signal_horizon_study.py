"""PI signal × horizon study: the two rules that would silently skew a row.

* The overnight counterfactual skips only the entry day's 15:45 ET flatten;
  the next session's flatten (Monday after a Friday entry) still closes it.
* The signal cutoff leaves the following session complete, so no horizon
  ends on a truncated bar.
"""
from __future__ import annotations

from datetime import datetime

from backend.db.models import Candle
from backend.timebase import NEW_YORK, UTC
from scripts.pi_signal_horizon_study import (
    FAMILIES,
    HORIZONS,
    configure,
    holds_through_close,
    latest_window,
)
from scripts.pi_exit_engine_grid_study import _load_base_params


def _ny(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=NEW_YORK).astimezone(UTC)


def _bar(ts):
    return Candle(timestamp=ts, open=1.0, high=1.0, low=1.0, close=1.0, volume=1,
                  symbol="MNQ", interval="1m")


def test_overnight_skips_only_the_entry_days_flatten():
    entry = _ny(2026, 9, 30, 11, 0)
    assert holds_through_close(entry, _ny(2026, 9, 30, 15, 45))        # entry day: hold
    assert holds_through_close(entry, _ny(2026, 9, 30, 23, 30))
    assert not holds_through_close(entry, _ny(2026, 10, 1, 15, 45))    # next session closes it
    friday = _ny(2026, 9, 25, 12, 0)
    assert not holds_through_close(friday, _ny(2026, 9, 28, 15, 45))   # Friday -> Monday


def test_cutoff_is_the_day_before_the_last_complete_session():
    bars = [_bar(_ny(2026, 9, d, hh, mm)) for d in (28, 29, 30) for hh, mm in ((11, 0), (15, 45), (16, 59))]
    bars.append(_bar(_ny(2026, 10, 1, 11, 0)))                          # 10-01 has no close yet
    window = latest_window(bars)
    assert window["last_session"].isoformat() == "2026-09-30"
    assert window["cutoff_day"].isoformat() == "2026-09-29"
    assert window["cutoff_utc"] == datetime(2026, 9, 29, 23, 59, 59, tzinfo=NEW_YORK).astimezone(UTC)


def test_control_puts_one_mark_per_trading_day_at_real_times_until_the_cutoff():
    from types import SimpleNamespace

    from scripts.pi_signal_horizon_study import control_rows

    real = [(_ny(2026, 9, 28, 13, 10), SimpleNamespace(future="MNQ", kind="青π")),
            (_ny(2026, 9, 29, 11, 5), SimpleNamespace(future="MNQ", kind="青π")),
            (_ny(2026, 9, 29, 12, 0), SimpleNamespace(future="MES", kind="青π"))]   # other symbol
    bars = [_bar(_ny(2026, 9, d, 11, 0)) for d in (28, 29, 30)] + [_bar(_ny(2026, 10, 1, 11, 0))]
    rows = control_rows(real, bars, "MNQ", "青π", datetime(2026, 9, 30).date())
    stamps = [datetime.fromisoformat(r["ts"]).astimezone(NEW_YORK) for r in rows]
    assert [s.strftime("%m-%d %H:%M") for s in stamps] == ["09-28 13:10", "09-29 11:05", "09-30 13:10"]
    assert {r["symbol"] for r in rows} == {"QQQ"} and rows[0]["marks"][0]["kind"] == "青π"


def test_matrix_gates_off_and_continuation_only_for_the_reopen_horizon():
    base = _load_base_params("MNQ")
    family = next(f for f in FAMILIES if f.key == "lv3_long")
    by_key = {h.key: configure(base, h, family) for h in HORIZONS}
    assert by_key["reopen"].pi_continue_long_kinds == ["青π"]           # positive: wired
    assert all(p.pi_continue_long_kinds == [] for k, p in by_key.items() if k != "reopen")
    assert all(p.tr_daily_loss_stop == 0 and p.pi_lv2_replace_pi is False for p in by_key.values())
    assert (by_key["m30"].pi_long_hold_min, by_key["m60"].pi_long_hold_min) == (30, 60)
