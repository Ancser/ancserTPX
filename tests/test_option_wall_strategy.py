from __future__ import annotations

import csv
import gzip
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.data.option_wall_signals import (
    OptionWallSignal,
    load_primary_strict_signals,
    primary_strict_status,
)
from backend.db.models import BacktestConfig, Candle, Direction, ExitReason, StrategyParams
from backend.strategy.option_wall import NO_TP_DISTANCE_POINTS, OptionWallStrategy


def _source_row(as_of: str, *, strict: bool, direction: int = 1) -> dict[str, object]:
    return {
        "as_of": as_of,
        "direction": direction,
        "gate_consensus_article_alignment_wall_room": strict,
        "oi_gamma_state": 1,
        "volume_gamma_state": 1,
        "article_price_vwap_distance_bps": -5.0,
        "article_price_return_15m_bps": 2.0,
        "dashboard_vol_call_wall_bps": 12.0,
        "dashboard_vol_put_wall_bps": -18.0,
        # Deliberate outcome bait: the production loader must not expose it.
        "pnl_best_full_period_exit_replay": 999999.0,
    }


def _write_artifact(root, rows) -> None:
    path = root / "option_wall_gamma_gate_trades.csv.gz"
    fields = list(rows[0])
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _signal(ts: datetime, direction: int = 1) -> OptionWallSignal:
    return OptionWallSignal(
        timestamp=ts,
        direction=direction,
        oi_gamma_state=1,
        volume_gamma_state=1,
        vwap_distance_bps=-5.0,
        return_15m_bps=2.0,
        call_wall_bps=12.0,
        put_wall_bps=-18.0,
    )


def _candles(start: datetime, end: datetime, price: float = 100.0) -> list[Candle]:
    result = []
    ts = start
    while ts <= end:
        result.append(Candle(
            timestamp=ts,
            open=price,
            high=price + 0.5,
            low=price - 0.5,
            close=price,
            volume=100,
            symbol="MNQ",
            interval="1m",
        ))
        ts += timedelta(minutes=1)
    return result


def test_primary_strict_loader_filters_gate_and_never_exposes_outcomes(tmp_path):
    _write_artifact(tmp_path, [
        _source_row("2026-01-05T15:00:00Z", strict=False),
        _source_row("2026-01-05T16:00:00Z", strict=True, direction=-1),
    ])

    signals = load_primary_strict_signals(tmp_path)

    assert len(signals) == 1
    assert signals[0].timestamp == datetime(2026, 1, 5, 16, tzinfo=timezone.utc)
    assert signals[0].direction == -1
    assert not hasattr(signals[0], "pnl")
    status = primary_strict_status(tmp_path)
    assert status["available"] is True
    assert status["signals"] == 1


def test_option_wall_defaults_build_pi_style_asymmetric_stop_without_hard_tp():
    at = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    params = StrategyParams(
        strategy="optionwall",
        contract_id="CON.F.US.MNQ.H26",
        option_wall_submodel="unknown-is-rejected-to-default",
    )
    strategy = OptionWallStrategy(params, signals=[_signal(at)])
    output = None
    for candle in _candles(at - timedelta(hours=3), at):
        output = strategy.evaluate(candle) or output

    assert strategy.submodel == "primary_strict"
    assert output is not None
    assert output.direction == Direction.BUY
    assert output.entry_price == 100.0
    assert output.sl_price == 96.0  # ATR blend=1 point; long stop=4x
    assert output.tp_price == output.entry_price + NO_TP_DISTANCE_POINTS
    assert output.zone_source == "option_wall"
    assert output.meta["option_wall"]["hard_tp_enabled"] is False
    assert output.meta["option_wall"]["historical_replay_only"] is True


def test_shared_backtest_engine_uses_60m_exit_and_single_portfolio(monkeypatch):
    from backend.backtest.engine import BacktestEngine
    import backend.strategy.option_wall as module

    at = datetime(2026, 1, 5, 15, tzinfo=timezone.utc)  # 10:00 ET
    monkeypatch.setattr(module, "load_primary_strict_signals", lambda: [_signal(at)])
    params = StrategyParams(
        strategy="optionwall",
        contract_id="CON.F.US.MNQ.H26",
        contract_size=1,
        tr_allowed_sessions=["RTH"],
        option_wall_max_hold_min=60,
        option_wall_max_trades_per_day=3,
    )
    engine = BacktestEngine(
        BacktestConfig(strategies=["optionwall"], symbol="MNQ"),
        strategy_params=params,
    )

    result = engine.run(_candles(
        at - timedelta(hours=3), at + timedelta(minutes=70),
    ))

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == at
    assert trade.exit_time == at + timedelta(minutes=60)
    assert trade.exit_reason == ExitReason.FLATTEN
    assert trade.duration_minutes == 60
    assert trade.zone_source == "option_wall"


def test_route_normalization_fixes_option_wall_to_rth_and_valid_defaults():
    from types import SimpleNamespace
    from backend.api.routes import _build_strategy_params_from_request

    params = _build_strategy_params_from_request(
        SimpleNamespace(
            strategy="optionwall",
            tr_allowed_sessions=["ASIA"],
            option_wall_submodel="not-a-real-model",
            option_wall_side_mode="nonsense",
        ),
        contract_size=1,
    )

    assert params.strategy == "optionwall"
    assert params.tr_allowed_sessions == ["RTH"]
    assert params.option_wall_submodel == "primary_strict"
    assert params.option_wall_side_mode == "all"
    assert params.option_wall_long_sl_atr == 4.0
    assert params.option_wall_short_sl_atr == 1.5
    assert params.option_wall_max_hold_min == 60


def test_live_start_explicitly_blocks_historical_only_option_wall():
    from fastapi import HTTPException
    from backend.api.routes import LiveStartRequest, live_start

    with pytest.raises(HTTPException, match="historical replay only") as raised:
        asyncio.run(live_start(LiveStartRequest(account_id=123, strategy="optionwall")))
    assert raised.value.status_code == 400
