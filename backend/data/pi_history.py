"""PI 歷史訊號的**唯一**載入點。

## 為什麼需要這支模組

先前八個研究腳本各自寫:

```python
rows = json.load(open(ROOT / "data" / "research" / "pi_signals.json", ...))
rows = [r for r in rows if not (r.get("pre_session") or is_pre_session(...))]
```

八份幾乎相同、但要各自維護的過濾邏輯。這不是風格問題 —— 它已經造成過一次
實際損害:2026-08-08 發現 bot 在美西 07:00 前重播前一日標記(佔 33% 訊息 /
49% 標記)時,回測 loader、實盤 listener、圖表 API 都改好了,**但研究腳本
繞過 loader 直接讀 json**,所以它們仍在用被污染的資料產出結論。

一個參數不該有八份真相。這裡是那一份。

## 分工

- `load_rows()`   —— 原始訊息列(dict),研究腳本用
- `load_marks()`  —— 攤平成 (ts, future, kind) 三元組,大多數研究要的形狀
- `backend.strategy.pi_signal._load_history()` 走 `load_rows()`,所以回測、
  實盤、圖表、研究看到的**是同一組訊號**

過濾規則本身住在 `backend.live.pi_listener.is_pre_session`(實盤也用同一個)。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from backend.live.pi_listener import DIRECTION, SYMBOL_MAP, is_pre_session

logger = logging.getLogger(__name__)

HIST_PATH = Path(__file__).resolve().parents[2] / "data" / "research" / "pi_signals.json"


def parse_ts(value: str) -> datetime:
    """把 json 裡的時戳轉成 UTC aware datetime。"""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def row_is_pre_session(row: dict) -> bool:
    """優先用檔案裡標好的欄位;舊檔沒有就用時間再判一次。

    兩者都留是刻意的:標記欄位讓「當初收集時的判定」可稽核,時間判定讓
    門檻改動(例如 06:35 → 07:00)不必重抓資料就能生效。
    """
    if row.get("pre_session"):
        return True
    try:
        return is_pre_session(parse_ts(row["ts"]))
    except Exception:
        return False


def load_rows(include_pre_session: bool = False, path: Path | None = None) -> list[dict]:
    """讀 pi_signals.json。預設**濾掉開盤前重播**。

    include_pre_session=True 只有在研究「重播本身」時才該用。
    """
    p = path or HIST_PATH
    try:
        rows = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("[PI] 找不到 %s —— 沒有歷史訊號可用"
                       "(先跑 scripts/pi_collect_history.py)", p.name)
        return []
    except Exception as exc:
        logger.warning("[PI] 讀取歷史訊號失敗 %s: %s", type(exc).__name__, exc)
        return []

    if include_pre_session:
        return rows
    kept = [r for r in rows if not row_is_pre_session(r)]
    dropped = len(rows) - len(kept)
    if dropped:
        logger.info("[PI] 濾除開盤前重播 %d 則 → 保留 %d 則", dropped, len(kept))
    return kept


def iter_marks(rows: list[dict]) -> Iterator[tuple[datetime, str, str, str]]:
    """攤平成 (ts, future, kind, size)。

    ⚠️ size 一併回傳只為了診斷,**不要拿它做決策** —— 圈類恆為「大」、
    π 類的大小是視覺系統的多餘分類,零資訊。強弱軸是 kind 本身。
    """
    for r in rows:
        fut = SYMBOL_MAP.get((r.get("symbol") or "").upper())
        if not fut:
            continue
        try:
            ts = parse_ts(r["ts"])
        except Exception:
            continue
        for mk in r.get("marks") or []:
            kind = mk.get("kind")
            if kind in DIRECTION:
                yield ts, fut, kind, mk.get("size") or "?"


def load_marks(kinds: tuple[str, ...] | None = None, **kw) -> list[tuple]:
    """常用捷徑:載入 + 攤平 + 依 kind 篩選 + 依時間排序。"""
    out = [m for m in iter_marks(load_rows(**kw)) if kinds is None or m[2] in kinds]
    out.sort(key=lambda x: x[0])
    return out
