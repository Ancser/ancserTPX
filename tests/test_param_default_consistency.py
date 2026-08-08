"""參數預設值只能有一份真相(CONFIG-002 / CONFIG-003)。

## CONFIG-002 的陷阱

```python
self.pi_long_only = bool(getattr(params, "pi_long_only", True))   # 改了這裡
...
pi_long_only: bool = False                                        # 但沒改 dataclass
```

`getattr` 的 fallback **只在屬性不存在時觸發**。dataclass 有這個欄位,所以
永遠拿到 `False` —— 改了策略端等於沒改。2026-08-08 實際踩到:把 PI 預設
改成只做多,策略端與 dataclass 都改了,**但 `routes.py` 還有第三份**
(`getattr(req, "pi_long_only", False)`),於是任何沒帶該欄位的 API 請求
仍然會把做空打開。

這裡只盯**策略行為參數**。對 dict / broker response 用
`getattr(x, "contract_id", None)` 是完全正常的,不在管轄範圍。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.db.models import StrategyParams

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "backend" / "db" / "models.py"
ROUTES = ROOT / "backend" / "api" / "routes.py"

# 會直接改變交易行為的參數。這些欄位的 fallback 必須等於 dataclass 預設,
# 否則「改預設」這個動作會靜默失效。
BEHAVIOUR_PARAMS = {
    "pi_long_only", "pi_signal_set", "pi_max_signal_age_min",
    "pi_short_sl_value", "pi_short_hold_min",
    "factor_sl_value", "factor_max_hold_bars", "rr_ratio",
    "trail_enabled", "trail_trigger_pct", "trail_lock_pct",
    "tr_daily_loss_stop", "tr_daily_win_stop", "tr_daily_profit_stop",
    "factor_warmup_bars", "strategy",
}


def _dataclass_defaults() -> dict:
    tree = ast.parse(MODELS.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "StrategyParams":
            for st in node.body:
                if (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)
                        and st.value is not None):
                    try:
                        out[st.target.id] = ast.literal_eval(st.value)
                    except Exception:
                        pass
    return out


def _param_construction_fallbacks(path: Path) -> list[tuple[int, str, object]]:
    """只看**建構 StrategyParams(...) 時**的 getattr fallback。

    範圍要窄。`getattr(req, "rr_ratio", "?")` 出現在 log 字串裡是正常的
    (`'?'` 是佔位符),`getattr(p, "strategy", "")` 用來做成員檢查也是對的
    (`''` 代表未知,填 `'factor'` 反而會謊稱它是 factor)。
    會誤報的測試會被放寬,放寬過的測試就不再擋任何東西。

    真正的 bug 類型只有一種:**用一個和 dataclass 不同的預設值去建 params**。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "StrategyParams"):
            continue
        for kw in call.keywords:
            if kw.arg is None:
                continue
            for n in ast.walk(kw.value):
                if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == "getattr" and len(n.args) == 3):
                    continue
                if not isinstance(n.args[1], ast.Constant):
                    continue
                try:
                    fb = ast.literal_eval(n.args[2])
                except Exception:
                    continue      # 非字面值(指向 _PARAM_DEFAULTS)—— 那正是我們要的
                out.append((n.lineno, kw.arg, fb))
    return out


def test_construction_scanner_finds_the_params_call():
    """正向斷言:掃不到 StrategyParams(...) 的話,主測試會空跑。"""
    hits = _param_construction_fallbacks(ROUTES)
    assert len(hits) > 10, f"只掃到 {len(hits)} 個建構參數 —— 掃描器壞了"


def test_dataclass_defaults_are_readable():
    """正向斷言:解析壞掉回空 dict 的話,下面全部變成假綠。"""
    d = _dataclass_defaults()
    assert len(d) > 50, f"只解析出 {len(d)} 個欄位"
    assert "pi_long_only" in d and "factor_sl_value" in d


def test_dataclass_defaults_match_the_live_object():
    """AST 解析出來的值必須等於實際建構出來的物件。"""
    inst = StrategyParams()
    for k, v in _dataclass_defaults().items():
        assert getattr(inst, k) == v, f"{k}: AST={v!r} 實際={getattr(inst, k)!r}"


@pytest.mark.parametrize("path", [ROUTES], ids=lambda p: p.name)
def test_no_hardcoded_fallback_for_behaviour_params(path):
    """行為參數不得在 routes 裡自己寫一份 fallback 字面值。

    要嘛不給 fallback,要嘛指向 `_PARAM_DEFAULTS.<field>`。寫死字面值就是
    第三份真相,而它會在「改了預設卻沒同步」時靜默生效。
    """
    defaults = _dataclass_defaults()
    bad = []
    for lineno, name, fb in _param_construction_fallbacks(path):
        if name not in BEHAVIOUR_PARAMS or name not in defaults:
            continue
        if fb is None:
            continue          # None + `or _PARAM_DEFAULTS.x` 是允許的寫法
        if fb != defaults[name]:
            bad.append(f"{path.name}:{lineno} {name} fallback={fb!r} "
                       f"但 dataclass 預設是 {defaults[name]!r}")
    assert not bad, (
        "行為參數的 fallback 與 dataclass 預設不一致:\n" + "\n".join(bad) +
        "\n改用 _PARAM_DEFAULTS.<field>,不要再寫一份字面值。"
    )


def test_routes_exposes_a_single_defaults_object():
    """`_PARAM_DEFAULTS` 是 routes 取預設值的唯一入口。"""
    from backend.api import routes
    assert isinstance(routes._PARAM_DEFAULTS, StrategyParams)
    assert "_PARAM_DEFAULTS = StrategyParams()" in ROUTES.read_text(encoding="utf-8")


def test_pi_defaults_agree_across_all_three_layers():
    """dataclass / 策略 / routes 三層對 PI 預設必須一致。

    這是 2026-08-08 那個 bug 的直接回歸測試 —— 當時三層各說各話。
    """
    from backend.api.routes import _PARAM_DEFAULTS
    from backend.strategy.pi_signal import PiSignalStrategy

    dc = StrategyParams()
    strat = PiSignalStrategy(dc)

    assert dc.pi_long_only is True
    assert _PARAM_DEFAULTS.pi_long_only is dc.pi_long_only
    assert _PARAM_DEFAULTS.pi_signal_set == dc.pi_signal_set
    assert strat.pi_short_kinds == (), "策略層仍然允許做空"


class TestTimeExitStaysOff:
    """CONFIG-003:時間出場對 FACTOR/PMO 永久關閉。

    這條保護的不是正確性,是**不要把已經否定過的東西加回來**。
    1.0.9 移除 HOLD 5m 系統改成純 SL/TP;PI 研究再次獨立驗證同一方向
    (時間出場 120m / 240m 都明顯更差)。UI 上曾有這個控制項,很容易被
    當成「加回來給使用者選也不錯」。
    """

    def test_dataclass_default_is_zero(self):
        assert StrategyParams().factor_max_hold_bars == 0

    def test_every_shipped_preset_has_it_off(self):
        import json
        presets = json.loads((ROOT / "data" / "presets.json").read_text(encoding="utf-8"))
        on = {n: c.get("factor_max_hold_bars")
              for n, c in presets["presets"].items()
              if c.get("factor_max_hold_bars")}
        assert not on, f"這些 preset 把時間出場打開了: {on}"

    def test_presets_were_actually_loaded(self):
        """正向斷言:preset 檔讀不到時上一條會空跑。"""
        import json
        presets = json.loads((ROOT / "data" / "presets.json").read_text(encoding="utf-8"))
        assert len(presets["presets"]) >= 3

    def test_removal_rationale_is_still_documented(self):
        src = MODELS.read_text(encoding="utf-8")
        line = next(l for l in src.splitlines() if "factor_max_hold_bars" in l)
        assert "removed" in line.lower() or "SL/TP" in line, \
            "時間出場為什麼被移除的說明不見了"
