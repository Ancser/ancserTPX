"""Give the continuous 1m store a contract ledger and per-bar contract identity.

Dry-run by default; ``--apply`` backs up the pickle + meta first.

1. Historical rolls come from what the store's own builds recorded: the
   Databento ``instrument_id`` rolls in ``meta.known_seams`` and in the latest
   reconciliation report (2020 onward).
2. The recent contract segments are identified from the data, not assumed:
   stored bars are matched bar-for-bar against raw TopstepX bars of each
   contract (the broker keeps ~60 days).  That yields the latest roll and, if
   present, a splice seam where one side carries an extra back-adjustment
   (the 2026-08-31 MNQ case: before it raw U26, after it U26 + 295).  The seam
   is repaired by applying that same measured adjustment to everything
   before it.  No bar is invented.
3. Every bar is stamped with ``contract_id`` / ``price_adjustment`` from the
   ledger and its symbol label is normalised to the store product.
4. Verification: no splice-sized one-minute jumps remain, the ledger's
   enforce() drops/re-expresses nothing, and the recent window matches raw
   broker bars exactly at the recorded adjustment.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

from backend.broker.topstepx import TopstepXClient  # noqa: E402
from backend.data import candle_store as cs  # noqa: E402
from backend.data import contract_ledger as L  # noqa: E402
from backend.data import market_data  # noqa: E402
from backend.db.models import BarUnit, Candle  # noqa: E402

UTC = timezone.utc
JUMP_THRESHOLD = {"MNQ": 120.0, "MES": 30.0}
FIRST_CONTRACT = "H20"          # both stores start 2020-01-01, before the Mar-2020 roll


def _recorded_rolls(symbol: str) -> list[dict]:
    meta = cs.load_meta(symbol)
    seen = {}
    for seam in meta.get("known_seams", []):
        for at, jump, _prev, _cur in seam.get("internal_rolls_flattened", []):
            seen[str(at)[:19]] = float(jump)
    report = market_data.derived_path("research", "futures_reconciliation_latest.json")
    if report.exists():
        for roll in json.loads(report.read_text(encoding="utf-8")).get("databento_rolls", {}).get(symbol, []):
            seen[str(roll["timestamp"])[:19].replace("T", " ")] = float(roll["raw_open_minus_previous_close"])
    rolls, code = [], FIRST_CONTRACT
    for at in sorted(seen):
        nxt = L.next_contract(code)
        rolls.append({"at": datetime.fromisoformat(at).replace(tzinfo=UTC), "from": code, "to": nxt,
                      "offset": seen[at], "basis": "databento instrument_id roll"})
        code = nxt
    return rolls


async def _broker_bars(symbol: str, contracts: list[str], start: datetime, end: datetime) -> dict:
    load_dotenv(ROOT / ".env")
    client = TopstepXClient(os.getenv("TOPSTEPX_USERNAME", ""), os.getenv("TOPSTEPX_API_KEY", ""))
    out = {}
    try:
        await client.authenticate()
        for code in contracts:
            bars = await client.get_historical_bars_paginated(
                L.make_contract_id(symbol, code), BarUnit.MINUTE, 1,
                start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"), end_time=end.strftime("%Y-%m-%dT%H:%M:%SZ"))
            out[code] = {b.timestamp.astimezone(UTC): b for b in bars}
    finally:
        await client.disconnect()
    return out


def _ohlc(b) -> tuple:
    return (float(b.open), float(b.high), float(b.low), float(b.close))


def _classify(stored: list[Candle], broker: dict) -> dict:
    """Per stored minute: (contract code, adjustment) when it equals a raw
    broker bar of that contract shifted by one constant, else None."""
    out = {}
    for bar in stored:
        ts = bar.timestamp.astimezone(UTC)
        found = None
        for code, rows in broker.items():
            raw = rows.get(ts)
            if raw is None:
                continue
            diffs = {round(a - b, 4) for a, b in zip(_ohlc(bar), _ohlc(raw))}
            if len(diffs) == 1:
                found = (code, diffs.pop())
                break
        out[ts] = found
    return out


def _segments(labels: dict) -> list[dict]:
    """Collapse consecutive identical (contract, adjustment) labels."""
    segs = []
    for ts in sorted(labels):
        lab = labels[ts]
        if lab is None:
            continue
        if segs and segs[-1]["label"] == lab:
            segs[-1]["end"] = ts
            segs[-1]["bars"] += 1
        else:
            segs.append({"label": lab, "start": ts, "end": ts, "bars": 1})
    return segs


def _jumps(bars: list[Candle], threshold: float) -> list[tuple]:
    out, prev = [], None
    for c in bars:
        if prev is not None and (c.timestamp - prev.timestamp) <= timedelta(minutes=3):
            if abs(c.open - prev.close) >= threshold:
                out.append((c.timestamp.astimezone(UTC), round(c.open - prev.close, 2)))
        prev = c
    return out


def migrate(symbol: str, apply: bool) -> dict:
    snap = cs.load_snapshot(symbol, use_cache=False)
    bars = list(snap.bars)
    rolls = _recorded_rolls(symbol)
    current = rolls[-1]["to"]
    report = {"symbol": symbol, "bars": len(bars), "recorded_rolls": len(rolls), "current_recorded": current,
              "jumps_before": [(t.isoformat(), j) for t, j in _jumps(bars, JUMP_THRESHOLD[symbol])]}

    # Recent window vs raw broker bars of the current and the next contract.
    end = bars[-1].timestamp.astimezone(UTC) + timedelta(minutes=1)
    start = max(bars[0].timestamp.astimezone(UTC), end - timedelta(days=55))
    nxt = L.next_contract(current)
    broker = asyncio.run(_broker_bars(symbol, [current, nxt], start, end))
    window = [b for b in bars if start <= b.timestamp.astimezone(UTC) < end]
    segs = _segments(_classify(window, broker))
    report["recent_segments"] = [
        {"contract": s["label"][0], "adjustment": s["label"][1], "start": s["start"].isoformat(),
         "end": s["end"].isoformat(), "bars": s["bars"]} for s in segs if s["bars"] >= 30]

    # Latest roll observed in the data: current -> next, where the store
    # switches from `current` (+a) to `nxt` (+0).
    shift_before, shift_amount = None, 0.0
    big = [s for s in segs if s["bars"] >= 30]
    for a, b in zip(big, big[1:]):
        (ca, aa), (cb, ab) = a["label"], b["label"]
        if ca == current and cb == nxt and ab == 0.0:
            rolls.append({"at": b["start"], "from": current, "to": nxt, "offset": aa,
                          "basis": "store switch verified against TopstepX raw bars"})
        elif ca == cb == current and aa != ab:
            # Splice seam inside one contract: the older side lacks an adjustment.
            shift_before, shift_amount = b["start"], ab - aa
    report["latest_roll"] = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in rolls[-1].items()}
    report["seam_repair"] = ({"before": shift_before.isoformat(), "shift": shift_amount}
                             if shift_before is not None else None)

    norm = L.normalize_rolls(rolls)
    code_ids, adjustments, stamped = {}, {}, []
    for c in bars:
        ts = c.timestamp.astimezone(UTC)
        delta = shift_amount if (shift_before is not None and ts < shift_before) else 0.0
        code = L.contract_at(norm, c.timestamp)
        if code not in adjustments:
            adjustments[code] = L.adjustment_for(norm, code)
            code_ids[code] = L.make_contract_id(symbol, code)
        stamped.append(Candle(
            timestamp=c.timestamp, open=c.open + delta, high=c.high + delta, low=c.low + delta,
            close=c.close + delta, volume=c.volume, symbol=symbol, interval=c.interval or "1m",
            source=getattr(c, "source", "topstepx"), contract_id=code_ids[code],
            price_adjustment=adjustments[code],
        ))
    kept, dropped, reexpressed = L.enforce(stamped, norm)
    after_jumps = _jumps(stamped, JUMP_THRESHOLD[symbol])
    recent_ok = Counter()
    for c in stamped:
        ts = c.timestamp.astimezone(UTC)
        raw = broker.get(L.expiry_code(c.contract_id), {}).get(ts)
        if raw is not None:
            diffs = {round(a - b, 4) for a, b in zip(_ohlc(c), _ohlc(raw))}
            recent_ok["exact_at_adjustment" if diffs == {round(c.price_adjustment, 4)} else "differs"] += 1
    report.update({
        "ledger": L.rolls_to_meta(rolls)[-4:],
        "ledger_rolls": len(rolls),
        "enforce_dropped": dropped, "enforce_reexpressed": reexpressed,
        "jumps_after": [(t.isoformat(), j) for t, j in after_jumps],
        "recent_vs_broker": dict(recent_ok),
        "contracts": dict(Counter(L.expiry_code(c.contract_id) for c in stamped[-200000:])),
    })
    ok = dropped == 0 and reexpressed == 0 and not after_jumps and len(stamped) == len(bars)
    report["verified"] = ok
    if apply:
        if not ok:
            raise SystemExit(f"{symbol}: verification failed, nothing written: {json.dumps(report, default=str)[:2000]}")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive = market_data.archive_root() / f"{symbol.lower()}_contract_ledger_{stamp}"
        archive.mkdir(parents=True)
        for path in (cs._store_path(symbol), cs._meta_path(symbol)):
            shutil.copy2(path, archive / path.name)
        digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        assert digest(cs._store_path(symbol)) == digest(archive / cs._store_path(symbol).name)
        meta = cs.load_meta(symbol)
        meta["contract_rolls"] = L.rolls_to_meta(rolls)
        if shift_before is not None:
            meta.setdefault("known_seams", []).append({
                "timestamp": shift_before.isoformat(), "kind": "splice_seam_repaired",
                "offset_applied": shift_amount, "action": "bars before this minute shifted by the measured "
                "adjustment of the bars after it (both matched raw TopstepX bars of the same contract)",
                "repaired_at": datetime.now(UTC).isoformat()})
        cs.save_meta(meta, symbol)
        cs.save(stamped, symbol)
        report["backup"] = str(archive)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=("MNQ", "MES"), required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = migrate(args.symbol, args.apply)
    out = market_data.derived_path("research", f"contract_ledger_migration_{args.symbol.lower()}_latest.json")
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
