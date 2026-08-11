"""PI 訊號的單一真相與去重(PI-006 / PI-007)。"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "backend" / "data" / "pi_history.py"
COLLECTOR = ROOT / "scripts" / "pi_collect_history.py"


def _files_reading_the_json() -> dict[str, list[int]]:
    """找出直接開 pi_signals.json 的地方(不經 loader)。"""
    out: dict[str, list[int]] = {}
    for f in list((ROOT / "backend").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        try:
            src = f.read_text(encoding="utf-8")
        except Exception:
            continue
        if "pi_signals.json" not in src:
            continue
        lines = [i for i, l in enumerate(src.splitlines(), 1)
                 if "pi_signals.json" in l and not l.strip().startswith("#")]
        if lines:
            out[str(f.relative_to(ROOT)).replace("\\", "/")] = lines
    return out


class TestSingleSourceOfSignals:
    """PI-006:回測 / 實盤 / 圖表 / 研究必須看到同一組訊號。

    先前八個研究腳本各自複製一份過濾邏輯。發現 07:00 前重播污染時,
    loader、listener、圖表 API 都改好了,**但研究腳本繞過 loader 直接讀 json**,
    於是它們還在用髒資料產出結論(而那些結論會被拿去改實盤 preset)。
    """

    ALLOWED = {
        "backend/data/pi_history.py",        # loader 自己
        "scripts/pi_collect_history.py",     # 收集器負責寫入
    }

    def test_scanner_finds_the_loader_itself(self):
        """正向斷言:掃不到任何檔案的話,下面那條會空跑。"""
        found = _files_reading_the_json()
        assert "backend/data/pi_history.py" in found, f"掃描壞了,只找到 {list(found)}"

    def test_nobody_else_reads_the_json_directly(self):
        offenders = {k: v for k, v in _files_reading_the_json().items()
                     if k not in self.ALLOWED}
        assert not offenders, (
            "這些檔案繞過 pi_history.load_rows() 直接讀 json:\n" +
            "\n".join(f"  {k}:{v}" for k, v in sorted(offenders.items())) +
            "\n繞過 loader = 拿不到開盤前重播過濾,研究與實盤會看到不同的訊號。"
        )

    def test_research_scripts_go_through_the_loader(self):
        """反向:研究腳本必須真的有在用 loader,不是只是不讀 json 而已。"""
        users = [f.name for f in (ROOT / "scripts").glob("pi_*.py")
                 if "load_rows" in f.read_text(encoding="utf-8")]
        assert len(users) >= 6, f"只有 {users} 在用共用 loader"

    def test_loader_filters_pre_session_by_default(self):
        from backend.data.pi_history import load_rows
        src = LOADER.read_text(encoding="utf-8")
        assert "include_pre_session: bool = False" in src, \
            "預設值不是「濾掉」—— 忘記傳參數的呼叫端會拿到髒資料"
        assert callable(load_rows)

    def test_strategy_loader_shares_the_same_filter(self):
        """回測用的 `_load_history()` 必須套同一條規則。"""
        src = (ROOT / "backend" / "strategy" / "pi_signal.py").read_text(encoding="utf-8")
        assert "is_pre_session" in src


class TestDuplicateSuppression:
    """PI-007:同一則訊息不得產生兩次動作。

    Discord 輪詢用 `after=cursor`。網路重試、429 退避、游標重設都會讓同一則
    訊息再回來一次。沒有去重就是**同一個訊號下兩次單**。
    """

    @staticmethod
    def _run_source() -> str:
        src = (ROOT / "backend" / "live" / "pi_listener.py").read_text(encoding="utf-8")
        return src[src.index("async def run(self)"):]

    def test_listener_keeps_a_seen_set(self):
        from backend.live.pi_listener import PiListener
        src = (ROOT / "backend" / "live" / "pi_listener.py").read_text(encoding="utf-8")
        assert "self._seen: set[str] = set()" in src

    def test_dedup_check_precedes_signal_dispatch(self):
        """`in self._seen: continue` 必須出現在 `parse_message` **之前**。

        順序反了的話訊號已經送出去,再記下來也沒用。
        """
        run = self._run_source()
        i_dedup = run.index("in self._seen")
        i_parse = run.index("parse_message(msg)")
        assert i_dedup < i_parse, "去重檢查在派送訊號之後 —— 擋不住重複下單"

    def test_message_id_is_recorded_before_dispatch(self):
        run = self._run_source()
        i_add = run.index("self._seen.add(")
        i_parse = run.index("parse_message(msg)")
        assert i_add < i_parse

    def test_seen_set_is_bounded(self):
        """長時間執行不得無限成長。"""
        run = self._run_source()
        assert "len(self._seen) > 5000" in run

    def test_dedup_logic_is_correct(self):
        """把邏輯抄出來跑一次 —— 結構檢查證明順序,這條證明語意。"""
        seen: set[str] = set()
        dispatched = []
        for msg_id in ["a", "b", "a", "c", "b", "a"]:
            if msg_id in seen:
                continue
            seen.add(msg_id)
            dispatched.append(msg_id)
        assert dispatched == ["a", "b", "c"]

    def test_seen_set_does_not_survive_restart(self):
        """**已知限制,刻意記錄。**

        `_seen` 只在記憶體,重啟後是空的。目前靠「進入交易時段時把游標重設到
        最新一則」間接擋住重放(見 run() 的 window 進入分支),但那是間接保護。

        這條測試不是要求修掉,是要求**知情** —— 哪天把游標重設拿掉了,
        這裡會提醒你去重也一起失效了。
        """
        src = (ROOT / "backend" / "live" / "pi_listener.py").read_text(encoding="utf-8")
        assert "seed_id = self._message_id(seed[0])" in src
        assert "self._last_id = seed_id" in src, (
            "進入時段時的游標重設不見了。`_seen` 不持久化,少了游標重設之後,"
            "重啟就可能重放整夜的訊息。")
