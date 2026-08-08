"""1.0.10: 收集 π 訊號機器人的完整歷史,解析成結構化訊號。

來源:Discord 頻道 1478899539845972078,機器人 ancserPiAlert(1514456965622005870)。
機器人已經把圖表整理成文字,格式:

    @everyone 🚨 π信号出现（SPY）

    • 紫圈 ×1（大 · 上部）

    图表见上方 ↑ 打开确认。

⚠️ 認證方式:`.env` 的 DISCORD_TOKEN 實測是**使用者 token**(用 `Bot {token}`
會回 401,裸 token 才 200)。以個人 token 讀取屬於 self-botting,違反 Discord ToS,
帳號有被停權風險。這是使用者已知情的既有設定,本腳本沿用,但只做**讀取**。

輸出 data/research/pi_signals.json,欄位保留原始 content,方便日後重解析。

用法:
    python scripts/pi_collect_history.py                # 全歷史
    python scripts/pi_collect_history.py --max-pages 20 # 只抓最近
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")
import httpx  # noqa: E402

CHANNEL_ID = "1478899539845972078"
BOT_ID = "1514456965622005870"
OUT = ROOT / "data" / "research" / "pi_signals.json"

# QQQ → MNQ(Nasdaq 100)、SPY → MES(S&P 500)。使用者指定的對應。
SYMBOL_MAP = {"QQQ": "MNQ", "SPY": "MES"}

_SYM = re.compile(r"[（(]\s*(QQQ|SPY)\s*[）)]")
# 「• 紫圈 ×1（大 · 上部）」或「• 淡蓝圈 ×1（大）」—— 位置欄位**可省略**
# (實測 259 則裡有 7 則沒有位置,第一版寫死要求位置導致整則解析失敗)
_MARK = re.compile(
    r"[•·・]\s*(\S+?)\s*[×x]\s*(\d+)\s*[（(]\s*([^·)）]+?)\s*(?:[·・]\s*([^)）]+?)\s*)?[）)]")


def parse(content: str) -> dict | None:
    m = _SYM.search(content)
    if not m:
        return None
    marks = []
    for mk in _MARK.finditer(content):
        kind, cnt, size, pos = mk.group(1), int(mk.group(2)), mk.group(3), mk.group(4)
        marks.append({"kind": kind, "count": cnt, "size": size.strip(),
                      "pos": (pos or "").strip() or None})
    if not marks:
        return None
    return {"symbol": m.group(1), "marks": marks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=0, help="0 = 抓到底")
    a = ap.parse_args()

    tok = os.getenv("DISCORD_TOKEN", "").strip()
    if not tok:
        print("✘ .env 沒有 DISCORD_TOKEN", file=sys.stderr)
        sys.exit(1)
    headers = {"Authorization": tok}      # 使用者 token:不加 "Bot " 前綴
    url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages"

    rows, before, pages, scanned = [], None, 0, 0
    while True:
        params = {"limit": 100}
        if before:
            params["before"] = before
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=30)
        except Exception as e:
            print(f"連線失敗 {type(e).__name__}: {e}", file=sys.stderr)
            break
        if r.status_code == 429:
            wait = float(r.json().get("retry_after", 2))
            print(f"  rate limit,等 {wait:.1f}s")
            time.sleep(wait + 0.5)
            continue
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {str(r.text)[:160]}", file=sys.stderr)
            break
        msgs = r.json()
        if not msgs:
            break
        scanned += len(msgs)
        for m in msgs:
            if m["author"]["id"] != BOT_ID:
                continue
            content = m.get("content") or ""
            p = parse(content)
            rows.append({
                "id": m["id"],
                "ts": m["timestamp"],
                "mention_everyone": bool(m.get("mention_everyone")),
                "symbol": p["symbol"] if p else None,
                "marks": p["marks"] if p else [],
                "content": content,          # 原樣保留,便於重解析
            })
        before = msgs[-1]["id"]
        pages += 1
        if pages % 10 == 0:
            print(f"  掃 {pages} 頁 / {scanned} 則,取得 {len(rows)} 個訊號", flush=True)
        if a.max_pages and pages >= a.max_pages:
            break
        time.sleep(0.3)

    rows.sort(key=lambda x: x["ts"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump(rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"\n掃描 {pages} 頁 / {scanned} 則 → bot 訊號 {len(rows)} 個")
    if rows:
        print(f"時間範圍 {rows[0]['ts'][:19]} → {rows[-1]['ts'][:19]}")
    unparsed = [r for r in rows if not r["symbol"]]
    print(f"無法解析: {len(unparsed)}")
    for r in unparsed[:3]:
        print("   ", repr(r["content"])[:150])

    print("\n標的分布:", dict(Counter(r["symbol"] for r in rows if r["symbol"])))
    kinds = Counter(mk["kind"] for r in rows for mk in r["marks"])
    print("標記種類:", dict(kinds))
    print("尺寸:", dict(Counter(mk["size"] for r in rows for mk in r["marks"])))
    print("位置:", dict(Counter(mk["pos"] for r in rows for mk in r["marks"])))
    print(f"\n寫入 {OUT}")


if __name__ == "__main__":
    main()
