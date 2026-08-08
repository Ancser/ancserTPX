"""1.0.10: π 訊號 Discord 即時監聽。

輪詢頻道的新訊息,解析 ancserPiAlert 機器人的 @everyone 推播,轉成結構化訊號。

方向(使用者確認,並經 366 筆歷史驗證):
    藍色系 淡蓝圈 / 深蓝圈 / 青π  → 做多
    紫色系 紫圈 / 粉π            → 做空

標的對應:QQQ → MNQ、SPY → MES。

⚠️ 認證:`.env` 的 DISCORD_TOKEN 實測是**使用者 token**(`Bot {token}` 回 401)。
以個人 token 讀取屬於 self-botting,違反 Discord ToS。這是既有設定,本模組沿用
但**只做讀取**,不發訊息、不加反應。

⚠️ 速率:使用者指定上限 30 次/分。內建 token bucket,預設輪詢 **30 秒**
(= 2 次/分),並完整處理 429 的 retry_after。

時段:只在 **6:30–13:00 America/Los_Angeles** 輪詢,場外完全不打 API。
實測 259 則歷史訊號有 257 則(99.2%)落在此區間,6:00–6:30 之間 0 則。
進入時段時會把游標重設到最新一則 —— 頻道整天貼圖片,隔夜累積數千則,
沿用舊游標會讓 `after` 從最舊那批開始回傳,要翻很多頁才追上。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CHANNEL_ID = "1478899539845972078"
BOT_ID = "1514456965622005870"
API = "https://discord.com/api/v10"

SYMBOL_MAP = {"QQQ": "MNQ", "SPY": "MES"}
# +1 做多 / −1 做空
DIRECTION = {"淡蓝圈": +1, "深蓝圈": +1, "青π": +1, "紫圈": -1, "粉π": -1}

_SYM = re.compile(r"[（(]\s*(QQQ|SPY)\s*[）)]")
_MARK = re.compile(
    r"[•·・]\s*(\S+?)\s*[×x]\s*(\d+)\s*[（(]\s*([^·)）]+?)\s*(?:[·・]\s*([^)）]+?)\s*)?[）)]")


@dataclass
class PiSignal:
    message_id: str
    ts: datetime
    equity: str                 # QQQ / SPY
    future: str                 # MNQ / MES
    direction: int              # +1 / −1
    kind: str                   # 紫圈 / 青π / …
    size: str                   # 大 / 中 / 小
    pos: Optional[str]          # 上部 / 中部 / 下部 / None
    raw: str = ""

    @property
    def side(self) -> str:
        return "long" if self.direction > 0 else "short"


def parse_message(msg: dict) -> list[PiSignal]:
    """一則訊息可能含多個標記 → 回傳多個訊號。非目標 bot 或無法解析 → 空list。"""
    if (msg.get("author") or {}).get("id") != BOT_ID:
        return []
    content = msg.get("content") or ""
    m = _SYM.search(content)
    if not m:
        return []
    equity = m.group(1)
    ts = datetime.fromisoformat(str(msg["timestamp"]).replace("Z", "+00:00"))
    out: list[PiSignal] = []
    for mk in _MARK.finditer(content):
        kind = mk.group(1)
        d = DIRECTION.get(kind, 0)
        if not d:
            logger.warning("[PI] 未知標記 %r,略過(需更新 DIRECTION 表)", kind)
            continue
        out.append(PiSignal(
            message_id=str(msg["id"]),
            ts=ts.astimezone(timezone.utc),
            equity=equity,
            future=SYMBOL_MAP[equity],
            direction=d,
            kind=kind,
            size=mk.group(3).strip(),
            pos=(mk.group(4) or "").strip() or None,
            raw=content,
        ))
    return out


@dataclass
class _Bucket:
    """簡單 token bucket:每 60 秒補滿 `limit` 個令牌。"""
    limit: int = 30
    _stamps: list = field(default_factory=list)

    async def take(self) -> None:
        while True:
            now = time.monotonic()
            self._stamps = [t for t in self._stamps if now - t < 60.0]
            if len(self._stamps) < self.limit:
                self._stamps.append(now)
                return
            await asyncio.sleep(max(0.1, 60.0 - (now - self._stamps[0]) + 0.05))


class PiListener:
    """輪詢頻道,對每個新訊號呼叫 on_signal。

    on_signal 可以是同步或 async。丟出的例外會被記錄但不中斷監聽 ——
    一筆下單失敗不該讓整條訊號流停掉。
    """

    def __init__(self, token: str, on_signal: Callable[[PiSignal], Optional[Awaitable]],
                 poll_seconds: float = 30.0, rate_limit_per_min: int = 30,
                 channel_id: str = CHANNEL_ID,
                 window_start: tuple[int, int] = (6, 30),
                 window_end: tuple[int, int] = (13, 0),
                 tz_name: str = "America/Los_Angeles"):
        self._token = token
        self._cb = on_signal
        self._poll = max(1.0, float(poll_seconds))
        self._bucket = _Bucket(limit=max(1, int(rate_limit_per_min)))
        self._channel = channel_id
        self._last_id: Optional[str] = None
        self._seen: set[str] = set()
        self._stop = asyncio.Event()
        # 1.0.10: 只在正常交易時段輪詢。實測 259 則歷史訊號有 257 則(99.2%)
        # 落在 6:00–13:00 PT,且 6:00–6:30 之間 0 則 —— 6:30 起點不漏訊號。
        # 用 zoneinfo 而非固定 UTC 偏移,夏令/冬令自動正確。
        self._win_start = window_start
        self._win_end = window_end
        self._tz = ZoneInfo(tz_name)
        self._in_window = False

    def in_window(self, now: Optional[datetime] = None) -> bool:
        t = (now or datetime.now(timezone.utc)).astimezone(self._tz)
        cur = (t.hour, t.minute)
        return self._win_start <= cur < self._win_end

    @classmethod
    def from_env(cls, on_signal, **kw) -> "PiListener":
        tok = os.getenv("DISCORD_TOKEN", "").strip()
        if not tok:
            raise RuntimeError("DISCORD_TOKEN 未設定")
        return cls(tok, on_signal, **kw)

    def stop(self) -> None:
        self._stop.set()

    async def _fetch(self, client, params) -> Optional[list]:
        await self._bucket.take()
        try:
            r = await client.get(f"{API}/channels/{self._channel}/messages",
                                 params=params,
                                 headers={"Authorization": self._token},
                                 timeout=20)
        except Exception as e:
            logger.warning("[PI] 取訊息失敗 %s: %s", type(e).__name__, e)
            return None
        if r.status_code == 429:
            wait = float((r.json() or {}).get("retry_after", 2.0))
            logger.warning("[PI] 429 rate limited,等 %.1fs", wait)
            await asyncio.sleep(wait + 0.5)
            return None
        if r.status_code != 200:
            logger.warning("[PI] HTTP %s: %s", r.status_code, str(r.text)[:160])
            return None
        return r.json()

    async def run(self) -> None:
        import httpx
        async with httpx.AsyncClient() as client:
            logger.info("[PI] 監聽啟動 —— 時段 %02d:%02d–%02d:%02d %s,每 %.0f 秒輪詢",
                        *self._win_start, *self._win_end, self._tz.key, self._poll)

            while not self._stop.is_set():
                now_in = self.in_window()
                if now_in and not self._in_window:
                    # 進入時段:把游標重設到最新一則,跳過整夜累積的圖片訊息。
                    # 訊號只在時段內出現,所以略過場外 backlog 是正確的 ——
                    # 若沿用舊游標,`after` 會從最舊的那批開始回傳,要翻很多頁。
                    seed = await self._fetch(client, {"limit": 1})
                    if seed:
                        self._last_id = seed[0]["id"]
                        logger.info("[PI] 進入交易時段,游標重設為 %s", self._last_id)
                    else:
                        logger.warning("[PI] 進入時段但取訊息失敗,仍繼續")
                elif not now_in and self._in_window:
                    logger.info("[PI] 離開交易時段,暫停輪詢")
                self._in_window = now_in

                if not now_in:
                    # 場外不打 API —— 省請求也避免無謂的速率消耗
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=min(60.0, self._poll * 2))
                    except asyncio.TimeoutError:
                        pass
                    continue

                params = {"limit": 50}
                if self._last_id:
                    params["after"] = self._last_id
                msgs = await self._fetch(client, params)
                if msgs:
                    # after 回傳由新到舊 → 反轉成時間順序處理
                    for msg in reversed(msgs):
                        self._last_id = max(self._last_id or "0", msg["id"], key=int)
                        if msg["id"] in self._seen:
                            continue
                        self._seen.add(msg["id"])
                        for sig in parse_message(msg):
                            logger.info("[PI] 訊號 %s %s %s %s(%s)",
                                        sig.equity, sig.future, sig.side, sig.kind, sig.size)
                            try:
                                res = self._cb(sig)
                                if asyncio.iscoroutine(res):
                                    await res
                            except Exception as e:
                                logger.exception("[PI] on_signal 例外 %s: %s",
                                                 type(e).__name__, e)
                    if len(self._seen) > 5000:
                        self._seen = set(list(self._seen)[-2000:])
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                except asyncio.TimeoutError:
                    pass
