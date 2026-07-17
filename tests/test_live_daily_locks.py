from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.db.models import Direction, StrategyParams, StrategyType, TradeSignal
from backend.live.engine import LiveTradingEngine


UTC = timezone.utc
CONTRACT = "CON.F.US.MNQ.U26"


class _Client:
    def __init__(self):
        self.get_positions = AsyncMock(return_value=[])


def _signal(direction: Direction = Direction.BUY) -> TradeSignal:
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=100.0,
        sl_price=90.0,
        tp_price=120.0,
        zone_id="bot-test",
        reason="bot test",
        zone_source="factor",
        timestamp=datetime(2026, 7, 16, 23, 0, tzinfo=UTC),
        order_type="market",
        meta={},
    )


def _close_fill(pnl: float, price: float = 90.0) -> dict:
    return {
        "id": "close-fill-1",
        "contractId": CONTRACT,
        "profitAndLoss": pnl,
        "price": price,
        "creationTimestamp": "2026-07-16T23:30:14+00:00",
    }


def _engine(root: Path, *, account_id: int = 22373660, trade_date: str = "2026-07-17"):
    params = StrategyParams(
        contract_id=CONTRACT,
        contract_size=3,
        tr_daily_loss_stop=1,
        tr_daily_win_stop=1,
    )
    with patch("backend.live.engine.EMAPMOSignalMessenger.from_env", return_value=MagicMock()):
        engine = LiveTradingEngine(
            _Client(),
            account_id,
            CONTRACT,
            contract_size=3,
            strategy_params=params,
        )
    engine._today = trade_date
    engine._daily_loss_count = 0
    engine._daily_win_count = 0
    engine._daily_risk_state_file = str(root / f"risk-{account_id}.json")
    engine._trades_file = str(root / f"trades-{account_id}.json")
    engine._exits_file = str(root / f"exits-{account_id}.json")
    engine._log = []
    return engine


class LiveDailyRiskStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_manual_outcomes_never_change_bot_lock_counters(self):
        engine = _engine(self.root)

        self.assertFalse(engine._record_daily_bot_outcome(-676.50, program_owned=False))
        self.assertFalse(engine._record_daily_bot_outcome(+200.00, program_owned=False))

        self.assertEqual(engine._daily_loss_count, 0)
        self.assertEqual(engine._daily_win_count, 0)
        self.assertFalse(Path(engine._daily_risk_state_file).exists())

    def test_bot_loss_survives_restart_and_remains_locked(self):
        first = _engine(self.root)
        self.assertTrue(first._record_daily_bot_outcome(-100.0, program_owned=True))
        self.assertEqual(first._daily_loss_count, 1)

        restarted = _engine(self.root)
        self.assertTrue(restarted._restore_daily_bot_risk_state())
        gate = restarted.get_status()["risk_gates"]["daily_loss"]

        self.assertEqual(gate["limit"], 1)
        self.assertEqual(gate["count"], 1)
        self.assertTrue(gate["resting"])
        self.assertEqual(gate["scope"], "bot_only")
        self.assertTrue(gate["persistent"])

    def test_new_topstep_day_resets_persisted_bot_counters(self):
        first = _engine(self.root, trade_date="2026-07-17")
        first._record_daily_bot_outcome(-100.0, program_owned=True)

        next_day = _engine(self.root, trade_date="2026-07-18")
        self.assertFalse(next_day._restore_daily_bot_risk_state())
        self.assertEqual(next_day._daily_loss_count, 0)
        saved = json.loads(Path(next_day._daily_risk_state_file).read_text(encoding="utf-8"))
        self.assertEqual(saved["topstep_trade_date"], "2026-07-18")
        self.assertEqual(saved["bot_loss_count"], 0)

    def test_missing_or_corrupt_state_fails_open_at_zero_without_crashing(self):
        engine = _engine(self.root)
        Path(engine._daily_risk_state_file).write_text("not-json", encoding="utf-8")

        self.assertFalse(engine._restore_daily_bot_risk_state())
        self.assertEqual(engine._daily_loss_count, 0)
        self.assertEqual(engine._daily_win_count, 0)

    def test_unowned_closing_fill_is_classified_manual(self):
        engine = _engine(self.root)
        reason, price, pnl, _ = engine._exit_reason_from_topstep_fill(
            _close_fill(-676.50),
            signal=None,
            forced=None,
        )
        self.assertEqual(reason, "manual")
        self.assertEqual(price, 90.0)
        self.assertEqual(pnl, -676.50)


class LiveDailyRiskSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def _run_close(self, *, program_owned: bool):
        engine = _engine(self.root)
        engine._open_position = {
            "contractId": CONTRACT,
            "averagePrice": 100.0,
            "size": 3,
            "side": 0,
        }
        engine._fill_price = 100.0
        engine._entry_time = datetime(2026, 7, 16, 23, 0, tzinfo=UTC)
        engine._active_signal = _signal() if program_owned else None
        engine._active_conf_payload = None
        engine.client.get_positions = AsyncMock(return_value=[])
        engine._latest_topstep_closing_fill = AsyncMock(return_value=_close_fill(-100.0))
        engine._refresh_account_snapshot = AsyncMock(return_value=True)
        engine._sweep_contract_open_orders = AsyncMock()
        engine.trend_follow.notify_trade_closed = MagicMock()

        await engine._sync_position()
        return engine

    async def test_observed_manual_close_does_not_lock_or_mutate_strategy(self):
        engine = await self._run_close(program_owned=False)

        self.assertEqual(engine._daily_loss_count, 0)
        self.assertEqual(engine._daily_win_count, 0)
        engine._sweep_contract_open_orders.assert_not_awaited()
        engine.trend_follow.notify_trade_closed.assert_not_called()
        rows = json.loads(Path(engine._trades_file).read_text(encoding="utf-8"))
        self.assertEqual(rows[-1]["exit_reason"], "manual")
        self.assertFalse(rows[-1]["managed_by_engine"])
        self.assertFalse(rows[-1]["lock_eligible"])

    async def test_observed_bot_loss_locks_and_persists(self):
        engine = await self._run_close(program_owned=True)

        self.assertEqual(engine._daily_loss_count, 1)
        self.assertTrue(Path(engine._daily_risk_state_file).exists())
        engine._sweep_contract_open_orders.assert_awaited_once_with("close")
        engine.trend_follow.notify_trade_closed.assert_called_once_with("sl")

        restarted = _engine(self.root)
        self.assertTrue(restarted._restore_daily_bot_risk_state())
        self.assertEqual(restarted._daily_loss_count, 1)
        self.assertTrue(restarted.get_status()["risk_gates"]["daily_loss"]["resting"])

    async def test_untracked_manual_position_is_not_size_fail_safe_flattened(self):
        engine = _engine(self.root)
        manual_position = {
            "contractId": CONTRACT,
            "averagePrice": 100.0,
            "size": 1,
            "side": 0,
        }
        engine.client.get_positions = AsyncMock(return_value=[manual_position])
        engine.flatten_now = AsyncMock()

        await engine._sync_position()

        self.assertEqual(engine._open_position, manual_position)
        self.assertIsNone(engine._active_signal)
        engine.flatten_now.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
