"""蠟燭庫的錨點與快取語意(DATA-002 / DATA-004)。

## DATA-002 為什麼重要

券商在換月時會用「當前前月」當錨點回溯調整舊合約的價格。但那個調整值是
**每次 fetch 重算**的 —— 增量抓取只涵蓋當前合約時,調整值退回 0,那批 bar
就以未調整的價格寫進 store,於是 store **內部**長出一道假跳空。

假跳空不會拋例外。它會讓回測在那個時間點附近看到一根不存在的大 K,
ATR 被撐大、SL/TP 寬度全錯,而且是永久性的(store 只增不減)。

`merge()` 的規則:**store 的錨點是權威**。incoming 換了錨就把 incoming
平移回來,已存的歷史一個字都不動。

## DATA-004 為什麼重要

`load()` 有 mtime 快取。回傳共用 list 的話,呼叫端(30 多處,其中
`accumulator.store_status()` 確實會做 `bars.sort()`)就地修改會污染快取。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.data import candle_store as cs
from backend.db.models import Candle

UTC = timezone.utc


def _bars(n: int, base: float, start: datetime | None = None, step: float = 1.0):
    t0 = start or datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    return [Candle(timestamp=t0 + timedelta(minutes=i),
                   open=base + i * step, high=base + i * step + 2,
                   low=base + i * step - 2, close=base + i * step + 1,
                   volume=100) for i in range(n)]


class _Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = cs.STORE_DIR
        cs.STORE_DIR = Path(self.tmp.name)
        cs.invalidate_cache()

    def tearDown(self):
        cs.STORE_DIR = self._orig
        cs.invalidate_cache()
        self.tmp.cleanup()


class ReanchorDetection(_Store):
    """DATA-002:偵測「大而一致的固定偏移」,不要誤判正常的 bar 修訂。"""

    def test_constant_large_offset_is_detected(self):
        old = _bars(40, 20000.0)
        new = [cs._shift(b, -50.0) for b in old]      # incoming 整段高 50 點
        by_ts = {cs._as_utc(b.timestamp): b for b in old}
        self.assertAlmostEqual(cs.detect_reanchor(new, by_ts), 50.0, places=4)

    def test_small_revisions_are_not_reanchor(self):
        """一兩檔的修訂是正常的,不能當成換錨去平移整段。"""
        old = _bars(40, 20000.0)
        new = [cs._shift(b, -0.25) for b in old]
        by_ts = {cs._as_utc(b.timestamp): b for b in old}
        self.assertIsNone(cs.detect_reanchor(new, by_ts))

    def test_noisy_diffs_are_not_reanchor(self):
        """偏移不一致 = 行情差異,不是換錨。九成同意才算。"""
        old = _bars(40, 20000.0)
        new = [cs._shift(b, -50.0 if i % 2 else -10.0) for i, b in enumerate(old)]
        by_ts = {cs._as_utc(b.timestamp): b for b in old}
        self.assertIsNone(cs.detect_reanchor(new, by_ts))

    def test_too_few_overlapping_bars_is_not_reanchor(self):
        """重疊太少不足以判斷 —— 寧可不動也不要亂平移。"""
        old = _bars(5, 20000.0)
        new = [cs._shift(b, -50.0) for b in old]
        by_ts = {cs._as_utc(b.timestamp): b for b in old}
        self.assertIsNone(cs.detect_reanchor(new, by_ts))


class MergePreservesStoredAnchor(_Store):
    """DATA-002:併入不得改動已存歷史,也不得留下假跳空。"""

    def test_stored_bars_are_never_rewritten_by_reanchored_incoming(self):
        old = _bars(40, 20000.0)
        cs.merge(old, "MNQ", 1)
        before = [(b.timestamp, b.close) for b in cs.load("MNQ", 1)]

        # 換錨後的同一段 + 往後延伸的新資料
        shifted = [cs._shift(b, -50.0) for b in old]
        ext = [cs._shift(b, -50.0) for b in
               _bars(20, 20040.0, start=old[-1].timestamp + timedelta(minutes=1))]
        cs.merge(shifted + ext, "MNQ", 1)

        after = {b.timestamp: b.close for b in cs.load("MNQ", 1)}
        for ts, close in before:
            self.assertAlmostEqual(after[ts], close, places=4,
                                   msg="已存的歷史被 incoming 的錨點改寫了")

    def test_no_synthetic_gap_at_the_seam(self):
        """接縫處的相鄰兩根不得出現 50 點的假跳空。"""
        old = _bars(40, 20000.0)
        cs.merge(old, "MNQ", 1)
        ext = [cs._shift(b, -50.0) for b in
               _bars(20, 20040.0, start=old[-1].timestamp + timedelta(minutes=1))]
        cs.merge([cs._shift(b, -50.0) for b in old] + ext, "MNQ", 1)

        bars = cs.load("MNQ", 1)
        jumps = [abs(bars[i + 1].open - bars[i].close) for i in range(len(bars) - 1)]
        self.assertLess(max(jumps), 10.0,
                        f"接縫處出現 {max(jumps):.2f} 點假跳空 —— 錨點沒保住")

    def test_reanchor_is_recorded_as_a_seam(self):
        """自動修正必須留痕,否則沒人知道資料被動過。"""
        old = _bars(40, 20000.0)
        cs.merge(old, "MNQ", 1)
        cs.merge([cs._shift(b, -50.0) for b in old], "MNQ", 1)
        seams = cs.known_seams("MNQ", 1)
        self.assertTrue(any(s.get("kind") == "reanchor_corrected" for s in seams))

    def test_normal_append_is_untouched(self):
        """對照組:沒換錨時 merge 必須原封不動地接上去。

        少了這條,一個「永遠平移」的 bug 也會讓上面幾條通過。
        """
        old = _bars(40, 20000.0)
        cs.merge(old, "MNQ", 1)
        ext = _bars(20, 20040.0, start=old[-1].timestamp + timedelta(minutes=1))
        cs.merge(ext, "MNQ", 1)
        got = {b.timestamp: b.close for b in cs.load("MNQ", 1)}
        for b in ext:
            self.assertAlmostEqual(got[b.timestamp], b.close, places=4)
        self.assertEqual(len(got), 60)


class StoreOnlyGrows(_Store):
    """DATA-001:只增不減。"""

    def test_merging_a_subset_does_not_truncate(self):
        cs.merge(_bars(40, 20000.0), "MNQ", 1)
        cs.merge(_bars(5, 20000.0), "MNQ", 1)
        self.assertEqual(len(cs.load("MNQ", 1)), 40)


class LoadReturnsShallowCopy(_Store):
    """DATA-004:呼叫端會就地 sort,共用 list 會污染快取。"""

    def test_mutating_the_result_does_not_affect_the_next_load(self):
        cs.merge(_bars(30, 20000.0), "MNQ", 1)
        first = cs.load("MNQ", 1)
        n = len(first)
        first.reverse()
        first.pop()
        second = cs.load("MNQ", 1)
        self.assertEqual(len(second), n, "快取被呼叫端就地修改污染了")
        self.assertEqual(second, sorted(second, key=lambda c: c.timestamp))

    def test_cache_actually_serves_the_second_call(self):
        """正向斷言:證明上面測的是快取路徑,不是每次都重讀檔案。"""
        cs.merge(_bars(30, 20000.0), "MNQ", 1)
        a = cs.load("MNQ", 1)
        b = cs.load("MNQ", 1)
        self.assertIsNot(a, b, "回傳了同一個 list 物件 —— 不是淺拷貝")
        self.assertIs(a[0], b[0], "連 Candle 都複製了 —— 那是深拷貝,太貴")


if __name__ == "__main__":
    unittest.main()
