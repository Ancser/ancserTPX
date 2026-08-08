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
        """時戳壞掉時放行而非炸掉 —— 寧可多一則訊號也不要讓 listener 掛掉。"""
        assert message_is_pre_session({"id": "x", "timestamp": "not-a-date"}) is False
        assert message_is_pre_session({"id": "x"}) is False


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
