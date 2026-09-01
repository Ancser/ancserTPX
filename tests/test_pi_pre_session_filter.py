"""PI 開盤前重播過濾的回歸測試。

為什麼需要測試:bot 每天在美西 06:30–07:00 之間重播**前一交易日**累積的
標記(259 筆歷史裡有 85 筆落在這區,佔 33%;06:33 一分鐘就佔 69 筆)。

這些不是即時訊號。漏擋的後果分兩層:
  - 回測:訊號數虛增、方向來自已走完的行情 → 結論整個作廢(實際發生過一次)
  - 實盤:**每天開盤必然多出假進場**,真金白銀

過濾點有四處(實盤 listener / 收集器 / 回測 loader / 圖表 API),外加 8 個
研究腳本各自繞過 loader 自己讀 json。任何一處漏掉就會前後不一致,所以這裡
把「不該進來的東西進不來」釘死。
"""
from datetime import datetime, timezone

import pytest

from backend.live.pi_listener import (
    SESSION_START_PT,
    is_pre_session,
    message_is_pre_session,
    parse_message,
)


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class TestBoundary:
    """07:00 PT 是分界。夏令 PDT=UTC-7、冬令 PST=UTC-8 都要對。"""

    @pytest.mark.parametrize("h,mi,want", [
        (13, 30, True),    # 06:30 PT 開盤
        (13, 33, True),    # 06:33 PT —— 主要重播批次
        (13, 39, True),    # 06:39 PT —— 使用者發現的第二批
        (13, 59, True),    # 06:59 PT
        (14, 0, False),    # 07:00 PT —— 分界,開始採用
        (14, 1, False),
        (20, 0, False),    # 13:00 PT 收工
    ])
    def test_pdt_summer(self, h, mi, want):
        assert is_pre_session(_utc(2026, 7, 15, h, mi)) is want

    @pytest.mark.parametrize("h,mi,want", [
        (14, 59, True),    # 06:59 PST
        (15, 0, False),    # 07:00 PST
    ])
    def test_pst_winter(self, h, mi, want):
        """時區換算不能寫死 UTC 偏移 —— 冬令差一小時就會濾錯一整段。"""
        assert is_pre_session(_utc(2026, 1, 15, h, mi)) is want

    def test_naive_datetime_treated_as_utc(self):
        assert is_pre_session(datetime(2026, 7, 15, 13, 33)) is True
        assert is_pre_session(datetime(2026, 7, 15, 20, 0)) is False

    def test_threshold_constant_is_seven_am(self):
        """有人把門檻改回 06:35 之類的話要立刻紅燈。"""
        assert SESSION_START_PT == (7, 0)


class TestLivePath:
    """實盤 listener:重播訊息不得產生訊號。這條護的是真錢。"""

    RECAP = {
        "id": "1",
        "timestamp": "2026-07-15T13:33:01.240000+00:00",   # 06:33 PT
        "author": {"id": "1514456965622005870"},
        "content": "@everyone 🚨 π信号出现（QQQ）\n\n• 淡蓝圈 ×1（大）\n• 青π ×1（中）\n",
    }
    INTRADAY = {
        "id": "2",
        "timestamp": "2026-07-15T17:12:44.000000+00:00",   # 10:12 PT
        "author": {"id": "1514456965622005870"},
        "content": "@everyone 🚨 π信号出现（QQQ）\n\n• 青π ×1（中）\n",
    }

    def test_recap_message_is_skipped(self):
        assert message_is_pre_session(self.RECAP) is True

    def test_intraday_message_is_kept(self):
        assert message_is_pre_session(self.INTRADAY) is False

    def test_recap_would_otherwise_produce_signals(self):
        """證明過濾**確實有在擋** —— 這則訊息本身是可解析、會出訊號的。

        少了這個斷言,就算 parse_message 壞掉回空,上面兩個測試也會通過。
        """
        assert len(parse_message(self.RECAP)) > 0

    def test_broken_timestamp_does_not_crash(self):
        """過濾器對壞時戳 fail-open(回 False = 不跳過)。"""
        assert message_is_pre_session({"id": "x", "timestamp": "not-a-date"}) is False
        assert message_is_pre_session({"id": "x"}) is False

    @pytest.mark.parametrize("ts", ["not-a-date", "", None, 12345, "2026-13-45"])
    def test_malformed_timestamp_does_not_kill_the_listener(self, ts):
        """**P0 回歸測試(1.0.10j)。**

        原本兩段合起來會殺掉 listener:
          `message_is_pre_session` 對壞時戳 fail-open → 不跳過
          → `parse_message` 直接 `fromisoformat` → ValueError
          → 而 run() 的 try **只包 callback,不包 parse_message**
          → 整個 listener task 死掉,之後所有訊號都收不到,且沒有任何提示。

        實盤上這是最壞的失敗型態:靜默停止工作,看起來像「今天沒有訊號」。
        """
        msg = {"id": "x", "timestamp": ts, "author": {"id": "1514456965622005870"},
               "content": self.INTRADAY["content"]}
        assert parse_message(msg) == []          # 略過,不是拋例外

    def test_valid_timestamp_still_parses(self):
        """正向斷言:證明上面不是因為 parse_message 對所有輸入都回空而通過。"""
        assert len(parse_message(self.INTRADAY)) == 1

    def test_run_loop_guards_parse_message(self):
        """結構性:run() 必須把 parse_message 包在 try 裡。

        只修 parse_message 不夠 —— 未來任何未預期的訊息形狀(欄位缺失、
        型別改變)都可能從別的地方拋出來。
        """
        from pathlib import Path
        src = Path("backend/live/pi_listener.py").read_text(encoding="utf-8")
        run = src[src.index("async def run(self)"):]
        i_parse = run.index("sigs = parse_message(msg)")
        before = run[max(0, i_parse - 200):i_parse]
        assert "try:" in before, "parse_message 沒有被 try 包住"
        i_guard = run.index("解析訊息")
        assert i_guard > i_parse, "沒有對應的 except 分支"


class TestBacktestPath:
    """回測 loader 不得載入重播訊號。"""

    def test_loader_filters_pre_session(self, monkeypatch, tmp_path):
        import json

        from backend.strategy import pi_signal as ps

        rows = [
            {"id": "1", "ts": "2026-07-15T13:33:01+00:00", "pre_session": True,
             "symbol": "QQQ", "marks": [{"kind": "青π", "count": 1, "size": "中"}],
             "content": ""},
            # 沒有 pre_session 欄位的舊檔 —— 必須靠時間再判一次
            {"id": "2", "ts": "2026-07-15T13:39:00+00:00",
             "symbol": "QQQ", "marks": [{"kind": "青π", "count": 1, "size": "中"}],
             "content": ""},
            {"id": "3", "ts": "2026-07-15T17:12:44+00:00", "pre_session": False,
             "symbol": "QQQ", "marks": [{"kind": "青π", "count": 1, "size": "中"}],
             "content": ""},
        ]
        f = tmp_path / "pi_signals.json"
        f.write_text(json.dumps(rows), encoding="utf-8")
        # 1.0.10: 載入點搬到共用 loader(PI-006),所以要 patch 它的路徑,
        # 不是 pi_signal 自己那個轉出來的別名。
        from backend.data import pi_history
        monkeypatch.setattr(pi_history, "HIST_PATH", f)
        monkeypatch.setattr(ps, "_HIST_CACHE", None)

        out = ps._load_history()
        assert len(out) == 1, "只有 10:12 PT 那筆該留下"
        assert not any(is_pre_session(ts) for ts, _ in out)

    def test_loader_filters_multi_mark_messages_even_after_session(self, monkeypatch, tmp_path):
        import json

        from backend.data import pi_history

        rows = [
            {"id": "single", "ts": "2026-07-15T17:12:44+00:00",
             "symbol": "QQQ", "marks": [{"kind": "青π", "count": 1}]},
            {"id": "aggregate", "ts": "2026-07-15T17:12:45+00:00",
             "symbol": "QQQ", "marks": [
                 {"kind": "淡蓝圈", "count": 1},
                 {"kind": "青π", "count": 1},
             ]},
        ]
        f = tmp_path / "pi_signals.json"
        f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

        assert len(pi_history.load_rows(path=f)) == 1
        assert pi_history.load_rows(path=f)[0]["id"] == "single"
        assert [row["id"] for row in pi_history.load_rows(
            include_pre_session=True, path=f
        )] == ["single", "aggregate"]

    def test_live_replay_rows_are_run_scoped_and_do_not_mutate_history_cache(self, tmp_path, monkeypatch):
        import json

        from backend.data import pi_history
        from backend.strategy import pi_signal as ps

        base = [{
            "id": "hist-1",
            "ts": "2026-08-10T17:12:00+00:00",
            "symbol": "QQQ",
            "marks": [{"kind": "青π", "size": "中", "count": 1}],
            "content": "",
        }]
        f = tmp_path / "pi_signals.json"
        f.write_text(json.dumps(base), encoding="utf-8")
        monkeypatch.setattr(pi_history, "HIST_PATH", f)
        monkeypatch.setattr(ps, "_HIST_CACHE", None)

        replay = [{
            "id": "live-1",
            "ts": "2026-08-11T17:12:49+00:00",
            "symbol": "QQQ",
            "marks": [{"kind": "青π", "size": "中", "count": 1}],
            "content": "",
        }]
        with_replay = ps._load_history(replay)
        assert {sig.message_id for _, sig in with_replay} == {"hist-1", "live-1"}

        # A normal run still sees only immutable historical rows.
        assert [sig.message_id for _, sig in ps._load_history()] == ["hist-1"]


class TestSizeIsNotUsedForFiltering:
    """size 是視覺系統的多餘分類,不得影響是否進場。

    實測 size 對圈類是常數(深蓝圈 13/13、淡蓝圈 97/97 都是「大」),
    只有 π 類在 中/小 之間變動,而使用者確認 π 符號本身沒有大小之分。
    強弱軸是**種類**(深蓝圈=大威力、淡蓝圈=小威力)。
    """

    @pytest.mark.parametrize("size", ["大", "中", "小", "", None])
    def test_same_kind_accepted_regardless_of_size(self, size):
        from backend.db.models import StrategyParams
        from backend.strategy.pi_signal import PiSignalStrategy

        s = PiSignalStrategy(StrategyParams())
        assert "青π" in s.pi_long_kinds
        # 過濾清單是種類的集合,不含任何尺寸資訊
        assert all(isinstance(k, str) for k in s.pi_long_kinds)
        assert not any(x in str(s.pi_long_kinds) for x in ("大", "中", "小"))

    def test_short_side_off_by_default(self):
        """濾乾淨後空方淨虧(PF 0.91),預設不做空。"""
        from backend.db.models import StrategyParams
        from backend.strategy.pi_signal import PiSignalStrategy

        assert PiSignalStrategy(StrategyParams()).pi_short_kinds == ()

    def test_long_only_overrides_signal_set(self):
        """pi_long_only 必須壓過含空方的 signal_set —— 否則設定會被繞過。"""
        from backend.db.models import StrategyParams
        from backend.strategy.pi_signal import PiSignalStrategy

        for st in ("pi_only", "all", "pi_strict"):
            s = PiSignalStrategy(StrategyParams(pi_signal_set=st, pi_long_only=True))
            assert s.pi_short_kinds == (), f"{st} 洩漏了空方"

    def test_explicit_matrix_kinds_override_legacy_signal_set(self):
        """The PI matrix can select exact kinds without changing old presets."""
        from backend.db.models import StrategyParams
        from backend.strategy.pi_signal import PiSignalStrategy

        s = PiSignalStrategy(StrategyParams(
            pi_signal_set="all",
            pi_long_only=False,
            pi_long_kinds=["青π"],
            pi_short_kinds=["粉π"],
        ))
        assert s.pi_long_kinds == ("青π",)
        assert s.pi_short_kinds == ("粉π",)

    def test_short_bubbles_are_record_only_even_when_explicitly_selected(self):
        """Circle level/size confusion must not re-enable short entries."""
        from types import SimpleNamespace

        from backend.db.models import StrategyParams
        from backend.strategy.pi_signal import PiSignalStrategy

        s = PiSignalStrategy(StrategyParams(
            pi_long_only=False,
            pi_long_kinds=["青π"],
            pi_short_kinds=["粉π", "紫圈"],
        ))
        assert s.pi_short_kinds == ("粉π",)
        base = dict(message_id="short-bubble", future="MNQ", equity="QQQ", size="中",
                    pos=None, raw="")
        assert not s.push(SimpleNamespace(direction=-1, kind="紫圈", **base))
        assert s.push(SimpleNamespace(direction=-1, kind="粉π", **{
            **base, "message_id": "short-pi",
        }))

    def test_empty_matrix_side_is_disabled(self):
        """Turning every switch off must not silently mean "allow all"."""
        from types import SimpleNamespace

        from backend.db.models import StrategyParams
        from backend.strategy.pi_signal import PiSignalStrategy

        s = PiSignalStrategy(StrategyParams(
            pi_long_only=False,
            pi_long_kinds=[],
            pi_short_kinds=[],
        ))
        base = dict(message_id="matrix", future="", equity="QQQ", size="",
                    pos=None, raw="")
        assert not s.push(SimpleNamespace(direction=1, kind="青π", **base))
        assert not s.push(SimpleNamespace(direction=-1, kind="粉π", **base))
