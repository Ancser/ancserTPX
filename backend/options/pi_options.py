"""PI long signal -> next-session QQQ/SPY call: the one execution definition.

Backtest (Backtest page model ``pi_options``) and the research scripts
(scripts/pi_option_limit_entry_study.py, scripts/pi_option_exit_grid_study.py)
all simulate entries and exits through the pure functions here, over
normalised minute quotes.  Quote-quality rules live with the Theta readers in
scripts/pi_theta_option_pilot.py; the path cache stores their verdict per
minute (``entry_ok`` / ``mark_ok``), so this module never re-derives them.

Point  ``(epoch_s, bid, ask, entry_ok, mark_ok)``: ``entry_ok`` = a buyable
       ask (ask > 0 with ask size); ``mark_ok`` = the bid is a valid economic
       mark (an observed zero bid counts, as a total loss).
Rules  (docs/RESEARCH_REPORT.md §4.5-4.6)
  entry   MARKET = the entry-minute ask; LIMIT -x% = (1-x) x that ask rounded
          down to $0.01, filled at the limit on the first later minute whose
          ask is buyable and <= the limit, before the exit time.
  size    a fixed number of contracts per signal (default 1), or whole
          contracts within a premium budget.
  exit    stop: first bid <= (1-SL) x fill, sold at that observed bid (gaps
          included); target: first bid >= (1+TP) x fill, filled at the target;
          else the bid at the exit time.  Watching starts the minute after
          the fill.
"""
from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.backtest.robustness import series_stats
from backend.data import market_data
from backend.timebase import NEW_YORK

MULTIPLIER = 100
COMMISSION_ROUND_TRIP = 1.30
CACHE_NAME = "pi_option_paths_v1.json.gz"
STRIKE_CHOICES = ("atm", "otm_3", "otm_5", "otm_7", "otm_10")
TIER_KINDS = {"LV2": "深蓝圈", "LV3": "青π"}
EXIT_TIMES = {"10:00": time(10, 0), "15:30": time(15, 30)}

Point = Tuple[int, Optional[float], Optional[float], bool, bool]


# ── pure execution rules ────────────────────────────────────────────────────

def limit_price(ask: float, discount: float) -> float:
    """Buy limit ``discount`` below ``ask``, rounded down to $0.01."""
    return math.floor((1 - discount) * ask * 100 + 1e-9) / 100


def limit_fill_index(points: Sequence[Point], start: int, limit: float,
                     cap_epoch: int) -> Optional[int]:
    """Index of the first point at/after ``start`` (and before the cap) whose
    ask is buyable and at or below ``limit``."""
    if limit <= 0:
        return None
    for index in range(start, len(points)):
        epoch, _bid, ask, entry_ok, _mark_ok = points[index]
        if epoch >= cap_epoch:
            return None
        if entry_ok and ask is not None and ask <= limit:
            return index
    return None


def exit_after(points: Sequence[Point], start: int, fill: float,
               stop: Optional[float], target: Optional[float],
               cap_epoch: int) -> Optional[Tuple[float, int, str]]:
    """``(price, epoch, reason)`` for a position filled at ``fill``, watching
    from index ``start``; None when the exit time has no valid bid."""
    stop_px = fill * (1 - stop) if stop else None
    target_px = fill * (1 + target) if target else None
    at_cap: Optional[Point] = None
    for index in range(start, len(points)):
        epoch, bid, _ask, _entry_ok, mark_ok = points[index]
        if epoch >= cap_epoch:
            if epoch == cap_epoch:
                at_cap = points[index]
            break
        if not mark_ok or bid is None:
            continue
        if stop_px is not None and bid <= stop_px:
            return bid, epoch, "sl"
        if target_px is not None and bid >= target_px:
            return target_px, epoch, "tp"
    if at_cap is not None and at_cap[4] and at_cap[1] is not None:
        return at_cap[1], cap_epoch, "time"
    return None


# ── settings ────────────────────────────────────────────────────────────────

@dataclass
class PiOptionSettings:
    tiers: Tuple[str, ...] = ("LV2", "LV3")
    strike: str = "otm_3"
    entry_discount: float = 0.0          # 0 = market; 0.2 = limit -20 %
    stop: Optional[float] = None         # 0.6 = stop at -60 % of premium
    target: Optional[float] = None       # 1.0 = take profit at +100 %
    exit_time: str = "15:30"             # next session
    contracts: Optional[int] = 1         # fixed contracts per signal; None = size by budget
    budget: float = 1000.0               # premium per signal when contracts is None
    start: Optional[date] = None
    end: Optional[date] = None

    def validate(self) -> "PiOptionSettings":
        self.tiers = tuple(t for t in self.tiers if t in TIER_KINDS) or ("LV2", "LV3")
        if self.strike not in STRIKE_CHOICES:
            raise ValueError(f"strike must be one of {STRIKE_CHOICES}")
        if self.exit_time not in EXIT_TIMES:
            raise ValueError(f"exit_time must be one of {tuple(EXIT_TIMES)}")
        if not 0 <= self.entry_discount < 1:
            raise ValueError("entry_discount must be in [0, 1)")
        for name in ("stop", "target"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                setattr(self, name, None)
        if self.stop is not None and self.stop >= 1:
            raise ValueError("stop must be below 100 %")
        if self.contracts is not None:
            if int(self.contracts) != self.contracts or self.contracts < 1:
                raise ValueError("contracts must be a whole number >= 1 (or null to size by budget)")
            self.contracts = int(self.contracts)
        if self.budget <= 0:
            raise ValueError("budget must be positive")
        return self


# ── path cache ──────────────────────────────────────────────────────────────

_CACHE: Dict[str, Any] = {"key": None, "value": None}


def cache_path() -> Path:
    return market_data.derived_path("research", CACHE_NAME)


def load_cache(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = path or cache_path()
    if not path.exists():
        return None
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _CACHE["key"] != key:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            _CACHE.update(key=key, value=json.load(handle))
    return _CACHE["value"]


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


def simulate_signal(signal: Dict[str, Any], settings: PiOptionSettings) -> Dict[str, Any]:
    """One cached signal under ``settings`` -> a trade or a no-trade reason."""
    strike = settings.strike
    chain = signal.get("strikes") or {}
    points = [tuple(p) for p in (signal.get("paths") or {}).get(strike, [])]
    if strike not in chain or not points:
        return {"status": "no_strike_path"}
    entry_epoch = int(signal["entry_epoch"])
    if points[0][0] != entry_epoch or not points[0][3] or points[0][2] is None:
        return {"status": "no_buyable_entry_ask"}
    ask0 = float(points[0][2])
    exit_date = date.fromisoformat(signal["exit_date"])
    cap = _epoch(datetime.combine(exit_date, EXIT_TIMES[settings.exit_time], tzinfo=NEW_YORK))
    if settings.entry_discount > 0:
        fill = limit_price(ask0, settings.entry_discount)
        index = limit_fill_index(points, 1, fill, cap)
        if index is None:
            return {"status": "limit_not_filled", "ask0": ask0, "limit": fill}
    else:
        fill, index = ask0, 0
    if settings.contracts:
        contracts = settings.contracts
    else:
        contracts = math.floor(settings.budget / (fill * MULTIPLIER) + 1e-12)
        if contracts < 1:
            return {"status": "premium_above_budget", "ask0": ask0}
    result = exit_after(points, index + 1, fill, settings.stop, settings.target, cap)
    if result is None:
        return {"status": "no_exit_mark", "ask0": ask0}
    price, exit_epoch, reason = result
    commission = COMMISSION_ROUND_TRIP * contracts
    gross = (price - fill) * MULTIPLIER * contracts
    return {
        "status": "ok", "ask0": ask0, "fill": fill, "exit": price,
        "limit": fill if settings.entry_discount > 0 else None,
        "signal_epoch": entry_epoch,
        "entry_epoch": points[index][0], "exit_epoch": exit_epoch, "reason": reason,
        "contracts": contracts, "gross": gross, "commission": commission,
        "net": gross - commission, "ret": price / fill - 1,
    }


def run_backtest(settings: PiOptionSettings, cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    settings.validate()
    cache = cache if cache is not None else load_cache()
    if cache is None:
        raise FileNotFoundError(
            "PI option path cache missing; build it with: python scripts/pi_option_path_cache.py")
    trades: List[Dict[str, Any]] = []
    missed: List[Dict[str, Any]] = []
    skipped: Dict[str, int] = defaultdict(int)
    considered = 0
    for signal in cache.get("signals", []):
        day = date.fromisoformat(signal["date"])
        if signal.get("tier") not in settings.tiers:
            continue
        if (settings.start and day < settings.start) or (settings.end and day > settings.end):
            continue
        considered += 1
        outcome = simulate_signal(signal, settings)
        context = {"symbol": signal["symbol"], "date": signal["date"], "tier": signal["tier"],
                   "strike": (signal.get("strikes") or {}).get(settings.strike),
                   "expiration": signal["expiration"], "spot": signal.get("spot")}
        if outcome["status"] != "ok":
            skipped[outcome["status"]] += 1
            # Every considered signal stays visible, not only the fills.
            missed.append({**outcome, **context, "signal_epoch": int(signal["entry_epoch"])})
            continue
        trades.append({**outcome, **context})
    trades.sort(key=lambda t: t["entry_epoch"])
    missed.sort(key=lambda t: t["signal_epoch"])
    pnls = [t["net"] for t in trades]
    stats = series_stats(pnls)
    by_date: Dict[str, float] = defaultdict(float)
    for trade in trades:
        by_date[trade["date"]] += trade["net"]
    best_two = sum(sorted(by_date.values(), reverse=True)[:2])
    rets = sorted(t["ret"] for t in trades)
    summary = {
        "signals": considered, "trades": len(trades),
        "fill_rate": round(len(trades) / considered, 4) if considered else None,
        "skipped": dict(skipped), "total": round(stats["pnl"], 2),
        "max_dd": round(stats["max_dd"], 2), "pf": round(stats["pf"], 4),
        "win_rate": round(stats["win"], 4),
        "without_best_2_dates": round(stats["pnl"] - best_two, 2),
        "ret_median": round(rets[len(rets) // 2], 4) if rets else None,
        "ret_min": round(rets[0], 4) if rets else None,
        "ret_max": round(rets[-1], 4) if rets else None,
        "data_window": cache.get("window"),
    }
    return {"trades": trades, "missed": missed, "summary": summary}
