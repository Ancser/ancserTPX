"""1.0.10: 收集 π 訊號機器人的完整歷史,解析成結構化訊號。

來源:Discord 頻道 1547062725060993066。頻道內的歷史回補由
hoppouseiki(470850237532209153) 發布,目前 Live 由 ancserPiAlert
(1514456965622005870) 發布;兩者都使用相同的 NY 事件時間格式。
機器人已經把圖表整理成文字,新格式:

    @everyone QQQ π信号出现
    NY 09/09/2026 11:27

    • Level 3 青π ×1

舊格式 `π信号出现（SPY）` 與括號尺寸也會相容解析:

    @everyone 🚨 π信号出现（SPY）

    • 紫圈 ×1（大 · 上部）

    图表见上方 ↑ 打开确认。

⚠️ 認證方式:`.env` 的 DISCORD_TOKEN 實測是**使用者 token**(用 `Bot {token}`
會回 401,裸 token 才 200)。以個人 token 讀取屬於 self-botting,違反 Discord ToS,
帳號有被停權風險。這是使用者已知情的既有設定,本腳本沿用,但只做**讀取**。

輸出 ancserMarketData/source/discord/pi/pi_signals.json。每次完整回補會以
頻道內容的 NY 事件時間重建檔案,保留 sender / sender_id、Discord 發布時間
與原始 content,並把舊檔先備份到同目錄的 archive;摘要寫到
derived/research/pi_channel_backfill_latest.json。

用法:
    python scripts/pi_collect_history.py                # 整個目前頻道
    python scripts/pi_collect_history.py --max-pages 20 # 只抓最近
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv  # noqa: E402

from backend.live.pi_listener import (  # noqa: E402
    CHANNEL_ID,
    DIRECTION,
    LIVE_PI_SENDER,
    _NY_EVENT_TS,
    _MARK,
    _SYM,
    is_pre_session,
    message_source_timestamp,
    pi_sender_role,
)
from backend.data import market_data  # noqa: E402
load_dotenv(ROOT / ".env")
import httpx  # noqa: E402

OUT = market_data.pi_source_root() / "pi_signals.json"
REPORT = market_data.derived_path("research", "pi_channel_backfill_latest.json")

def parse(content: str) -> dict | None:
    m = _SYM.search(content)
    if not m:
        return None
    marks = []
    for mk in _MARK.finditer(content):
        level = mk.group("level")
        size = f"Level {level}" if level else (mk.group("size") or "").strip()
        marks.append({
            "kind": mk.group("kind"),
            "count": int(mk.group("count")),
            "size": size,
            "level": int(level) if level else None,
            "pos": (mk.group("pos") or "").strip() or None,
        })
    if not marks:
        return None
    return {"symbol": m.group(1).upper(), "marks": marks}


def build_history_row(message: dict) -> dict | None:
    """Convert one active-channel Discord message to canonical history shape.

    The embedded ``NY`` timestamp is authoritative for ``ts``.  The Discord
    delivery timestamp remains in ``discord_timestamp`` so bulk reposts can
    be audited without changing the event time used by backtest/replay.
    """
    sender = pi_sender_role(message)
    if sender is None:
        return None
    source_ts = message_source_timestamp(message)
    if source_ts is None:
        return None
    content = str(message.get("content") or "")
    parsed = parse(content)
    author = message.get("author") or {}
    return {
        "id": str(message.get("id") or ""),
        "channel_id": CHANNEL_ID,
        "sender": sender,
        "sender_id": str(author.get("id") or ""),
        "sender_name": str(
            author.get("username") or author.get("global_name") or ""
        ),
        "discord_timestamp": message.get("timestamp"),
        "timestamp_source": (
            "ny_content" if _NY_EVENT_TS.search(content) else "discord_timestamp"
        ),
        "ts": source_ts.isoformat(),
        "pre_session": is_pre_session(source_ts),
        "mention_everyone": bool(message.get("mention_everyone")),
        "symbol": parsed["symbol"] if parsed else None,
        "marks": parsed["marks"] if parsed else [],
        "content": content,
    }


def _mark_key(row: dict, mark: dict) -> tuple | None:
    """Return the PI-007 identity used to deduplicate reposts."""
    try:
        stamp = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc).replace(second=0, microsecond=0)
    symbol = str(row.get("symbol") or "").upper()
    kind = str(mark.get("kind") or "")
    if symbol not in {"QQQ", "SPY"} or kind not in DIRECTION:
        return None
    level = mark.get("level")
    source_level = str(level) if level is not None else str(mark.get("size") or "")
    return stamp.isoformat(), symbol, kind, source_level


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Deduplicate history/repost marks while retaining source provenance.

    If both senders carry the same source-minute mark, the live ``pialert``
    row wins.  A duplicate report records the discarded sender/message ID;
    strategy consumers receive one canonical mark.
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            str(row.get("ts") or ""),
            0 if row.get("sender") == LIVE_PI_SENDER else 1,
            str(row.get("id") or ""),
        ),
    )
    seen: set[tuple] = set()
    kept: list[dict] = []
    duplicates: list[dict] = []
    for row in ordered:
        marks = list(row.get("marks") or [])
        if not marks:
            kept.append(row)
            continue
        unique_marks: list[dict] = []
        for mark in marks:
            key = _mark_key(row, mark)
            if key is None or key not in seen:
                unique_marks.append(mark)
                if key is not None:
                    seen.add(key)
            else:
                duplicates.append({
                    "identity": list(key),
                    "sender": row.get("sender"),
                    "sender_id": row.get("sender_id"),
                    "message_id": row.get("id"),
                })
        if unique_marks:
            if len(unique_marks) != len(marks):
                row = dict(row)
                row["marks"] = unique_marks
            kept.append(row)
    return kept, duplicates


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


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
            row = build_history_row(m)
            if row is not None:
                rows.append(row)
        before = msgs[-1]["id"]
        pages += 1
        if pages % 10 == 0:
            print(f"  掃 {pages} 頁 / {scanned} 則,取得 {len(rows)} 個訊號", flush=True)
        if a.max_pages and pages >= a.max_pages:
            break
        time.sleep(0.3)

    if not rows:
        print(
            "\n沒有從目前 PI 頻道取得可記錄訊息；保留既有 canonical history，"
            "避免空回應覆蓋資料。",
            file=sys.stderr,
        )
        sys.exit(2)

    rows.sort(key=lambda x: (x["ts"], x.get("id", "")))
    canonical, duplicates = dedupe_rows(rows)

    backup_path = None
    if OUT.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = OUT.parent / "archive" / f"pi_signals.before_full_channel_{stamp}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(OUT, backup)
        backup_path = str(backup)

    _atomic_write_json(OUT, canonical)

    source_times = [
        r["ts"] for r in canonical
        if r.get("timestamp_source") == "ny_content" and r.get("ts")
    ]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "channel_id": CHANNEL_ID,
        "channel_name": "pi",
        "history_policy": "replace_from_full_active_channel_content_time",
        "message_delivery_range": {
            "start": min(
                (str(r.get("discord_timestamp")) for r in rows if r.get("discord_timestamp")),
                default=None,
            ),
            "end": max(
                (str(r.get("discord_timestamp")) for r in rows if r.get("discord_timestamp")),
                default=None,
            ),
        },
        "source_event_range": {
            "start": min(source_times, default=None),
            "end": max(source_times, default=None),
        },
        "discord_messages_scanned": scanned,
        "eligible_source_messages": len(rows),
        "canonical_messages": len(canonical),
        "duplicate_marks_removed": len(duplicates),
        "duplicate_marks": duplicates,
        "sender_counts": dict(Counter(r.get("sender") for r in rows)),
        "sender_ids": {
            sender: sorted({
                str(r.get("sender_id") or "")
                for r in rows if r.get("sender") == sender
            })
            for sender in sorted({r.get("sender") for r in rows})
        },
        "pre_session_messages": sum(bool(r.get("pre_session")) for r in canonical),
        "unparsed_messages": sum(not r.get("symbol") for r in canonical),
        "multi_mark_messages": sum(len(r.get("marks") or []) >= 2 for r in canonical),
        "canonical_mark_count": sum(len(r.get("marks") or []) for r in canonical),
        "mark_kinds": dict(Counter(
            mk.get("kind") for r in canonical for mk in (r.get("marks") or [])
        )),
        "previous_history_backup": backup_path,
    }
    _atomic_write_json(REPORT, report)

    print(f"\n掃描 {pages} 頁 / {scanned} 則 → 可記錄訊息 {len(rows)} 個")
    print(f"canonical history: {len(canonical)} 則; 去重標記: {len(duplicates)}")
    if source_times:
        print(f"內容事件時間範圍 {min(source_times)[:19]} → {max(source_times)[:19]}")
    print("來源:", dict(Counter(r.get("sender") for r in rows)))
    print("標記種類:", report["mark_kinds"])
    print(f"\n寫入 {OUT}")
    print(f"稽核報告 {REPORT}")


if __name__ == "__main__":
    main()
