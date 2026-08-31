"""Executable contracts for the research-only prop intraday harness."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backend.backtest.prop_intraday_research import (
    ET,
    EntryCandidate,
    MeanReversionConfig,
    OrbConfig,
    VwapPullbackConfig,
    _session_from_bars,
    build_rth_sessions,
    generate_mean_reversion_candidates,
    generate_orb_candidates,
    generate_vwap_pullback_candidates,
    is_news_blocked,
    load_news_events,
    recommended_configs,
    simulate_candidate,
    simulate_candidates,
)
from backend.db.models import Candle


def _bar(
    day: date,
    hour: int,
    minute: int,
    *,
    op: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
    volume: int = 100,
) -> Candle:
    et_ts = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
    return Candle(
        timestamp=et_ts.astimezone(timezone.utc),
        open=op,
        high=high,
        low=low,
        close=close,
        volume=volume,
        symbol="MES",
        interval="1m",
    )


def _minute_bars(day: date, rows: list[dict]) -> list[Candle]:
    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET)
    out = []
    for i, row in enumerate(rows):
        ts = start + timedelta(minutes=i)
        out.append(
            Candle(
                timestamp=ts.astimezone(timezone.utc),
                open=row.get("open", row.get("close", 100.0)),
                high=row.get("high", row.get("close", 100.0) + 0.5),
                low=row.get("low", row.get("close", 100.0) - 0.5),
                close=row.get("close", 100.0),
                volume=row.get("volume", 100),
                symbol="MES",
                interval="1m",
            )
        )
    return out


def _orb_session() -> object:
    day = date(2026, 7, 6)
    rows = []
    for _ in range(15):
        rows.append({"open": 95, "high": 100, "low": 90, "close": 95, "volume": 100})
    rows.append({"open": 99, "high": 102, "low": 99, "close": 101, "volume": 200})
    rows.append({"open": 101, "high": 112, "low": 100, "close": 110, "volume": 100})
    rows.extend({"open": 110, "high": 111, "low": 109, "close": 110, "volume": 100} for _ in range(20))
    return _session_from_bars(day, _minute_bars(day, rows), 94.0)


def test_rth_clock_uses_new_york_dst_not_a_fixed_utc_hour():
    winter = date(2026, 1, 5)
    summer = date(2026, 7, 6)
    winter_bar = _bar(winter, 9, 30)
    summer_bar = _bar(summer, 9, 30)

    sessions, skipped = build_rth_sessions(
        [summer_bar, winter_bar], require_flatten_bar=False
    )

    assert skipped == 0
    assert [session.session_date for session in sessions] == [winter, summer]
    assert sessions[0].bars[0].timestamp.hour == 14  # EST
    assert sessions[1].bars[0].timestamp.hour == 13  # EDT
    assert all(session.et_times[0].strftime("%H:%M") == "09:30" for session in sessions)


def test_incomplete_day_is_not_silently_treated_as_a_1550_flatten_session():
    day = date(2026, 7, 6)
    bars = [_bar(day, 9, 30), _bar(day, 12, 59)]

    sessions, skipped = build_rth_sessions(bars, require_flatten_bar=True)

    assert sessions == []
    assert skipped == 1


def test_orb_requires_body_volume_vwap_and_width_then_enters_next_bar():
    session = _orb_session()
    config = OrbConfig(
        "test_orb",
        confirm_minutes=1,
        volume_multiple=1.2,
        risk_dollars=200,
    )

    candidates, news_blocked = generate_orb_candidates(session, config, "MES")

    assert news_blocked == 0
    assert len(candidates) == 1  # positive assertion: the breakout really exists
    candidate = candidates[0]
    assert candidate.direction == 1
    assert candidate.stop_price == pytest.approx(89.5)
    assert candidate.meta["width"] == pytest.approx(10.0)

    trade, _, skip = simulate_candidate(session, candidate, "MES")
    assert skip == ""
    assert trade is not None
    assert trade.entry_time == session.bars[16].timestamp
    assert trade.entry_time > candidate.signal_time
    assert trade.contracts == 3
    assert trade.tp1_hit is True
    assert trade.pnl > 0

    no_volume, _ = generate_orb_candidates(
        session,
        OrbConfig("blocked", confirm_minutes=1, volume_multiple=2.1),
        "MES",
    )
    assert no_volume == []


def test_orb_width_override_changes_the_candidate_set():
    session = _orb_session()  # exactly 10 MES points wide

    accepted, _ = generate_orb_candidates(
        session,
        OrbConfig(
            "width_10",
            confirm_minutes=1,
            opening_width_min=10.0,
            opening_width_max=30.0,
        ),
        "MES",
    )
    rejected_min, _ = generate_orb_candidates(
        session,
        OrbConfig(
            "width_12",
            confirm_minutes=1,
            opening_width_min=12.0,
            opening_width_max=30.0,
        ),
        "MES",
    )
    rejected_max, _ = generate_orb_candidates(
        session,
        OrbConfig(
            "width_9",
            confirm_minutes=1,
            opening_width_min=8.0,
            opening_width_max=9.0,
        ),
        "MES",
    )

    assert len(accepted) == 1
    assert rejected_min == []
    assert rejected_max == []


def test_news_window_blocks_the_actual_next_bar_entry(tmp_path):
    source = tmp_path / "news.csv"
    source.write_text(
        "timestamp_et,event\n2026-07-06T09:46:00-04:00,major release\n",
        encoding="utf-8",
    )
    events = load_news_events(source)
    session = _orb_session()

    candidates, blocked = generate_orb_candidates(
        session,
        OrbConfig("news", confirm_minutes=1),
        "MES",
        news_events=events,
    )

    assert len(events) == 1  # positive guard: the CSV was actually parsed
    assert is_news_blocked(session.bars[16].timestamp, {date(2026, 7, 6): events}, 10)
    assert candidates == []
    assert blocked == 1


def test_vwap_pullback_needs_two_available_confirmations():
    day = date(2026, 7, 6)
    rows = []
    for i in range(15):
        close = 100.0 + i
        rows.append(
            {
                "open": close - 0.25,
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
                "volume": 100,
            }
        )
    # A low-volume bullish reclaim that reaches the rising session VWAP.
    rows.append({"open": 113.0, "high": 116.0, "low": 107.0, "close": 115.0, "volume": 40})
    rows.extend({"open": 115, "high": 116, "low": 114, "close": 115, "volume": 100} for _ in range(5))
    session = _session_from_bars(day, _minute_bars(day, rows), 99.0)
    config = VwapPullbackConfig(
        "pb",
        confirm_minutes=1,
        penetration_points=4.0,
        stop_buffer_points=4.0,
        trend_bars=3,
        volume_lookback=5,
        rsi_low=0,
        rsi_high=100,
    )

    candidates, _ = generate_vwap_pullback_candidates(session, config, "MES")

    assert candidates  # positive assertion: trend + pullback + reclaim exists
    assert candidates[0].direction == 1
    assert candidates[0].vwap_invalidation_buffer == 4.0

    high_volume_rows = list(rows)
    high_volume_rows[15] = dict(high_volume_rows[15], volume=200)
    high_volume_session = _session_from_bars(
        day, _minute_bars(day, high_volume_rows), 99.0
    )
    rejected, _ = generate_vwap_pullback_candidates(
        high_volume_session, config, "MES"
    )
    assert rejected == []


def test_mean_reversion_uses_only_causal_range_filters_and_small_gap():
    day = date(2026, 7, 6)
    rows = []
    for i in range(23):
        close = 99.5 if i % 2 == 0 else 100.5
        rows.append(
            {
                "open": close - 0.1,
                "high": close + 0.4,
                "low": close - 0.4,
                "close": close,
                "volume": 100,
            }
        )
    rows.extend({"open": 99, "high": 100, "low": 98, "close": 99, "volume": 100} for _ in range(5))
    rows[24] = {"open": 98.75, "high": 99.5, "low": 96.0, "close": 99.25, "volume": 100}
    session = _session_from_bars(day, _minute_bars(day, rows), 99.5)
    config = MeanReversionConfig(
        "mr",
        confirm_minutes=1,
        entry_sigma=1.5,
        stop_buffer_points=2.0,
        vwap_flat_max_pct=0.01,
        bb_expansion_max_ratio=10.0,
    )

    candidates, _ = generate_mean_reversion_candidates(session, config, "MES")

    assert candidates  # positive assertion: a lower-band hammer was formed
    assert candidates[0].direction == 1
    assert candidates[0].target1_kind == "absolute"

    large_gap = _session_from_bars(day, _minute_bars(day, rows), 90.0)
    rejected, _ = generate_mean_reversion_candidates(large_gap, config, "MES")
    assert rejected == []


def test_simulator_forbids_overlapping_positions():
    day = date(2026, 7, 6)
    rows = [
        {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
        for _ in range(20)
    ]
    session = _session_from_bars(day, _minute_bars(day, rows), 100.0)
    first = EntryCandidate(
        strategy="TEST",
        variant="one_position",
        direction=1,
        signal_index=2,
        signal_time=session.bars[2].timestamp,
        stop_price=90,
        target1_kind="points",
        target1_value=100,
        target1_fraction=1,
        max_hold_minutes=6,
    )
    second = EntryCandidate(
        strategy="TEST",
        variant="one_position",
        direction=-1,
        signal_index=4,
        signal_time=session.bars[4].timestamp,
        stop_price=110,
        target1_kind="points",
        target1_value=100,
        target1_fraction=1,
        max_hold_minutes=6,
    )

    trades, skipped = simulate_candidates(session, [first, second], "MES", 2)

    assert len(trades) == 1  # a real first position was simulated
    assert skipped["overlap"] == 1


def test_same_bar_equal_distance_tie_resolves_to_stop():
    day = date(2026, 7, 6)
    rows = [
        {"open": 100, "high": 101, "low": 99, "close": 100},
        {"open": 100, "high": 112, "low": 88, "close": 100},
        {"open": 100, "high": 101, "low": 99, "close": 100},
    ]
    session = _session_from_bars(day, _minute_bars(day, rows), 100.0)
    candidate = EntryCandidate(
        strategy="TEST",
        variant="tie",
        direction=1,
        signal_index=0,
        signal_time=session.bars[0].timestamp,
        stop_price=90,
        target1_kind="points",
        target1_value=10,
        target1_fraction=1,
    )

    trade, _, skip = simulate_candidate(session, candidate, "MES")

    assert skip == ""
    assert trade is not None
    assert trade.exit_reason == "sl"
    assert trade.exit_price == 90
    assert trade.pnl < 0


def test_predeclared_suite_covers_all_three_families_and_all_names_are_unique():
    configs = recommended_configs("MNQ")

    assert any(isinstance(config, OrbConfig) for config in configs)
    assert any(isinstance(config, VwapPullbackConfig) for config in configs)
    assert any(isinstance(config, MeanReversionConfig) for config in configs)
    assert len({config.name for config in configs}) == len(configs)
