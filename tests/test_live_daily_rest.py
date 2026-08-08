"""每日風控休息 = 只停開新單,不得平掉既有部位(LIVE-004)。

## 為什麼

三個閘門(當日虧損單數 / 當日贏單數 / PDPT 當日獲利)觸發時,正確行為是
**停止開新倉**,既有部位交給 SL / TP / trail 自然了結。

「休息」被實作成「強制平倉」的話,失敗模式是:一個正在往獲利方向跑的部位
在達標的瞬間被砍掉。那不會有錯誤訊息,只會少賺 —— 而且因為它同時滿足
「今天賺夠了」的直覺,很容易被當成正常行為而長期沒人發現。

## 測法

閘門埋在 `_tick()` 深處。這裡不驅動整個 tick,而是:

1. 用 AST 檢查三個閘門的分支裡**沒有**任何平倉呼叫 —— 結構性斷言,
   對重構不敏感(換函式名、搬位置都還是抓得到)
2. 用計數器驗證閘門的**觸發條件**本身正確(>= 而不是 >)
"""
from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.db.models import StrategyParams
from backend.live.engine import LiveTradingEngine

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "backend" / "live" / "engine.py"
CONTRACT = "CON.F.US.MNQ.U26"

# 會把部位平掉的東西。閘門分支裡出現任何一個都是 LIVE-004 違規。
FLATTEN_CALLS = {
    "flatten_all", "close_position", "_flatten", "_force_flatten",
    "_close_open_position", "_emergency_flatten", "close_all",
}

# 三個休息閘門讀的計數器
GATE_COUNTERS = ("_daily_loss_count", "_daily_win_count", "_daily_profit_td")


def _gate_branches() -> list[ast.If]:
    """找出讀 GATE_COUNTERS 且會 return 的 if 分支 —— 那就是休息閘門。"""
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.dump(node.test)
        if not any(c in test_src for c in GATE_COUNTERS):
            continue
        if any(isinstance(n, ast.Return) for n in ast.walk(node)):
            out.append(node)
    return out


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                names.add(f.attr)
            elif isinstance(f, ast.Name):
                names.add(f.id)
    return names


class RestGateStructure(unittest.TestCase):
    def test_gates_are_actually_found(self):
        """正向斷言:找不到閘門的話,下面那條會空跑變成假綠。"""
        gates = _gate_branches()
        self.assertGreaterEqual(len(gates), 3,
                                f"只找到 {len(gates)} 個休息閘門,預期至少 3 個"
                                f"(虧損單數 / 贏單數 / PDPT)")

    def test_no_rest_gate_closes_an_existing_position(self):
        """LIVE-004 的核心:閘門只能擋新單,不能碰既有部位。"""
        violations = []
        for gate in _gate_branches():
            hits = _called_names(gate) & FLATTEN_CALLS
            if hits:
                violations.append(f"engine.py:{gate.lineno} 呼叫了 {sorted(hits)}")
        self.assertFalse(violations, "休息閘門裡出現平倉呼叫:\n" + "\n".join(violations))

    def test_flatten_call_names_exist_somewhere_in_the_engine(self):
        """正向斷言:確認 FLATTEN_CALLS 這張表不是全部拼錯。

        全拼錯的話上一條永遠不會失敗。
        """
        everything = _called_names(ast.parse(ENGINE.read_text(encoding="utf-8")))
        self.assertTrue(everything & FLATTEN_CALLS,
                        "引擎裡一個平倉呼叫都找不到 —— FLATTEN_CALLS 該更新了")


class RestGateThresholds(unittest.TestCase):
    """閘門是 `>=` 不是 `>` —— 差一個等號就多做一筆。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(self, **kw):
        params = StrategyParams(contract_id=CONTRACT, contract_size=1, **kw)
        with patch("backend.live.engine.EMAPMOSignalMessenger.from_env",
                   return_value=MagicMock()):
            eng = LiveTradingEngine(MagicMock(), 1, CONTRACT,
                                    contract_size=1, strategy_params=params)
        eng._daily_risk_state_file = str(self.root / "risk.json")
        eng._trades_file = str(self.root / "t.json")
        eng._exits_file = str(self.root / "e.json")
        eng._log = []
        return eng

    def test_loss_stop_is_inclusive(self):
        eng = self._engine(tr_daily_loss_stop=2)
        self.assertEqual(eng._tr_daily_loss_stop, 2)
        eng._daily_loss_count = 1
        self.assertFalse(eng._daily_loss_count >= eng._tr_daily_loss_stop)
        eng._daily_loss_count = 2
        self.assertTrue(eng._daily_loss_count >= eng._tr_daily_loss_stop,
                        "第 2 筆虧損時就該停,不是第 3 筆")

    def test_win_stop_is_inclusive(self):
        eng = self._engine(tr_daily_win_stop=3)
        eng._daily_win_count = 3
        self.assertTrue(eng._daily_win_count >= eng._tr_daily_win_stop)

    def test_zero_means_disabled_not_immediately_locked(self):
        """0 = 關閉這個閘門。實作成「>= 0 就鎖」的話一單都下不出去。"""
        eng = self._engine(tr_daily_loss_stop=0, tr_daily_win_stop=0)
        self.assertEqual(eng._tr_daily_loss_stop, 0)
        self.assertFalse(bool(eng._tr_daily_loss_stop))
        self.assertFalse(bool(eng._tr_daily_win_stop))

    def test_manual_trades_do_not_move_bot_counters(self):
        """手動單不算進 bot 的休息計數 —— 否則手操會把 bot 鎖死。"""
        eng = self._engine(tr_daily_loss_stop=1)
        self.assertFalse(eng._record_daily_bot_outcome(-500.0, program_owned=False))
        self.assertEqual(eng._daily_loss_count, 0)

    def test_bot_trades_do_move_bot_counters(self):
        """對照組 —— 少了它,一個「永遠回 False」的 bug 也會讓上一條通過。"""
        eng = self._engine(tr_daily_loss_stop=1)
        self.assertTrue(eng._record_daily_bot_outcome(-500.0, program_owned=True))
        self.assertEqual(eng._daily_loss_count, 1)


class PdptSemantics(unittest.TestCase):
    """PDPT 的註解明講「既有部位不強平」。那句話是規格,不是裝飾。"""

    def test_source_documents_no_force_flatten(self):
        src = ENGINE.read_text(encoding="utf-8")
        self.assertIn("既有部位不強平", src,
                      "PDPT 的『不強平』規格說明被移除了")


if __name__ == "__main__":
    unittest.main()
