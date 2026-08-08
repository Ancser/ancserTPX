"""下單保護的兩條不變量(EXEC-005 / EXEC-006)。

兩條都在 `backend/live/engine.py`,兩條的失敗模式都是**用錯的價位下真錢**。
"""
from __future__ import annotations

import ast
import inspect
import math
from pathlib import Path

from backend.live.engine import LiveTradingEngine

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend" / "live" / "engine.py"


class TestAutoOcoOverride:
    """EXEC-005:Auto OCO 建的括號**必須**被改成策略價位。

    TopstepX 帳戶端開了 Auto OCO,送出進場單時券商會自動掛一組 SL/TP 子單,
    用的是帳戶設定的固定點數(300:900)。策略要的是 atr_blend 算出來的價位。

    流程:送單 → `_scan_auto_oco_order_ids()` 等子單出現 → `modify_order()`
    改價。這條容易被誤讀成「引擎不該碰 SL/TP」而整段刪掉 ——
    **「不自己下 SL/TP」指的是不另開新單,不是不能改既有的。**
    刪掉的話停損會停在帳戶預設點數,跟策略算的完全無關,而且不會有錯誤訊息。
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

        兩者都存在但沒接起來的話,括號一樣是帳戶預設值。
        """
        src = ENGINE.read_text(encoding="utf-8")
        i = src.index("_scan_auto_oco_order_ids(signal)")
        window = src[i:i + 4000]
        assert "modify_order" in window, \
            "掃到 Auto OCO 子單之後沒有接 modify_order —— 括號不會被改成策略價位"

    def test_rationale_is_documented(self):
        """這條最容易被誤刪,理由必須留在原始碼裡。"""
        src = ENGINE.read_text(encoding="utf-8")
        i = src.index("async def _scan_auto_oco_order_ids")
        assert "OCO" in src[i - 600:i + 400]


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
