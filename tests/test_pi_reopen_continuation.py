from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.db.models import (
    Candle,
    Direction,
    StrategyParams,
    StrategyType,
    TradeSignal,
)
from backend.live import engine as live_engine_module
from backend.live.engine import LiveTradingEngine
from backend.strategy.pi_signal import PiSignalStrategy


UTC = timezone.utc
CONTRACT = "CON.F.US.MNQ.U26"


def _params(**overrides) -> StrategyParams:
    values = {
        "strategy": "pi",
        "contract_id": CONTRACT,
        "contract_size": 1,
        "pi_long_only": True,
        "pi_long_kinds": ["深蓝圈"],
        "pi_short_kinds": [],
        "pi_short_levels": [],
        "pi_continue_long_kinds": ["深蓝圈"],
        "pi_continue_short_kinds": [],
        "pi_continue_short_levels": [],
        "pi_reopen_max_gap_r": 1.0,
        "factor_sl_value": 1.0,
        "factor_max_trades_per_day": 0,
        "rr_ratio": 2,
        "tr_allowed_sessions": None,
    }
    values.update(overrides)
    return StrategyParams(**values)


def _candle(ts: datetime, *, price: float = 10_000.0, high=None, low=None) -> Candle:
    return Candle(
        timestamp=ts,
        open=price,
        high=price + 0.5 if high is None else high,
        low=price - 0.5 if low is None else low,
        close=price,
        volume=10,
    )


def _pi_trade_signal(*, kind="深蓝圈", level=2, direction=Direction.BUY):
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=100.0,
        sl_price=90.0 if direction == Direction.BUY else 110.0,
        tp_price=120.0 if direction == Direction.BUY else 80.0,
        zone_id="pi-source",
        reason="PI source signal",
        order_type="market",
        meta={"pi": {"kind": kind, "level": level, "message_id": "source-1"}},
    )


def test_continuation_selector_matches_each_signal_kind_and_short_level():
    long_strategy = PiSignalStrategy(_params())
    assert long_strategy.continuation_enabled(_pi_trade_signal())
    assert not long_strategy.continuation_enabled(_pi_trade_signal(kind="淡蓝圈", level=1))
    continued = _pi_trade_signal()
    continued.meta["pi_continuation"] = {"reentries_for_source": 1}
    assert not long_strategy.continuation_enabled(continued)

    short_strategy = PiSignalStrategy(_params(
        pi_long_only=False,
        pi_long_kinds=[],
        pi_short_kinds=["紫圈"],
        pi_short_levels=[2],
        pi_continue_long_kinds=[],
        pi_continue_short_kinds=["紫圈"],
        pi_continue_short_levels=[2],
    ))
    assert short_strategy.continuation_enabled(
        _pi_trade_signal(kind="紫圈", level=2, direction=Direction.SELL)
    )
    assert not short_strategy.continuation_enabled(
        _pi_trade_signal(kind="紫圈", level=1, direction=Direction.SELL)
    )


def test_reopen_builder_uses_fresh_atr_and_applies_r_gap_cap():
    strategy = PiSignalStrategy(_params())
    strategy._atr_blend = Mock(return_value=2.0)
    ticket = {
        "direction": "buy",
        "source_entry": 100.0,
        "original_sl": 90.0,
        "original_tp": 120.0,
        "flat_price": 102.0,
        "flat_time": "2026-07-15T19:45:00+00:00",
        "pi": {"kind": "深蓝圈", "level": 2, "message_id": "source-1"},
        "source_reason": "PI QQQ 深蓝圈",
    }
    candle = _candle(datetime(2026, 7, 15, 22, 0, tzinfo=UTC), price=104.0)

    signal = strategy.build_reopen_continuation(ticket, candle, max_gap_r=0.5)

    assert signal is not None
    assert signal.entry_price == 104.0
    assert signal.sl_price == 102.0
    assert signal.tp_price == 108.0
    assert signal.meta["pi"]["kind"] == "深蓝圈"
    assert signal.meta["pi_continuation"]["reopen_gap_r"] == 0.2
    assert signal.meta["pi_continuation"]["atr_blend"] == 2.0
    assert strategy.build_reopen_continuation(ticket, _candle(
        datetime(2026, 7, 15, 22, 1, tzinfo=UTC), price=113.0
    ), max_gap_r=1.0) is None


def _backtest_rows():
    row = {
        "id": "lv2-source",
        "ts": "2026-07-15T19:00:00+00:00",
        "symbol": "QQQ",
        "marks": [{"kind": "深蓝圈", "size": "Level 2", "level": 2}],
    }
    return [row]


def _backtest_candles(*, stop_touch=False, target_touch=False, late_reopen=False):
    start = datetime(2026, 7, 15, 18, 15, tzinfo=UTC)  # 14:15 ET; ATR warm-up
    end = datetime(2026, 7, 15, 22, 25, tzinfo=UTC)    # 18:25 ET
    out = []
    ts = start
    while ts <= end:
        low = 10_000.0 - 0.5
        high = 10_000.0 + 0.5
        if stop_touch and ts == datetime(2026, 7, 15, 20, 15, tzinfo=UTC):
            low = 9_990.0  # 16:15 ET while the source ticket is flat
        if target_touch and ts == datetime(2026, 7, 15, 20, 15, tzinfo=UTC):
            high = 10_010.0  # 16:15 ET while the source ticket is flat
        if late_reopen and datetime(2026, 7, 15, 22, 0, tzinfo=UTC) <= ts < datetime(
            2026, 7, 15, 22, 6, tzinfo=UTC
        ):
            ts += timedelta(minutes=1)
            continue
        out.append(_candle(ts, low=low, high=high))
        ts += timedelta(minutes=1)
    return out


def _run_pi_backtest(monkeypatch, *, stop_touch=False, target_touch=False, late_reopen=False):
    import backend.strategy.pi_signal as pi_signal_module

    monkeypatch.setattr(
        pi_signal_module,
        "_load_history",
        lambda replay_rows=None: pi_signal_module._rows_to_signals(replay_rows or []),
    )
    engine = BacktestEngine(
        config=BacktestConfig(
            strategies=["trend"],
            initial_capital=50_000.0,
            symbol="MNQ",
        ),
        strategy_params=_params(),
        record_equity=False,
        pi_replay_rows=_backtest_rows(),
    )
    result = engine.run(_backtest_candles(
        stop_touch=stop_touch,
        target_touch=target_touch,
        late_reopen=late_reopen,
    ))
    return engine, result


def test_backtest_engine_arms_and_reenters_lv2_once_at_same_day_reopen(monkeypatch):
    engine, result = _run_pi_backtest(monkeypatch)

    continuations = [
        trade for trade in result.trades
        if (trade.meta or {}).get("pi_continuation")
    ]
    assert len(continuations) == 1
    assert continuations[0].meta["pi"]["kind"] == "深蓝圈"
    assert continuations[0].meta["pi_continuation"]["reentries_for_source"] == 1
    assert [event["event"] for event in engine.pi_reopen_events] == [
        "armed", "reentered",
    ]


def test_backtest_cancels_reopen_if_original_stop_is_touched_while_flat(monkeypatch):
    engine, result = _run_pi_backtest(monkeypatch, stop_touch=True)

    assert not any((trade.meta or {}).get("pi_continuation") for trade in result.trades)
    assert any(event["event"] == "invalidated" for event in engine.pi_reopen_events)


def test_backtest_cancels_reopen_if_original_target_is_reached_while_flat(monkeypatch):
    engine, result = _run_pi_backtest(monkeypatch, target_touch=True)

    assert not any((trade.meta or {}).get("pi_continuation") for trade in result.trades)
    assert any(event["event"] == "invalidated" for event in engine.pi_reopen_events)


def test_backtest_expires_ticket_when_first_reopen_bar_is_late(monkeypatch):
    engine, result = _run_pi_backtest(monkeypatch, late_reopen=True)

    assert not any((trade.meta or {}).get("pi_continuation") for trade in result.trades)
    assert any(event["event"] == "expired" for event in engine.pi_reopen_events)


def _live_engine(tmp_path, params=None):
    with patch(
        "backend.live.engine.EMAPMOSignalMessenger.from_env",
        return_value=MagicMock(),
    ):
        engine = LiveTradingEngine(
            MagicMock(),
            account_id=123,
            contract_id=CONTRACT,
            strategy_params=params or _params(),
        )
    engine._pi_reopen_state_file = str(tmp_path / "live-pi-reopen.json")
    return engine


def test_live_ticket_persists_restores_and_cancels_on_original_stop_touch(tmp_path):
    params = _params()
    engine = _live_engine(tmp_path, params)
    engine._open_position = {"contractId": CONTRACT, "size": 1}
    engine._active_signal = _pi_trade_signal()
    engine._fill_price = 100.0
    engine._active_signal.original_entry_price = 100.0
    engine._active_signal.original_sl_price = 90.0
    engine._active_signal.original_tp_price = 120.0
    source_bar = _candle(datetime(2026, 7, 15, 19, 44, tzinfo=UTC), price=101.0)
    flat_time = datetime(2026, 7, 15, 19, 45, tzinfo=UTC)

    assert engine._arm_pi_reopen_ticket(source_bar, flat_time)
    assert json.loads((tmp_path / "live-pi-reopen.json").read_text(encoding="utf-8"))["pi"]["kind"] == "深蓝圈"

    restored = _live_engine(tmp_path, params)
    assert restored._restore_pi_reopen_ticket()
    assert restored._pi_reopen_ticket["flat_time"] == flat_time.isoformat()
    restored._update_pi_reopen_ticket(_candle(
        flat_time, price=100.0, high=101.0, low=89.5,
    ))
    assert restored._pi_reopen_ticket is None
    assert not (tmp_path / "live-pi-reopen.json").exists()


def test_live_reopen_routes_continuation_through_existing_market_order_path(
    tmp_path, monkeypatch,
):
    engine = _live_engine(tmp_path)
    engine._pi_reopen_ticket = {
        "version": 1,
        "account_id": 123,
        "contract_id": CONTRACT,
        "direction": "buy",
        "source_entry": 100.0,
        "original_sl": 90.0,
        "original_tp": 120.0,
        "flat_price": 100.0,
        "flat_time": "2026-07-15T19:45:00+00:00",
        "pi": {"kind": "深蓝圈", "level": 2, "message_id": "source-1"},
        "source_reason": "PI Level 2 source",
    }
    engine.trend_follow._atr_blend = Mock(return_value=2.0)
    engine._today = "2026-07-15"
    engine._get_topstep_trade_date = Mock(return_value="2026-07-15")
    engine._sync_position = AsyncMock()
    engine._last_account_refresh = time.time()
    engine._monitor_auto_oco_protection = AsyncMock(return_value=False)
    engine._fetch_latest_candles = AsyncMock(return_value=[
        _candle(datetime(2026, 7, 15, 22, 0, tzinfo=UTC), price=100.0)
    ])
    engine._last_candle_time = None
    engine._last_status_log_minute = -1
    engine._get_phase = Mock(return_value="ASIA")
    engine._get_order_short = Mock(return_value="FLAT")
    engine.detector.update = Mock()
    engine.confluence = None
    engine._append_history = Mock()
    engine._update_tf_breakout = Mock()
    engine._emapmo_messenger.enqueue_from_live = Mock(return_value=False)
    engine._place_market_entry = AsyncMock(return_value=True)
    monkeypatch.setattr(
        live_engine_module,
        "utc_now_naive",
        lambda: datetime(2026, 7, 15, 22, 1),
    )

    asyncio.run(engine._tick())

    engine._place_market_entry.assert_awaited_once()
    submitted = engine._place_market_entry.await_args.args[0]
    assert submitted.meta["pi_continuation"]["reentries_for_source"] == 1
    assert submitted.meta["pi"]["kind"] == "深蓝圈"
    assert engine._pi_reopen_ticket is None
