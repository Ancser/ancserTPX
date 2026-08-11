"""1.0.9: 跨商品 1m 資料累積器(伺服器背景任務 + CLI 共用核心)。

## 問題

在此之前,累積只有兩個觸發點,而且都只針對「UI 當下選中的合約」:
    frontend/static/ancserTPX.js:4870   connectAPI()
    frontend/static/ancserTPX.js:5139   _ensureBacktestData()

所以跑 MNQ 回測不會累積 MES、開著網頁不動不會累積、實盤執行中也不會
(_store_save 只在 /api/data/fetch-historical 內被呼叫)。

而券商只保留約 60 天 1m 資料 —— 任何商品超過 60 天沒抓,中間就出現
**永久補不回來的空洞**(store 只增不減,但缺掉的那段再也拿不到)。

## 換月正確性

current_quarterly_contract_id() 用日曆判定(季月第 3 個週五 − 8 天),但
實測流動性晚約 4 天才搬家(MNQ 2026-06-11 00:00Z 成交量 1563 → 36)。
換月窗口內本模組會同時抓新舊兩張約,**逐日取成交量較大者**,避免把空盤
報價寫進 store。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from backend.data import candle_store
from backend.db.models import BarUnit, current_quarterly_contract_id

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ("MNQ", "MES")
RETENTION_DAYS = 60       # 券商 1m 保留期(實測 H26 及更早全空)
OVERLAP_HOURS = 3         # 增量重疊,避免邊界漏根
ROLL_WINDOW_DAYS = 12     # 日曆換月日前後視為換月窗口
MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}
_CODE_MONTH = {v: k for k, v in MONTH_CODE.items()}


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _third_friday(year: int, month: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    n = 0
    while True:
        if d.weekday() == 4:
            n += 1
            if n == 3:
                return d
        d += timedelta(days=1)


def _split_code(cid: str) -> tuple[int, int]:
    code = cid.rsplit(".", 1)[-1]
    return _CODE_MONTH[code[0]], 2000 + int(code[1:])


def in_roll_window(symbol: str, now: datetime) -> bool:
    month, year = _split_code(current_quarterly_contract_id(symbol, now))
    roll = _third_friday(year, month) - timedelta(days=8)
    return abs((now - roll).days) <= ROLL_WINDOW_DAYS


def prev_contract_id(symbol: str, now: datetime) -> str:
    month, year = _split_code(current_quarterly_contract_id(symbol, now))
    pm, py = (month - 3, year) if month > 3 else (12, year - 1)
    return f"CON.F.US.{symbol}.{MONTH_CODE[pm]}{py % 100:02d}"


def store_status(symbol: str) -> dict:
    snapshot = candle_store.load_snapshot(symbol, 1)
    now = datetime.now(timezone.utc)
    if not snapshot.bars:
        return {"symbol": symbol, "bars": 0, "first": None, "last": None,
                "age_days": None, "state": "EMPTY"}
    # The immutable snapshot already owns sorted bounds. Do not materialize a
    # multi-million-pointer list merely to inspect count/first/last.
    lo, hi = snapshot.first_time, snapshot.last_time
    age = (now - hi).days
    return {"symbol": symbol, "bars": len(snapshot.bars), "first": lo, "last": hi,
            "age_days": age,
            "state": "FRESH" if age <= 3 else ("STALE" if age < RETENTION_DAYS else "HOLE")}


async def _fetch(client, symbol: str, since: datetime, now: datetime,
                 log=logger.info) -> list:
    cids = [current_quarterly_contract_id(symbol, now)]
    if in_roll_window(symbol, now):
        cids.append(prev_contract_id(symbol, now))
        log(f"[accumulate] {symbol} 換月窗口 → 同時抓 "
            + ", ".join(c.rsplit('.', 1)[-1] for c in cids))

    start = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    by_cid: dict[str, list] = {}
    for cid in cids:
        try:
            bars = await client.get_historical_bars_paginated(
                cid, BarUnit.MINUTE, 1, start_time=start, end_time=end)
        except Exception as exc:
            log(f"[accumulate] {symbol} {cid} 抓取失敗: {type(exc).__name__}: {exc}")
            continue
        by_cid[cid] = [b for b in bars if getattr(b, "volume", 0) > 0]

    if len(by_cid) <= 1:
        return next(iter(by_cid.values()), [])

    # 逐日取量大者 —— 日曆換月比實際量能換月早約 4 天
    daily: dict = {}
    for cid, bars in by_cid.items():
        for b in bars:
            d = _utc(b.timestamp).date()
            daily.setdefault(d, {})
            daily[d][cid] = daily[d].get(cid, 0) + getattr(b, "volume", 0)
    winner = {d: max(v, key=v.get) for d, v in daily.items()}
    for d in sorted(daily):
        if len(daily[d]) > 1:
            log(f"[accumulate] {symbol} {d}: 取 {winner[d].rsplit('.',1)[-1]} "
                + " ".join(f"{c.rsplit('.',1)[-1]}={v:,}" for c, v in daily[d].items()))
    out = []
    for cid, bars in by_cid.items():
        out += [b for b in bars if winner.get(_utc(b.timestamp).date()) == cid]
    return out


async def accumulate_once(symbols: Optional[Iterable[str]] = None,
                          client=None, log=logger.info) -> dict:
    """把每個商品補到最新。回傳 {symbol: {added, total, last}}。

    client 為 None 時自行建立(用 .env 憑證)並在結束時關閉。
    """
    symbols = list(symbols or DEFAULT_SYMBOLS)
    now = datetime.now(timezone.utc)
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
                since = floor
            bars = await _fetch(client, s, since, now, log)
            if not bars:
                out[s] = {"added": 0, "total": st["bars"], "last": st["last"]}
                continue
            # merge() intentionally remains synchronous for CLI callers; the
            # server accumulator runs its full dict/sort/pickle transaction in
            # a worker. candle_store serializes transactions per symbol/base.
            total, added = await asyncio.to_thread(candle_store.merge, bars, s, 1)
            after = await asyncio.to_thread(store_status, s)
            out[s] = {"added": added, "total": total, "last": after["last"]}
            if added:
                log(f"[accumulate] {s} +{added} 根 → {total:,} 根,"
                    f"最新 {after['last']:%Y-%m-%d %H:%M}")
    finally:
        if owned:
            try:
                await client.close()
            except Exception:
                pass
    return out


async def accumulator_task(interval_s: int = 3600,
                           symbols: Optional[Iterable[str]] = None) -> None:
    """伺服器背景任務:定期把所有商品補到最新。

    與 UI 操作完全解耦 —— 只要伺服器活著就會累積,不管你在看哪個商品、
    有沒有跑回測、有沒有開實盤。
    """
    await asyncio.sleep(30)          # 讓啟動流程先完成
    while True:
        try:
            await accumulate_once(symbols)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[accumulate] 本輪失敗: {type(exc).__name__}: {exc}")
        await asyncio.sleep(interval_s)
