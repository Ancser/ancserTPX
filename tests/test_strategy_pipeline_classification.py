"""策略管線分類的一致性測試(CONFIG-001)。

## 為什麼

`FACTOR_PIPELINE_STRATEGIES` / `ZONELESS_STRATEGIES`(`backend/db/models.py`)
是**手動維護的白名單**,而 backtest 與 live 兩個引擎都讀它們來決定:

- 持倉期間要不要繼續呼叫 `observe()`(不呼叫 → 內部 5m 聚合斷層 →
  `_atr_blend()` 算錯 → SL/TP 寬度不同)
- 要不要每根 K 棒跑 volume-profile zone detector

1.0.9 時 MOMENTUM / BETAFIB 就是漏加,**實測同一組參數 PnL 差 33%**,
而且慢 20 倍。失敗模式完全靜默:不會拋例外、不會有 log,只是回測和實盤
悄悄跑出不同的數字。

## 這裡測什麼

不是「白名單內容應該長怎樣」—— 那是產品決策。是**一致性**:

1. 引擎認得的每一個 strategy_mode 都必須被**明確分類**過。
   新增策略卻沒在下面的表裡登記 → 測試紅,強迫做一次有意識的決定。
2. 被列進 `FACTOR_PIPELINE_STRATEGIES` 的策略,必須真的有 `observe()`,
   否則引擎會在持倉期間對它呼叫不存在的方法。
3. backtest 與 live 兩邊認得的 strategy_mode 必須一致 —— 一邊有一邊沒有,
   就是 live != backtest。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from backend.db.models import (
    FACTOR_PIPELINE_STRATEGIES,
    ZONELESS_STRATEGIES,
    ZONELESS_ZONE_RENDER,
)

ROOT = Path(__file__).resolve().parents[1]
BT = ROOT / "backend" / "backtest" / "engine.py"
LIVE = ROOT / "backend" / "live" / "engine.py"

# 每個引擎認得的 strategy_mode 都要在這裡登記一次。
#   factor_pipeline —— 持倉期間仍需 observe()(研究出身、自帶 5m 聚合的策略)
#   zoneless        —— 不需要 volume-profile zone detector
#
# ⚠️ 新增策略時**必須**同時更新這張表與 models.py 的白名單。
#    只改一邊,下面的測試就會紅 —— 那正是它存在的目的。
EXPECTED = {
    #                 factor_pipeline, zoneless
    "factor":        (True,  True),
    "momentum":      (True,  True),   # 1.0.9 漏加過,PnL 差 33%
    "betafib":       (True,  True),   # 同上
    "pi":            (True,  True),   # 1.0.10 新增
    "optionwall":    (True,  True),   # hourly tape + completed 5m ATR blend
    "fade":          (False, True),   # 用自己的前日 VA 水位,不需要 detector zone
    "sigma":         (False, True),   # 自己算 sigma 帶,不需要 detector zone
}

# live 專屬、不走 strategy_mode dispatch 的 ML 模式
LIVE_ONLY_MODES = {"confluence"}


def _modes_in(path: Path) -> set[str]:
    """抓出原始碼裡所有 `strategy_mode == "X"` 的字面值。

    用 AST 而不是 regex:regex 會連註解和字串裡的內容一起抓,
    而這個測試的價值就建立在「這份清單真的等於引擎認得的東西」。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
            continue
        left, right = node.left, node.comparators[0]
        is_mode = (isinstance(left, ast.Attribute) and left.attr == "strategy_mode")
        if is_mode and isinstance(right, ast.Constant) and isinstance(right.value, str):
            out.add(right.value)
    return out


def test_ast_extraction_actually_finds_modes():
    """正向斷言:確認上面的抓取真的有效。

    少了這條,`_modes_in()` 一旦壞掉回空集合,下面所有測試都會變成假綠。
    """
    modes = _modes_in(BT)
    assert len(modes) >= 5, f"只抓到 {modes} —— dispatch 的解析壞了"
    assert "factor" in modes and "pi" in modes


@pytest.mark.parametrize("path,label", [(BT, "backtest"), (LIVE, "live")])
def test_every_engine_mode_is_classified(path, label):
    """引擎認得但沒登記的 strategy_mode = 有人新增策略卻沒想過管線分類。"""
    unknown = _modes_in(path) - set(EXPECTED) - LIVE_ONLY_MODES
    assert not unknown, (
        f"{label} 引擎認得 {sorted(unknown)},但 EXPECTED 表裡沒有。\n"
        f"新增策略必須同時決定:要不要進 FACTOR_PIPELINE_STRATEGIES"
        f"(持倉期間是否繼續 observe)與 ZONELESS_STRATEGIES(是否需要 zone detector)。"
        f"漏加的後果是靜默的 —— 1.0.9 實測 PnL 差 33%。"
    )


def test_backtest_and_live_recognise_the_same_strategies():
    """兩邊認得的不一樣 = live != backtest。"""
    bt = _modes_in(BT) - LIVE_ONLY_MODES
    lv = _modes_in(LIVE) - LIVE_ONLY_MODES
    assert bt == lv, (
        f"只有 backtest 認得:{sorted(bt - lv)}\n"
        f"只有 live 認得:{sorted(lv - bt)}"
    )


@pytest.mark.parametrize("mode,flags", sorted(EXPECTED.items()))
def test_whitelist_membership_matches_expected(mode, flags):
    want_pipeline, want_zoneless = flags
    assert (mode in FACTOR_PIPELINE_STRATEGIES) is want_pipeline, (
        f"{mode} 的 FACTOR_PIPELINE_STRATEGIES 歸屬與登記不符")
    assert (mode in ZONELESS_STRATEGIES) is want_zoneless, (
        f"{mode} 的 ZONELESS_STRATEGIES 歸屬與登記不符")


def test_whitelists_contain_no_unknown_strategies():
    """白名單裡有引擎不認得的名字 = 改名或刪除策略時留下的殘骸。"""
    known = set(EXPECTED)
    for name, wl in (("FACTOR_PIPELINE_STRATEGIES", FACTOR_PIPELINE_STRATEGIES),
                     ("ZONELESS_STRATEGIES", ZONELESS_STRATEGIES),
                     ("ZONELESS_ZONE_RENDER", ZONELESS_ZONE_RENDER)):
        stale = set(wl) - known
        assert not stale, f"{name} 含引擎不認得的 {sorted(stale)}"


def test_factor_pipeline_strategies_all_implement_observe():
    """引擎會在持倉期間對這些策略呼叫 observe()。沒有這個方法就是 AttributeError。

    這條抓的是「加進白名單但介面不相容」—— 和忘記加進白名單相反的錯誤。
    """
    from backend.db.models import StrategyParams

    missing = []
    for mode in FACTOR_PIPELINE_STRATEGIES:
        params = StrategyParams(strategy=mode)
        from backend.backtest.engine import BacktestEngine
        eng = BacktestEngine.__new__(BacktestEngine)
        eng.strategy_params = params
        # 只跑 dispatch 那段:直接建策略物件,不啟動整個引擎
        strat = _build_strategy(mode, params)
        if not hasattr(strat, "observe"):
            missing.append(f"{mode} → {type(strat).__name__}")
    assert not missing, f"在 FACTOR_PIPELINE_STRATEGIES 但沒有 observe(): {missing}"


def _build_strategy(mode: str, params):
    if mode == "factor":
        from backend.strategy.factor import FactorSignalStrategy
        return FactorSignalStrategy(params=params)
    if mode == "pi":
        from backend.strategy.pi_signal import PiSignalStrategy
        return PiSignalStrategy(params=params)
    if mode == "optionwall":
        from backend.strategy.option_wall import OptionWallStrategy
        return OptionWallStrategy(params=params, signals=[])
    if mode == "momentum":
        from backend.strategy.research_lab import MomentumContinuation
        return MomentumContinuation(params=params)
    if mode == "betafib":
        from backend.strategy.research_lab import BetaFibRetrace
        return BetaFibRetrace(params=params)
    raise AssertionError(f"未知策略 {mode} —— 這個 helper 也要跟著更新")


def test_zone_render_list_is_subset_of_zoneless():
    """不需要 zone detector 卻要渲染 zone,是矛盾的組合。

    (fade 例外:它另有自己的前日 VA 水位,所以不在 zoneless 也不在 render。)
    """
    assert set(ZONELESS_ZONE_RENDER) <= set(ZONELESS_STRATEGIES)


def test_models_comment_still_documents_the_failure_mode():
    """這幾個常數旁邊的註解記著「為什麼」。它跟白名單本身一樣重要。

    註解被刪掉時,下一個人只會看到三個沒有理由的字串 tuple。
    """
    src = (ROOT / "backend" / "db" / "models.py").read_text(encoding="utf-8")
    head = src[: src.index("FACTOR_PIPELINE_STRATEGIES =")]
    tail = head[-1600:]
    assert "33%" in tail, "白名單的失敗模式說明(PnL 差 33%)被移除了"
    assert "observe" in tail
