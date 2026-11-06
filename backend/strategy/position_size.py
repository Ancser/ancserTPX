"""Entry contract count — the one definition shared by backtest and live.

1.5.1: `downsize_risk_dollars`. When the planned stop risk at the full
`contract_size` exceeds this many dollars, the entry is taken with ONE
contract instead. The signal is never skipped and the SL is never narrowed.

Why not the removed 1.0.9 `max_risk_ticks`: CLAMP narrowed the SL and turned
early exits into full stops; BLOCK skipped the wide-SL trades, which for PI
include the largest winners (R12, non-monotonic results). Downsizing keeps
every trade and its bracket and only halves exposure on the wide ones.
R13 (docs/RESEARCH_REPORT.md, 2 MNQ, Topstep $1,000 DLL replayed on intraday
unrealized P&L): $1,000 -> 5 of 87 trades on 1 lot, net $3,500 -> $3,850,
maxDD $3,546 -> $2,556, DLL days 2 -> 0; $1,000-$1,500 was a plateau.

Default None = always `contract_size` (pre-1.5.1 behaviour).
"""
from __future__ import annotations

from typing import Any, Optional

REDUCED_CONTRACTS = 1


def planned_risk_dollars(entry: float, sl: float, point_value: float, contracts: int) -> float:
    return abs(float(entry) - float(sl)) * float(point_value) * int(contracts)


def entry_contracts(
    params: Any, contract_size: int, entry: float, sl: float, point_value: float,
) -> int:
    """Contracts to enter for a signal with this entry and stop."""
    full = max(1, int(contract_size or 1))
    limit = getattr(params, "downsize_risk_dollars", None)
    try:
        limit_f = float(limit) if limit not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        limit_f = None
    if not limit_f or limit_f <= 0 or full <= REDUCED_CONTRACTS:
        return full
    if planned_risk_dollars(entry, sl, point_value, full) > limit_f:
        return REDUCED_CONTRACTS
    return full
