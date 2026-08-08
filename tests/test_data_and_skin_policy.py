"""資料散佈政策與皮膚不得移除功能(DATA-006 / UI-001)。"""
from __future__ import annotations

import pickle
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "data" / "store"
SEED = STORE / "seed"
SKIN = ROOT / "frontend" / "static" / "tpx-glass-skin.js"


class TestStoreIsNotVersioned:
    """DATA-006:完整 store 不進版控,seed 不得含 Databento 資料。

    兩個獨立理由,缺一不可:

    1. **技術** —— 完整 store 210MB/商品,accumulator 每小時重寫整個檔案。
       git 對二進位檔是每個版本存一份完整 blob,追蹤它會讓 repo 無上限膨脹
       (先前 42 個歷史版本已佔掉 .git 的 259MB),而且 >100MB 直接推不上去。
    2. **授權** —— seed 隨 repo 散佈。Databento 是付費授權資料,
       不該公開散佈;TopstepX 段是自己帳號抓的。

    第 2 點光靠 .gitignore 擋不住 —— 有人重生 seed 時忘了濾就漏出去了。
    所以要驗**內容**,不是只驗規則。
    """

    def test_gitignore_excludes_the_full_store(self):
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "data/store/*.pkl" in rules

    def test_gitignore_whitelists_the_seed(self):
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "!data/store/seed/*.pkl" in rules

    def test_git_actually_ignores_the_accumulated_store(self):
        """驗實際行為,不是只驗規則字串 —— 規則順序寫錯的話字串在但沒效果。"""
        r = subprocess.run(
            ["git", "check-ignore", "data/store/MNQ_accumulated_1m.pkl"],
            cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, "完整 store 沒有被 gitignore 擋住"

    def test_git_does_not_ignore_the_seed(self):
        r = subprocess.run(["git", "check-ignore", "data/store/seed/MNQ_seed_1m.pkl"],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode != 0, "seed 被擋住了 —— 全新 clone 會沒有開機資料"

    def test_no_accumulated_store_is_tracked(self):
        r = subprocess.run(["git", "ls-files", "data/store/"],
                           cwd=ROOT, capture_output=True, text=True)
        tracked = [l for l in r.stdout.splitlines() if l.endswith(".pkl")]
        bad = [t for t in tracked if "/seed/" not in t]
        assert not bad, f"這些完整 store 被追蹤了: {bad}"

    def test_seed_files_are_tracked(self):
        """正向斷言:seed 沒被追蹤的話上一條會空跑通過。"""
        r = subprocess.run(["git", "ls-files", "data/store/seed/"],
                           cwd=ROOT, capture_output=True, text=True)
        assert [l for l in r.stdout.splitlines() if l.endswith(".pkl")]

    @pytest.mark.parametrize("sym", ["MNQ", "MES"])
    def test_seed_contains_only_topstepx_sourced_bars(self, sym):
        """**授權檢查**:seed 裡不得混進 Databento 資料。"""
        f = SEED / f"{sym}_seed_1m.pkl"
        if not f.exists():
            pytest.skip(f"{f.name} 不存在")
        bars = pickle.loads(f.read_bytes())
        sources = {getattr(b, "source", None) for b in bars}
        assert sources == {"topstepx"}, (
            f"{f.name} 含非 TopstepX 來源 {sources - {'topstepx'}} —— "
            f"Databento 是付費授權資料,不得隨 repo 散佈")

    @pytest.mark.parametrize("sym", ["MNQ", "MES"])
    def test_seed_is_small_enough_to_version(self, sym):
        f = SEED / f"{sym}_seed_1m.pkl"
        if not f.exists():
            pytest.skip(f"{f.name} 不存在")
        mb = f.stat().st_size / 1048576
        assert mb < 25, f"{f.name} 已經 {mb:.1f}MB —— seed 是開機種子,不是資料庫"


class TestSkinDoesNotRemoveFunctionality:
    """UI-001:套用皮膚不得移除語言切換。

    2026-08-08 修復前,`tpx-glass-skin.js` 直接
    `document.querySelector("#lang-toggle")?.remove()` —— 套上玻璃皮膚之後
    使用者就再也切不了語言。

    皮膚是外觀,不該拿掉功能。而且因為 base DOM 裡按鈕還在,
    任何只看 HTML 的檢查都會以為它還在。
    """

    def test_skin_does_not_remove_the_language_toggle(self):
        src = SKIN.read_text(encoding="utf-8")
        for bad in ('#lang-toggle")?.remove()',
                    "#lang-toggle')?.remove()",
                    '#lang-toggle").remove()'):
            assert bad not in src, f"皮膚又把語言切換刪掉了: {bad}"

    def test_skin_reparents_the_existing_button(self):
        """必須是**搬移**既有節點,不是複製一個新的。

        複製的話 onclick / i18n 的 DOM walker 會對到兩個 id 相同的元素。
        """
        src = SKIN.read_text(encoding="utf-8")
        assert 'byId("lang-toggle")' in src
        assert "appendChild(lang)" in src

    def test_base_html_still_provides_the_button(self):
        """正向斷言:base DOM 沒有按鈕的話,上面兩條都沒意義。"""
        html = (ROOT / "frontend" / "static" / "ancserTPX.html").read_text(encoding="utf-8")
        assert 'id="lang-toggle"' in html
        assert "toggleLanguage()" in html

    def test_skin_removes_the_header_after_moving_the_button(self):
        """皮膚會 `header.remove()`。按鈕必須在那之前被搬走,否則一起消失。"""
        src = SKIN.read_text(encoding="utf-8")
        i_move = src.index("appendChild(lang)")
        i_kill = src.index("header.remove()")
        assert i_move < i_kill, "語言按鈕在 header 被移除之後才搬 —— 已經來不及了"
