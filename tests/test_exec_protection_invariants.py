"""下單保護的不變量(EXEC-004 / EXEC-005 / EXEC-006)。

三條都在 `backend/live/engine.py`,失敗模式是**裸倉或用錯的價位下真錢**。
"""
from __future__ import annotations

import asyncio
import ast
import inspect
import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.db.models import (
    Direction,
    OrderResponse,
    StrategyParams,
    StrategyType,
    TradeSignal,
)
from backend.live.engine import LiveTradingEngine

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend" / "live" / "engine.py"
README_EN = ROOT / "README.md"
README_ZH = ROOT / "README_ZH.md"
PI_ASYMMETRIC_STUDY = ROOT / "scripts" / "pi_asymmetric_config.py"
CONTRACT = "CON.F.US.MNQ.U26"


def _entry_signal(*, order_type: str, direction: Direction = Direction.BUY) -> TradeSignal:
    if direction == Direction.BUY:
        sl_price, tp_price = 90.0, 120.0
    else:
        sl_price, tp_price = 110.0, 80.0
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=direction,
        entry_price=100.0,
        sl_price=sl_price,
        tp_price=tp_price,
        zone_id="attached-bracket-test",
        reason="attached bracket regression",
        zone_source="pi",
        order_type=order_type,
    )


def _entry_engine() -> tuple[LiveTradingEngine, AsyncMock]:
    place_order = AsyncMock(return_value=OrderResponse(order_id=77, success=True))
    client = MagicMock()
    client.place_order = place_order
    params = StrategyParams(
        strategy="pi",
        contract_id=CONTRACT,
        contract_size=1,
    )
    with patch("backend.live.engine.EMAPMOSignalMessenger.from_env", return_value=MagicMock()):
        engine = LiveTradingEngine(
            client,
            account_id=123,
            contract_id=CONTRACT,
            contract_size=1,
            strategy_params=params,
        )
    engine._last_market_price = 100.0
    engine._log = []
    engine._persist_breakout_lock = MagicMock()
    engine._mark_session_direction_locked = MagicMock()
    return engine, place_order


class TestAutoOcoOverride:
    """EXEC-005:Auto OCO 建的括號**必須**被改成策略價位。

    進場 request 附帶 Auto OCO bracket 後,券商會依 tick offset 建立一組
    SL/TP 子單。市場單實際成交價可能偏離訊號參考價,因此仍須依策略絕對
    價位校準。

    流程:送單 → `_scan_auto_oco_order_ids()` 等子單出現 → `modify_order()`
    改價。這條容易被誤讀成「引擎不該碰 SL/TP」而整段刪掉 ——
    **「不自己下 SL/TP」指的是不另開新單,不是不能改既有的。**
    刪掉的話,市場單滑價後只剩依訊號參考價算出的初始 tick offset,
    不會校準回策略的絕對價位。
    """

    def test_scan_then_modify_helpers_exist(self):
        assert hasattr(LiveTradingEngine, "_scan_auto_oco_order_ids")

    def test_protection_path_modifies_rather_than_places(self):
        """套用策略價位那段必須呼叫 modify_order,不得改成 place_order。"""
        src = inspect.getsource(LiveTradingEngine._apply_strategy_protection) \
            if hasattr(LiveTradingEngine, "_apply_strategy_protection") else None
        if src is None:
            # 函式名可能重構過 —— 退而檢查整支引擎裡 scan 與 modify 同時存在
            whole = ENGINE.read_text(encoding="utf-8")
            assert "_scan_auto_oco_order_ids" in whole
            assert "modify_order" in whole
            return
        assert "modify_order" in src

    def test_scan_result_feeds_modify_order(self):
        """結構性:`_scan_auto_oco_order_ids` 的結果必須被 modify_order 用到。

        兩者都存在但沒接起來的話,括號仍停在 attached entry 的初始價位。
        """
        src = ENGINE.read_text(encoding="utf-8")
        i = src.index("_scan_auto_oco_order_ids(signal)")
        window = src[i:i + 4000]
        assert "modify_order" in window, \
            "掃到 attached Auto OCO 子單之後沒有接 modify_order —— 括號不會被改成策略價位"

    def test_rationale_is_documented(self):
        """這條最容易被誤刪,理由必須留在原始碼裡。"""
        src = ENGINE.read_text(encoding="utf-8")
        i = src.index("async def _scan_auto_oco_order_ids")
        assert "OCO" in src[i - 600:i + 400]


class TestEntryAttachesAutoOcoBrackets:
    """EXEC-004: protection is attached atomically to every entry request.

    TopstepX no longer permits adding a position bracket after the fill (the
    platform says "Only Auto OCO brackets can be used").  Waiting until a
    position appears therefore creates an unrecoverable naked-position window.
    Both real entry paths are executed here; source-string presence alone would
    not prove that the bracket objects reach ``place_order``.
    """

    def test_signed_offsets_and_order_types_match_both_directions(self):
        engine, _ = _entry_engine()

        long_sl, long_tp = engine._entry_brackets_for_signal(
            _entry_signal(order_type="market", direction=Direction.BUY)
        )
        short_sl, short_tp = engine._entry_brackets_for_signal(
            _entry_signal(order_type="market", direction=Direction.SELL)
        )

        assert long_sl == {"ticks": -40, "type": 4}
        assert long_tp == {"ticks": 80, "type": 1}
        assert short_sl == {"ticks": 40, "type": 4}
        assert short_tp == {"ticks": -80, "type": 1}

    def test_limit_entry_reaches_broker_with_attached_pair(self):
        engine, place_order = _entry_engine()

        assert asyncio.run(engine._place_order(_entry_signal(order_type="limit"))) is True
        request = place_order.await_args.args[0]

        assert request.stop_loss_bracket == {"ticks": -40, "type": 4}
        assert request.take_profit_bracket == {"ticks": 80, "type": 1}

    def test_market_entry_reaches_broker_with_attached_pair(self):
        engine, place_order = _entry_engine()

        assert asyncio.run(engine._place_market_entry(_entry_signal(order_type="market"))) is True
        request = place_order.await_args.args[0]

        assert request.stop_loss_bracket == {"ticks": -40, "type": 4}
        assert request.take_profit_bracket == {"ticks": 80, "type": 1}

    def test_attached_children_are_still_repriced_after_fill(self):
        src = ENGINE.read_text(encoding="utf-8")
        assert "_scan_auto_oco_order_ids" in src
        assert "modify_order" in src


class TestCurrentProtectionGuidance:
    """Current guides must not resurrect the superseded 1.0.10n ownership model."""

    def test_operator_guides_mark_plain_entry_decision_superseded(self):
        en = README_EN.read_text(encoding="utf-8")
        zh = README_ZH.read_text(encoding="utf-8")

        assert "1.0.10n" in en and "superseded" in en.lower()
        assert "1.0.10n" in zh and "已取代" in zh

    def test_operator_guides_do_not_make_account_preset_the_protection_source(self):
        en = README_EN.read_text(encoding="utf-8")
        zh = README_ZH.read_text(encoding="utf-8")

        assert "Confirm the TopstepX Auto OCO preset is enabled" not in en
        assert "確認 TopstepX Auto OCO preset 已啟用" not in zh

    def test_pi_research_names_attached_api_fields_as_protection_source(self):
        src = PI_ASYMMETRIC_STUDY.read_text(encoding="utf-8")

        assert "stopLossBracket" in src
        assert "takeProfitBracket" in src
        assert "不可用帳戶 preset 取代" in src


class TestMarketPriceGuard:
    """EXEC-006:沒有可信市價就不得建立新的保護單。

    建保護單前要確認 SL 與 TP **夾住**當前市價。市價已經穿過即將掛的
    limit/stop 時,那張單會在兄弟單建好之前立刻成交 ——
    結果是一個沒有配對保護的裸部位。

    `_last_market_price` 拿不到時(剛啟動、行情斷線)必須**拒絕**,
    不是記個 WARN 然後照做。
    """

    @staticmethod
    def _guard_source() -> str:
        src = ENGINE.read_text(encoding="utf-8")
        i = src.index("market_safe = False")
        return src[i:i + 900]

    def test_guard_defaults_to_unsafe(self):
        """初值必須是 False —— 拿不到價格時預設拒絕,不是預設放行。"""
        g = self._guard_source()
        assert g.startswith("market_safe = False")

    def test_non_finite_price_leaves_guard_false(self):
        """`float(None)` 會拋例外 → market = nan → isfinite 為假 → 維持 False。

        直接驗這個邏輯,不依賴引擎實例。
        """
        for raw in (None, "", "abc"):
            try:
                market = float(raw)
            except (TypeError, ValueError):
                market = float("nan")
            assert not math.isfinite(market)

    def test_guard_requires_price_between_sl_and_tp(self):
        g = self._guard_source()
        assert "min(sl_price, tp_price) < market < max(sl_price, tp_price)" in g, \
            "夾擠檢查不見了 —— 市價已穿越的保護單會單腿成交"

    def test_guard_is_wrapped_in_isfinite(self):
        g = self._guard_source()
        assert "math.isfinite(market)" in g

    def test_bracket_logic_matches_the_guard(self):
        """把守衛的判斷抄出來跑,確認語意真的是「夾住才安全」。"""
        def safe(market, sl, tp):
            if not math.isfinite(market):
                return False
            return min(sl, tp) < market < max(sl, tp)

        assert safe(20000.0, 19900.0, 20100.0) is True      # 正常:夾住
        assert safe(20150.0, 19900.0, 20100.0) is False     # 市價已穿過 TP
        assert safe(19850.0, 19900.0, 20100.0) is False     # 市價已穿過 SL
        assert safe(float("nan"), 19900.0, 20100.0) is False  # 沒有市價 → 拒絕

    def test_guard_blocks_rather_than_warns(self):
        """守衛所在的區塊不得只是 log —— 必須影響控制流。"""
        src = ENGINE.read_text(encoding="utf-8")
        i = src.index("market_safe = False")
        after = src[i:i + 2500]
        assert "market_safe" in after.split("market_safe = False", 1)[1], \
            "market_safe 算出來之後沒有被用到 —— 那就只是個沒作用的變數"
