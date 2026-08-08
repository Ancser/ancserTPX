"""換月判定必須看 instrument_id,不能看跳幅大小(DATA-003)。

## 這是已經發生過的 bug

`scripts/databento_backfill.py` 第一版用「跳幅 > 40 點 = 換月」去猜,結果把
**2026-04-10 12:30 UTC(美東 8:30 數據發布)的 +69.00 點真實行情**當成換月
抹平了。當時那一分鐘成交量從 2,690 暴增到 3,881、價格站穩不回補,而且原始
MNQM6 合約本身有完全相同的跳空 —— 它就是真行情。

抹平真行情的後果不可逆:那批 bar 寫進 store 後,回測從此看不到那天的走勢。
(當時是靠備份還原重跑。)

判準很簡單:**真換月時 instrument_id 一定變,行情波動時一定不變。**

`find_rolls` 是純函式,不需要 databento 套件(那是延遲 import),可以直接測。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pd = pytest.importorskip("pandas")

from scripts.databento_backfill import find_rolls  # noqa: E402

UTC = timezone.utc


def _frame(rows):
    """rows = [(minute_offset, instrument_id, open, close)]"""
    t0 = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
    idx = [t0 + timedelta(minutes=m) for m, *_ in rows]
    return pd.DataFrame(
        {"instrument_id": [r[1] for r in rows],
         "open": [r[2] for r in rows],
         "close": [r[3] for r in rows]},
        index=pd.DatetimeIndex(idx),
    )


def test_roll_is_detected_when_instrument_id_changes():
    df = _frame([(0, 111, 20000.0, 20001.0),
                 (1, 111, 20001.0, 20002.0),
                 (2, 222, 20260.0, 20261.0)])     # 換約 + 大跳空
    rolls = find_rolls(df)
    assert len(rolls) == 1
    ts, gap, prev_iid, iid = rolls[0]
    assert (prev_iid, iid) == (111, 222)
    assert gap == pytest.approx(20260.0 - 20002.0)


def test_large_real_move_without_id_change_is_not_a_roll():
    """**這是那個 bug 的回歸測試。**

    2026-04-10 的 +69 點:同一個 instrument_id,只是行情。
    誤判成換月就會把它抹平。
    """
    # 跳幅必須落在**接縫**上(open 相對前一根 close),那才是換月判定看的地方。
    # 第一版把 +69 放在同一根的 open→close 之間,接縫跳幅是 0 ——
    # 那樣連「用跳幅猜」的錯誤實作都會通過,測了等於沒測。
    df = _frame([(29, 333, 20000.00, 20000.25),
                 (30, 333, 20069.25, 20069.50),   # 接縫 +69.00,真實行情
                 (31, 333, 20069.50, 20070.00)])
    assert find_rolls(df) == [], "把真實行情誤判成換月了 —— 那批資料會被抹平"


@pytest.mark.parametrize("jump", [40.0, 69.0, 120.0, 268.5, 500.0])
def test_no_jump_size_however_large_triggers_a_roll(jump):
    """跳幅多大都不算換月。268.50 是 store 裡真實存在、刻意不修的那個。"""
    df = _frame([(0, 444, 20000.0, 20000.0),
                 (1, 444, 20000.0 + jump, 20000.0 + jump)])   # 跳幅在接縫上
    assert find_rolls(df) == []


def test_id_change_with_tiny_gap_is_still_a_roll():
    """反向:換約時價差很小也必須認得出來。

    只看跳幅的實作會漏掉這種 —— 那是「換月沒被偵測到」的另一半。
    """
    df = _frame([(0, 555, 20000.0, 20000.0),
                 (1, 666, 20000.5, 20001.0)])
    rolls = find_rolls(df)
    assert len(rolls) == 1
    assert rolls[0][2:] == (555, 666)


def test_multiple_rolls_are_all_reported():
    df = _frame([(0, 1, 100.0, 100.0),
                 (1, 2, 200.0, 200.0),
                 (2, 2, 200.0, 200.0),
                 (3, 3, 300.0, 300.0)])
    assert [r[2:] for r in find_rolls(df)] == [(1, 2), (2, 3)]


def test_single_contract_history_has_no_rolls():
    df = _frame([(i, 777, 20000.0 + i, 20000.0 + i) for i in range(50)])
    assert find_rolls(df) == []


def test_source_still_warns_against_guessing_by_jump_size():
    """那段註解記著這個 bug 為什麼存在。它跟程式碼一樣重要。

    註解沒了,下一個人只會看到一個「看起來可以簡化成比較跳幅」的迴圈。
    """
    src = (ROOT / "scripts" / "databento_backfill.py").read_text(encoding="utf-8")
    head = src[src.index("def find_rolls"):src.index("def find_rolls") + 900]
    assert "instrument_id" in head
    assert "69" in head, "2026-04-10 +69 點那個實例被從註解裡刪掉了"
