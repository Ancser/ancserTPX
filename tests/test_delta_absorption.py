from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from backend.api.routes import BacktestRequest, _build_strategy_params_from_request
from backend.db.models import Candle, Direction, StrategyParams
from backend.data.orderflow import footprint_cache_path, write_footprint_cache
from backend.live.databento_orderflow import DatabentoMboLiveFeed, databento_raw_symbol
from backend.strategy.exit_policy import resolve_exit_policy
from backend.strategy.delta_absorption import (
    CachedDeltaContextProvider,
    DeltaAbsorptionStrategy,
    build_delta_chart_overlay,
)
from backend.timebase import UTC


def _bar(epoch: int, *, buy: int, sell: int, close: float = 100.0) -> dict:
    return {
        "epoch": epoch,
        "time": datetime.fromtimestamp(epoch, UTC).isoformat(),
        "open": 100.0,
        "high": 101.0,
        "low": 100.0,
        "close": close,
        "buy": buy,
        "sell": sell,
        "delta": buy - sell,
        "trades": 1,
        "cells": [[400, buy, sell, 0, 0, buy + sell, buy + sell]],
    }


def _payload(trade_date: str, bars: list[dict]) -> dict:
    return {
        "meta": {
            "trade_date": trade_date,
            "symbol": "MNQ",
            "session": "RTH",
            "tick_size": 0.25,
            "interval_seconds": 60,
        },
        "bars": bars,
    }


def test_footprint_profile_requires_one_complete_prior_rth_session(tmp_path):
    previous_epoch = int(datetime(2026, 8, 31, 13, 30, tzinfo=UTC).timestamp())
    good = _payload(
        "2026-08-31",
        [_bar(previous_epoch + i * 60, buy=50, sell=50) for i in range(390)],
    )
    current_epoch = int(datetime(2026, 9, 1, 13, 30, tzinfo=UTC).timestamp())
    current = _payload("2026-09-01", [_bar(current_epoch, buy=50, sell=50)])
    provider = CachedDeltaContextProvider(
        symbol="MNQ",
        root=tmp_path,
        day_payloads={"2026-08-31": good, "2026-09-01": current},
    )

    snap = provider.snapshot(datetime(2026, 9, 1, 13, 31, tzinfo=UTC))
    assert snap is not None
    assert snap["previous_profile_date"] == "2026-08-31"
    assert snap["profile"] == {
        "poc": 100.0,
        "vah": 100.0,
        "val": 100.0,
        "total_volume": 39000.0,
    }

    broken = dict(good, bars=good["bars"][:-1] + [
        _bar(previous_epoch + 391 * 60, buy=50, sell=50)
    ])
    broken_provider = CachedDeltaContextProvider(
        symbol="MNQ",
        root=tmp_path,
        day_payloads={"2026-08-31": broken, "2026-09-01": current},
    )
    broken_snap = broken_provider.snapshot(
        datetime(2026, 9, 1, 13, 31, tzinfo=UTC)
    )
    assert broken_snap is not None
    assert broken_snap["profile"] is None
    assert broken_snap["previous_profile_date"] is None


def test_delta_absorption_emits_one_causal_signal_and_uses_pi_exit_shape(tmp_path):
    prior_epoch = int(datetime(2026, 8, 31, 13, 30, tzinfo=UTC).timestamp())
    prior = _payload(
        "2026-08-31",
        [_bar(prior_epoch + i * 60, buy=50, sell=50) for i in range(390)],
    )
    current_epoch = int(datetime(2026, 9, 1, 13, 30, tzinfo=UTC).timestamp())
    current_bars = []
    for i in range(31):
        # The first 21 minutes establish a small baseline.  Minutes 21–25
        # are the old negative-delta window; minutes 26–30 repeat the
        # negative delta while price stalls on prior VAL.
        delta = -20 if i >= 21 else 10
        current_bars.append(
            _bar(
                current_epoch + i * 60,
                buy=50 if delta < 0 else 60,
                sell=70 if delta < 0 else 50,
                close=100.25 if i == 30 else 100.0,
            )
        )
    current = _payload("2026-09-01", current_bars)
    provider = CachedDeltaContextProvider(
        symbol="MNQ",
        root=tmp_path,
        day_payloads={"2026-08-31": prior, "2026-09-01": current},
    )
    params = StrategyParams(
        strategy="delta_absorption",
        contract_id="CON.F.US.MNQ.U26",
        contract_size=1,
        factor_sl_value=4.0,
        rr_ratio=3,
        pi_short_sl_value=1.5,
        pi_long_hold_min=0,
        pi_short_hold_min=60,
        trail_enabled=False,
        trail_trigger_pct=0,
        delta_window=5,
        delta_baseline_window=30,
        delta_strength_multiplier=1.0,
        delta_weakening_ratio=0.70,
        delta_stall_ticks=1,
        delta_value_lookback=10,
        delta_value_touch_ticks=0,
        delta_source="whole",
        delta_gate="location",
        delta_pattern="absorption",
        delta_side_mode="long_only",
        delta_require_profile=True,
    )
    strategy = DeltaAbsorptionStrategy(params, context_provider=provider)

    # Warm the shared PI ATR-blend accumulator before the first RTH decision.
    warm_start = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    for i in range(60):
        ts = warm_start + timedelta(minutes=i)
        strategy.observe(
            Candle(ts, 100.0, 101.0, 99.0, 100.0, 100, "MNQ", "1m"),
            [],
            True,
        )
    for row in current_bars:
        ts = datetime.fromtimestamp(row["epoch"], UTC)
        strategy.observe(
            Candle(ts, row["open"], row["high"], row["low"], row["close"], 100, "MNQ", "1m"),
            [],
            True,
        )

    # The current completed MBO minute is available only on the following
    # platform candle, so evaluation at 14:01 uses data through 14:00.
    decision = Candle(
        datetime(2026, 9, 1, 14, 1, tzinfo=UTC),
        100.25,
        100.75,
        100.0,
        100.5,
        100,
        "MNQ",
        "1m",
    )
    signal = strategy.evaluate(decision, [], True)
    assert signal is not None
    assert signal.direction is Direction.BUY
    assert signal.entry_price == 100.5
    assert signal.sl_price < signal.entry_price < signal.tp_price
    assert signal.tp_points == 3 * signal.sl_points
    assert signal.meta["delta_absorption"]["pattern"] == "absorption"
    assert signal.meta["delta_absorption"]["previous_profile_date"] == "2026-08-31"

    overlay = strategy.get_chart_overlay()
    assert overlay["model"] == "delta_absorption"
    assert overlay["value_areas"] == [{
        "session_date": "2026-09-01",
        "profile_date": "2026-08-31",
        "poc": 100.0,
        "vah": 100.0,
        "val": 100.0,
        "start_time": "2026-09-01T13:30:00+00:00",
        "end_time": "2026-09-01T14:00:00+00:00",
    }]
    assert overlay["events"]
    event = next(event for event in overlay["events"] if event["qualified"])
    assert event["qualified"] is True
    assert event["touched"] is True
    assert event["stalled"] is True
    assert {"delta_decay", "delta_pressure", "direction"}.issubset(event)

    # The same decision candle is idempotent; a chart refresh cannot duplicate
    # a signal for the same completed MBO minute.
    assert strategy.evaluate(decision, [], True) is None
    assert len(strategy.get_chart_overlay()["events"]) == len(overlay["events"])


def test_delta_decay_is_kept_as_standalone_chart_evidence(tmp_path):
    strategy = DeltaAbsorptionStrategy(
        StrategyParams(
            strategy="delta_absorption",
            delta_weakening_ratio=0.70,
            delta_strength_multiplier=1.0,
        ),
        context_provider=CachedDeltaContextProvider(root=tmp_path),
    )
    event_epoch = int(datetime(2026, 9, 1, 14, 0, tzinfo=UTC).timestamp())
    strategy._record_chart_event(
        snapshot={
            "date": "2026-09-01",
            "previous_profile_date": "2026-08-31",
        },
        bars=[_bar(event_epoch, buy=1, sell=1)],
        index=0,
        direction=Direction.BUY,
        profile=None,
        family=None,
        gate_passed=False,
        p0=0.0,
        p1=0.0,
        n0=100.0,
        n1=80.0,
        scale=100.0,
        absorption=False,
        exhaustion=False,
        stalled=False,
        touched=False,
        reclaimed=False,
        confirming=False,
        aligned=False,
        vwap=None,
    )

    overlay = strategy.get_chart_overlay()
    assert len(overlay["events"]) == 1
    assert overlay["events"][0]["delta_decay"] is True
    assert overlay["events"][0]["delta_pressure"] is False


def test_delta_chart_overlay_replays_cached_mbo_and_uses_prior_70pct_profile(tmp_path):
    prior_epoch = int(datetime(2026, 8, 31, 13, 30, tzinfo=UTC).timestamp())
    prior = _payload(
        "2026-08-31",
        [_bar(prior_epoch + i * 60, buy=50, sell=50) for i in range(390)],
    )
    current_epoch = int(datetime(2026, 9, 1, 13, 30, tzinfo=UTC).timestamp())
    current = _payload(
        "2026-09-01",
        [
            _bar(
                current_epoch + i * 60,
                buy=(60 if i < 21 else (20 if i < 26 else 30)),
                sell=(50 if i < 21 else (120 if i < 26 else 110)),
                close=100.25 if i == 30 else 100.0,
            )
            for i in range(31)
        ],
    )
    write_footprint_cache(
        prior, footprint_cache_path("2026-08-31", "MNQ", root=tmp_path),
    )
    write_footprint_cache(
        current, footprint_cache_path("2026-09-01", "MNQ", root=tmp_path),
    )

    overlay = build_delta_chart_overlay(
        datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
        datetime(2026, 9, 1, 14, 1, tzinfo=UTC),
        params=StrategyParams(
            strategy="delta_absorption",
            delta_side_mode="long_only",
            delta_require_profile=True,
        ),
        root=tmp_path,
    )

    assert overlay["available"] is True
    assert overlay["mbo_available"] is True
    assert overlay["status"] == "ok"
    assert overlay["mbo_bars"] == 31
    assert overlay["value_areas"] == [{
        "session_date": "2026-09-01",
        "profile_date": "2026-08-31",
        "poc": 100.0,
        "vah": 100.0,
        "val": 100.0,
        "start_time": "2026-09-01T13:30:00+00:00",
        "end_time": "2026-09-01T14:01:00+00:00",
        "profile_pct": 0.70,
    }]
    assert any(event["delta_decay"] for event in overlay["events"])
    assert any(event["stalled"] for event in overlay["events"])


def test_delta_chart_overlay_reports_prior_profile_only_without_fabricating_events(tmp_path):
    prior_epoch = int(datetime(2026, 8, 31, 13, 30, tzinfo=UTC).timestamp())
    prior = _payload(
        "2026-08-31",
        [_bar(prior_epoch + i * 60, buy=50, sell=50) for i in range(390)],
    )
    write_footprint_cache(
        prior, footprint_cache_path("2026-08-31", "MNQ", root=tmp_path),
    )

    overlay = build_delta_chart_overlay(
        datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
        datetime(2026, 9, 1, 14, 0, tzinfo=UTC),
        root=tmp_path,
    )

    assert overlay["available"] is False
    assert overlay["mbo_available"] is False
    assert overlay["status"] == "prior_profile_only"
    assert overlay["events"] == []
    assert overlay["value_areas"][0]["profile_date"] == "2026-08-31"
    assert overlay["value_areas"][0]["profile_pct"] == 0.70


def test_databento_symbol_mapping_and_missing_key_are_safe(monkeypatch):
    assert databento_raw_symbol("CON.F.US.MNQ.U26") == "MNQU6"
    assert databento_raw_symbol("CON.F.US.ENQ.U26") == "ENQU6"
    assert databento_raw_symbol("MNQZ6") == "MNQZ6"

    monkeypatch.setattr(
        "backend.live.databento_orderflow._read_dotenv_key",
        lambda: "",
    )
    feed = DatabentoMboLiveFeed(
        contract_id="CON.F.US.MNQ.U26",
        api_key="",
    )
    assert feed.start() is False
    status = feed.status()
    assert status["state"] == "missing_key"
    assert status["connected"] is False
    assert "DATABENTO_API_KEY" in status["error"]


def test_databento_live_checkpoint_is_restored_and_snapshot_can_seed_book(tmp_path):
    feed = DatabentoMboLiveFeed(
        contract_id="CON.F.US.MNQ.U26",
        api_key="test-key",
        root=tmp_path,
    )
    checkpoint = _payload("2026-09-09", [_bar(1_789_000_000, buy=25, sell=10)])
    write_footprint_cache(checkpoint, feed._runtime_path("2026-09-09"))
    restored = feed._load_payload("2026-09-09")
    assert restored is not None
    assert restored["bars"][0]["delta"] == 15
    assert "2026-09-09" in feed._candidate_dates()

    feed._snapshot_flag = 32
    feed._on_record(SimpleNamespace(
        ts_event=1_789_000_000_000_000_000,
        action="A",
        side="B",
        order_id=1,
        size=2,
        price=20_000_000_000_000,
        flags=32,
    ))
    assert feed._aggregator is not None


def test_delta_request_defaults_are_the_researched_pi_exit_profile():
    params = _build_strategy_params_from_request(
        BacktestRequest(strategy="delta_absorption"),
        contract_size=3,
    )
    assert params.tr_allowed_sessions == ["RTH"]
    assert params.factor_sl_value == 4.0
    assert params.factor_tp_value == 12.0
    assert params.factor_sl_rule == "atr_blend"
    assert params.factor_tp_rule == "atr_blend"
    assert params.rr_ratio == 3
    assert params.pi_short_sl_value == 1.5
    assert params.pi_long_hold_min == 0
    assert params.pi_short_hold_min == 60
    assert params.trail_enabled is False
    assert params.trail_trigger_pct == 0.0

    # Non-Pydantic callers with explicit values keep the same override path.
    explicit = _build_strategy_params_from_request(
        SimpleNamespace(
            strategy="delta_absorption",
            factor_sl_value=2.0,
            factor_tp_value=4.0,
            factor_sl_rule="atr",
            factor_tp_rule="atr",
            rr_ratio=2,
            pi_short_sl_value=2.5,
            trail_enabled=True,
            trail_trigger_pct=0.3,
        ),
        contract_size=1,
    )
    assert explicit.factor_sl_value == 2.0
    assert explicit.factor_tp_value == 4.0
    assert explicit.rr_ratio == 2
    assert explicit.pi_short_sl_value == 2.5
    assert explicit.trail_enabled is True
    assert explicit.trail_trigger_pct == 0.3


def test_delta_uses_the_same_directional_exit_policy_as_pi():
    params = StrategyParams(
        strategy="delta_absorption",
        factor_sl_value=4.0,
        rr_ratio=3,
        pi_short_sl_value=1.5,
        pi_long_hold_min=0,
        pi_short_hold_min=60,
        trail_enabled=False,
        trail_trigger_pct=0,
    )
    for direction in (Direction.BUY, Direction.SELL):
        delta = resolve_exit_policy(params, "delta_absorption", direction)
        pi = resolve_exit_policy(params, "pi", direction)
        assert delta.model == "delta_absorption"
        assert pi.model == "pi"
        assert delta.max_hold_minutes == pi.max_hold_minutes
        assert delta.hard_tp_enabled == pi.hard_tp_enabled
        assert delta.trail_mode == pi.trail_mode
        assert delta.trail_trigger_pct == pi.trail_trigger_pct
        assert delta.trail_offset_ticks == pi.trail_offset_ticks
