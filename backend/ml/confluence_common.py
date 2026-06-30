# ============================================================
# 文件: scripts/confluence_common.py
# 狀態: v1.0.6 (explainable confluence — shared script helpers)
# 用途: 資料抓取/快取/時間框選擇 的共用邏輯 (run / optimize / train / validate)
# ============================================================
"""Shared helpers so every confluence script handles base-candle resolution,
caching and timeframe selection identically.

base_minutes:
  - 1  -> fetch 1m bars; timeframes 5m..4h (full precision, short history)
  - 5  -> fetch 5m bars; DROP the degenerate 5m TF (1 candle/bucket), keep
          10m..4h; the same candle count then spans 5x more calendar time.
"""

from __future__ import annotations

import asyncio
import os
import pickle
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]   # backend/ml/ -> project root
load_dotenv(ROOT / ".env")

from backend.broker.topstepx import TopstepXClient
from backend.db.models import BarUnit
# timeframes_for_base lives in backend (single source of truth); re-exported here
# so existing `from scripts.confluence_common import timeframes_for_base` keeps working.
from backend.strategy.consolidation import AREA_TIMEFRAME_MINUTES, timeframes_for_base  # noqa: F401

CONTRACT_ID = "CON.F.US.MNQ.M26"
DATA_DIR = ROOT / "data" / "historical"
MODEL_DIR = ROOT / "data" / "models"
OUT_DIR = ROOT / "data" / "machinelearning"
STORE_DIR = ROOT / "data" / "store"          # persistent accumulating bars (option C)


def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    return v in ("1", "true", "yes", "on") if v else default


# ============================================================
# Persistent accumulating store (option C):
# TopstepX only serves the live front contract (~60d). To ever reach >1yr we
# keep a GROWING pickle keyed by timestamp: each download is merged in, and
# bars previously saved are retained even after the contract rolls and the API
# stops serving them. Run periodically (manual or scheduled) and the dataset
# extends a little each time, eventually spanning a year+.
# ============================================================

def store_path(base: int, symbol: str = "MNQ") -> Path:
    return STORE_DIR / f"{symbol}_accumulated_{base}m.pkl"


def load_store(base: int, symbol: str = "MNQ"):
    """Load the persistent accumulated bars (sorted), or [] if none yet."""
    p = store_path(base, symbol)
    if not p.exists():
        return []
    bars = pickle.load(open(p, "rb"))
    return sorted(bars, key=lambda c: c.timestamp)


def merge_into_store(new_bars: list, base: int, symbol: str = "MNQ"):
    """Upsert new_bars (by timestamp) into the persistent store and save.
    Returns (total_count, added_count). Newer fetch wins on a timestamp clash
    (live revision); old bars the API no longer serves are kept."""
    p = store_path(base, symbol)
    existing = load_store(base, symbol)
    by_ts = {_as_naive(b.timestamp): b for b in existing}
    before = len(by_ts)
    for b in new_bars:
        by_ts[_as_naive(b.timestamp)] = b
    merged = sorted(by_ts.values(), key=lambda c: c.timestamp)
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as fh:
        pickle.dump(merged, fh)
    return len(merged), len(merged) - before


def _cache_path(contract_id: str, days: int, base: int) -> Path:
    safe = contract_id.replace(".", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return DATA_DIR / f"{safe}_{base}m_{days}d_{stamp}.pkl"


async def _fetch_bars(contract_id: str, days: int, base: int):
    username = os.getenv("TOPSTEPX_USERNAME", "").strip()
    api_key = os.getenv("TOPSTEPX_API_KEY", "").strip()
    use_demo = env_bool("TOPSTEPX_USE_DEMO", False)
    if not username or not api_key:
        raise SystemExit("Missing TOPSTEPX_USERNAME / TOPSTEPX_API_KEY in .env")
    client = TopstepXClient(username=username, api_key=api_key, use_demo=use_demo)
    await client.authenticate()
    now = datetime.utcnow()
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[fetch] {contract_id} {base}m  {start} ~ {end}  "
          f"({'demo' if use_demo else 'prod'})", flush=True)
    candles = await client.get_historical_bars_paginated(
        contract_id=contract_id, unit=BarUnit.MINUTE, unit_number=base,
        start_time=start, end_time=end,
    )
    return sorted(candles or [], key=lambda c: c.timestamp)


async def _fetch_bars_window(contract_id: str, start: datetime, end: datetime, base: int):
    """Fetch an EXPLICIT [start, end] window (UTC) — used for old expired
    contracts whose liquid life is outside a trailing-`days` window."""
    username = os.getenv("TOPSTEPX_USERNAME", "").strip()
    api_key = os.getenv("TOPSTEPX_API_KEY", "").strip()
    use_demo = env_bool("TOPSTEPX_USE_DEMO", False)
    if not username or not api_key:
        raise SystemExit("Missing TOPSTEPX_USERNAME / TOPSTEPX_API_KEY in .env")
    client = TopstepXClient(username=username, api_key=api_key, use_demo=use_demo)
    await client.authenticate()
    s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    e = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[fetch] {contract_id} {base}m  {s} ~ {e}  "
          f"({'demo' if use_demo else 'prod'})", flush=True)
    candles = await client.get_historical_bars_paginated(
        contract_id=contract_id, unit=BarUnit.MINUTE, unit_number=base,
        start_time=s, end_time=e,
    )
    return sorted(candles or [], key=lambda c: c.timestamp)


def load_or_fetch_window(contract_id: str, start: datetime, end: datetime,
                         base: int, allow_fetch: bool = True):
    """Cached bars for an explicit UTC window. Cache key = contract+base+range
    (date-stable, so re-runs are offline). Returns [] (not fatal) if empty."""
    safe = contract_id.replace(".", "_")
    name = f"{safe}_{base}m_{start:%Y%m%d}_{end:%Y%m%d}.pkl"
    path = DATA_DIR / name
    if path.exists():
        candles = pickle.load(open(path, "rb"))
        print(f"[cache] {len(candles)} bars from {path.name}", flush=True)
        return sorted(candles, key=lambda c: c.timestamp)
    if not allow_fetch:
        print(f"[warn] no cache for {name} and fetch disabled — skipping", flush=True)
        return []
    candles = asyncio.run(_fetch_bars_window(contract_id, start, end, base))
    if not candles:
        print(f"[warn] {contract_id} returned 0 bars for window — skipping", flush=True)
        return []
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        pickle.dump(candles, fh)
    print(f"[cache] saved {len(candles)} bars -> {path.name}", flush=True)
    return candles


def load_or_fetch(contract_id: str, days: int, base: int, allow_fetch: bool = True):
    """Return cached bars for (contract, days, base) or fetch+cache them."""
    safe = contract_id.replace(".", "_")
    today = _cache_path(contract_id, days, base)
    if today.exists():
        candles = pickle.load(open(today, "rb"))
        print(f"[cache] {len(candles)} bars from {today.name}", flush=True)
        return sorted(candles, key=lambda c: c.timestamp)
    prior = sorted(DATA_DIR.glob(f"{safe}_{base}m_{days}d_*.pkl"))
    if prior:
        candles = pickle.load(open(prior[-1], "rb"))
        print(f"[cache] {len(candles)} bars from {prior[-1].name}", flush=True)
        return sorted(candles, key=lambda c: c.timestamp)
    if not allow_fetch:
        raise SystemExit(f"No cache for {contract_id} {days}d {base}m and fetch disabled")
    candles = asyncio.run(_fetch_bars(contract_id, days, base))
    if not candles:
        raise SystemExit("Fetched 0 bars — check contract / credentials / range")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with today.open("wb") as fh:
        pickle.dump(candles, fh)
    print(f"[cache] saved {len(candles)} bars -> {today.name}", flush=True)
    return candles


# ============================================================
# Multi-contract stitching (to obtain >1 year despite single quarterly
# contracts only living ~3 months).  Each quarterly contract OWNS a
# non-overlapping calendar window — the period during which it is the liquid
# front month — so no two contracts contribute a candle for the same
# timestamp.  This avoids the "overlap-date double counting / parameter
# mixing" the user warned about: every bar in the stitched series comes from
# exactly ONE contract.
# ============================================================

# CME quarterly cycle: H=Mar, M=Jun, U=Sep, Z=Dec
_QTR_ORDER = ("H", "M", "U", "Z")
_QTR_MONTH = {"H": 3, "M": 6, "U": 9, "Z": 12}


def _parse_contract(contract_id: str):
    """'CON.F.US.MNQ.M26' -> (prefix='CON.F.US.MNQ', code='M', year=2026)."""
    head, tail = contract_id.rsplit(".", 1)
    code, yy = tail[0], tail[1:]
    return head, code, 2000 + int(yy)


def _format_contract(prefix: str, code: str, year: int) -> str:
    return f"{prefix}.{code}{year % 100:02d}"


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    first_fri = 1 + (4 - d.weekday()) % 7      # weekday(): Mon=0..Fri=4
    return date(year, month, first_fri + 14)   # +2 weeks -> 3rd Friday


def _roll_dt(contract_id: str) -> datetime:
    """When liquidity leaves this contract for the next one — ~8 calendar days
    before expiry (CME equity-index roll). Bars at/after this belong to the
    NEXT contract. Returned as a naive UTC datetime for slicing."""
    _, code, year = _parse_contract(contract_id)
    exp = _third_friday(year, _QTR_MONTH[code])
    roll = exp - timedelta(days=8)
    return datetime(roll.year, roll.month, roll.day)


def _prev_contract(contract_id: str) -> str:
    prefix, code, year = _parse_contract(contract_id)
    i = _QTR_ORDER.index(code)
    if i == 0:                       # H (Mar) -> previous is Z (Dec) of prior year
        return _format_contract(prefix, "Z", year - 1)
    return _format_contract(prefix, _QTR_ORDER[i - 1], year)


def mnq_ladder(front_contract: str, n: int) -> list:
    """The n quarterly contracts ending at (and including) `front_contract`,
    oldest first. e.g. (M26, 4) -> [U25, Z25, H26, M26]."""
    out = [front_contract]
    for _ in range(max(0, n - 1)):
        out.append(_prev_contract(out[-1]))
    return list(reversed(out))


def _as_naive(ts: datetime) -> datetime:
    return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts


def stitch_contracts(contracts: list, days: int, base: int, allow_fetch: bool = True):
    """Fetch each quarterly contract over ITS OWN lifetime and splice on roll
    boundaries so the series is continuous AND non-overlapping. Contract C_i
    owns (roll(C_{i-1}), roll(C_i)] ; the newest (front) contract owns
    everything after its predecessor's roll up to now (still live).

    Each contract is fetched with an EXPLICIT window around its ownership span
    (old expired contracts are outside any trailing-`days` window, so a plain
    last-`days` fetch returns 0 bars for them). `days` is ignored here."""
    contracts = sorted(contracts, key=_roll_dt)
    now = datetime.utcnow()
    pad = timedelta(days=6)
    pieces, prev_roll = [], None
    for idx, cid in enumerate(contracts):
        is_front = idx == len(contracts) - 1
        own_hi = now if is_front else _roll_dt(cid)
        own_lo = prev_roll if prev_roll is not None else (_roll_dt(cid) - timedelta(days=100))
        fetch_start = own_lo - pad
        fetch_end = min(now, own_hi + pad)
        bars = load_or_fetch_window(cid, fetch_start, fetch_end, base, allow_fetch=allow_fetch)
        hi = None if is_front else _roll_dt(cid)
        seg = []
        for b in bars:
            t = _as_naive(b.timestamp)
            if prev_roll is not None and t <= prev_roll:
                continue            # owned by an earlier contract
            if hi is not None and t > hi:
                continue            # owned by a later contract
            seg.append(b)
        lo_s = prev_roll.date() if prev_roll else "start"
        hi_s = hi.date() if hi else "now"
        print(f"[stitch] {cid}: kept {len(seg):>6} bars  ({lo_s} .. {hi_s}]", flush=True)
        pieces.extend(seg)
        prev_roll = _roll_dt(cid)
    pieces.sort(key=lambda b: b.timestamp)
    # safety: drop any exact-duplicate timestamps (no double counting)
    deduped, seen = [], set()
    for b in pieces:
        key = _as_naive(b.timestamp)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(b)
    span = ""
    if deduped:
        span = f"  {_as_naive(deduped[0].timestamp).date()} .. {_as_naive(deduped[-1].timestamp).date()}"
    print(f"[stitch] total {len(deduped)} bars from {len(contracts)} contracts{span}", flush=True)
    return deduped


def resolve_candles(contract: str, days: int, base: int, stitch: int = 1,
                    allow_fetch: bool = True, use_store: bool = False,
                    symbol: str = "MNQ"):
    """Pick the data source:
      • use_store=True  -> the persistent accumulated store (option C, offline)
      • stitch>1        -> non-overlapping multi-contract splice
      • else            -> single trailing-`days` fetch/cache."""
    if use_store:
        bars = load_store(base, symbol)
        if not bars:
            raise SystemExit(
                f"Accumulated store empty for {symbol} {base}m. "
                f"Run: python -m scripts.accumulate_history --base-min {base}")
        a = _as_naive(bars[0].timestamp).date()
        b = _as_naive(bars[-1].timestamp).date()
        print(f"[store] {len(bars)} bars  {a} .. {b}", flush=True)
        return bars
    if stitch and stitch > 1:
        ladder = mnq_ladder(contract, stitch)
        print(f"[stitch] ladder = {ladder}", flush=True)
        return stitch_contracts(ladder, days, base, allow_fetch=allow_fetch)
    return load_or_fetch(contract, days, base, allow_fetch=allow_fetch)
