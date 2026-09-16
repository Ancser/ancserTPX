from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from backend.backtest.prop_intraday_research import RthSession
from backend.db.models import Candle
from backend.timebase import UTC
from scripts.daily_direction_hold_study import (
    DailyObservation,
    _net_pnl,
    build_observations,
    policy_direction,
)


ET = ZoneInfo("America/New_York")


def _session(day: date, *, base: float, previous_close: float | None = None) -> RthSession:
    start = datetime.combine(day, datetime.min.time().replace(hour=9, minute=30), tzinfo=ET)
    bars = []
    et_times = []
    vwaps = []
    for offset in range(390):
        et_ts = start + timedelta(minutes=offset)
        close = base + (0.02 * offset)
        bars.append(
            Candle(
                timestamp=et_ts.astimezone(UTC),
                open=close - 0.01,
                high=close + 0.10,
                low=close - 0.10,
                close=close,
                volume=100,
                symbol="MNQ",
                interval="1m",
            )
        )
        et_times.append(et_ts)
        vwaps.append(base)
    return RthSession(
        session_date=day,
        bars=tuple(bars),
        et_times=tuple(et_times),
        vwap=tuple(vwaps),
        vwap_std=tuple(0.0 for _ in bars),
        previous_rth_close=previous_close,
    )


def test_build_observations_uses_completed_entry_and_two_exit_definitions():
    sessions = [_session(date(2026, 1, 2), base=100.0), _session(date(2026, 1, 5), base=102.0)]
    observations = build_observations(sessions)
    current = [row for row in observations if row.session_date == date(2026, 1, 5)]
    assert len(current) == 6
    row = next(item for item in current if item.entry_offset == 60)
    assert row.entry_time == sessions[1].bars[60].timestamp
    assert row.exit_prices["pre_flatten"] == sessions[1].bars[374].close
    assert row.exit_prices["rth_close"] == sessions[1].bars[389].close


def test_policy_direction_is_causal_and_goes_flat_when_filters_disagree():
    row = DailyObservation(
        session_date=date(2026, 1, 5),
        entry_offset=60,
        entry_time=datetime(2026, 1, 5, 15, 30, tzinfo=UTC),
        entry_price=101.0,
        exit_prices={"rth_close": 105.0, "pre_flatten": 104.0},
        rth_open=100.0,
        vwap=100.5,
        opening_range_high=100.75,
        opening_range_low=99.5,
        previous_day_direction=-1,
        previous_high=103.0,
        previous_low=99.0,
    )
    assert policy_direction(row, "vwap_side") == 1
    assert policy_direction(row, "vwap_and_previous_day") == 0
    assert policy_direction(row, "vwap_and_opening_range") == 1


def test_net_pnl_charges_canonical_cost_and_stress():
    row = DailyObservation(
        session_date=date(2026, 1, 5),
        entry_offset=60,
        entry_time=datetime(2026, 1, 5, 15, 30, tzinfo=UTC),
        entry_price=100.0,
        exit_prices={"rth_close": 101.0},
        rth_open=100.0,
        vwap=100.0,
        opening_range_high=100.0,
        opening_range_low=99.0,
        previous_day_direction=0,
        previous_high=None,
        previous_low=None,
    )
    assert _net_pnl(
        row,
        1,
        "rth_close",
        point_value=2.0,
        round_turn_cost=1.24,
        tick_value=0.50,
        stress_ticks=14,
    ) == -6.24
