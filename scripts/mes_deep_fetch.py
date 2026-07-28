"""1.0.9: MES 深度歷史回補 — 探測券商可給的最大回溯,並按季約拼接前月序列。

問題: data/store/MES_accumulated_1m.pkl 只有 2026-06-07 起,且前 8 天是 U26
換月前的稀薄成交(日量 1.6k~66k vs 換月後 ~945k),這段會污染回測 fill 假設。

做法:
  1. 對每個季約(H26/M26/U26...)分別抓 1m,回報實際拿到的區間與 bar 數
     → 這就是「券商能存到多久」的答案。
  2. 只保留各約「前月期間」的 bars(換月 = 季月第 3 個週五前 8 天,對齊
     backend.db.models.current_quarterly_contract_id 的慣例),拼成連續前月序列。
  3. --write 才寫入 candle_store,預設只探測不落盤。

用法:
  python scripts/mes_deep_fetch.py                 # 只探測
  python scripts/mes_deep_fetch.py --write         # 探測 + 重建 store
  python scripts/mes_deep_fetch.py --symbol MNQ --write
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.data import candle_store  # noqa: E402
from backend.db.models import BarUnit  # noqa: E402

MONTH_CODE = {3: "H", 6: "M", 9: "U", 12: "Z"}
ROLL_LEAD_DAYS = 8  # 與 current_quarterly_contract_id 一致


def LOG(msg: str) -> None:
    print(msg, flush=True)


def third_friday(year: int, month: int) -> datetime:
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    fridays = 0
    while True:
        if d.weekday() == 4:
            fridays += 1
            if fridays == 3:
                return d
        d += timedelta(days=1)


def quarterly_windows(back_quarters: int = 6):
    """回傳 [(contract_suffix, front_start_utc, front_end_utc)],新→舊。

    某季約的「前月期間」= 前一季約 roll 日 → 自己的 roll 日。
    roll 日 = 季月第 3 個週五 − 8 天。
    """
    now = datetime.now(timezone.utc)
    quarters = []
    y, m = now.year, ((now.month - 1) // 3 + 1) * 3
    for _ in range(back_quarters + 1):
        quarters.append((y, m))
        m -= 3
        if m <= 0:
            m += 12
            y -= 1
    rolls = {(y, m): third_friday(y, m) - timedelta(days=ROLL_LEAD_DAYS)
             for (y, m) in quarters}

    out = []
    for i, (y, m) in enumerate(quarters):
        roll = rolls[(y, m)]
        if roll > now:
            # 尚未成為前月(例如當季已 roll 到下一季),仍可能是當前前月
            pass
        prev_y, prev_m = (y, m - 3) if m > 3 else (y - 1, 12)
        prev_roll = rolls.get((prev_y, prev_m))
        if prev_roll is None:
            continue
        front_start = prev_roll
        front_end = min(roll, now)
        if front_end <= front_start:
            continue
        out.append((f"{MONTH_CODE[m]}{str(y)[-2:]}", front_start, front_end))
    return out


async def probe(symbol: str, back_quarters: int, write: bool) -> None:
    from backend.broker.topstepx import TopstepXClient
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    username = os.getenv("TOPSTEPX_USERNAME")
    api_key = os.getenv("TOPSTEPX_API_KEY")
    if not username or not api_key:
        raise SystemExit("no TOPSTEPX credentials in .env")

    client = TopstepXClient(
        username=username, api_key=api_key,
        use_demo=os.getenv("TOPSTEPX_USE_DEMO", "false").lower() == "true")
    await client.authenticate()

    windows = quarterly_windows(back_quarters)
    LOG(f"[{symbol}] probing {len(windows)} quarterly contracts (newest first)")

    fetched: list[tuple[str, list]] = []  # (suffix, bars) 新→舊
    for suffix, start, end in windows:
        cid = f"CON.F.US.{symbol}.{suffix}"
        # 日曆 roll 只是抓取範圍的粗略指引;實際切點稍後用成交量交叉決定,
        # 所以兩端各多抓 3 週緩衝以確保重疊期完整。
        q_start = (start - timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q_end = (end + timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            bars = await client.get_historical_bars_paginated(
                cid, BarUnit.MINUTE, 1, start_time=q_start, end_time=q_end)
        except Exception as exc:
            LOG(f"  {cid:24s} FETCH FAILED: {type(exc).__name__}: {exc}")
            continue
        if not bars:
            LOG(f"  {cid:24s} empty — broker retention likely ends here")
            continue
        bars = [b for b in bars if getattr(b, "volume", 0) > 0]
        lo = min(b.timestamp for b in bars)
        hi = max(b.timestamp for b in bars)
        LOG(f"  {cid:24s} {len(bars):>7d} bars  {lo:%Y-%m-%d} -> {hi:%Y-%m-%d}")
        fetched.append((suffix, bars))

    try:
        await client.close()
    except Exception:
        pass

    stitched = _volume_roll_stitch(fetched)

    if not stitched:
        LOG("no bars stitched — nothing to write")
        return

    stitched.sort(key=lambda c: c.timestamp)
    lo = stitched[0].timestamp
    hi = stitched[-1].timestamp
    span_days = (_utc(hi) - _utc(lo)).days
    LOG(f"\n[{symbol}] STITCHED front-month series: {len(stitched)} bars "
        f"{lo:%Y-%m-%d} -> {hi:%Y-%m-%d}  ({span_days} days)")

    # 換月價差報告(拼接處會有跳空,回測需知道)
    prev_day = None
    for b in stitched:
        d = _utc(b.timestamp).date()
        if prev_day and (d - prev_day).days > 3:
            LOG(f"  gap: {prev_day} -> {d}")
        prev_day = d

    if not write:
        LOG("\n(dry run — pass --write to rebuild the store)")
        return

    path = candle_store._store_path(symbol, 1)
    if path.exists():
        backup = path.with_suffix(".pkl.bak")
        path.replace(backup)
        LOG(f"existing store moved to {backup.name}")
    candle_store.save(stitched, symbol, 1)
    candle_store.save_meta({
        "frozen_through": None,
        "segments": [],
        "total_bars": len(stitched),
        "first_ts": _utc(lo).isoformat(),
        "last_ts": _utc(hi).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "mes_deep_fetch stitched front-month",
    }, symbol, 1)
    LOG(f"store written: {path}")


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _daily_volume(bars) -> dict:
    out: dict = {}
    for b in bars:
        d = _utc(b.timestamp).date()
        out[d] = out.get(d, 0) + getattr(b, "volume", 0)
    return out


def _volume_roll_stitch(fetched):
    """成交量交叉換月:新約日量首次超過舊約的那天起改用新約。

    日曆式 roll(第 3 個週五 − 8 天)對 ES/MES 太早 —— 實測 M26→U26 真正的
    量能交叉在 6/15,用日曆切會留下 2 個只有 1/3 正常量的假日子,回測 fill
    會失真。改用兩約重疊期的日成交量比較,取第一個「新約 > 舊約」的日期。

    fetched: [(suffix, bars)] 新→舊。回傳依時間排序的拼接序列。
    """
    if not fetched:
        return []

    # cross[i] = fetched[i](較新)自哪天起接手 fetched[i+1](較舊)
    cross: list = [None] * len(fetched)
    for i in range(len(fetched) - 1):
        new_suffix, new_bars = fetched[i]
        old_suffix, old_bars = fetched[i + 1]
        nv, ov = _daily_volume(new_bars), _daily_volume(old_bars)
        overlap = sorted(set(nv) & set(ov))
        pick = None
        for d in overlap:
            # 忽略週日半場等極低量日,避免雜訊誤判
            if nv[d] + ov[d] < 100_000:
                continue
            if nv[d] > ov[d]:
                pick = d
                break
        if pick is None and overlap:
            pick = overlap[-1]
        cross[i] = pick
        LOG(f"  roll {old_suffix} -> {new_suffix}: volume crossover {pick}"
            f" (overlap {len(overlap)}d)")

    stitched: list = []
    for i, (suffix, bars) in enumerate(fetched):
        start_day = cross[i]                       # 本約接手日(最舊的約為 None)
        end_day = cross[i - 1] if i > 0 else None  # 交棒給更新約的日期
        kept = [b for b in bars
                if (start_day is None or _utc(b.timestamp).date() >= start_day)
                and (end_day is None or _utc(b.timestamp).date() < end_day)]
        LOG(f"  {suffix}: {len(kept):>7d} bars kept  "
            f"[{start_day or 'begin'} .. {end_day or 'now'})")
        stitched.extend(kept)
    return sorted(stitched, key=lambda c: c.timestamp)


async def patch_seam(symbol: str, write: bool) -> None:
    """修補「日曆換月 → 量能換月」之間的稀薄接縫,保留其餘累積歷史。

    store 是按 symbol(非 contract)以 timestamp 為鍵累積的,而每次抓取用的是
    current_quarterly_contract_id() —— 那是純日曆判定(季月第 3 個週五 − 8 天)。
    真實流動性大約晚 4 天才搬家,所以日曆 roll 之後、量能 roll 之前的那幾天,
    store 裡存的是「還沒接手的新約」的空盤報價(實測 MNQ 2026-06-11 00:00Z
    成交量從 1563 直接掉到 36)。

    這裡只重抓舊約在該窗口的 bars 蓋回去,不動其他任何一天 —— 因為 store 的
    歷史已經超出券商 60 天保留期(MNQ 有 75 天),整包重建會永久損失資料。
    """
    from backend.broker.topstepx import TopstepXClient
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    username = os.getenv("TOPSTEPX_USERNAME")
    api_key = os.getenv("TOPSTEPX_API_KEY")
    if not username or not api_key:
        raise SystemExit("no TOPSTEPX credentials in .env")

    existing = sorted(candle_store.load(symbol, 1), key=lambda c: c.timestamp)
    if not existing:
        raise SystemExit(f"{symbol} store empty")
    LOG(f"[{symbol}] store has {len(existing)} bars "
        f"{existing[0].timestamp:%Y-%m-%d} → {existing[-1].timestamp:%Y-%m-%d}")

    daily = _daily_volume(existing)
    weekdays = {d: v for d, v in daily.items() if d.weekday() < 5}
    if not weekdays:
        raise SystemExit("no weekday bars")
    median = sorted(weekdays.values())[len(weekdays) // 2]
    thin = sorted(d for d, v in weekdays.items() if v < median * 0.35)
    if not thin:
        LOG("no thin weekdays found — nothing to patch")
        return
    LOG(f"  median weekday volume {median:,}; thin days (<35%): "
        + ", ".join(f"{d}({v:,})" for d, v in
                    ((d, weekdays[d]) for d in thin)))

    client = TopstepXClient(
        username=username, api_key=api_key,
        use_demo=os.getenv("TOPSTEPX_USE_DEMO", "false").lower() == "true")
    await client.authenticate()

    patched = {_utc(c.timestamp): c for c in existing}
    total_replaced = 0
    for suffix, front_start, front_end in quarterly_windows(4):
        # 舊約 = 在這些 thin 日仍是主力的那一張
        # front_end = 這張舊約的日曆換月日;稀薄接縫就在它之後的幾天內
        targets = [d for d in thin
                   if front_end.date() <= d <= front_end.date() + timedelta(days=6)]
        if not targets:
            continue
        cid = f"CON.F.US.{symbol}.{suffix}"
        lo = min(targets) - timedelta(days=1)
        hi = max(targets) + timedelta(days=2)
        LOG(f"  refetching {cid} for {lo} .. {hi}")
        try:
            bars = await client.get_historical_bars_paginated(
                cid, BarUnit.MINUTE, 1,
                start_time=lo.strftime("%Y-%m-%dT00:00:00Z"),
                end_time=hi.strftime("%Y-%m-%dT00:00:00Z"))
        except Exception as exc:
            LOG(f"    FAILED: {type(exc).__name__}: {exc}")
            continue
        repl = [b for b in bars
                if _utc(b.timestamp).date() in set(targets)
                and getattr(b, "volume", 0) > 0]
        if not repl:
            LOG("    no replacement bars returned")
            continue
        newvol = {}
        for b in repl:
            newvol[_utc(b.timestamp).date()] = newvol.get(_utc(b.timestamp).date(), 0) + b.volume
        for d in sorted(newvol):
            LOG(f"    {d}: {weekdays.get(d, 0):>9,} → {newvol[d]:>9,}")
        # 先清掉該日所有舊 bars,再放入新約的(避免殘留空盤分鐘)
        for ts in [t for t in patched if t.date() in set(targets)]:
            del patched[ts]
        for b in repl:
            patched[_utc(b.timestamp)] = b
        total_replaced += len(repl)

    try:
        await client.close()
    except Exception:
        pass

    if not total_replaced:
        LOG("nothing replaced")
        return
    merged = sorted(patched.values(), key=lambda c: c.timestamp)
    LOG(f"\n[{symbol}] {len(existing)} → {len(merged)} bars "
        f"({total_replaced} replaced from the previous contract)")
    if not write:
        LOG("(dry run — pass --write to save)")
        return
    path = candle_store._store_path(symbol, 1)
    path.replace(path.with_suffix(".pkl.bak"))
    candle_store.save(merged, symbol, 1)
    LOG(f"store written: {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MES")
    ap.add_argument("--quarters", type=int, default=6)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--patch-seam", action="store_true",
                    help="只修補換月接縫的稀薄日,保留其餘累積歷史(MNQ 用這個)")
    args = ap.parse_args()
    if args.patch_seam:
        asyncio.run(patch_seam(args.symbol, args.write))
    else:
        asyncio.run(probe(args.symbol, args.quarters, args.write))


if __name__ == "__main__":
    main()
