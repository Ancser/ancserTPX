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


class TestLanguageSwitchRemoved:
    """UI-001 is retired because the product now has one English UI."""

    def test_language_switch_code_and_markup_are_removed(self):
        html = (ROOT / "frontend" / "static" / "ancserTPX.html").read_text(encoding="utf-8")
        js = (ROOT / "frontend" / "static" / "ancserTPX.js").read_text(encoding="utf-8")
        css = (ROOT / "frontend" / "static" / "ancserTPX.css").read_text(encoding="utf-8")
        skin = SKIN.read_text(encoding="utf-8")

        assert '<html lang="en">' in html
        assert 'id="lang-toggle"' not in html
        assert "toggleLanguage" not in js
        assert "UI_LANG" not in js
        assert "tip.zh" not in js
        assert "lang-toggle" not in skin
        assert ".lang-toggle" not in css

    def test_normal_preset_controls_are_not_removed_with_language_switch(self):
        html = (ROOT / "frontend" / "static" / "ancserTPX.html").read_text(encoding="utf-8")
        assert 'id="preset-bt"' in html
        assert 'id="preset-live"' in html
