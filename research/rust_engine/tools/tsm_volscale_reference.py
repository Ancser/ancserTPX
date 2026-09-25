from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

DATA_ROOT = Path(r"F:\ancserQuant\ancserMarketData")
PRICE_ROOT = DATA_ROOT / "source" / "futures" / "continuous_1m"
BASE_ROOT = DATA_ROOT / "derived" / "research" / "tff_confirmed_60s_tsm_20260925T0745Z"
OUT = DATA_ROOT / "derived" / "research" / "tsm_volscale_reference_20260925T1628Z"
SYMBOLS = ("MNQ", "MES")
PARAMETERS = {
    "lookback_completed_cme_returns": 20,
    "estimator": "population standard deviation of simple close-to-close returns, annualized by sqrt(252)",
    "target_annualized_volatility": 0.30,
    "contract_rule": "round half up (0.30 / realized volatility), clipped to 1 through 3 contracts",
}
EXPECTED_CANONICAL_SHA256 = {
    "MNQ": "c9088546c058f3c4c68892dc1807cf256b78ca07303e8943951d341dec3fab1f",
    "MES": "f30223bfd52c2a37ad61af62460e38f6e137a87c988746647474e7a78ee3e817",
}
POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}
TICK_VALUE = {"MNQ": 0.50, "MES": 1.25}
ROUND_TURN_COST = {"MNQ": 1.24, "MES": 1.24}
WINDOW = 20
TARGET = 0.30
MIN_CONTRACTS = 1
MAX_CONTRACTS = 3
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sessions(symbol: str) -> tuple[list[dict], Path]:
    source = PRICE_ROOT / f"{symbol}_accumulated_1m.pkl"
    with source.open("rb") as stream:
        bars = pickle.load(stream)
    bars.sort(key=lambda bar: bar.timestamp)
    daily: dict[date, list] = {}
    for bar in bars:
        chicago = bar.timestamp.astimezone(__import__("zoneinfo").ZoneInfo("America/Chicago"))
        trade_date = chicago.date() + (timedelta(days=1) if chicago.hour >= 17 else timedelta())
        session = daily.get(trade_date)
        if session is None:
            daily[trade_date] = [float(bar.open), float(bar.close), 1]
        else:
            session[1] = float(bar.close)
            session[2] += 1
    rows = [
        {"date": trade_date.isoformat(), "close": values[1], "bars": values[2]}
        for trade_date, values in sorted(daily.items())
        if values[2] >= 300
    ]
    return rows, source


def read_ledger(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    return rows


def vol_at_entry(row: dict, session_index: dict[str, int], closes: list[float]) -> float:
    entry_index = session_index[row["entry_date"]]
    completed_index = entry_index - 1
    first_return_end = completed_index - WINDOW + 1
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(first_return_end, completed_index + 1)]
    mean = math.fsum(returns) / len(returns)
    variance = math.fsum((value - mean) ** 2 for value in returns) / len(returns)
    return math.sqrt(variance) * math.sqrt(252.0)


def scaled_row(symbol: str, source: dict, direction: int, contracts: int, realized_vol: float) -> dict:
    result = dict(source)
    entry = float(source["entry_price"])
    exit_price = float(source["exit_price"])
    gross = (exit_price - entry) * POINT_VALUE[symbol] * direction * contracts
    cost = ROUND_TURN_COST[symbol] * contracts
    net = gross - cost
    result.update(
        {
            "signal": direction,
            "gross_pnl": gross,
            "cost": cost,
            "net_pnl": net,
            "stress_14t_pnl": net - 14.0 * TICK_VALUE[symbol] * contracts,
            "contracts": contracts,
            "realized_vol_20": realized_vol,
        }
    )
    return result


def write_ledger(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise RuntimeError(f"refusing to write empty reference ledger: {path}")
    fields = list(rows[0])
    with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=False)
    inputs = {}
    ledgers = {}
    symbol_summary = {}
    for symbol in SYMBOLS:
        sessions, canonical_path = canonical_sessions(symbol)
        canonical_hash = sha256(canonical_path)
        expected = EXPECTED_CANONICAL_SHA256[symbol]
        if canonical_hash.lower() != expected.lower():
            raise RuntimeError(f"canonical pickle hash changed for {symbol}: {canonical_hash}")
        dates = [row["date"] for row in sessions]
        index_by_date = {value: index for index, value in enumerate(dates)}
        closes = [row["close"] for row in sessions]
        base_path = BASE_ROOT / f"trades_{symbol}_price_only_60_session_tsm.csv.gz"
        base_rows = read_ledger(base_path)
        vol_tsm, vol_passive, vol_tff_tsm, vol_tff_passive = [], [], [], []
        sizes = []
        for row in base_rows:
            realized_vol = vol_at_entry(row, index_by_date, closes)
            if not math.isfinite(realized_vol) or realized_vol <= 0.0:
                raise RuntimeError(f"invalid realized volatility for {symbol} {row['entry_date']}")
            contracts = min(MAX_CONTRACTS, max(MIN_CONTRACTS, math.floor(TARGET / realized_vol + 0.5)))
            direction = int(row["signal"])
            trend_row = scaled_row(symbol, row, direction, contracts, realized_vol)
            passive_row = scaled_row(symbol, row, 1, contracts, realized_vol)
            vol_tsm.append(trend_row)
            vol_passive.append(passive_row)
            if row["tff_aligned"].lower() == "true":
                vol_tff_tsm.append(trend_row)
                vol_tff_passive.append(passive_row)
            sizes.append(contracts)

        strategy_rows = {
            "vol20_scaled_price_tsm": vol_tsm,
            "vol20_scaled_passive_long_paired_on_price_tsm": vol_passive,
            "vol20_scaled_tff_tsm": vol_tff_tsm,
            "vol20_scaled_passive_long_paired_on_tff_tsm": vol_tff_passive,
        }
        for strategy, rows in strategy_rows.items():
            ledger_path = OUT / f"trades_{symbol}_{strategy}.csv.gz"
            write_ledger(ledger_path, rows)
            ledgers[str(ledger_path)] = {"rows": len(rows), "sha256": sha256(ledger_path)}
        inputs[str(canonical_path)] = {"sha256": canonical_hash, "bytes": canonical_path.stat().st_size}
        inputs[str(base_path)] = {"sha256": sha256(base_path), "rows": len(base_rows)}
        symbol_summary[symbol] = {
            "canonical_sessions": len(sessions),
            "canonical_start": dates[0],
            "canonical_end": dates[-1],
            "price_tsm_intervals": len(vol_tsm),
            "tff_aligned_intervals": len(vol_tff_tsm),
            "contract_count_distribution": {str(size): sizes.count(size) for size in sorted(set(sizes))},
            "annualized_realized_volatility_min": min(vol_at_entry(row, index_by_date, closes) for row in base_rows),
            "annualized_realized_volatility_max": max(vol_at_entry(row, index_by_date, closes) for row in base_rows),
        }

    manifest = {
        "schema": "ancsertpx.tsm-volscale-python-reference.v1",
        "protocol": PARAMETERS,
        "input_hashes": inputs,
        "reference_ledger_hashes": ledgers,
        "symbols": symbol_summary,
        "source_path": str(Path(__file__).resolve()),
        "source_sha256": sha256(Path(__file__).resolve()),
    }
    (OUT / "reference_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUT), "symbols": symbol_summary, "ledger_count": len(ledgers)}, indent=2))


if __name__ == "__main__":
    main()
