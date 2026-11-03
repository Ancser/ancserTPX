"""Which exchange contract owns each minute of a continuous candle store.

A continuous 1m store is a sequence of quarterly contracts joined at *rolls*.
The roll ledger (kept in the store's meta sidecar) is the single truth for:

* which contract a minute belongs to (``contract_at``);
* the additive back-adjustment that puts that contract on the store's price
  level, i.e. the level of the newest contract (``adjustment_for``):
  ``stored price = raw exchange price + adjustment``.

``enforce`` is applied by every store writer.  It drops bars whose contract
does not own their minute (the 2026-09-10/11 U26-in-Z26 mix) and re-expresses
stamped bars at the store level whatever adjustment their writer applied (the
2026-08-31 seam: a batch already adjusted to Z26 was journaled into a store
still on U26).  Bars without a contract id (history written before contract
identity existed) pass unchanged.

Contracts are compared by expiry code (``Z26``) so the MNQ store can accept
the price-identical ENQ/NQ series as before.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.db.models import Candle
from backend.timebase import as_utc

logger = logging.getLogger(__name__)

QUARTER_CODES = "HMUZ"
_CODE_MONTH = {"H": 3, "M": 6, "U": 9, "Z": 12}


def expiry_code(contract_id: str) -> str:
    """'CON.F.US.MNQ.Z26' -> 'Z26' (also accepts a bare 'Z26')."""
    code = str(contract_id or "").strip().upper().rsplit(".", 1)[-1]
    if len(code) != 3 or code[0] not in QUARTER_CODES or not code[1:].isdigit():
        raise ValueError(f"not a quarterly contract id: {contract_id!r}")
    return code


def contract_root(contract_id: str) -> str:
    parts = str(contract_id or "").upper().split(".")
    return parts[3] if len(parts) >= 5 else ""


def make_contract_id(root: str, code: str) -> str:
    return f"CON.F.US.{root.upper()}.{expiry_code(code)}"


def next_contract(contract_id: str) -> str:
    code = expiry_code(contract_id)
    i = QUARTER_CODES.index(code[0])
    year = int(code[1:])
    nxt = QUARTER_CODES[(i + 1) % 4] + f"{(year + (1 if i == 3 else 0)) % 100:02d}"
    root = contract_root(contract_id)
    return make_contract_id(root, nxt) if root else nxt


def contract_month(contract_id: str) -> Tuple[int, int]:
    code = expiry_code(contract_id)
    return 2000 + int(code[1:]), _CODE_MONTH[code[0]]


def normalize_rolls(rolls: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for roll in rolls or ():
        try:
            at = as_utc(roll["at"] if isinstance(roll["at"], datetime)
                        else datetime.fromisoformat(str(roll["at"]).replace("Z", "+00:00")))
            out.append({
                "at": at,
                "from": expiry_code(roll["from"]),
                "to": expiry_code(roll["to"]),
                "offset": float(roll["offset"]),
                "basis": str(roll.get("basis") or ""),
            })
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda r: r["at"])
    return out


def rolls_to_meta(rolls: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {"at": r["at"].isoformat(), "from": r["from"], "to": r["to"],
         "offset": r["offset"], "basis": r["basis"]}
        for r in normalize_rolls(rolls)
    ]


def contract_at(rolls: List[Dict[str, Any]], ts: datetime) -> Optional[str]:
    """Expiry code owning minute ``ts``; None when the ledger is empty."""
    if not rolls:
        return None
    ts = as_utc(ts)
    if ts < rolls[0]["at"]:
        return rolls[0]["from"]
    lo, hi = 0, len(rolls) - 1
    while lo < hi:                      # last roll with at <= ts
        mid = (lo + hi + 1) // 2
        if rolls[mid]["at"] <= ts:
            lo = mid
        else:
            hi = mid - 1
    return rolls[lo]["to"]


def adjustment_for(rolls: List[Dict[str, Any]], contract_id: str) -> Optional[float]:
    """Back-adjustment of ``contract_id`` onto the newest contract's level."""
    if not rolls:
        return None
    try:
        code = expiry_code(contract_id)
    except ValueError:
        return None
    codes = [rolls[0]["from"]] + [r["to"] for r in rolls]
    if code not in codes:
        return None
    index = codes.index(code)
    return float(sum(r["offset"] for r in rolls[index:]))


def roll_offset(old_bars: Iterable[Candle], new_bars: Iterable[Candle],
                roll_at: datetime) -> Tuple[Optional[float], Optional[datetime]]:
    """New-minus-old close at the roll anchor minute.

    The anchor is the first minute both contracts traded at/after ``roll_at``,
    else the last shared minute before it.  Returns ``(None, None)`` when the
    contracts share no minute.
    """
    roll_at = as_utc(roll_at)
    old = {as_utc(b.timestamp): b for b in old_bars}
    new = {as_utc(b.timestamp): b for b in new_bars}
    common = sorted(set(old) & set(new))
    if not common:
        return None, None
    after = [ts for ts in common if ts >= roll_at]
    anchor = after[0] if after else common[-1]
    return float(new[anchor].close - old[anchor].close), anchor


def shift_bar(bar: Candle, delta: float, adjustment: Optional[float]) -> Candle:
    return replace(
        bar, open=bar.open + delta, high=bar.high + delta,
        low=bar.low + delta, close=bar.close + delta, price_adjustment=adjustment,
    )


def enforce(bars: Iterable[Candle], rolls: List[Dict[str, Any]]) -> Tuple[List[Candle], int, int]:
    """Keep only bars of the contract owning their minute, at the store level.

    Returns ``(kept, dropped, reexpressed)``.  With an empty ledger nothing is
    changed (a store that has not been migrated keeps its old behaviour).
    """
    source = list(bars)
    if not rolls:
        return source, 0, 0
    kept: List[Candle] = []
    dropped = reexpressed = 0
    adjustments: Dict[str, Optional[float]] = {}
    for bar in source:
        cid = getattr(bar, "contract_id", None)
        if not cid:
            kept.append(bar)
            continue
        try:
            code = expiry_code(cid)
        except ValueError:
            dropped += 1
            continue
        if contract_at(rolls, bar.timestamp) != code:
            dropped += 1
            continue
        if code not in adjustments:
            adjustments[code] = adjustment_for(rolls, code)
        target = adjustments[code]
        have = getattr(bar, "price_adjustment", None) or 0.0
        if target is not None and abs(target - have) > 1e-9:
            bar = shift_bar(bar, target - have, target)
            reexpressed += 1
        elif target is not None and getattr(bar, "price_adjustment", None) is None:
            bar = replace(bar, price_adjustment=target)
        kept.append(bar)
    return kept, dropped, reexpressed
