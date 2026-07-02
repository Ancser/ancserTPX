"""Research: one-hour cooldown after each losing trade.

Compares each preset with its original behavior vs a 60-minute no-new-entry
cooldown after any net-losing exit. This is research-only; it does not alter
live/backtest production code.

Run:
  PYTHONIOENCODING=utf-8 python -m scripts.loss_cooldown_compare
"""
from __future__ import annotations

import copy
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.data import candle_store
from backend.db.models import (
    BacktestConfig,
    Candle,
    StrategyParams,
    _extract_symbol,
    get_commission_rt,
    get_fees_rt,
    get_tick_size,
)
from backend.strategy.consolidation import build_zone_detector
from backend.terminal_live import (
    BUILTIN_PRESETS,
    CLAUDE_701_PRESET_1,
    CLAUDE_701_PRESET_2,
    FABLE_702_PRESET_1,
    _build_strategy_params,
)


ROOT = Path(__file__).resolve().parents[1]
OUT_TXT = ROOT / "data" / "machinelearning" / "loss_cooldown_compare.txt"
OUT_JSON = ROOT / "data" / "machinelearning" / "loss_cooldown_compare.json"

PRESETS = (
    ("Claude #2", CLAUDE_701_PRESET_2),
    ("Claude #1", CLAUDE_701_PRESET_1),
    ("FABLE #1", FABLE_702_PRESET_1),
)


class LossCooldownBacktest(BacktestEngine):
    def __init__(self, *args, cooldown_minutes: int = 60, **kwargs):
        super().__init__(*args, **kwargs)
        self.cooldown_minutes = int(cooldown_minutes)
        self.cooldown_until: Optional[datetime] = None
        self.cooldown_triggers = 0
        self.cooldown_bar_blocks = 0
        self._last_blocked_bar: Optional[datetime] = None

    def _trend_session_allowed(self, ts: datetime) -> bool:
        if not super()._trend_session_allowed(ts):
            return False
        if self.cooldown_until is not None and ts < self.cooldown_until:
            if ts != self._last_blocked_bar:
                self.cooldown_bar_blocks += 1
                self._last_blocked_bar = ts
            return False
        return True

    def _execute_exit(self, candle: Candle, exit_price: float, reason):
        super()._execute_exit(candle, exit_price, reason)
        t = self._last_closed_trade
        if t is not None and (t.pnl or 0.0) < 0:
            self.cooldown_until = candle.timestamp + timedelta(minutes=self.cooldown_minutes)
            self.cooldown_triggers += 1


def build_timeline_for_params(candles: list[Candle], params: StrategyParams) -> list[dict]:
    method = str(getattr(params, "method", "single") or "single").lower()
    tf_combo = list(getattr(params, "tf_combo", None) or [])
    overlap_combo = tf_combo if method == "overlap" and len(tf_combo) >= 2 else None
    det = build_zone_detector(
        area_timeframe=getattr(params, "area_timeframe", "5m") or "5m",
        value_area_pct=float(getattr(params, "value_area_pct", 0.80) or 0.80),
        tick_size=get_tick_size(getattr(params, "contract_id", "") or "CON.F.US.MNQ.M26"),
        max_recent=10,
        tf_combo=overlap_combo,
        overlap_trade_tf=getattr(params, "tr_overlap_trade_tf", "merged"),
    )
    timeline = []
    last_key = None
    cur = []
    for c in candles:
        det.update(c)
        recent = det.get_recent_zones()
        key = tuple(str(z.zone_id) for z in recent)
        if key != last_key:
            last_key = key
            cur = list(recent)
        timeline.append({
            "active": cur[-1] if cur else None,
            "mature": bool(cur),
            "recent": cur,
        })
    return timeline


def _config(params: StrategyParams) -> BacktestConfig:
    cid = params.contract_id
    return BacktestConfig(
        strategies=["trend"],
        initial_capital=50_000.0,
        symbol=_extract_symbol(cid),
        commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80) or 0.80),
    )


def _run(params: StrategyParams, candles: list[Candle], timeline: list[dict], cooldown: bool) -> dict:
    engine_cls = LossCooldownBacktest if cooldown else BacktestEngine
    engine = engine_cls(
        config=_config(params),
        strategy_params=copy.deepcopy(params),
        zone_timeline=timeline,
        record_equity=False,
    )
    result = engine.run(candles)
    m = result.metrics
    day = defaultdict(float)
    gain = loss = 0.0
    for t in result.trades:
        p = t.pnl or 0.0
        day[_topstep_trade_date(t.entry_time)] += p
        if p > 0:
            gain += p
        else:
            loss += p
    monthly_rate = 0.0
    if day:
        d0 = datetime.fromisoformat(sorted(day.keys())[0]).date()
        d1 = datetime.fromisoformat(sorted(day.keys())[-1]).date()
        span_days = max(1, (d1 - d0).days + 1)
        monthly_rate = float(m.total_pnl) * 30.44 / span_days
    return {
        "trades": int(m.total_trades),
        "win_rate": round(float(m.win_rate), 4),
        "pnl": round(float(m.total_pnl), 1),
        "gain": round(gain, 1),
        "loss": round(loss, 1),
        "pf": round(float(m.profit_factor), 3),
        "max_dd": round(float(m.max_drawdown), 1),
        "expect": round(float(m.expectancy), 2),
        "worst_day": round(min(day.values()) if day else 0.0, 1),
        "monthly_avg": round(monthly_rate, 1),
        "score": round(float(m.total_pnl) / max(float(m.max_drawdown), 100.0), 3),
        "cooldown_triggers": int(getattr(engine, "cooldown_triggers", 0)),
        "cooldown_bar_blocks": int(getattr(engine, "cooldown_bar_blocks", 0)),
    }


def _fmt(row: dict) -> str:
    return (
        f"{row['preset']:<10} {row['mode']:<8} "
        f"{row['trades']:>5} {100 * row['win_rate']:>6.1f}% "
        f"{row['pnl']:>+10.1f} {row['max_dd']:>8.1f} "
        f"{row['pf']:>6.2f} {row['score']:>7.2f} "
        f"{row['loss']:>+10.1f} {row['worst_day']:>+9.1f} "
        f"{row['cooldown_triggers']:>4} {row['cooldown_bar_blocks']:>6}"
    )


def main() -> None:
    logging.getLogger("backend").setLevel(logging.WARNING)
    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    if not candles:
        raise SystemExit("No MNQ 1m candles in local store")

    lines = [
        "Research: 60m cooldown after any losing trade",
        f"candles: {len(candles)}  {candles[0].timestamp} -> {candles[-1].timestamp}",
        "",
        f"{'Preset':<10} {'Mode':<8} {'n':>5} {'win%':>7} {'pnl':>10} "
        f"{'maxDD':>8} {'PF':>6} {'PNL/DD':>7} {'loss':>10} "
        f"{'worstDay':>9} {'CD':>4} {'bars':>6}",
        "-" * 100,
    ]
    rows: list[dict] = []

    timeline_cache: dict[tuple, list[dict]] = {}
    for label, preset_name in PRESETS:
        preset = BUILTIN_PRESETS[preset_name]
        params = _build_strategy_params(preset, preset.get("contract_id"))
        key = (
            params.area_timeframe,
            params.value_area_pct,
            params.method,
            tuple(params.tf_combo),
            params.tr_overlap_trade_tf,
            params.contract_id,
        )
        if key not in timeline_cache:
            print(f"building timeline {label}: {key}", flush=True)
            timeline_cache[key] = build_timeline_for_params(candles, params)
        timeline = timeline_cache[key]

        for mode, cooldown in (("base", False), ("cool60m", True)):
            print(f"running {label} {mode}", flush=True)
            row = _run(params, candles, timeline, cooldown)
            row.update({
                "preset": label,
                "preset_name": preset_name,
                "mode": mode,
                "params": {
                    "area_timeframe": params.area_timeframe,
                    "value_area_pct": params.value_area_pct,
                    "method": params.method,
                    "tf_combo": list(params.tf_combo),
                    "tr_overlap_trade_tf": params.tr_overlap_trade_tf,
                    "rr_ratio": params.rr_ratio,
                    "breakout_confirm_bars": params.breakout_confirm_bars,
                    "tr_exit_mode": params.tr_exit_mode,
                    "tr_daily_loss_stop": params.tr_daily_loss_stop,
                    "tr_allowed_sessions": params.tr_allowed_sessions,
                    "cooldown_minutes": 60 if cooldown else 0,
                },
            })
            rows.append(row)
            lines.append(_fmt(row))

    lines.extend(["", "Delta cool60m - base:"])
    for label, _ in PRESETS:
        base = next(r for r in rows if r["preset"] == label and r["mode"] == "base")
        cool = next(r for r in rows if r["preset"] == label and r["mode"] == "cool60m")
        lines.append(
            f"{label:<10} trades {cool['trades'] - base['trades']:+d}, "
            f"PNL {cool['pnl'] - base['pnl']:+.1f}, "
            f"maxDD {cool['max_dd'] - base['max_dd']:+.1f}, "
            f"PF {cool['pf'] - base['pf']:+.3f}, "
            f"loss {cool['loss'] - base['loss']:+.1f}, "
            f"score {cool['score'] - base['score']:+.3f}"
        )

    text = "\n".join(lines) + "\n"
    OUT_TXT.parent.mkdir(parents=True, exist_ok=True)
    OUT_TXT.write_text(text, encoding="utf-8")
    OUT_JSON.write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "candles": len(candles),
                "range": [
                    candles[0].timestamp.isoformat(),
                    candles[-1].timestamp.isoformat(),
                ],
                "cooldown_minutes": 60,
                "results": rows,
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
