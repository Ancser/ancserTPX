# ============================================================
# 文件: scripts/accumulate_history.py
# 狀態: v1.0.6 (explainable confluence — option C: persistent accumulator)
# 用途: 自動解析現役前月合約 → 抓「目前可得的完整歷史」→ 併入會長大的持久檔
# 關聯文件:
#   ← backend/broker/topstepx.py            (TopstepXClient, front-month resolve)
#   → scripts/confluence_common.py          (merge_into_store / load_store)
# 執行:
#   python -m scripts.accumulate_history                         # 累積 1m (標準基礎)
#   python -m scripts.accumulate_history --days 65               # 自訂窗口
#   python -m scripts.accumulate_history --base-min 5            # (舊) 也可累積 5m
# ============================================================
"""Option C — grow a local dataset past what the broker will ever serve.

TopstepX only returns the CURRENT front contract (~60 days). This script:
  1. auto-resolves the current tradable MNQ front-month contract,
  2. downloads its full available window (paginated),
  3. MERGES it (upsert by timestamp) into a persistent pickle that is never
     truncated — so bars fetched weeks ago survive even after the contract
     rolls and the API drops them.

Run it on a schedule (e.g. weekly). Each run extends the tail a little; over
months the store spans a year+ and feeds train/validate via `--use-store`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from backend.broker.topstepx import TopstepXClient
from backend.db.models import BarUnit
from scripts.confluence_common import (
    env_bool, load_store, merge_into_store, store_path,
)


async def _fetch_full(symbol: str, days: int, base: int):
    username = os.getenv("TOPSTEPX_USERNAME", "").strip()
    api_key = os.getenv("TOPSTEPX_API_KEY", "").strip()
    use_demo = env_bool("TOPSTEPX_USE_DEMO", False)
    if not username or not api_key:
        raise SystemExit("Missing TOPSTEPX_USERNAME / TOPSTEPX_API_KEY in .env")
    client = TopstepXClient(username=username, api_key=api_key, use_demo=use_demo)
    await client.authenticate()

    # auto front-month: always grab the CURRENT tradable contract
    contract_id = symbol
    try:
        resolved = await client.get_front_month_contract_id(symbol)
        if resolved:
            contract_id = resolved
    except Exception as e:
        print(f"[warn] front-month resolve failed: {e}; using '{symbol}'", flush=True)

    now = datetime.utcnow()
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[fetch] {contract_id} {base}m  {start} ~ {end}  "
          f"({'demo' if use_demo else 'prod'})", flush=True)
    bars = await client.get_historical_bars_paginated(
        contract_id=contract_id, unit=BarUnit.MINUTE, unit_number=base,
        start_time=start, end_time=end,
    )
    return contract_id, sorted(bars or [], key=lambda c: c.timestamp)


def _span(bars) -> str:
    if not bars:
        return "(empty)"
    a = bars[0].timestamp
    b = bars[-1].timestamp
    days = (b - a).days
    return f"{a.date()} .. {b.date()}  (~{days}d)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="MNQ", help="front-month root (auto-resolved)")
    ap.add_argument("--base-min", type=int, default=1, help="minutes per candle (standardized: 1)")
    ap.add_argument("--days", type=int, default=65, help="trailing window to request")
    args = ap.parse_args()

    base = max(1, args.base_min)
    before = load_store(base, args.symbol)
    print(f"[store] {store_path(base, args.symbol).name}: "
          f"{len(before)} bars  {_span(before)}", flush=True)

    contract_id, fresh = asyncio.run(_fetch_full(args.symbol, args.days, base))
    if not fresh:
        print("[warn] fetch returned 0 bars — store unchanged", flush=True)
        return
    print(f"[fetch] got {len(fresh)} bars from {contract_id}  {_span(fresh)}", flush=True)

    total, added = merge_into_store(fresh, base, args.symbol)
    after = load_store(base, args.symbol)
    print(f"[store] merged: +{added} new  ->  {total} bars  {_span(after)}", flush=True)
    if added == 0:
        print("[store] (no new timestamps — already up to date)", flush=True)


if __name__ == "__main__":
    main()
