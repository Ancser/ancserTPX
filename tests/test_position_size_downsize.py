"""1.5.1 downsize_risk_dollars: wide-stop entries take 1 lot, in BOTH engines.

Failure modes guarded here:
* the backtest and live engines sizing the same signal differently;
* live opening 1 lot while its protection/size checks still expect
  contract_size — the Auto OCO children would not be found and the 5-minute
  size check would emergency-flatten a correct position.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.backtest.engine import BacktestEngine
from backend.db.models import (
    Candle,
    Direction,
    OrderResponse,
    StrategyParams,
    StrategyType,
    TradeSignal,
)
from backend.live.engine import LiveTradingEngine
from backend.strategy.position_size import entry_contracts, planned_risk_dollars

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "CON.F.US.MNQ.Z26"
PV = 2.0  # MNQ


def _signal(sl: float) -> TradeSignal:
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW, direction=Direction.BUY,
        entry_price=100.0, sl_price=sl, tp_price=200.0,
        zone_id="downsize-test", reason="downsize", zone_source="pi", order_type="market",
    )


def _params(limit) -> StrategyParams:
    return StrategyParams(strategy="pi", contract_id=CONTRACT, contract_size=2,
                          downsize_risk_dollars=limit)


# ── the shared rule ────────────────────────────────────────────────────────

def test_rule_downsizes_only_above_the_limit():
    # 10 points x $2 x 2 lots = $40 planned risk
    assert planned_risk_dollars(100.0, 90.0, PV, 2) == 40.0
    assert entry_contracts(_params(None), 2, 100.0, 90.0, PV) == 2   # off
    assert entry_contracts(_params(50), 2, 100.0, 90.0, PV) == 2     # under: full size
    assert entry_contracts(_params(40), 2, 100.0, 90.0, PV) == 2     # equal: full size
    assert entry_contracts(_params(39), 2, 100.0, 90.0, PV) == 1     # over: 1 lot
    assert entry_contracts(_params(39), 1, 100.0, 90.0, PV) == 1     # already 1 lot
    assert entry_contracts(_params(0), 2, 100.0, 90.0, PV) == 2      # 0 = off


# ── backtest ──────────────────────────────────────────────────────────────

def _backtest_contracts(limit, sl: float) -> int:
    engine = BacktestEngine(strategy_params=_params(limit), record_equity=False)
    candle = Candle(timestamp=datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc),
                    open=100.0, high=101.0, low=99.0, close=100.0, volume=10)
    engine._execute_entry(_signal(sl), candle)
    return engine._open_position.contracts


def test_backtest_entry_uses_the_rule():
    assert _backtest_contracts(None, 90.0) == 2      # positive: a trade is opened at full size
    assert _backtest_contracts(39, 90.0) == 1
    assert _backtest_contracts(39, 95.0) == 2        # $20 risk stays full size


# ── live ──────────────────────────────────────────────────────────────────

def _live(limit) -> tuple[LiveTradingEngine, AsyncMock]:
    place_order = AsyncMock(return_value=OrderResponse(order_id=77, success=True))
    client = MagicMock()
    client.place_order = place_order
    with patch("backend.live.engine.EMAPMOSignalMessenger.from_env", return_value=MagicMock()):
        engine = LiveTradingEngine(client, account_id=123, contract_id=CONTRACT,
                                   contract_size=2, strategy_params=_params(limit))
    engine._last_market_price = 100.0
    engine._log = []
    engine._persist_breakout_lock = MagicMock()
    engine._mark_session_direction_locked = MagicMock()
    return engine, place_order


def test_live_market_entry_sends_the_downsized_order():
    engine, place_order = _live(39)
    assert asyncio.run(engine._place_market_entry(_signal(90.0))) is True
    assert place_order.await_args.args[0].size == 1
    assert engine._position_size == 1

    engine, place_order = _live(39)
    assert asyncio.run(engine._place_market_entry(_signal(95.0))) is True
    assert place_order.await_args.args[0].size == 2           # positive: full size kept
    assert engine._position_size == 2


def test_live_and_backtest_size_the_same_signal_identically():
    for limit in (None, 39, 50):
        for sl in (90.0, 95.0, 80.0):
            engine, _ = _live(limit)
            assert engine._entry_size_for_signal(_signal(sl)) == _backtest_contracts(limit, sl), (limit, sl)


def test_auto_oco_children_of_a_one_lot_entry_are_found():
    engine, _ = _live(39)
    signal = _signal(90.0)
    engine._position_size = engine._entry_size_for_signal(signal)
    assert engine._position_size == 1
    orders = [
        {"id": 501, "contractId": CONTRACT, "side": 1, "size": 1, "type": 4, "stopPrice": 90.0},
        {"id": 502, "contractId": CONTRACT, "side": 1, "size": 1, "type": 1, "limitPrice": 200.0},
    ]
    assert engine._select_auto_oco_orders(orders, signal) == (501, 502)


def _code(text: str) -> str:
    return re.sub(r"(?m)^\s*#.*$", "", text)


def test_position_bound_live_paths_never_use_the_configured_size():
    """Every path that touches the open position must follow the entry's size."""
    src = _code((ROOT / "backend" / "live" / "engine.py").read_text(encoding="utf-8"))
    for fn in ("_select_auto_oco_orders", "_sync_auto_oco_protection",
               "_emergency_market_close", "_check_trailing_sl_live", "_sync_position",
               "_persist_trade_record", "_persist_exit_record"):
        start = src.index(f"def {fn}(")
        end = src.find("\n    def ", start + 10)
        end = src.find("\n    async def ", start + 10) if end == -1 else min(
            end, src.find("\n    async def ", start + 10) if src.find("\n    async def ", start + 10) != -1 else end)
        body = src[start:end]
        assert "self.contract_size" not in body, fn
        assert "self._position_size" in body, fn                   # positive: it uses the entry size
