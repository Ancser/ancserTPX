"""Verify the ZL PMO preset through the production BacktestEngine.

The research script tests the Pine-derived idea directly.  This script checks
the app integration path: StrategyParams -> EMAPMOStrategy -> BacktestEngine.
It intentionally uses the engine's normal flatten/session/risk behavior.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.backtest.engine import BacktestEngine
from backend.backtest.metrics import MetricsCalculator
from backend.db.models import (
    BacktestConfig,
    Candle,
    StrategyParams,
    get_commission_rt,
    get_fees_rt,
)


def _parse_ts(raw: str) -> datetime:
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def load_candles(path: Path, shift_to_bar_close: bool = True) -> list[Candle]:
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = _parse_ts(row["timestamp"])
                if shift_to_bar_close:
                    ts = ts + timedelta(minutes=4)
                candles.append(
                    Candle(
                        timestamp=ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row.get("volume") or 0)),
                        symbol="ZL",
                        interval="5m",
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return sorted(candles, key=lambda c: c.timestamp)


def _metrics_dict(trades) -> dict[str, Any]:
    metrics = MetricsCalculator().calculate_all(list(trades), 100000.0)
    def finite(value: float, cap: float = 999.0) -> float:
        v = float(value)
        if math.isinf(v) or math.isnan(v):
            return cap
        return v
    return {
        "trades": metrics.total_trades,
        "pnl": round(metrics.total_pnl, 2),
        "max_dd": round(metrics.max_drawdown, 2),
        "profit_factor": round(finite(metrics.profit_factor), 4),
        "win_rate": round(metrics.win_rate, 4),
        "expectancy": round(metrics.expectancy, 3),
        "total_gain": round(metrics.total_gain, 2),
        "total_loss": round(metrics.total_loss, 2),
    }


def _monthly(trades) -> list[dict[str, Any]]:
    by_month: dict[str, list] = defaultdict(list)
    for trade in trades:
        if trade.exit_time is None:
            continue
        by_month[trade.exit_time.strftime("%Y-%m")].append(trade)
    rows = []
    for month in sorted(by_month):
        row = {"month": month}
        row.update(_metrics_dict(by_month[month]))
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv",
        default=str(ROOT / "scratchpad" / "yahoo_futures_5m" / "ZL_5m_yahoo.csv"),
        help="ZL 5m CSV with timestamp/open/high/low/close/volume.",
    )
    parser.add_argument(
        "--out",
        default=str(ROOT / "scratchpad" / "icefishball_pine_strategy" / "zl" / "engine_verify.json"),
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    candles = load_candles(csv_path)
    if not candles:
        raise SystemExit(f"No candles loaded from {csv_path}")

    contract_id = "CON.F.US.ZL.N26"
    params = StrategyParams(
        strategy="pmo",
        contract_id=contract_id,
        contract_size=1,
        candle_seconds=300,
        tr_allowed_sessions=["ASIA", "EURO", "PRE", "RTH", "AH"],
        tr_one_trade_per_session=False,
        one_trade_per_session_direction=False,
        trail_enabled=False,
        tr_trail_enabled=False,
        trail_trigger_pct=0.0,
        tr_trail_trigger_pct=0.0,
        pmo_timeframe_minutes=5,
        pmo_signal_mode="normal",
        pmo_sl_atr=1.0,
        pmo_tp_atr=1.0,
        pmo_max_hold_bars=24,
        pmo_max_trades_per_day=3,
        pmo_warmup_bars=150,
    )
    config = BacktestConfig(
        symbol="ZL",
        interval="5m",
        initial_capital=100000.0,
        commission_rt=get_commission_rt(contract_id),
        fees_rt=get_fees_rt(contract_id),
        max_daily_loss=999999.0,
    )
    engine = BacktestEngine(config, strategy_params=params, record_equity=False)
    result = engine.run(candles)
    trades = result.trades
    payload = {
        "source_csv": str(csv_path),
        "candles": len(candles),
        "span_utc": [candles[0].timestamp.isoformat(), candles[-1].timestamp.isoformat()],
        "note": (
            "Production-engine verification. Uses PMO strategy mode, market entry on "
            "completed 5m signal, app flatten window, ATR SL/TP, and max hold."
        ),
        "params": {
            "contract_id": contract_id,
            "contract_size": 1,
            "pmo_signal_mode": "normal",
            "pmo_sl_atr": 1.0,
            "pmo_tp_atr": 1.0,
            "pmo_max_hold_bars": 24,
            "pmo_max_trades_per_day": 3,
        },
        "metrics": _metrics_dict(trades),
        "monthly": _monthly(trades),
        "exit_counts": dict(sorted(defaultdict(int, {
            str(k): sum(1 for t in trades if str(t.exit_reason.value if t.exit_reason else "") == str(k))
            for k in {t.exit_reason.value for t in trades if t.exit_reason}
        }).items())),
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload["metrics"], indent=2))
    print(f"saved={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
