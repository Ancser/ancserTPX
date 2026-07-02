"""1.0.8 研究(僅腳本):前日 VA 四腿分別勝率。

回答:上面做多(brkLong 突破 VAH 追多)、下面做空(brkShort 跌穿 VAL 追空)、
中間盤整腿(fadeLong @VAL 接多 / fadeShort @VAH 放空,TP=POC)各自 n/勝率/pnl。

Run:  PYTHONIOENCODING=utf-8 python -m scripts.futureman_legs_winrate
"""
from __future__ import annotations

import copy
import logging
from collections import defaultdict

from backend.data import candle_store
from backend.db.models import BacktestConfig, _extract_symbol, get_commission_rt, get_fees_rt
from backend.terminal_live import BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params
from scripts.futureman_study import FuturemanBacktest


class LegWinBacktest(FuturemanBacktest):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.play_win = defaultdict(int)

    def _execute_exit(self, candle, exit_price, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None and (t.pnl or 0.0) > 0:
            self.play_win[self._cur_play] += 1


def main():
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    print(f"candles {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}", flush=True)

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id")
    base = _build_strategy_params(preset, cid)
    base.tr_allowed_sessions = None
    base.one_trade_per_session_direction = False
    base.tr_one_trade_per_session = False
    base.full_tp_lock = 0
    base.tr_full_tp_lock = 0

    config = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid), value_area_pct=0.80,
    )
    eng = LegWinBacktest(config=config, strategy_params=copy.deepcopy(base),
                         zone_timeline=None, record_equity=False,
                         rr=2.0, fades=True, breakouts=True)
    eng.run(candles)

    label = {
        "brkLong": "上面做多 brkLong(升穿前日VAH 追多)",
        "brkShort": "下面做空 brkShort(跌穿前日VAL 追空)",
        "fadeLong": "中間接多 fadeLong(VAL 接多 → POC)",
        "fadeShort": "中間放空 fadeShort(VAH 放空 → POC)",
    }
    print(f"\n{'腿':<40} {'n':>5} {'win%':>7} {'pnl':>10}", flush=True)
    print("-" * 66, flush=True)
    for k in ("brkLong", "brkShort", "fadeLong", "fadeShort"):
        n = eng.play_n.get(k, 0)
        w = eng.play_win.get(k, 0)
        p = eng.play_pnl.get(k, 0.0)
        wr = (100 * w / n) if n else 0.0
        print(f"{label[k]:<40} {n:>5} {wr:>6.1f}% {p:>+10.1f}", flush=True)
    fn = eng.play_n.get("fadeLong", 0) + eng.play_n.get("fadeShort", 0)
    fw = eng.play_win.get("fadeLong", 0) + eng.play_win.get("fadeShort", 0)
    fp = eng.play_pnl.get("fadeLong", 0.0) + eng.play_pnl.get("fadeShort", 0.0)
    print(f"{'中間盤整腿合計(fade 雙向)':<40} {fn:>5} {100*fw/max(fn,1):>6.1f}% {fp:>+10.1f}", flush=True)


if __name__ == "__main__":
    main()
