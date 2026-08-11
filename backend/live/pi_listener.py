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
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from backend.data.pi_live_audit import (
    append_message_event,
    append_signal_event,
    append_status_event,
    load_message_ids,
    load_message_timestamps,
)

logger = logging.getLogger(__name__)

CHANNEL_ID = "1478899539845972078"
BOT_ID = "1514456965622005870"
API = "https://discord.com/api/v10"

SYMBOL_MAP = {"QQQ": "MNQ", "SPY": "MES"}
# +1 做多 / −1 做空
DIRECTION = {"淡蓝圈": +1, "深蓝圈": +1, "青π": +1, "紫圈": -1, "粉π": -1}
# Circle strength/size is a presentation classification, not a reliable
# trading feature. Keep parsed short circles auditable while the strategy
# enforces the current record-only policy for every short bubble kind.
SHORT_BUBBLE_KINDS = frozenset(
    kind for kind, direction in DIRECTION.items()
    if direction < 0 and kind.endswith("圈")
)

# ── 開盤前訊號過濾(1.0.10) ──────────────────────────────────────────
# bot 在美西開盤(06:30)後的頭半小時會重播**前一交易日**累積的標記,
# 不是即時訊號。最集中的是 06:33 的兩則(QQQ + SPY,間隔中位數 0.22 秒):
#
#   259 筆歷史中 69 筆(27%)出在 06:33 這一分鐘,40 個交易日裡 38 天都有
#   每則標記數:06:33 批 2.64(中位 3) vs 盤中 1.10(中位 1)
#   67 則之中 63 則(94%)的標記種類完全落在「前一交易日盤中」已出現過的集合裡
#
# 06:39 那批(7 筆)同樣有 5/7 落在前一日集合內 —— 重播不只發生在 06:33。
# 逐分鐘挑會挑不完(06:42 / 06:45 / 06:48 / 06:54 / 06:57 都有零星幾筆),
# 所以使用者指示直接**整段砍掉 07:00 之前**:85/259 筆。
#
# 拿重播回測 = 用今天的價格交易昨天的訊號。實盤照它下單更糟。
PI_TZ = ZoneInfo("America/Los_Angeles")
SESSION_START_PT = (7, 0)
# The listener intentionally avoids an unbounded Discord history replay.  On
# window entry it seeds the newest message, then only requests newer messages
# with Discord's ``after`` cursor; pre-session rows that are fetched are
# recorded and discarded before parsing/strategy dispatch.
HISTORY_FETCH_MODE = "seed_latest_then_after_cursor"


def is_pre_session(ts: datetime) -> bool:
    """該訊息是否在美西 07:00 之前(開盤後半小時的重播區)。"""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    t = ts.astimezone(PI_TZ)
    return (t.hour, t.minute) < SESSION_START_PT


def message_is_pre_session(msg: dict) -> bool:
    """Discord 訊息物件是否落在開盤前重播區。時戳壞掉時回 False(照常處理)。"""
    try:
        ts = datetime.fromisoformat(str(msg.get("timestamp", "")).replace("Z", "+00:00"))
    except Exception:
        return False
    return is_pre_session(ts)


def message_source_timestamp(msg: dict) -> Optional[datetime]:
    """Return a Discord message timestamp as UTC, or ``None`` if malformed."""
    try:
        stamp = datetime.fromisoformat(
            str((msg or {}).get("timestamp", "")).replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


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
    # Local dispatch time is diagnostic only.  ``ts`` remains Discord's
    # source/event timestamp and is the timestamp used by max-signal-age.
    received_at: Optional[datetime] = None

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
    # 1.0.10 P0:這裡原本直接 fromisoformat,時戳壞掉就拋 ValueError。
    # 而呼叫端 run() 的 try 只包住 callback,**不包 parse_message** ——
    # 所以一則畸形訊息會讓整個 listener task 死掉,之後所有訊號都收不到,
    # 而且沒有任何 log 說發生了什麼。
    #
    # `message_is_pre_session()` 對壞時戳是 fail-open(回 False = 不跳過),
    # 那個設計本身沒錯(寧可多一則訊號也不要漏),但它把畸形訊息直接送進
    # 這裡。兩邊合起來就從「放行」變成「崩潰」。
    try:
        ts = datetime.fromisoformat(str(msg.get("timestamp", "")).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        logger.warning("[PI] 訊息 %s 的時戳無法解析 %r —— 略過該則,listener 繼續",
                       msg.get("id"), msg.get("timestamp"))
        return []
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
                 tz_name: str = "America/Los_Angeles",
                 record_only: bool = False):
        self._token = token
        self._cb = on_signal
        # A recorder is intentionally independent from trading presets.  It
        # parses and audits every eligible PI mark, but never invokes a
        # strategy callback.  The normal Live listener keeps the default
        # ``False`` path for trading.
        self._record_only = bool(record_only)
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
        # Observable listener health.  The engine exposes this snapshot through
        # /live/status so a live UI never has to infer listener liveness from the
        # unchanged "running" intent flag.
        self._last_poll_monotonic: Optional[float] = None
        self._last_success_monotonic: Optional[float] = None
        self._last_error: Optional[str] = None
        self._consecutive_errors = 0
        self._signals_audited = 0
        self._audit_write_errors = 0
        # In-memory transport counters mirror the durable status rows and
        # keep /live/status useful even when the audit file is unavailable.
        self._poll_count = 0
        self._fetch_count = 0
        self._fetch_success_count = 0
        self._fetch_error_count = 0
        self._messages_seen = 0
        self._messages_invalid = 0
        self._messages_duplicates = 0
        self._messages_pre_session = 0
        self._messages_unparsed = 0
        self._callbacks = 0
        self._callback_errors = 0
        self._last_cursor: Optional[str] = None
        self._last_message_id: Optional[str] = None
        self._last_fetch_batch_size = 0
        self._last_fetch_source_ts: Optional[datetime] = None
        self._last_status_event: Optional[str] = None

    def _audit_status(self, event: str, **fields) -> None:
        """Write a best-effort listener status row without affecting polling."""
        self._last_status_event = str(event)
        if not append_status_event(event, **fields):
            self._audit_write_errors += 1

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

    def _record_error(self, reason: str) -> None:
        self._last_error = str(reason)
        self._consecutive_errors += 1

    def _record_success(self) -> None:
        self._last_success_monotonic = time.monotonic()
        self._last_error = None
        self._consecutive_errors = 0

    def get_health(self) -> dict:
        """Return a non-sensitive, JSON-safe listener health snapshot."""
        now = time.monotonic()

        def age(stamp: Optional[float]) -> Optional[float]:
            return round(max(0.0, now - stamp), 1) if stamp is not None else None

        return {
            "in_window": self._in_window,
            "last_poll_age_seconds": age(self._last_poll_monotonic),
            "last_success_age_seconds": age(self._last_success_monotonic),
            "consecutive_errors": self._consecutive_errors,
            "last_error": self._last_error,
            "signals_audited": self._signals_audited,
            "audit_write_errors": self._audit_write_errors,
            "poll_count": self._poll_count,
            "fetch_count": self._fetch_count,
            "fetch_success_count": self._fetch_success_count,
            "fetch_error_count": self._fetch_error_count,
            "messages_seen": self._messages_seen,
            "messages_invalid": self._messages_invalid,
            "messages_duplicates": self._messages_duplicates,
            "messages_pre_session": self._messages_pre_session,
            "messages_unparsed": self._messages_unparsed,
            "callbacks": self._callbacks,
            "callback_errors": self._callback_errors,
            "last_cursor": self._last_cursor,
            "last_message_id": self._last_message_id,
            "last_fetch_batch_size": self._last_fetch_batch_size,
            "last_fetch_source_ts": (
                self._last_fetch_source_ts.isoformat()
                if self._last_fetch_source_ts else None
            ),
            "last_status_event": self._last_status_event,
            "history_fetch_mode": HISTORY_FETCH_MODE,
            "record_only": self._record_only,
        }

    async def _fetch(self, client, params) -> Optional[list]:
        await self._bucket.take()
        self._fetch_count += 1
        self._last_poll_monotonic = time.monotonic()
        try:
            r = await client.get(f"{API}/channels/{self._channel}/messages",
                                 params=params,
                                 headers={"Authorization": self._token},
                                 timeout=20)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[PI] 取訊息失敗 %s: %s", type(e).__name__, e)
            self._fetch_error_count += 1
            self._audit_status(
                "fetch_error",
                reason=f"request_{type(e).__name__}",
                params=params,
            )
            self._record_error(f"request_{type(e).__name__}")
            return None
        if r.status_code == 429:
            try:
                payload = r.json()
                wait = float(payload.get("retry_after", 2.0)) if isinstance(payload, dict) else 2.0
            except (TypeError, ValueError, AttributeError):
                wait = 2.0
            wait = min(300.0, max(0.0, wait))
            logger.warning("[PI] 429 rate limited,等 %.1fs", wait)
            self._fetch_error_count += 1
            self._audit_status("fetch_error", reason="http_429", params=params,
                               retry_after=wait)
            self._record_error("http_429")
            await asyncio.sleep(wait + 0.5)
            return None
        if r.status_code != 200:
            logger.warning("[PI] HTTP %s: %s", r.status_code, str(r.text)[:160])
            self._fetch_error_count += 1
            self._audit_status("fetch_error", reason=f"http_{r.status_code}",
                               params=params, status_code=r.status_code)
            self._record_error(f"http_{r.status_code}")
            return None
        try:
            payload = r.json()
        except Exception as e:
            logger.warning("[PI] JSON 解析失敗 %s: %s", type(e).__name__, e)
            self._fetch_error_count += 1
            self._audit_status("fetch_error", reason="invalid_json", params=params)
            self._record_error("invalid_json")
            return None
        if not isinstance(payload, list):
            logger.warning("[PI] Discord 回應格式錯誤: %s", type(payload).__name__)
            self._fetch_error_count += 1
            self._audit_status("fetch_error", reason="invalid_payload", params=params,
                               payload_type=type(payload).__name__)
            self._record_error("invalid_payload")
            return None
        self._record_success()
        self._fetch_success_count += 1
        self._last_fetch_batch_size = len(payload)
        source_times = []
        for message in payload:
            try:
                stamp = datetime.fromisoformat(
                    str((message or {}).get("timestamp", "")).replace("Z", "+00:00")
                )
            except (AttributeError, TypeError, ValueError):
                continue
            source_times.append(stamp.astimezone(timezone.utc))
        self._last_fetch_source_ts = max(source_times) if source_times else None
        self._audit_status(
            "fetch_success",
            params=params,
            batch_size=len(payload),
            newest_message_id=self._message_id(payload[0]) if payload else None,
            oldest_message_id=self._message_id(payload[-1]) if payload else None,
            newest_source_ts=self._last_fetch_source_ts,
        )
        return payload

    async def backfill_recent(
        self,
        *,
        now: Optional[datetime] = None,
        days: int = 2,
        max_pages: int = 200,
    ) -> dict:
        """Record all eligible messages from today and yesterday.

        This is deliberately a record-only operation.  It walks Discord's
        newest-first history backwards until either the local two-day cutoff
        or a message already present in the durable audit stream is reached.
        Parsed marks are written to the live audit file, but no strategy
        callback is invoked, so a web/terminal restart can repair the chart
        without replaying trades.
        """
        if not self._record_only:
            raise RuntimeError("backfill_recent requires record_only=True")

        try:
            day_count = max(1, min(7, int(days)))
        except (TypeError, ValueError):
            day_count = 2
        try:
            page_limit = max(1, min(200, int(max_pages)))
        except (TypeError, ValueError):
            page_limit = 200

        local_now = (now or datetime.now(timezone.utc)).astimezone(self._tz)
        cutoff_local = datetime.combine(
            local_now.date() - timedelta(days=day_count - 1),
            datetime.min.time(),
            tzinfo=self._tz,
        )
        cutoff = cutoff_local.astimezone(timezone.utc)
        # The audit stream can grow over a long-running installation; keep
        # its restart scan off the asyncio event loop.
        known_ids, known_timestamps = await asyncio.gather(
            asyncio.to_thread(load_message_ids),
            asyncio.to_thread(load_message_timestamps),
        )
        before: Optional[str] = None
        pages = 0
        new_messages = 0
        duplicate_boundary = False
        cutoff_reached = False
        self._audit_status(
            "backfill_started",
            cutoff=cutoff,
            days=day_count,
            max_pages=page_limit,
            record_only=True,
        )

        import httpx

        try:
            async with httpx.AsyncClient() as client:
                while pages < page_limit and not cutoff_reached:
                    params = {"limit": 100}
                    if before:
                        params["before"] = before
                    msgs = await self._fetch(client, params)
                    if msgs is None or not msgs:
                        break
                    pages += 1

                    # Discord returns newest-first.  Only the prefix newer
                    # than the durable boundary is eligible; once an already
                    # recorded message is encountered, older pages are not
                    # needed for this catch-up run.
                    pending: list[dict] = []
                    for msg in msgs:
                        stamp = message_source_timestamp(msg)
                        if stamp is not None and stamp < cutoff:
                            cutoff_reached = True
                            break
                        msg_id = self._message_id(msg)
                        if (
                            (msg_id and msg_id in known_ids)
                            or (stamp is not None and stamp.isoformat() in known_timestamps)
                        ):
                            duplicate_boundary = True
                            break
                        pending.append(msg)

                    if pending:
                        await self._dispatch_messages(pending)
                        new_messages += len(pending)
                        known_ids.update(
                            msg_id for msg_id in (self._message_id(m) for m in pending)
                            if msg_id
                        )
                        known_timestamps.update(
                            stamp.isoformat()
                            for stamp in (message_source_timestamp(m) for m in pending)
                            if stamp is not None
                        )

                    if duplicate_boundary or cutoff_reached:
                        break

                    # ``before`` must move backwards even if the final row is
                    # malformed.  Snowflake ids are ordered by creation time.
                    oldest_id = next(
                        (self._message_id(msg) for msg in reversed(msgs) if self._message_id(msg)),
                        None,
                    )
                    if not oldest_id or oldest_id == before:
                        break
                    before = oldest_id
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record_error(f"backfill_{type(exc).__name__}")
            self._audit_status("backfill_error", reason=f"{type(exc).__name__}: {exc}")
            logger.exception("[PI] Discord history backfill failed: %s", exc)

        result = {
            "pages": pages,
            "new_messages": new_messages,
            "duplicate_boundary": duplicate_boundary,
            "cutoff_reached": cutoff_reached,
            "cutoff": cutoff.isoformat(),
        }
        self._audit_status("backfill_complete", **result)
        return result

    async def run(self) -> None:
        import httpx
        async with httpx.AsyncClient() as client:
            self._audit_status(
                "listener_started",
                channel_id=self._channel,
                window_start=f"{self._win_start[0]:02d}:{self._win_start[1]:02d}",
                window_end=f"{self._win_end[0]:02d}:{self._win_end[1]:02d}",
                timezone=self._tz.key,
                poll_seconds=self._poll,
                history_fetch_mode=HISTORY_FETCH_MODE,
                record_only=self._record_only,
            )
            logger.info("[PI] 監聽啟動 —— 時段 %02d:%02d–%02d:%02d %s,每 %.0f 秒輪詢",
                        *self._win_start, *self._win_end, self._tz.key, self._poll)

            while not self._stop.is_set():
                delay = self._poll
                try:
                    delay = await self._poll_once(client)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # Last-resort containment: a future response/message shape
                    # must be visible in health, but must not silently kill the
                    # task that feeds live PI signals.
                    self._audit_status("loop_error", reason=f"loop_{type(e).__name__}")
                    self._record_error(f"loop_{type(e).__name__}")
                    logger.exception("[PI] 輪詢迴圈例外 %s: %s —— listener 繼續",
                                     type(e).__name__, e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    async def _poll_once(self, client) -> float:
        """Run one bounded poll cycle and return the delay before the next."""
        self._poll_count += 1
        was_in_window = self._in_window
        now_in = self.in_window()
        if now_in and not self._in_window:
            # 進入時段:把游標重設到最新一則,跳過整夜累積的圖片訊息。
            seed = await self._fetch(client, {"limit": 1})
            seed_id = self._message_id(seed[0]) if seed else None
            if seed_id:
                self._last_id = seed_id
                self._last_cursor = seed_id
                seed_message = seed[0]
                if not append_message_event(seed_message, event="cursor_seed"):
                    self._audit_write_errors += 1
                self._audit_status(
                    "window_entered",
                    seed_id=seed_id,
                    seed_source_ts=seed_message.get("timestamp"),
                    seed_pre_session=message_is_pre_session(seed_message),
                    history_fetch_mode=HISTORY_FETCH_MODE,
                )
                logger.info("[PI] 進入交易時段,游標重設為 %s", self._last_id)
            else:
                if seed:
                    self._record_error("invalid_seed_message")
                    self._audit_status("window_entered", seed_id=None,
                                       seed_invalid=True)
                logger.warning("[PI] 進入時段但取訊息失敗,仍繼續")
        elif not now_in and self._in_window:
            logger.info("[PI] 離開交易時段,暫停輪詢")
        self._in_window = now_in
        if was_in_window and not now_in:
            self._audit_status("window_exited", cursor=self._last_id)

        if not now_in:
            # 場外不打 API —— 省請求也避免無謂的速率消耗
            self._audit_status("poll_complete", in_window=False,
                               cursor=self._last_id, batch_size=0)
            return min(60.0, self._poll * 2)

        params = {"limit": 50}
        if self._last_id:
            params["after"] = self._last_id
        msgs = await self._fetch(client, params)
        if msgs:
            await self._dispatch_messages(msgs)
        self._last_cursor = self._last_id
        self._audit_status(
            "poll_complete",
            in_window=now_in,
            cursor=self._last_id,
            batch_size=len(msgs or []),
            messages_seen=self._messages_seen,
            signals_audited=self._signals_audited,
        )
        return self._poll

    @staticmethod
    def _message_id(msg) -> Optional[str]:
        if not isinstance(msg, dict):
            return None
        msg_id = str(msg.get("id") or "")
        return msg_id if msg_id.isdigit() else None

    async def _dispatch_messages(self, msgs: list) -> None:
        """Validate and dispatch one Discord batch without leaking bad messages."""
        # after 回傳由新到舊 → 反轉成時間順序處理
        for msg in reversed(msgs):
            self._messages_seen += 1
            msg_id = self._message_id(msg)
            if not msg_id:
                logger.warning("[PI] 略過格式錯誤的 Discord 訊息")
                self._messages_invalid += 1
                self._audit_status("invalid_message", batch_size=len(msgs))
                self._record_error("invalid_message")
                continue
            self._last_id = max(self._last_id or "0", msg_id, key=int)
            self._last_cursor = self._last_id
            self._last_message_id = msg_id
            if msg_id in self._seen:
                self._messages_duplicates += 1
                continue
            self._seen.add(msg_id)
            # 開盤後半小時是前一交易日的重播,不是即時訊號。
            if message_is_pre_session(msg):
                self._messages_pre_session += 1
                if not append_message_event(msg, event="pre_session_skip"):
                    self._audit_write_errors += 1
                logger.info("[PI] 略過開盤前重播訊息 %s", msg_id)
                continue
            try:
                sigs = parse_message(msg)
            except Exception as e:
                self._record_error(f"parse_{type(e).__name__}")
                if not append_message_event(
                    msg,
                    event="parse_error",
                    error=f"{type(e).__name__}: {e}",
                ):
                    self._audit_write_errors += 1
                logger.exception("[PI] 解析訊息 %s 失敗 %s: %s —— 略過該則",
                                 msg_id, type(e).__name__, e)
                continue
            if not sigs:
                # Keep a durable explanation for a target-symbol message that
                # had no supported mark (bad timestamp/regex/unknown level).
                if ((msg.get("author") or {}).get("id") == BOT_ID
                        and _SYM.search(msg.get("content") or "")):
                    self._messages_unparsed += 1
                    if not append_message_event(msg, event="unparsed"):
                        self._audit_write_errors += 1
            for sig in sigs:
                received_at = datetime.now(timezone.utc)
                sig.received_at = received_at
                # Record before strategy filtering/callback so a signal that
                # is intentionally not traded is still auditable.
                audit_event = "recorded" if self._record_only else "received"
                if append_signal_event(sig, event=audit_event, received_at=received_at):
                    self._signals_audited += 1
                else:
                    self._audit_write_errors += 1
                logger.info("[PI] 訊號 %s %s %s %s(%s)",
                            sig.equity, sig.future, sig.side, sig.kind, sig.size)
                # The standalone web/terminal recorder stops at the audit
                # boundary.  It must never call a strategy, even when a saved
                # preset would otherwise allow this kind.
                if self._record_only:
                    continue
                try:
                    self._callbacks += 1
                    res = self._cb(sig)
                    if asyncio.iscoroutine(res):
                        res = await res
                    append_signal_event(
                        sig,
                        event="callback",
                        received_at=received_at,
                        accepted=res if isinstance(res, bool) else None,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._callback_errors += 1
                    self._record_error(f"callback_{type(e).__name__}")
                    append_signal_event(
                        sig,
                        event="callback_error",
                        received_at=received_at,
                        error=f"{type(e).__name__}: {e}",
                    )
                    logger.exception("[PI] on_signal 例外 %s: %s",
                                     type(e).__name__, e)
        if len(self._seen) > 5000:
            self._seen = set(list(self._seen)[-2000:])
        self._audit_status(
            "batch_processed",
            batch_size=len(msgs),
            cursor=self._last_id,
            messages_seen=self._messages_seen,
            messages_pre_session=self._messages_pre_session,
            messages_unparsed=self._messages_unparsed,
            signals_audited=self._signals_audited,
            callbacks=self._callbacks,
            callback_errors=self._callback_errors,
        )
