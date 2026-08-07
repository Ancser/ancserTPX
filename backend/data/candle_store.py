# ============================================================
# 文件: backend/data/candle_store.py
# 狀態: v1.0.6 (unified persistent candle accumulator)
# 關聯文件:
#   ← backend/api/routes.py          (web fetch_historical uses this)
#   ← scripts/confluence_common.py   (research scripts have a parallel store)
# ============================================================
"""Persistent, append-only candle store for MNQ/NQ 1m bars.

Location: ``data/store/{symbol}_accumulated_1m.pkl``

Design principles
─────────────────
1. **Only-grows, never truncates.**  Bars fetched weeks ago survive even after
   the contract rolls and the API drops them.  This is the training-data lake.
2. **Incremental.**  ``merge()`` upserts by timestamp; existing bars are kept,
   newer fetch wins on clash (revision).  The caller need only fetch the
   *delta* since ``last_ts()`` — typically a few hundred bars.
3. **Gap-aware.**  ``detect_gaps()`` finds interior holes that are NOT expected
   exchange maintenance / weekends, so the caller can re-fetch just those
   windows to recover wifi-drop damage.
4. **Per-contract completeness.**  A sidecar ``…meta.json`` records which date
   ranges are verified gap-free.  Once a contract segment is frozen (rolled),
   it is never re-tested.
5. **Day-completeness boundary.**  ``last_complete_day_end()`` returns the
   timestamp of the last bar of the last *fully closed* trading day, so
   incremental fetch never writes a partial day into the store.  Uses the
   CME daily maintenance gap (≈22:00 UTC, 3pm PT summer) as the boundary.
"""

from __future__ import annotations

import json
import logging
import pickle
import time as _time
import calendar
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from backend.db.models import Candle

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]           # project root
STORE_DIR = ROOT / "data" / "store"

# ── Expected gap patterns (NOT missing data) ──────────────────────────────
# CME Equity Index futures: daily maintenance 16:00–17:00 America/Chicago.
# That is 21:00–22:00 UTC during US daylight time and 22:00–23:00 UTC during
# standard time. Stored 1m bars therefore commonly jump 20:59→22:00 UTC in
# summer or 21:59→23:00 UTC in winter.
#
# We use GENEROUS windows so DST shifts / early-close days / holidays don't
# trigger false gap alerts.  Any interior gap that does NOT match these
# patterns is flagged as potential wifi-drop damage.

_MAX_DAILY_GAP_MIN = 100            # anything ≤100 min within the window → daily
_MIN_WEEKEND_GAP_HOURS = 36         # anything ≥36h spanning a Sat → weekend/holiday
_CT = ZoneInfo("America/Chicago")

# CME session boundary: bars stop appearing around 22:00 UTC and resume ~23:00
# UTC.  A "complete trading day" ends just before the maintenance gap.
_SESSION_CLOSE_UTC_HOUR = 22        # 22:00 UTC ≈ 3pm PT (summer)


# ── Helpers ───────────────────────────────────────────────────────────────

def _as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    weeks = calendar.monthcalendar(year, month)
    days = [week[weekday] for week in weeks if week[weekday]]
    return date(year, month, days[n - 1])


def _last_weekday(year: int, month: int, weekday: int) -> date:
    weeks = calendar.monthcalendar(year, month)
    days = [week[weekday] for week in weeks if week[weekday]]
    return date(year, month, days[-1])


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    actual = date(year, month, day)
    if actual.weekday() == 5:  # Saturday -> Friday
        return actual - timedelta(days=1)
    if actual.weekday() == 6:  # Sunday -> Monday
        return actual + timedelta(days=1)
    return actual


def _is_us_futures_holiday(day: date) -> bool:
    """Common CME equity-index holiday/early-close dates.

    This is intentionally narrow: it prevents known holiday closes from being
    treated as wifi loss without suppressing an arbitrary multi-hour weekday
    hole that should still be recovered.
    """
    y = day.year
    holidays = {
        _observed_fixed_holiday(y, 1, 1),       # New Year
        _nth_weekday(y, 1, calendar.MONDAY, 3), # MLK Day
        _nth_weekday(y, 2, calendar.MONDAY, 3), # Presidents Day
        _last_weekday(y, 5, calendar.MONDAY),   # Memorial Day
        _observed_fixed_holiday(y, 6, 19),      # Juneteenth
        _observed_fixed_holiday(y, 7, 4),       # Independence Day
        _nth_weekday(y, 9, calendar.MONDAY, 1), # Labor Day
        _nth_weekday(y, 11, calendar.THURSDAY, 4), # Thanksgiving
        _observed_fixed_holiday(y, 12, 25),     # Christmas
        _observed_fixed_holiday(y + 1, 1, 1),   # next New Year observed on Dec 31
    }
    return day in holidays


def _store_path(symbol: str = "MNQ", base: int = 1) -> Path:
    return STORE_DIR / f"{symbol}_accumulated_{base}m.pkl"


def _meta_path(symbol: str = "MNQ", base: int = 1) -> Path:
    return STORE_DIR / f"{symbol}_accumulated_{base}m.meta.json"


# ── Core: load / save / merge ─────────────────────────────────────────────

def load(symbol: str = "MNQ", base: int = 1) -> List[Candle]:
    """Load the persistent store (sorted), or [] if none yet."""
    p = _store_path(symbol, base)
    if not p.exists():
        return []
    try:
        bars = pickle.load(open(p, "rb"))
        return sorted(bars, key=lambda c: c.timestamp)
    except Exception as e:
        logger.warning(f"[CandleStore] failed to load {p}: {e}")
        return []


def save(bars: List[Candle], symbol: str = "MNQ", base: int = 1) -> None:
    """Write the full bar list to disk (atomic via tmp+rename)."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    p = _store_path(symbol, base)
    tmp = p.with_suffix(".pkl.tmp")
    with tmp.open("wb") as fh:
        pickle.dump(sorted(bars, key=lambda c: c.timestamp), fh)
    tmp.replace(p)
    logger.info(f"[CandleStore] saved {len(bars)} bars → {p.name}")


# ── Re-anchor detection (1.0.10) ──────────────────────────────────────────
# 換月時 routes._build_continuous_candles 會用「當前前月」當價格錨點回溯調整
# 舊合約。但那個調整值是**每次 fetch 重算**的,而本 store 是只增不改的 ——
# 增量抓取只涵蓋當前合約時 prev_bars 為空,調整值退回 0,那批 bar 就以未調整
# 狀態寫進來,與先前已調整的舊 bar 並存。
#
# 結果就是 2026-06-11T00:00Z 那個 268.50 點的假跳空:接縫前套了 +269.25,
# 接縫後沒套。原始 M26 在該點是連續的,跳空純粹是拼接產物。
#
# 修法:**store 自己就是錨點。** 併入前先用重疊區量測 incoming 相對 store
# 的偏移,若偏移大且高度一致(= 換了錨,不是正常的 bar 修訂),就把 incoming
# 平移回 store 的錨位再寫入。已寫入的歷史一律不動。
_REANCHOR_MIN_POINTS = 5.0      # 小於此視為正常 bar 修訂,不處理
_REANCHOR_MIN_AGREE = 0.90      # 重疊 bar 至少九成同意才算換錨
_REANCHOR_MIN_SAMPLES = 20      # 重疊太少不足以判斷,寧可不動


def detect_reanchor(new_bars: List[Candle],
                    existing_by_ts: Dict[datetime, Candle]) -> Optional[float]:
    """量測 incoming 相對 store 的固定價格偏移。

    回傳 incoming − store 的偏移量;判定不是換錨則回傳 None。
    正常的 bar 修訂差異小且雜亂,換錨則是大而高度一致的常數。
    """
    diffs = []
    for b in new_bars:
        old = existing_by_ts.get(_as_utc(b.timestamp))
        if old is not None:
            diffs.append(round(b.close - old.close, 4))
    if len(diffs) < _REANCHOR_MIN_SAMPLES:
        return None
    counts: Dict[float, int] = {}
    for d in diffs:
        counts[d] = counts.get(d, 0) + 1
    offset, hits = max(counts.items(), key=lambda kv: kv[1])
    if abs(offset) < _REANCHOR_MIN_POINTS:
        return None
    if hits / len(diffs) < _REANCHOR_MIN_AGREE:
        return None
    return offset


def _shift(c: Candle, delta: float) -> Candle:
    return replace(c, open=c.open - delta, high=c.high - delta,
                   low=c.low - delta, close=c.close - delta)


def merge(new_bars: List[Candle], symbol: str = "MNQ",
          base: int = 1) -> Tuple[int, int]:
    """Upsert new_bars into the persistent store.  Returns (total, added).
    Newer fetch wins on timestamp clash (bar revision).

    1.0.10: 併入前先做換錨偵測。store 的價格錨點是權威,incoming 若換了錨
    就先平移回來 —— 否則換月時會在 store 內部長出假跳空。
    """
    existing = load(symbol, base)
    by_ts: Dict[datetime, Candle] = {
        _as_utc(b.timestamp): b for b in existing
    }
    before = len(by_ts)

    offset = detect_reanchor(new_bars, by_ts) if by_ts else None
    if offset is not None:
        logger.warning(
            "[CandleStore] %s re-anchor detected: incoming is %+.2f vs store. "
            "Shifting incoming back to the store anchor so no synthetic gap is "
            "created. (This is the 2026-06-11 bug class.)", symbol, offset)
        new_bars = [_shift(b, offset) for b in new_bars]
        _record_seam(symbol, base, {
            "at": datetime.now(timezone.utc).isoformat(),
            "kind": "reanchor_corrected",
            "offset": offset,
            "bars": len(new_bars),
            "note": "incoming shifted to store anchor; stored history untouched",
        })

    for b in new_bars:
        by_ts[_as_utc(b.timestamp)] = b
    merged = sorted(by_ts.values(), key=lambda c: c.timestamp)
    save(merged, symbol, base)
    added = len(merged) - before
    return len(merged), added


# ── Gap detection ─────────────────────────────────────────────────────────

def is_expected_gap(gap_start: datetime, gap_end: datetime) -> bool:
    """Return True if the gap matches a known maintenance / weekend pattern."""
    gs = _as_utc(gap_start)
    ge = _as_utc(gap_end)
    dur_min = (ge - gs).total_seconds() / 60

    # Weekend / holiday: ≥36h and spans a Saturday
    if dur_min >= _MIN_WEEKEND_GAP_HOURS * 60:
        # Check if any Saturday falls within the gap
        day = gs.date()
        end_day = ge.date()
        while day <= end_day:
            if day.weekday() == 5:  # Saturday
                return True
            day += timedelta(days=1)
        # Long gap but no Saturday → likely a holiday; still expected
        return True

    gs_ct = gs.astimezone(_CT)
    ge_ct = ge.astimezone(_CT)

    # Holiday early close: equity-index futures commonly stop around noon CT
    # and reopen for the next trade date at 17:00 CT. Only accept this pattern
    # on a calculated US holiday so normal weekday data loss remains visible.
    if (
        dur_min <= 8 * 60
        and ge_ct.hour == 17
        and gs_ct.date() == ge_ct.date()
        and _is_us_futures_holiday(gs_ct.date())
    ):
        return True

    # Daily maintenance. Gap endpoints are timestamps of the last bar before
    # the halt and first bar after it, so test the first missing minute rather
    # than ``gs.hour`` (20:59 UTC is a valid summer endpoint).
    if dur_min <= _MAX_DAILY_GAP_MIN:
        missing_start_ct = (gs + timedelta(minutes=1)).astimezone(_CT)
        if missing_start_ct.hour == 16 and ge_ct.hour == 17:
            return True

    return False


def detect_gaps(candles: List[Candle], tolerance_min: int = 3,
                after_ts: Optional[datetime] = None
                ) -> List[Tuple[datetime, datetime, float]]:
    """Find interior gaps > tolerance_min that are NOT expected maintenance.

    Returns list of (gap_start, gap_end, duration_minutes).
    If ``after_ts`` is given, only scan bars after that timestamp (skip the
    frozen/verified prefix).
    """
    if len(candles) < 2:
        return []
    sorted_c = sorted(candles, key=lambda c: c.timestamp)
    gaps = []
    for i in range(1, len(sorted_c)):
        prev_ts = _as_utc(sorted_c[i - 1].timestamp)
        curr_ts = _as_utc(sorted_c[i].timestamp)
        if after_ts and prev_ts < _as_utc(after_ts):
            continue
        dur = (curr_ts - prev_ts).total_seconds() / 60.0
        if dur > tolerance_min and not is_expected_gap(prev_ts, curr_ts):
            gaps.append((prev_ts, curr_ts, dur))
    return gaps


# ── Day-completeness boundary ─────────────────────────────────────────────

def last_complete_day_end(candles: List[Candle]) -> Optional[datetime]:
    """Timestamp of the last bar before the most recent daily maintenance gap.

    This is the boundary between "verified complete" data and the potentially
    still-forming current session.  Incremental fetch should start HERE (not
    from the very last bar, which might be mid-session and incomplete).
    """
    if not candles:
        return None
    sorted_c = sorted(candles, key=lambda c: c.timestamp)
    # Walk backward from the end to find the latest daily-maintenance gap
    for i in range(len(sorted_c) - 1, 0, -1):
        prev_ts = _as_utc(sorted_c[i - 1].timestamp)
        curr_ts = _as_utc(sorted_c[i].timestamp)
        dur = (curr_ts - prev_ts).total_seconds() / 60.0
        if dur > 30:  # any significant gap
            if is_expected_gap(prev_ts, curr_ts):
                # prev_ts is the last bar of a complete session
                return prev_ts
    # No maintenance gap found (data is all within one session)
    return None


# ── Completeness metadata ─────────────────────────────────────────────────

def load_meta(symbol: str = "MNQ", base: int = 1) -> dict:
    p = _meta_path(symbol, base)
    if not p.exists():
        return {"frozen_through": None, "segments": [], "total_bars": 0}
    try:
        return json.load(open(p, "r", encoding="utf-8"))
    except Exception:
        return {"frozen_through": None, "segments": [], "total_bars": 0}


def save_meta(meta: dict, symbol: str = "MNQ", base: int = 1) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    p = _meta_path(symbol, base)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)


# ── Known-bad seam registry (1.0.10) ──────────────────────────────────────
# 已知的資料瑕疵記在 meta 裡,而不是去改資料。回測照常使用全部 bar
# (使用者的要求:預設一起回測),但任何回看窗口跨過這些點的結果都該存疑。

def _record_seam(symbol: str, base: int, entry: dict) -> None:
    meta = load_meta(symbol, base)
    seams = meta.setdefault("known_seams", [])
    seams.append(entry)
    save_meta(meta, symbol, base)


def known_seams(symbol: str = "MNQ", base: int = 1) -> List[dict]:
    """已標記的資料接縫。回測/研究可據此判斷結果是否落在污染窗內。"""
    return load_meta(symbol, base).get("known_seams", [])


def seams_within(start: datetime, end: datetime, symbol: str = "MNQ",
                 base: int = 1) -> List[dict]:
    """回傳落在 [start, end] 區間內、且會影響價格連續性的接縫。"""
    hits = []
    for s in known_seams(symbol, base):
        ts = s.get("timestamp")
        if not ts:
            continue
        try:
            t = _as_utc(datetime.fromisoformat(str(ts)))
        except Exception:
            continue
        if _as_utc(start) <= t <= _as_utc(end):
            hits.append(s)
    return hits


def advance_frozen(candles: List[Candle], symbol: str = "MNQ",
                   base: int = 1) -> None:
    """Run gap detection on the unfrozen tail; if clean, advance frozen_through
    to the last complete day boundary.  This means the next run only gap-checks
    the new tail — "只跑一次完整度測試"."""
    meta = load_meta(symbol, base)
    frozen_ts = None
    if meta.get("frozen_through"):
        try:
            frozen_ts = datetime.fromisoformat(str(meta["frozen_through"]))
        except Exception:
            pass

    gaps = detect_gaps(candles, after_ts=frozen_ts)
    if gaps:
        logger.warning(
            f"[CandleStore] {len(gaps)} unexpected gaps in unfrozen tail "
            f"(first: {gaps[0][0]} → {gaps[0][1]}, {gaps[0][2]:.0f}min)")
        return  # don't advance until gaps are filled

    boundary = last_complete_day_end(candles)
    if boundary and (frozen_ts is None or boundary > frozen_ts):
        meta["frozen_through"] = boundary.isoformat()
        meta["total_bars"] = len(candles)
        if candles:
            meta["first_ts"] = _as_utc(candles[0].timestamp).isoformat()
            meta["last_ts"] = _as_utc(candles[-1].timestamp).isoformat()
        meta["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_meta(meta, symbol, base)
        logger.info(f"[CandleStore] frozen_through advanced to {boundary.isoformat()}")
