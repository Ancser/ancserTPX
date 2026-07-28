"""1.0.9: 資料累積 CLI —— 核心邏輯在 backend/data/accumulator.py。

伺服器啟動時已內建每小時自動累積(backend/main.py lifespan)。這支腳本
用於:伺服器沒開時手動補、排程工作、或只想看狀態。

    python scripts/accumulate_data.py                 # 跑一次(MNQ + MES)
    python scripts/accumulate_data.py --check         # 只報告狀態
    python scripts/accumulate_data.py --symbols MNQ,MES,MGC
    python scripts/accumulate_data.py --daemon --interval 3600

Windows 排程(每小時):
    schtasks /create /tn ancserTPX-accumulate /sc hourly ^
      /tr "cmd /c cd /d F:\ancserQuant\ancserTPX && python scripts\accumulate_data.py"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backend.data.accumulator import (  # noqa: E402
    DEFAULT_SYMBOLS, RETENTION_DAYS, accumulate_once, store_status,
)


def LOG(*a) -> None:
    print(f"[{datetime.now():%H:%M:%S}]", *a, flush=True)


def report(symbols) -> None:
    now = datetime.now(timezone.utc)
    LOG(f"券商保留約 {RETENTION_DAYS} 天 → 可補回的最早日期 "
        f"{(now - timedelta(days=RETENTION_DAYS)):%Y-%m-%d}")
    LOG(f"{'商品':<6}{'根數':>10}  {'最早':<12}{'最新':<18}{'落後':>6}  狀態")
    for s in symbols:
        st = store_status(s)
        if not st["bars"]:
            LOG(f"{s:<6}{'(空)':>10}")
            continue
        flag = {"FRESH": "✅ 新鮮", "STALE": "⚠ 需補",
                "HOLE": "❌ 已產生永久空洞"}[st["state"]]
        LOG(f"{s:<6}{st['bars']:>10,}  {st['first']:%Y-%m-%d}  "
            f"{st['last']:%Y-%m-%d %H:%M}{st['age_days']:>5}天  {flag}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--check", action="store_true", help="只報告狀態,不抓取")
    ap.add_argument("--daemon", action="store_true", help="常駐循環")
    ap.add_argument("--interval", type=int, default=3600)
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    while True:
        report(symbols)
        if not args.check:
            try:
                asyncio.run(accumulate_once(symbols, log=lambda m: LOG(m)))
            except KeyboardInterrupt:
                LOG("中斷"); return
            except Exception as exc:
                LOG(f"❌ 本輪失敗: {type(exc).__name__}: {exc}")
            report(symbols)
        if not args.daemon:
            return
        LOG(f"下一輪 {args.interval}s 後\n")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            LOG("中斷"); return


if __name__ == "__main__":
    main()
