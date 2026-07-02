"""Compare FABLE #1 trend across timeframes and session filters.

Keeps FABLE #1 parameters fixed:
  VA70 RR4 C3 SL80 Ladder daily-loss-stop=4

Varies:
  area_timeframe: 15m, 30m, 1h, 4h
  tr_allowed_sessions: ASIA vs ALL

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.fable_tf_session_compare
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from backend.backtest.sweep import build_trend_zone_timeline, _run_one
from backend.data import candle_store
from backend.terminal_live import (
    BUILTIN_PRESETS,
    FABLE_702_PRESET_1,
    _build_strategy_params,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_TXT = ROOT / "data" / "machinelearning" / "fable_tf_session_compare.txt"
OUT_JSON = ROOT / "data" / "machinelearning" / "fable_tf_session_compare.json"

TIMEFRAMES = ("15m", "30m", "1h", "4h")
SESSION_VARIANTS = (
    ("ASIA", ["ASIA"]),
    ("ALL", ["ASIA", "EURO", "PRE", "RTH", "AH"]),
)


def _fmt(row: dict) -> str:
    return (
        f"{row['tf']:<4} {row['session']:<4} "
        f"{row['trades']:>5} {100 * row['win_rate']:>6.1f}% "
        f"{row['pnl']:>+10.1f} {row['max_dd']:>8.1f} "
        f"{row['pf']:>6.2f} {row['score']:>7.2f} "
        f"{row['expect']:>+8.2f} {row['worst_day']:>+9.1f} "
        f"{row['monthly_avg']:>+10.1f}"
    )


def main() -> None:
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit("No MNQ 1m candles in local store")

    preset = BUILTIN_PRESETS[FABLE_702_PRESET_1]
    cid = preset.get("contract_id")
    base = _build_strategy_params(preset, cid)
    base.strategy = "trend"
    base.method = "single"
    base.tf_combo = []
    base.value_area_pct = 0.70
    base.rr_ratio = 4
    base.breakout_confirm_bars = 3
    base.tr_exit_mode = "ladder"
    base.tr_daily_loss_stop = 4

    results: list[dict] = []
    lines = [
        f"preset: {FABLE_702_PRESET_1}",
        f"candles: {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}",
        "fixed: VA70 RR4 C3 SL80 Ladder daily_loss_stop=4 MNQx1",
        "",
        f"{'TF':<4} {'Sess':<4} {'n':>5} {'win%':>7} {'pnl':>10} "
        f"{'maxDD':>8} {'PF':>6} {'PNL/DD':>7} {'expect':>8} "
        f"{'worstDay':>9} {'monthly':>10}",
        "-" * 94,
    ]

    done = 0
    total = len(TIMEFRAMES) * (1 + len(SESSION_VARIANTS))
    for tf in TIMEFRAMES:
        done += 1
        print(f"[{done}/{total}] building {tf} VA70 timeline", flush=True)
        timeline = build_trend_zone_timeline(
            candles,
            area_timeframe=tf,
            value_area_pct=0.70,
            tick_size=0.25,
        )
        for session_label, sessions in SESSION_VARIANTS:
            done += 1
            print(f"[{done}/{total}] running {tf} {session_label}", flush=True)
            p = copy.deepcopy(base)
            p.area_timeframe = tf
            p.tr_allowed_sessions = sessions
            row = _run_one(p, candles, timeline)
            row.update({
                "tf": tf,
                "session": session_label,
                "sessions": sessions,
                "params": {
                    "area_timeframe": tf,
                    "value_area_pct": 0.70,
                    "rr_ratio": 4,
                    "breakout_confirm_bars": 3,
                    "tr_exit_mode": "ladder",
                    "tr_daily_loss_stop": 4,
                    "tr_allowed_sessions": sessions,
                },
            })
            results.append(row)
            lines.append(_fmt(row))

    best_score = max(results, key=lambda r: (r["score"], r["pnl"]))
    best_pnl = max(results, key=lambda r: r["pnl"])
    best_dd = min((r for r in results if r["pnl"] > 0), key=lambda r: r["max_dd"], default=None)
    lines.extend([
        "",
        f"best_score: {best_score['tf']} {best_score['session']} "
        f"PNL={best_score['pnl']:+.1f} DD={best_score['max_dd']:.1f} "
        f"PF={best_score['pf']:.2f} score={best_score['score']:.2f}",
        f"best_pnl:   {best_pnl['tf']} {best_pnl['session']} "
        f"PNL={best_pnl['pnl']:+.1f} DD={best_pnl['max_dd']:.1f} "
        f"PF={best_pnl['pf']:.2f} score={best_pnl['score']:.2f}",
    ])
    if best_dd is not None:
        lines.append(
            f"lowest_DD_positive: {best_dd['tf']} {best_dd['session']} "
            f"PNL={best_dd['pnl']:+.1f} DD={best_dd['max_dd']:.1f} "
            f"PF={best_dd['pf']:.2f} score={best_dd['score']:.2f}"
        )

    text = "\n".join(lines) + "\n"
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text(text, encoding="utf-8")
    OUT_JSON.write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "preset": FABLE_702_PRESET_1,
                "candles": len(candles),
                "range": [
                    candles[0].timestamp.isoformat(),
                    candles[-1].timestamp.isoformat(),
                ],
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(text, flush=True)
    print(f"wrote {OUT_TXT}", flush=True)
    print(f"wrote {OUT_JSON}", flush=True)


if __name__ == "__main__":
    main()
