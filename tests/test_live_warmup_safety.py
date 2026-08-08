"""實盤熱身的安全性測試(LIVE-001 / LIVE-002 / LIVE-007)。

## 為什麼

`LiveTradingEngine.start()` 會把歷史 K 棒餵進 detector 與策略,把 breakout
狀態重建到「就像它一直開著」的樣子。這段程式碼跑在**真的會下單的引擎**上,
卻沒有任何測試 —— `backend/live/engine.py` 有 5,088 行,先前只有 2 個測試檔
碰到它,而且都不碰熱身。

熱身出錯的失敗模式是靜默的:它不會拋例外,它會讓**第一根實盤 K 棒**帶著
錯誤的狀態做決策。

## 這裡不測什麼

不測策略數學、不測成交。只測「熱身這個動作本身不該做的事」。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.db.models import Candle, StrategyParams
from backend.live.engine import LiveTradingEngine

UTC = timezone.utc
CONTRACT = "CON.F.US.MNQ.U26"


class _Client:
    """券商 client 替身。任何送單類呼叫都會被記錄下來。"""

    def __init__(self):
        self.get_positions = AsyncMock(return_value=[])
        self.get_open_orders = AsyncMock(return_value=[])
        self.get_orders = AsyncMock(return_value=[])
        self.get_trade_history = AsyncMock(return_value=[])
        self.get_account_info = AsyncMock(return_value=MagicMock(balance=50000.0))
        self.place_order = AsyncMock()
        self.modify_order = AsyncMock()
        self.cancel_order = AsyncMock()
        self.close_position = AsyncMock()
        self.flatten_all = AsyncMock()
        self.connect_market_ws = AsyncMock()
        self.subscribe_trades = MagicMock()
        self.subscribe_quotes = MagicMock()

    @property
    def order_calls(self) -> int:
        return (self.place_order.await_count + self.close_position.await_count
                + self.flatten_all.await_count)


def _candles(n: int = 60, start: datetime | None = None) -> list[Candle]:
    t0 = start or datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
    out = []
    for i in range(n):
        base = 20000.0 + i * 2.0          # 單調上行 —— 一定會製造出突破
        out.append(Candle(timestamp=t0 + timedelta(minutes=i), open=base,
                          high=base + 5, low=base - 5, close=base + 1, volume=100))
    return out


def _engine(root: Path) -> tuple[LiveTradingEngine, _Client]:
    client = _Client()
    params = StrategyParams(contract_id=CONTRACT, contract_size=1)
    with patch("backend.live.engine.EMAPMOSignalMessenger.from_env",
               return_value=MagicMock()):
        eng = LiveTradingEngine(client, 22373660, CONTRACT,
                                contract_size=1, strategy_params=params)
    eng._daily_risk_state_file = str(root / "risk.json")
    eng._trades_file = str(root / "trades.json")
    eng._exits_file = str(root / "exits.json")
    eng._log = []
    return eng, client


def _warm_up(eng: LiveTradingEngine, candles: list[Candle]) -> None:
    """只跑 start() 裡的熱身區段。

    直接 await start() 會連上 websocket、開背景任務。這裡把 start() 之後
    的即時部分擋掉,只保留熱身 —— 測的仍然是**引擎自己的那段程式碼**,
    不是複製一份出來測(複製版本會跟著實作漂移,測了等於沒測)。
    """
    with patch.object(eng, "_connect_realtime", new=AsyncMock(), create=True), \
         patch.object(eng, "_start_background_tasks", new=AsyncMock(), create=True):
        try:
            asyncio.run(asyncio.wait_for(eng.start(candles), timeout=20))
        except (TypeError, AttributeError, asyncio.TimeoutError, RuntimeError):
            # start() 尾端的即時連線在測試環境接不起來。熱身區段在它之前,
            # 已經跑完了 —— 下面的斷言驗的就是那一段的結果。
            pass


class WarmUpPlacesNoOrders(unittest.TestCase):
    """LIVE-001:熱身重建狀態,不得下單。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_warm_up_never_calls_broker_order_apis(self):
        eng, client = _engine(self.root)
        _warm_up(eng, _candles(120))
        self.assertEqual(client.order_calls, 0,
                         "熱身期間對券商送出了單 —— 那是拿歷史行情下真錢")

    def test_warm_up_actually_consumed_the_candles(self):
        """正向斷言:證明上面那條不是因為熱身根本沒跑才通過的。

        少了這條,任何讓熱身提早 return 的改動都會讓「沒下單」變成假綠。
        """
        eng, _ = _engine(self.root)
        cs = _candles(120)
        _warm_up(eng, cs)
        self.assertEqual(eng._candles_processed, len(cs))
        self.assertEqual(len(eng._all_candles), len(cs))

    def test_warm_up_sorts_candles_chronologically(self):
        """券商 API 回傳是新到舊。沒排序的話 breakout 狀態是亂序建出來的。"""
        eng, _ = _engine(self.root)
        cs = _candles(60)
        _warm_up(eng, list(reversed(cs)))          # 故意反著餵
        stored = [c.timestamp for c in eng._all_candles]
        self.assertEqual(stored, sorted(stored), "熱身沒有把歷史 K 棒排序")
        self.assertEqual(eng._last_candle_time, cs[-1].timestamp.isoformat())


class WarmUpWithoutHistory(unittest.TestCase):
    """LIVE-002:沒有歷史資料時必須明確記為 error,不得靜默當成正常。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_history_is_logged_as_error(self):
        eng, _ = _engine(self.root)
        _warm_up(eng, [])
        joined = " ".join(str(x) for x in eng._log)
        self.assertIn("warm-up skipped", joined,
                      "沒有歷史 K 棒卻沒有留下任何紀錄")

    def test_empty_history_places_no_orders(self):
        eng, client = _engine(self.root)
        _warm_up(eng, [])
        self.assertEqual(client.order_calls, 0)

    def test_empty_history_leaves_zero_processed(self):
        eng, _ = _engine(self.root)
        _warm_up(eng, [])
        self.assertEqual(eng._candles_processed, 0)
        self.assertIsNone(eng._last_candle_time)


class BreakoutResetOutsideSession(unittest.TestCase):
    """LIVE-007:session 不允許的時段必須重置 breakout 確認狀態。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reset_helper_delegates_to_strategy(self):
        """`_reset_breakout_confirmation` 必須真的傳達到策略物件。

        它有兩條分支(策略有沒有 reset_breakout_confirmation),兩條都要通。
        """
        eng, _ = _engine(self.root)

        specific = MagicMock(spec=["reset_breakout_confirmation"])
        eng.trend_follow = specific
        eng._reset_breakout_confirmation()
        specific.reset_breakout_confirmation.assert_called_once()

        generic = MagicMock(spec=["reset"])
        eng.trend_follow = generic
        eng._reset_breakout_confirmation()
        generic.reset.assert_called_once()

    def test_out_of_session_bars_trigger_reset(self):
        """整段歷史都落在不允許的 session → 每根都該重置,一次 observe 都不該有。"""
        eng, _ = _engine(self.root)
        strat = MagicMock(spec=["observe", "reset_breakout_confirmation"])
        eng.trend_follow = strat
        with patch.object(eng, "_trend_session_allowed", return_value=False):
            _warm_up(eng, _candles(30))
        strat.observe.assert_not_called()
        self.assertGreater(strat.reset_breakout_confirmation.call_count, 0)

    def test_in_session_bars_are_observed(self):
        """對照組:session 允許時就該 observe —— 否則上面那條是假綠。"""
        eng, _ = _engine(self.root)
        strat = MagicMock(spec=["observe", "reset_breakout_confirmation"])
        eng.trend_follow = strat
        with patch.object(eng, "_trend_session_allowed", return_value=True):
            _warm_up(eng, _candles(30))
        self.assertGreater(strat.observe.call_count, 0)
        strat.reset_breakout_confirmation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
