"""Range-based exit study for CLAUDE trend presets.

Research-only:
  - Keep the existing CLAUDE trend entry logic.
  - After fill, optionally resize SL to zone_range * sl_frac.
  - Replace fixed TP/trail with no-TP range ladder:
      step = zone_range * ladder_frac
      +1 step => SL to breakeven
      +2 steps => lock +1 step, etc.

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.trend_range_ladder_study
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import math
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.api.routes import _get_merged_zone_timeline, _get_precomputed_zone_timeline
from backend.backtest.engine import BacktestEngine
from backend.data import candle_store
from backend.db.models import BacktestConfig, Direction, _extract_symbol, get_commission_rt, get_fees_rt
from backend.terminal_live import (
    BUILTIN_PRESETS,
    CLAUDE_701_PRESET_1,
    CLAUDE_701_PRESET_2,
    CLAUDE_701_PRESET_3,
    _build_strategy_params,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "trend_range_ladder"
OUT_CSV = OUT_DIR / "results.csv"
OUT_TOP = OUT_DIR / "top_latest.csv"
OUT_JSON = OUT_DIR / "latest.json"
OUT_MD = OUT_DIR / "report.md"
OUT_CHECKPOINT = OUT_DIR / "checkpoint.json"

INITIAL_CAPITAL = 50_000.0
TICK = 0.25
FAR_TP = 1_000_000.0


@dataclass(frozen=True)
class Variant:
    preset_name: str
    sl_mode: str         # current | range
    sl_frac: float       # used when sl_mode=range
    ladder_frac: float   # range step

    @property
    def tag(self) -> str:
        sl = "SL=current" if self.sl_mode == "current" else f"SL={self.sl_frac:.2g}Rng"
        return f"{self.preset_name}|{sl}|Ladder={self.ladder_frac:.2g}Rng"


class TrendRangeLadderBacktest(BacktestEngine):
    def __init__(self, *args, variant: Variant, **kwargs):
        super().__init__(*args, **kwargs)
        self.variant = variant
        self._range_step = 0.0
        self._max_step = 0

    def _execute_entry(self, signal, candle):
        super()._execute_entry(signal, candle)
        pos = self._open_position
        if not pos:
            return
        rng = abs(float(getattr(pos, "breakout_range", 0.0) or 0.0))
        if rng <= 0:
            rng = abs(float(signal.breakout_range or 0.0))
        rng = max(rng, 4 * TICK)

        if self.variant.sl_mode == "range":
            sl_dist = max(4 * TICK, self.variant.sl_frac * rng)
            if pos.direction == Direction.BUY:
                pos.sl_price = round((pos.entry_price - sl_dist) / TICK) * TICK
            else:
                pos.sl_price = round((pos.entry_price + sl_dist) / TICK) * TICK
            pos.original_sl_price = pos.sl_price

        self._range_step = max(4 * TICK, self.variant.ladder_frac * rng)
        self._max_step = 0
        pos.tp_price = round((pos.entry_price + FAR_TP if pos.direction == Direction.BUY else pos.entry_price - FAR_TP) / TICK) * TICK
        pos.original_tp_price = pos.tp_price

    def _check_trailing_sl(self, candle):
        pos = self._open_position
        if not pos or self._range_step <= 0:
            return
        fav = (candle.close - pos.entry_price) if pos.direction == Direction.BUY else (pos.entry_price - candle.close)
        step_n = int(math.floor(fav / self._range_step))
        if step_n > self._max_step:
            self._max_step = step_n
        if self._max_step < 1:
            return
        lock_steps = self._max_step - 1
        if pos.direction == Direction.BUY:
            new_sl = round((pos.entry_price + lock_steps * self._range_step) / TICK) * TICK
            if new_sl > pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True
        else:
            new_sl = round((pos.entry_price - lock_steps * self._range_step) / TICK) * TICK
            if new_sl < pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True


def _timeline_for(candles, params):
    va = float(getattr(params, "value_area_pct", 0.80) or 0.80)
    skip = bool(getattr(params, "skip_zone_stability", False))
    method = str(getattr(params, "method", "single") or "single").lower()
    combo = list(getattr(params, "tf_combo", None) or [])
    if method == "overlap" and len(combo) >= 2:
        ordered = tuple(combo)
        return _get_merged_zone_timeline(
            candles,
            va,
            skip,
            ordered,
            str(getattr(params, "tr_overlap_trade_tf", "merged") or "merged"),
        )
    tf = str(getattr(params, "area_timeframe", "5m") or "5m")
    return _get_precomputed_zone_timeline(candles, va, skip, tf)


def _config(params) -> BacktestConfig:
    cid = params.contract_id
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80) or 0.80),
    )


def _variants() -> list[Variant]:
    presets = [CLAUDE_701_PRESET_1, CLAUDE_701_PRESET_2, CLAUDE_701_PRESET_3]
    rows: list[Variant] = []
    for preset in presets:
        for ladder_frac in (0.1, 0.2, 0.3, 0.5):
            rows.append(Variant(preset, "current", 0.0, ladder_frac))
            for sl_frac in (0.2, 0.3, 0.5):
                rows.append(Variant(preset, "range", sl_frac, ladder_frac))
    return rows


def _run_variant(candles, timelines: dict[str, list[dict]], variant: Variant) -> dict[str, Any]:
    preset = BUILTIN_PRESETS[variant.preset_name]
    params = _build_strategy_params(preset, preset.get("contract_id", "CON.F.US.MNQ.U26"))
    params = copy.deepcopy(params)
    params.tr_exit_mode = "tp"
    params.trail_enabled = False
    params.tr_trail_enabled = False
    timeline = timelines[variant.preset_name]
    start = time.time()
    result = TrendRangeLadderBacktest(
        config=_config(params),
        strategy_params=params,
        zone_timeline=timeline,
        record_equity=False,
        variant=variant,
    ).run(candles)
    m = result.metrics
    return {
        **asdict(variant),
        "variant": variant.tag,
        "trades": int(m.total_trades),
        "pnl": round(float(m.total_pnl), 2),
        "max_dd": round(float(m.max_drawdown), 2),
        "profit_factor": round(float(m.profit_factor), 4),
        "win_rate": round(float(m.win_rate), 4),
        "expectancy": round(float(m.expectancy), 3),
        "total_loss": round(float(m.total_loss), 2),
        "total_gain": round(float(m.total_gain), 2),
        "calmar": round(float(m.calmar_ratio), 4),
        "elapsed_sec": round(time.time() - start, 3),
    }


def _score(row: dict[str, Any]) -> float:
    pnl = float(row["pnl"])
    dd = max(100.0, float(row["max_dd"]))
    loss = abs(float(row["total_loss"]))
    pf = float(row["profit_factor"])
    trades = int(row["trades"])
    if trades < 20 or pnl <= 0:
        return -1e9
    return pnl - 0.9 * dd - max(0.0, loss - pnl) * 0.1 + 250.0 * max(0.0, pf - 1.4)


def _write(rows: list[dict[str, Any]], next_index: int, total: int, done: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    enriched = []
    for r in rows:
        rr = dict(r)
        reasons = []
        if rr["trades"] < 40:
            reasons.append("sample<40")
        if rr["pnl"] <= 0:
            reasons.append("pnl<=0")
        if rr["max_dd"] > 1000:
            reasons.append("maxDD>1000")
        if abs(rr["total_loss"]) > rr["pnl"]:
            reasons.append("loss>pnl")
        if rr["profit_factor"] < 1.4:
            reasons.append("PF<1.4")
        rr["score"] = round(_score(rr), 2)
        rr["reasons"] = ",".join(reasons)
        rr["verdict"] = "PASS" if not reasons and rr["pnl"] > 2000 else ("CAUTION" if rr["pnl"] > 0 else "FAIL")
        enriched.append(rr)
    enriched.sort(key=lambda r: r["score"], reverse=True)
    if enriched:
        fields = list(enriched[0].keys())
        with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(enriched)
        with OUT_TOP.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(enriched[:50])
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "done": done,
        "next_index": next_index,
        "total": total,
        "tested": len(rows),
        "passes": sum(1 for r in enriched if r["verdict"] == "PASS"),
        "top": enriched[:25],
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_CHECKPOINT.write_text(json.dumps({
        "next_index": next_index,
        "total": total,
        "done": done,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Trend Range Ladder Study",
        "",
        f"Generated: {payload['created_at']}",
        f"Progress: {len(rows)}/{total}; done={done}",
        "",
        "| rank | verdict | variant | trades | pnl | maxDD | PF | win% | total loss | reasons |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i, r in enumerate(enriched[:25], 1):
        lines.append(
            f"| {i} | {r['verdict']} | {r['variant']} | {r['trades']} | {r['pnl']:+.0f} | "
            f"{r['max_dd']:.0f} | {r['profit_factor']:.2f} | {100*r['win_rate']:.1f}% | "
            f"{r['total_loss']:+.0f} | {r['reasons']} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="maximum new variants to run; 0 = all")
    args = ap.parse_args()
    logging.getLogger("backend").setLevel(logging.WARNING)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    variants = _variants()

    rows: list[dict[str, Any]] = []
    done_indexes: set[int] = set()
    if OUT_CSV.exists():
        with OUT_CSV.open("r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                idx = int(float(r.pop("index", -1))) if "index" in r else -1
                if idx >= 0:
                    done_indexes.add(idx)
                    r["index"] = idx
                for k in ("sl_frac", "ladder_frac", "pnl", "max_dd", "profit_factor", "win_rate", "expectancy", "total_loss", "total_gain", "calmar", "elapsed_sec"):
                    if k in r:
                        r[k] = float(r[k])
                for k in ("trades",):
                    if k in r:
                        r[k] = int(float(r[k]))
                rows.append({k: v for k, v in r.items() if k not in ("score", "verdict", "reasons")})

    print(f"candles={len(candles)} variants={len(variants)} existing={len(done_indexes)}", flush=True)
    timelines: dict[str, list[dict]] = {}
    for preset_name in (CLAUDE_701_PRESET_1, CLAUDE_701_PRESET_2, CLAUDE_701_PRESET_3):
        preset = BUILTIN_PRESETS[preset_name]
        params = _build_strategy_params(preset, preset.get("contract_id", "CON.F.US.MNQ.U26"))
        t0 = time.time()
        timelines[preset_name] = _timeline_for(candles, params)
        print(f"timeline {preset_name[:20]}... {len(timelines[preset_name])} bars in {time.time()-t0:.1f}s", flush=True)

    new_runs = 0
    for idx, variant in enumerate(variants):
        if idx in done_indexes:
            continue
        if args.limit and new_runs >= args.limit:
            break
        row = _run_variant(candles, timelines, variant)
        row["index"] = idx
        rows.append(row)
        new_runs += 1
        _write(rows, idx + 1, len(variants), done=False)
        best = sorted(rows, key=_score, reverse=True)[0]
        print(
            f"[{len(rows)}/{len(variants)}] best {best['pnl']:+.0f} DD={best['max_dd']:.0f} "
            f"PF={best['profit_factor']:.2f} {best['variant']}",
            flush=True,
        )

    complete = len({int(r["index"]) for r in rows if "index" in r}) >= len(variants)
    _write(rows, len(variants) if complete else max((int(r["index"]) for r in rows if "index" in r), default=-1) + 1, len(variants), done=complete)
    print(f"Wrote {OUT_MD}", flush=True)


if __name__ == "__main__":
    main()
