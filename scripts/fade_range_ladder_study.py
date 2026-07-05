"""Previous-day VA range fade/breakout ladder study.

Research-only script for the 1.0.9 question:
  - Fade entries are good, but SL/TP may be poor.
  - Test SL as 20%-50% of previous VA range.
  - Test fixed TP as POC or 10%-100% of previous VA range.
  - Test range-ladder exits with 10%-30% range steps and no fixed TP.
  - Allow more than one entry per day/play.
  - Also test breakout legs: long above prev VAH, short below prev VAL.

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.fade_range_ladder_study
"""

from __future__ import annotations

import copy
import csv
import json
import logging
import math
import time
import argparse
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    Candle,
    Direction,
    StrategyType,
    TradeSignal,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
)
from backend.strategy.volume_profile import VolumeProfileCalculator
from backend.terminal_live import BUILTIN_PRESETS, CLAUDE_701_PRESET_1, _build_strategy_params

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "fade_range_ladder"
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
    entry_mode: str          # fade | breakout | both
    session: str             # ALL | ASIA
    sl_frac: float           # SL distance = prev VA range * sl_frac
    target_mode: str         # poc | fixed | ladder
    target_frac: float       # fixed TP or ladder step fraction
    max_entries: int         # per Topstep day per play
    stop_after_losses: int   # 0 off; daily loss count breaker

    @property
    def tag(self) -> str:
        if self.target_mode == "poc":
            tgt = "TP=POC"
        elif self.target_mode == "fixed":
            tgt = f"TP={self.target_frac:.2g}Rng"
        else:
            tgt = f"Ladder={self.target_frac:.2g}Rng"
        maxe = "inf" if self.max_entries >= 99 else str(self.max_entries)
        return (
            f"{self.entry_mode}|{self.session}|SL={self.sl_frac:.2g}Rng|"
            f"{tgt}|maxE={maxe}|lossStop={self.stop_after_losses}"
        )


class RangeFadeStrategy:
    """Prev-day VA strategy with range-percent stops and optional range ladder."""

    TICK_SIZE = TICK
    MIN_STOP_TICKS = 4
    MIN_TP_TICKS = 4
    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, variant: Variant):
        self.variant = variant
        self.levels: Optional[dict[str, Any]] = None
        self._prev_close: Optional[float] = None
        self._counts: defaultdict[str, int] = defaultdict(int)
        self._last_key: Optional[str] = None
        self._state = "idle"

    def set_levels(self, levels: Optional[dict[str, Any]]) -> None:
        self.levels = levels
        self._last_key = None
        self._state = "idle"

    def reset(self) -> None:
        self._prev_close = None
        self._counts.clear()
        self._last_key = None
        self._state = "idle"

    def reset_state_only(self) -> None:
        self._state = "idle"

    def reset_breakout_confirmation(self) -> None:
        self._state = "idle"

    def warmup(self, candle: Candle) -> None:
        pass

    def observe(self, candle: Candle, zones, is_mature) -> None:
        pass

    def notify_trade_closed(self, exit_reason: str) -> None:
        self._state = "idle"
        self._last_key = None

    def notify_order_cancelled(self) -> None:
        self._state = "idle"
        if self._last_key:
            self._counts[self._last_key] = max(0, self._counts[self._last_key] - 1)
            self._last_key = None

    def set_traded_breakouts(self, keys) -> None:
        pass

    def mark_breakout_used(self, zone_id, direction) -> None:
        pass

    def unlock_breakout(self, zone_id, direction) -> None:
        pass

    def get_phase_label(self) -> str:
        return "RANGE_FADE_RESEARCH"

    @property
    def raw_state(self) -> str:
        return self._state

    @staticmethod
    def _round_tick(price: float) -> float:
        return round(price / TICK) * TICK

    def _can_use(self, day: str, play: str) -> bool:
        return self._counts[f"{day}:{play}"] < self.variant.max_entries

    def _target_price(self, direction: Direction, entry: float, poc: float, rng: float) -> float:
        if self.variant.target_mode == "poc":
            return self._round_tick(poc)
        if self.variant.target_mode == "ladder":
            return self._round_tick(entry + FAR_TP if direction == Direction.BUY else entry - FAR_TP)
        dist = max(self.MIN_TP_TICKS * TICK, self.variant.target_frac * rng)
        return self._round_tick(entry + dist if direction == Direction.BUY else entry - dist)

    def _mk(self, candle: Candle, play: str, direction: Direction, entry: float, sl: float, tp: float, order_type: str) -> TradeSignal:
        lv = self.levels or {}
        day = str(lv.get("date", "unknown"))
        key = f"{day}:{play}"
        self._counts[key] += 1
        self._last_key = key
        self._state = "confirmed"
        rng = abs(float(lv.get("vah", 0.0)) - float(lv.get("val", 0.0)))
        return TradeSignal(
            strategy=StrategyType.TREND_FOLLOW,
            direction=direction,
            entry_price=self._round_tick(entry),
            sl_price=self._round_tick(sl),
            tp_price=self._round_tick(tp),
            zone_id=f"FRL:{day}:{play}",
            reason=f"FADE_RANGE_LADDER {play} {self.variant.tag}",
            timestamp=candle.timestamp,
            breakout_range=rng,
            order_type=order_type,
            meta={
                "strategy_family": "fade_range_ladder",
                "mode": self.variant.entry_mode,
                "play": play,
                "target_mode": self.variant.target_mode,
                "target_frac": self.variant.target_frac,
                "range": rng,
                "range_step": max(self.MIN_TP_TICKS * TICK, self.variant.target_frac * rng),
            },
        )

    def evaluate(self, candle: Candle, zones=None, is_mature: bool = True) -> Optional[TradeSignal]:
        lv = self.levels
        prev_close = self._prev_close
        self._prev_close = candle.close
        if not lv:
            return None
        poc = float(lv["poc"])
        vah = float(lv["vah"])
        val = float(lv["val"])
        day = str(lv["date"])
        rng = max(TICK, vah - val)
        sl_dist = max(self.MIN_STOP_TICKS * TICK, self.variant.sl_frac * rng)
        mode = self.variant.entry_mode

        if mode in ("breakout", "both") and prev_close is not None:
            if prev_close <= vah < candle.close and self._can_use(day, "brkLong"):
                entry = candle.close
                tp = self._target_price(Direction.BUY, entry, poc, rng)
                return self._mk(candle, "brkLong", Direction.BUY, entry, entry - sl_dist, tp, "market")
            if prev_close >= val > candle.close and self._can_use(day, "brkShort"):
                entry = candle.close
                tp = self._target_price(Direction.SELL, entry, poc, rng)
                return self._mk(candle, "brkShort", Direction.SELL, entry, entry + sl_dist, tp, "market")

        if mode in ("fade", "both") and val < candle.close < vah:
            if self._can_use(day, "fadeShort") and (vah - poc) > self.MIN_TP_TICKS * TICK:
                entry = vah
                tp = self._target_price(Direction.SELL, entry, poc, rng)
                return self._mk(candle, "fadeShort", Direction.SELL, entry, entry + sl_dist, tp, "limit")
            if self._can_use(day, "fadeLong") and (poc - val) > self.MIN_TP_TICKS * TICK:
                entry = val
                tp = self._target_price(Direction.BUY, entry, poc, rng)
                return self._mk(candle, "fadeLong", Direction.BUY, entry, entry - sl_dist, tp, "limit")
        return None


class RangeFadeBacktest(BacktestEngine):
    """BacktestEngine wrapper that feeds previous-day VP levels to RangeFadeStrategy."""

    def __init__(self, *args, variant: Variant, **kwargs):
        super().__init__(*args, **kwargs)
        self.variant = variant
        self.trend_follow = RangeFadeStrategy(variant)
        self._pending_max_age = self.trend_follow.PENDING_TIMEOUT_CANDLES
        self._vp = VolumeProfileCalculator(TICK, float(self.config.value_area_pct))
        self._day: Optional[str] = None
        self._day_candles: list[Candle] = []
        self._ladder_step = 0.0
        self._ladder_max_step = 0
        self.play_pnl: defaultdict[str, float] = defaultdict(float)
        self.play_n: defaultdict[str, int] = defaultdict(int)
        self._cur_play = "?"

    def _process_candle(self, candle: Candle):
        day = _topstep_trade_date(candle.timestamp)
        if day != self._day:
            if self._day_candles:
                try:
                    vp = self._vp.calculate(self._day_candles)
                    self.trend_follow.set_levels({
                        "date": day,
                        "poc": vp.poc,
                        "vah": vp.vah,
                        "val": vp.val,
                    })
                except ValueError:
                    pass
            self._day = day
            self._day_candles = []
        self._day_candles.append(candle)
        super()._process_candle(candle)

    def _execute_entry(self, signal: TradeSignal, candle: Candle):
        super()._execute_entry(signal, candle)
        pos = self._open_position
        if not pos:
            return
        self._cur_play = str((signal.meta or {}).get("play", "?"))
        self._ladder_step = float((signal.meta or {}).get("range_step") or 0.0)
        self._ladder_max_step = 0

    def _check_trailing_sl(self, candle: Candle):
        if self.variant.target_mode != "ladder":
            return super()._check_trailing_sl(candle)
        pos = self._open_position
        if not pos or self._ladder_step <= 0:
            return
        fav = (candle.close - pos.entry_price) if pos.direction == Direction.BUY else (pos.entry_price - candle.close)
        step_n = int(math.floor(fav / self._ladder_step))
        if step_n > self._ladder_max_step:
            self._ladder_max_step = step_n
        if self._ladder_max_step < 1:
            return
        lock_steps = self._ladder_max_step - 1
        if pos.direction == Direction.BUY:
            new_sl = round((pos.entry_price + lock_steps * self._ladder_step) / TICK) * TICK
            if new_sl > pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True
        else:
            new_sl = round((pos.entry_price - lock_steps * self._ladder_step) / TICK) * TICK
            if new_sl < pos.sl_price:
                pos.sl_price = new_sl
                self._trail_sl_triggered = True

    def _execute_exit(self, candle: Candle, exit_price: float, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None:
            self.play_pnl[self._cur_play] += t.pnl or 0.0
            self.play_n[self._cur_play] += 1


def _variants() -> list[Variant]:
    out: list[Variant] = []
    target_specs = [("poc", 0.0), ("fixed", 0.1), ("fixed", 0.2), ("fixed", 0.3), ("fixed", 0.5), ("fixed", 1.0)]
    target_specs += [("ladder", 0.1), ("ladder", 0.2), ("ladder", 0.3)]
    for entry_mode in ("fade", "breakout", "both"):
        for session in ("ALL", "ASIA"):
            for sl_frac in (0.2, 0.3, 0.4, 0.5):
                for target_mode, target_frac in target_specs:
                    for max_entries in (1, 3, 99):
                        for stop_after_losses in (0, 2):
                            out.append(Variant(entry_mode, session, sl_frac, target_mode, target_frac, max_entries, stop_after_losses))
    return out


def _config(params) -> BacktestConfig:
    cid = params.contract_id
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=INITIAL_CAPITAL,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=0.80,
    )


def _row_from_result(index: int, total: int, variant: Variant, result, engine: RangeFadeBacktest, elapsed: float) -> dict[str, Any]:
    m = result.metrics
    legs = " ".join(f"{k}:{engine.play_n[k]}:{engine.play_pnl[k]:+.0f}" for k in sorted(engine.play_n))
    return {
        "index": index,
        "total": total,
        "variant": variant.tag,
        **asdict(variant),
        "trades": int(m.total_trades),
        "pnl": round(float(m.total_pnl), 2),
        "max_dd": round(float(m.max_drawdown), 2),
        "profit_factor": round(float(m.profit_factor), 4),
        "win_rate": round(float(m.win_rate), 4),
        "expectancy": round(float(m.expectancy), 3),
        "total_loss": round(float(m.total_loss), 2),
        "total_gain": round(float(m.total_gain), 2),
        "calmar": round(float(m.calmar_ratio), 4),
        "legs": legs,
        "elapsed_sec": round(elapsed, 3),
    }


def _score(row: dict[str, Any]) -> float:
    pnl = float(row["pnl"])
    dd = max(100.0, float(row["max_dd"]))
    loss = abs(float(row["total_loss"]))
    pf = float(row["profit_factor"])
    trades = int(row["trades"])
    if trades < 20 or pnl <= 0:
        return -1e9
    quality_penalty = max(0.0, loss - pnl) * 0.15
    return pnl - 0.9 * dd - quality_penalty + 300.0 * max(0.0, pf - 1.4)


def _write_outputs(rows: list[dict[str, Any]], next_index: int, total: int, done: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if rows:
        fields = list(rows[0].keys()) + ["score", "verdict", "reasons"]
        enriched = []
        for r in rows:
            rr = dict(r)
            rr["score"] = round(_score(rr), 2)
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
            rr["reasons"] = ",".join(reasons)
            rr["verdict"] = "PASS" if not reasons and rr["pnl"] > 2000 else ("CAUTION" if rr["pnl"] > 0 else "FAIL")
            enriched.append(rr)
        enriched.sort(key=lambda x: x["score"], reverse=True)
        with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(enriched)
        with OUT_TOP.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(enriched[:50])
        summary = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "done": done,
            "next_index": next_index,
            "total": total,
            "tested": len(rows),
            "passes": sum(1 for r in enriched if r["verdict"] == "PASS"),
            "top": enriched[:25],
            "files": {
                "results_csv": str(OUT_CSV.relative_to(ROOT)),
                "top_csv": str(OUT_TOP.relative_to(ROOT)),
                "report_md": str(OUT_MD.relative_to(ROOT)),
            },
        }
        OUT_JSON.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# Fade Range Ladder Study",
            "",
            f"Generated: {summary['created_at']}",
            f"Progress: {len(rows)}/{total} variants; done={done}",
            "",
            "| rank | verdict | variant | trades | pnl | maxDD | PF | win% | total loss | reasons |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for i, r in enumerate(enriched[:25], 1):
            lines.append(
                f"| {i} | {r['verdict']} | {r['variant']} | {r['trades']} | "
                f"{r['pnl']:+.0f} | {r['max_dd']:.0f} | {r['profit_factor']:.2f} | "
                f"{100*r['win_rate']:.1f}% | {r['total_loss']:+.0f} | {r['reasons']} |"
            )
        OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    OUT_CHECKPOINT.write_text(json.dumps({
        "next_index": next_index,
        "total": total,
        "done": done,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="maximum new variants to run in this invocation; 0 = all remaining")
    ap.add_argument("--flush-every", type=int, default=5, help="write partial results every N new variants")
    args = ap.parse_args()
    logging.getLogger("backend").setLevel(logging.WARNING)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    candles = candle_store.load("MNQ", 1)
    candles.sort(key=lambda c: c.timestamp)
    timeline = [{} for _ in candles]  # skip trend zone detector; this strategy only uses prev-day VP.

    preset = BUILTIN_PRESETS[CLAUDE_701_PRESET_1]
    cid = preset.get("contract_id", "CON.F.US.MNQ.U26")
    base = _build_strategy_params(preset, cid)
    base.contract_size = 1
    base.value_area_pct = 0.80
    base.trail_enabled = False
    base.tr_trail_enabled = False
    base.one_trade_per_session_direction = False
    base.tr_one_trade_per_session = False
    base.full_tp_lock = 0
    base.tr_full_tp_lock = 0
    base.tr_exit_mode = "tp"

    variants = _variants()
    rows: list[dict[str, Any]] = []
    if OUT_CSV.exists():
        with OUT_CSV.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        # Convert numeric fields back to numbers for scoring/output consistency.
        normalized = []
        for r in rows:
            rr = dict(r)
            for k in ("index", "total", "max_entries", "stop_after_losses", "trades"):
                if k in rr:
                    rr[k] = int(float(rr[k]))
            for k in ("sl_frac", "target_frac", "pnl", "max_dd", "profit_factor", "win_rate", "expectancy", "total_loss", "total_gain", "calmar", "elapsed_sec"):
                if k in rr:
                    rr[k] = float(rr[k])
            normalized.append({k: v for k, v in rr.items() if k not in ("score", "verdict", "reasons")})
        rows = normalized
    done_indexes = {int(r["index"]) for r in rows if "index" in r}

    total = len(variants)
    print(f"candles={len(candles)} variants={total} existing={len(done_indexes)}", flush=True)
    last_flush = time.time()
    new_runs = 0
    for i, variant in enumerate(variants):
        if i in done_indexes:
            continue
        if args.limit and new_runs >= args.limit:
            break
        p = copy.deepcopy(base)
        p.tr_allowed_sessions = None if variant.session == "ALL" else [variant.session]
        p.tr_daily_loss_stop = variant.stop_after_losses
        start = time.time()
        engine = RangeFadeBacktest(
            config=_config(p),
            strategy_params=p,
            zone_timeline=timeline,
            record_equity=False,
            variant=variant,
        )
        result = engine.run(candles)
        rows.append(_row_from_result(i, total, variant, result, engine, time.time() - start))
        new_runs += 1
        if new_runs % max(1, args.flush_every) == 0 or time.time() - last_flush > 60:
            _write_outputs(rows, i + 1, total, done=False)
            best = sorted(rows, key=_score, reverse=True)[0]
            print(
                f"[{len(rows)}/{total}] best {best['pnl']:+.0f} DD={best['max_dd']:.0f} "
                f"PF={best['profit_factor']:.2f} {best['variant']}",
                flush=True,
            )
            last_flush = time.time()
    complete = len({int(r["index"]) for r in rows if "index" in r}) >= total
    _write_outputs(rows, total if complete else max((int(r["index"]) for r in rows if "index" in r), default=-1) + 1, total, done=complete)
    best = sorted(rows, key=_score, reverse=True)[0] if rows else None
    if best:
        print(
            f"DONE best {best['pnl']:+.0f} DD={best['max_dd']:.0f} PF={best['profit_factor']:.2f} "
            f"{best['variant']}",
            flush=True,
        )
    print(f"Wrote {OUT_MD}", flush=True)


if __name__ == "__main__":
    main()
