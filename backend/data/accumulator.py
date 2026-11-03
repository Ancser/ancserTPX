"""1m 資料累積器(伺服器背景任務 + CLI 共用核心)。

## 問題

歷史版本的累積只有兩個觸發點,而且都只針對「UI 當下選中的合約」:
    frontend/static/ancserTPX.js:4870   connectAPI()
    frontend/static/ancserTPX.js:5139   _ensureBacktestData()

所以跑 MNQ 回測不會累積 MES、開著網頁不動不會累積、實盤執行中也不會。

目前背景任務維持自動抓取，但只寫入 candle_store 的 pending journal；
它不會為了查看或保存而解包完整 store。完整 merge 交給明確的歷史操作。

而券商只保留約 60 天 1m 資料 —— 任何商品超過 60 天沒抓,中間就出現
**永久補不回來的空洞**(store 只增不減,但缺掉的那段再也拿不到)。

## 換月正確性(1.3.0)

store 的合約帳本(`contract_ledger`,meta["contract_rolls"])決定每一分鐘屬於哪張約。
新的換月沿用 live 交易與連續圖表同一條規則(`contract_roll_start`:季月第 3 個週五 − 8 天):
時間到了就量兩張約在錨點分鐘的價差、回溯調整整個 store 一次、記入帳本,之後只寫新約。
舊版「逐日取成交量大者」會在換月後寫回舊約,已移除。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta
from typing import Iterable, Optional

from backend.broker.topstepx import contract_roll_start
from backend.data import candle_store, contract_ledger
from backend.db.models import BarUnit, current_quarterly_contract_id
from backend.timebase import UTC, as_utc as _utc, utc_now

logger = logging.getLogger(__name__)

# MNQ is the default product in this app.  MES is activated only after the
# user actually selects/uses an MES contract; this keeps an unused ES product
# out of startup network work as well as out of memory.
DEFAULT_SYMBOLS = ("MNQ",)
RETENTION_DAYS = 60       # 券商 1m 保留期(實測 H26 及更早全空)
OVERLAP_HOURS = 3         # 增量重疊,避免邊界漏根
ROLL_WINDOW_DAYS = 12     # 日曆換月日前後視為換月窗口
UNKNOWN_STORE_WARMUP_DAYS = 2  # old pkl without bounds metadata: recent only
MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}
_CODE_MONTH = {v: k for k, v in MONTH_CODE.items()}

_SYMBOL_ALIASES = {
    "ENQ": "MNQ",
    "NQ": "MNQ",
    "MNQ": "MNQ",
    "ES": "MES",
    "MES": "MES",
}
_ACTIVE_SYMBOLS = set(DEFAULT_SYMBOLS)
_ACTIVE_SYMBOLS_LOCK = threading.Lock()


def normalize_symbol(symbol: str) -> str:
    """Map contract aliases to the two supported candle-store products."""
    raw = str(symbol or "").strip().upper()
    return _SYMBOL_ALIASES.get(raw, raw)


def activate_symbol(symbol: str) -> str:
    """Start tracking a product after an explicit UI/API use of its contract."""
    normalized = normalize_symbol(symbol)
    if normalized not in {"MNQ", "MES"}:
        return normalized
    with _ACTIVE_SYMBOLS_LOCK:
        _ACTIVE_SYMBOLS.add(normalized)
    logger.info("[accumulate] activated %s after explicit contract use", normalized)
    return normalized


def active_symbols() -> tuple[str, ...]:
    with _ACTIVE_SYMBOLS_LOCK:
        return tuple(sorted(_ACTIVE_SYMBOLS))


def store_status(symbol: str) -> dict:
    # Startup status must never decode the full persistent pickle.  The
    # lightweight sidecars include canonical bounds plus the pending journal.
    symbol = normalize_symbol(symbol)
    status = candle_store.lightweight_status(symbol, 1)
    now = utc_now()
    last = status.get("last")
    if last is not None and status.get("age_days") is None:
        status["age_days"] = (now - last).days
    return status


async def _ensure_store_roll(client, symbol: str, now: datetime,
                             log=logger.info) -> tuple:
    """Roll the store once its current contract's calendar roll has passed.

    Uses the same rule live trading and the continuous chart use
    (``contract_roll_start``: third Friday - 8 days).  The price offset is
    measured from both contracts' bars at the roll anchor.  Returns
    ``(store contract id, roll instant if a roll was just applied)``; the id is
    None for a store without a contract ledger (then the calendar front is
    used directly).
    """
    rolls = await asyncio.to_thread(candle_store.contract_rolls, symbol)
    if not rolls:
        return None, None
    current = contract_ledger.make_contract_id(symbol, contract_ledger.contract_at(rolls, now))
    roll_at = contract_roll_start(current)
    if roll_at is None or now < roll_at:
        return current, None
    nxt = contract_ledger.next_contract(current)
    start = (roll_at - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = min(now, roll_at + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        old = await client.get_historical_bars_paginated(
            current, BarUnit.MINUTE, 1, start_time=start, end_time=end)
        new = await client.get_historical_bars_paginated(
            nxt, BarUnit.MINUTE, 1, start_time=start, end_time=end)
    except Exception as exc:
        log(f"[accumulate] {symbol} roll {current} -> {nxt} deferred: {type(exc).__name__}: {exc}")
        return current, None
    offset, anchor = contract_ledger.roll_offset(old, new, roll_at)
    if offset is None:
        log(f"[accumulate] {symbol} roll {current} -> {nxt} deferred: no shared minute yet")
        return current, None
    result = await asyncio.to_thread(
        candle_store.apply_contract_roll, symbol, roll_at, current, nxt, offset,
        f"calendar_roll; anchor {anchor.isoformat()}",
    )
    log(f"[accumulate] {symbol} store rolled {current.rsplit('.', 1)[-1]} -> "
        f"{nxt.rsplit('.', 1)[-1]} at {roll_at:%Y-%m-%d %H:%M}Z, offset {offset:+.2f}: {result}")
    return nxt, roll_at


async def _fetch(client, symbol: str, since: datetime, now: datetime,
                 log=logger.info) -> list:
    """Bars of the store's contract (per the contract ledger) for [since, now].

    Replaces the former per-day volume winner, which could write the old
    contract after the roll the rest of the app uses.  Minutes before the
    store's latest roll are fetched from the previous contract; the store's
    writer keeps only the contract that owns each minute.
    """
    store_cid, rolled_at = await _ensure_store_roll(client, symbol, now, log)
    current = store_cid or current_quarterly_contract_id(symbol, now)
    if rolled_at is not None and rolled_at < since:
        since = rolled_at        # the roll removed the old contract from here on
    cids = [current]
    rolls = await asyncio.to_thread(candle_store.contract_rolls, symbol)
    if rolls and since < rolls[-1]["at"]:
        cids.insert(0, contract_ledger.make_contract_id(symbol, rolls[-1]["from"]))

    start = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out: list = []
    for cid in cids:
        try:
            bars = await client.get_historical_bars_paginated(
                cid, BarUnit.MINUTE, 1, start_time=start, end_time=end)
        except Exception as exc:
            log(f"[accumulate] {symbol} {cid} 抓取失敗: {type(exc).__name__}: {exc}")
            continue
        out += [b for b in bars if getattr(b, "volume", 0) > 0]
    return out


async def accumulate_once(symbols: Optional[Iterable[str]] = None,
                          client=None, log=logger.info) -> dict:
    """把每個商品補到最新。回傳 {symbol: {added, total, last}}。

    client 為 None 時自行建立(用 .env 憑證)並在結束時關閉。
    """
    raw_symbols = list(symbols) if symbols is not None else list(active_symbols())
    symbols = list(dict.fromkeys(
        normalize_symbol(symbol) for symbol in raw_symbols
        if normalize_symbol(symbol) in {"MNQ", "MES"}
    ))
    if not symbols:
        symbols = list(DEFAULT_SYMBOLS)
    now = utc_now()
    owned = client is None
    if owned:
        from backend.broker.topstepx import TopstepXClient
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        user, key = os.getenv("TOPSTEPX_USERNAME"), os.getenv("TOPSTEPX_API_KEY")
        if not user or not key:
            log("[accumulate] .env 缺少 TOPSTEPX 憑證,略過")
            return {}
        client = TopstepXClient(
            username=user, api_key=key,
            use_demo=os.getenv("TOPSTEPX_USE_DEMO", "false").lower() == "true")
        await client.authenticate()

    out: dict = {}
    try:
        for s in symbols:
            # A cold snapshot may decode/sort millions of rows. Keep even that
            # first inspection off the FastAPI event loop.
            st = await asyncio.to_thread(store_status, s)
            floor = now - timedelta(days=RETENTION_DAYS - 1)
            if st["last"]:
                since = st["last"] - timedelta(hours=OVERLAP_HOURS)
                if since < floor:
                    log(f"[accumulate] {s} ⚠ 落後 {st['age_days']} 天且超出保留期 —— "
                        f"{st['last']:%Y-%m-%d} 至 {floor:%Y-%m-%d} 永久缺失")
                    since = floor
            else:
                # Existing stores created before bounds metadata are known to
                # contain history, but inspecting that pickle would defeat
                # lazy startup.  Warm only a recent bounded window; an
                # explicit backtest/left-edge read will materialize the full
                # store and merge the journal later.
                warmup_days = (
                    UNKNOWN_STORE_WARMUP_DAYS
                    if st.get("canonical_exists") else RETENTION_DAYS - 1
                )
                since = now - timedelta(days=warmup_days)
            bars = await _fetch(client, s, since, now, log)
            if not bars:
                out[s] = {"added": 0, "total": st["bars"], "last": st["last"]}
                continue
            # The background path never calls merge(): it writes a deduplicated
            # journal and therefore never decodes/re-saves the full pickle.
            pending_count, added = await asyncio.to_thread(
                candle_store.append_pending, bars, s, 1
            )
            after = await asyncio.to_thread(store_status, s)
            out[s] = {
                "added": added,
                "total": after["bars"],
                "last": after["last"],
                "pending": pending_count,
            }
            if added:
                log(f"[accumulate] {s} +{added} 根 → pending {pending_count:,} 根,"
                    f"最新 {after['last']:%Y-%m-%d %H:%M} (未解包完整 store)")
    finally:
        if owned:
            try:
                await client.close()
            except Exception:
                pass
    return out


async def accumulator_task(interval_s: int = 3600,
                           symbols: Optional[Iterable[str]] = None) -> None:
    """伺服器背景任務:定期抓取已啟用商品的最新增量。

    It saves to the lightweight pending journal.  It does not load the full
    store; MES joins the active set only after explicit contract use.
    """
    configured = tuple(dict.fromkeys(
        normalize_symbol(symbol) for symbol in (symbols or ())
        if normalize_symbol(symbol) in {"MNQ", "MES"}
    ))
    await asyncio.sleep(30)          # 讓啟動流程先完成
    while True:
        try:
            selected = tuple(dict.fromkeys((*configured, *active_symbols())))
            await accumulate_once(selected)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[accumulate] 本輪失敗: {type(exc).__name__}: {exc}")
        await asyncio.sleep(interval_s)
