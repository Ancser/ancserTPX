from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

from backend.data import candle_store, pi_history
from backend.db.models import Candle, Direction, get_tick_size
from backend.live.pi_listener import PiSignal
from backend.strategy.exit_policy import resolve_exit_policy
from backend.strategy.pi_signal import PiSignalStrategy, _signal_level
from backend.terminal_live import _build_strategy_params, _load_default_preset
from backend.timebase import as_utc, topstep_trade_date, topstep_trade_date_bounds


ROOT = Path(__file__).resolve().parents[1]
RUST_WORKSPACE = ROOT / "research" / "rust_engine"
CONTRACT_ID = "CON.F.US.MES.Z26"
EXPECTED_PRESET_SHA256 = "4899a94b277bc96f51940ced0fb6e0c5a734edd822578b903912f00643b0e64c"
EXPECTED_PI_HISTORY_SHA256 = "0da002d25405144eb285f84c64f3320513574c12ba3efa26a79e32eba2c2ea38"


class _RecordingPiStrategy(PiSignalStrategy):
    def __init__(self, params):
        self.push_observations = []
        self.source_observations = []
        self.decisions = []
        super().__init__(params)

    def push(self, signal):
        accepted = super().push(signal)
        self.push_observations.append(
            {
                "mark_id": signal.message_id,
                "ts": signal.ts.isoformat(),
                "future": signal.future,
                "kind": signal.kind,
                "level": _signal_level(signal),
                "accepted": accepted,
            }
        )
        return accepted

    def _source_signal(self, signal, candle, now):
        out = super()._source_signal(signal, candle, now)
        self.source_observations.append(
            {
                "mark_id": signal.message_id,
                "bar_ts": as_utc(candle.timestamp).isoformat(),
                "accepted": out is not None,
            }
        )
        return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(row):
    return (
        row["mark_id"],
        row.get("ts", row.get("bar_ts")),
        row.get("future"),
        row.get("kind"),
        row.get("level"),
        row["accepted"],
    )


def _rust_policy(policy):
    return {
        "model": policy["model"],
        "max_hold_minutes": policy["max_hold_minutes"],
        "hard_tp_enabled": policy["hard_tp_enabled"],
        "trail_mode": policy["trail_mode"],
        "trail_trigger_pct": policy["trail_trigger_pct"],
        "trail_offset_ticks": policy["trail_offset_ticks"],
        "trail_lock_pct": policy["trail_lock_pct"],
        "ladder_trigger_r": policy["ladder_trigger_r"],
        "ladder_gap_r": policy["ladder_gap_r"],
    }


def _native_params(python_strategy, params):
    return {
        "future": python_strategy.pi_future,
        "contract_size": params.contract_size,
        "tick_size": get_tick_size(params.contract_id),
        "timeframe_minutes": python_strategy.tf_minutes,
        "sl_atr": python_strategy.sl_atr,
        "rr_ratio": python_strategy.rr,
        "max_trades_per_day": python_strategy.max_trades_per_day,
        "long_only": python_strategy.pi_long_only,
        "long_kinds": list(python_strategy.pi_long_kinds),
        "short_kinds": list(python_strategy.pi_short_kinds),
        "short_levels": python_strategy.pi_short_levels,
        "short_sl": python_strategy.pi_short_sl,
        "max_signal_age_minutes": python_strategy.pi_max_age_min,
        "side_mode": params.factor_side_mode,
        "exit": {
            "long_hold_minutes": python_strategy.pi_long_hold,
            "short_hold_minutes": python_strategy.pi_short_hold,
            "trail_enabled": bool(params.tr_trail_enabled),
            "trail_trigger_pct": float(params.tr_trail_trigger_pct),
            "trail_offset_ticks": int(params.tr_trail_sl_ticks),
            "tp_ticks": int(params.tr_tp_ticks),
            "exit_mode": params.tr_exit_mode,
        },
    }


def _assert_atr_trace_point(actual, python_strategy, bar_index):
    current = python_strategy._cur
    assert current is not None
    expected = {
        "bar_index": bar_index,
        "current_bucket_start_utc": as_utc(current[0]).isoformat(),
        "current_open": current[1],
        "current_high": current[2],
        "current_low": current[3],
        "current_close": current[4],
        "current_volume": int(current[5]),
        "completed_bars": len(python_strategy._bars),
        "atr14": python_strategy._atr(14),
        "atr50": python_strategy._atr(50),
        "atr_blend": python_strategy._atr_blend(),
    }
    for field in (
        "bar_index",
        "current_bucket_start_utc",
        "current_open",
        "current_high",
        "current_low",
        "current_close",
        "current_volume",
        "completed_bars",
    ):
        assert actual[field] == expected[field], {
            "bar_index": bar_index,
            "field": field,
            "python": expected[field],
            "rust": actual[field],
        }
    for field in ("atr14", "atr50", "atr_blend"):
        expected_value = expected[field]
        actual_value = actual[field]
        if expected_value is None:
            assert actual_value is None, {
                "bar_index": bar_index,
                "field": field,
                "python": expected_value,
                "rust": actual_value,
            }
        else:
            assert actual_value is not None and math.isfinite(actual_value), {
                "bar_index": bar_index,
                "field": field,
                "python": expected_value,
                "rust": actual_value,
            }
            assert abs(actual_value - expected_value) <= 1e-10, {
                "bar_index": bar_index,
                "field": field,
                "python": expected_value,
                "rust": actual_value,
            }


def _run_native(fixture_path):
    cargo = shutil.which("cargo")
    if cargo is None:
        cargo = str(Path.home() / ".cargo" / "bin" / "cargo.exe")
    run = subprocess.run(
        [
            cargo,
            "run",
            "--offline",
            "-p",
            "ancsertpx-live-runtime",
            "--",
            "pi-replay",
            str(fixture_path),
        ],
        cwd=RUST_WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )
    assert run.returncode == 0, run.stderr[-6000:]
    return json.loads(run.stdout)


def test_native_rust_pi_matches_python_on_canonical_mes_data(tmp_path):
    preset_name, preset, preset_source = _load_default_preset()
    assert preset_name == "PI 2MES BOTH BEST"
    preset_sha256 = _sha256(ROOT / "data" / "presets.json")
    history_sha256 = _sha256(pi_history.HIST_PATH)
    assert preset_sha256 == EXPECTED_PRESET_SHA256
    assert history_sha256 == EXPECTED_PI_HISTORY_SHA256
    params = _build_strategy_params(preset, CONTRACT_ID)
    assert params.contract_id == CONTRACT_ID

    python_strategy = _RecordingPiStrategy(params)
    history = python_strategy._hist
    assert history, "the canonical PI loader must produce source marks"
    first_mark = min(timestamp for timestamp, _ in history)
    last_mark = max(timestamp for timestamp, _ in history)

    snapshot = candle_store.load_snapshot("MES", 1)
    assert snapshot.bars, f"MES candle store is empty: {snapshot.source_path}"
    start = first_mark - timedelta(days=30)
    end = last_mark + timedelta(minutes=3)
    bars = [
        candle
        for candle in snapshot.bars
        if start <= as_utc(candle.timestamp) <= end
    ]
    assert len(bars) > 10_000, "the parity cohort needs real MES warmup and event bars"

    marks_json = [
        {
            "ts": timestamp.isoformat(),
            "message_id": signal.message_id,
            "equity": signal.equity,
            "future": signal.future,
            "direction": signal.direction,
            "kind": signal.kind,
            "size": signal.size,
            "level": _signal_level(signal),
            "pos": signal.pos,
        }
        for timestamp, signal in history
    ]
    bars_json = [
        {
            "ts": as_utc(candle.timestamp).isoformat(),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": int(candle.volume or 0),
        }
        for candle in bars
    ]
    fixture = {
        "schema": "ancsertpx.live-runtime-pi-replay.v1",
        "params": _native_params(python_strategy, params),
        "marks": marks_json,
        "bars": bars_json,
        "trace_atr": True,
    }
    fixture_path = tmp_path / "pi-mes-canonical.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    native = _run_native(fixture_path)

    # Run and compare the same frozen bars through the production Python strategy.
    native_atr_trace = native["atr_trace"]
    assert len(native_atr_trace) == len(bars)
    for bar_index, candle in enumerate(bars):
        signal = python_strategy.evaluate(candle)
        _assert_atr_trace_point(native_atr_trace[bar_index], python_strategy, bar_index)
        if signal is None:
            continue
        policy = resolve_exit_policy(params, "pi", signal.direction)
        signal_meta = signal.meta["pi"]
        atr_blend = python_strategy._atr_blend()
        assert atr_blend is not None
        side = "buy" if signal.direction == Direction.BUY else "sell"
        width = atr_blend
        if side == "sell" and python_strategy.sl_atr > 0:
            width *= python_strategy.pi_short_sl / python_strategy.sl_atr
        python_strategy.decisions.append(
            {
                "message_id": signal_meta["message_id"],
                "signal_ts": signal_meta["signal_ts"],
                "bar_ts": as_utc(candle.timestamp).isoformat(),
                "zone_id": signal.zone_id,
                "trade_date": topstep_trade_date(candle.timestamp),
                "equity": signal_meta["equity"],
                "future": "MES",
                "kind": signal_meta["kind"],
                "level": signal_meta["level"],
                "size": signal_meta["size"],
                "direction": side,
                "quantity": params.contract_size,
                "entry": signal.entry_price,
                "stop_loss": signal.sl_price,
                "take_profit": signal.tp_price,
                "atr_blend": atr_blend,
                "risk_width": width,
                "reason": signal.reason,
                "exit_policy": {
                    "model": policy.model,
                    "max_hold_minutes": policy.max_hold_minutes,
                    "hard_tp_enabled": policy.hard_tp_enabled,
                    "trail_mode": policy.trail_mode.value,
                    "trail_trigger_pct": policy.trail_trigger_pct,
                    "trail_offset_ticks": policy.trail_offset_ticks,
                    "trail_lock_pct": policy.trail_lock_pct,
                    "ladder_trigger_r": policy.ladder_trigger_r,
                    "ladder_gap_r": policy.ladder_gap_r,
                },
            }
        )

    assert native_atr_trace[0]["completed_bars"] == 0
    assert next(point for point in native_atr_trace if point["completed_bars"] == 6)["atr14"] is None
    first_atr14 = next(point for point in native_atr_trace if point["completed_bars"] == 7)
    assert first_atr14["atr14"] is not None and first_atr14["atr50"] is None
    assert next(point for point in native_atr_trace if point["completed_bars"] == 24)["atr50"] is None
    first_atr50 = next(point for point in native_atr_trace if point["completed_bars"] == 25)
    assert first_atr50["atr50"] is not None
    assert max(point["completed_bars"] for point in native_atr_trace) == 400
    assert native_atr_trace[-1]["completed_bars"] == 400

    python_pushes = python_strategy.push_observations
    native_pushes = native["push_attempts"]
    push_parity = [
        (row["mark_id"], row["ts"], row["future"], row["kind"], row["level"], row["accepted"])
        for row in native_pushes
    ] == [_identity(row) for row in python_pushes]
    assert push_parity, {
        "native_first": native_pushes[:3],
        "python_first": python_pushes[:3],
        "native_count": len(native_pushes),
        "python_count": len(python_pushes),
    }

    python_sources = python_strategy.source_observations
    native_sources = native["source_attempts"]
    source_parity = [
        (row["mark_id"], row["bar_ts"], row["accepted"]) for row in native_sources
    ] == [
        (row["mark_id"], row["bar_ts"], row["accepted"]) for row in python_sources
    ]
    assert source_parity, {
        "native_first": native_sources[:3],
        "python_first": python_sources[:3],
        "native_count": len(native_sources),
        "python_count": len(python_sources),
    }

    python_decisions = python_strategy.decisions
    native_decisions = native["decisions"]
    assert len(native_decisions) == len(python_decisions) and native_decisions, {
        "native_count": len(native_decisions),
        "python_count": len(python_decisions),
    }
    float_fields = ("entry", "stop_loss", "take_profit", "atr_blend", "risk_width")
    exact_fields = (
        "message_id",
        "signal_ts",
        "bar_ts",
        "zone_id",
        "trade_date",
        "equity",
        "future",
        "kind",
        "level",
        "size",
        "direction",
        "quantity",
        "reason",
    )
    for index, (expected, actual) in enumerate(zip(python_decisions, native_decisions)):
        for field in exact_fields:
            assert actual[field] == expected[field], {
                "index": index,
                "field": field,
                "python": expected[field],
                "rust": actual[field],
            }
        for field in float_fields:
            assert abs(actual[field] - expected[field]) <= 1e-8, {
                "index": index,
                "field": field,
                "python": expected[field],
                "rust": actual[field],
            }
        actual_policy = _rust_policy(actual["exit_policy"])
        assert actual_policy == expected["exit_policy"], {
            "index": index,
            "python_policy": expected["exit_policy"],
            "rust_policy": actual_policy,
        }

    source_path = snapshot.source_path
    print(
        json.dumps(
            {
                "preset_name": preset_name,
                "preset_source": preset_source,
                "preset_sha256": preset_sha256,
                "pi_history_sha256": history_sha256,
                "mes_candle_store": str(source_path),
                "mes_store_version_mtime_ns_size": snapshot.version,
                "cohort_start": bars[0].timestamp.isoformat(),
                "cohort_end": bars[-1].timestamp.isoformat(),
                "bars": len(bars),
                "marks": len(history),
                "push_attempts": len(python_pushes),
                "push_accepted": sum(row["accepted"] for row in python_pushes),
                "push_rejected": sum(not row["accepted"] for row in python_pushes),
                "source_attempts": len(python_sources),
                "decisions": len(python_decisions),
                "decisions_by_side": {
                    "buy": sum(row["direction"] == "buy" for row in python_decisions),
                    "sell": sum(row["direction"] == "sell" for row in python_decisions),
                },
                "preset_data_source": preset_source,
            },
            ensure_ascii=True,
        )
    )


def test_native_rust_pi_daily_signal_cap_matches_python(tmp_path):
    _, preset, _ = _load_default_preset()
    params = _build_strategy_params(preset, CONTRACT_ID)
    python_strategy = _RecordingPiStrategy(params)

    snapshot = candle_store.load_snapshot("MES", 1)
    trade_date = "2026-03-06"
    start, end = topstep_trade_date_bounds(trade_date)
    bars = candle_store.select_range(snapshot, start=start, end=end)
    assert len(bars) >= 160, f"expected full MES warmup/session for {trade_date}"
    selected_bars = bars[150:154]
    assert len(selected_bars) == 4

    synthetic_history = []
    marks_json = []
    for index, candle in enumerate(selected_bars):
        timestamp = as_utc(candle.timestamp)
        message_id = f"parity-cap-{index}"
        signal = PiSignal(
            message_id=message_id,
            ts=timestamp,
            equity="SPY",
            future="MES",
            direction=1,
            kind="青π",
            size="Level 3",
            pos=None,
            level=3,
        )
        synthetic_history.append((timestamp, signal))
        marks_json.append(
            {
                "ts": timestamp.isoformat(),
                "message_id": message_id,
                "equity": "SPY",
                "future": "MES",
                "direction": 1,
                "kind": "青π",
                "size": "Level 3",
                "level": 3,
                "pos": None,
            }
        )
    python_strategy._hist = synthetic_history
    python_strategy._hist_i = 0

    for candle in bars:
        signal = python_strategy.evaluate(candle)
        if signal is None:
            continue
        python_strategy.decisions.append(signal)

    fixture = {
        "schema": "ancsertpx.live-runtime-pi-replay.v1",
        "params": _native_params(python_strategy, params),
        "marks": marks_json,
        "bars": [
            {
                "ts": as_utc(candle.timestamp).isoformat(),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": int(candle.volume or 0),
            }
            for candle in bars
        ],
    }
    fixture_path = tmp_path / "pi-mes-daily-cap.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    native = _run_native(fixture_path)

    assert "atr_trace" not in native
    assert len(python_strategy.decisions) == 3
    assert len(native["decisions"]) == 3
    assert [row["accepted"] for row in python_strategy.source_observations] == [
        True,
        True,
        True,
        False,
    ]
    assert [row["accepted"] for row in native["source_attempts"]] == [
        True,
        True,
        True,
        False,
    ]
    assert native["source_attempts"][-1]["reason"] == "daily_signal_cap"
    assert [row["message_id"] for row in native["decisions"]] == [
        row.meta["pi"]["message_id"] for row in python_strategy.decisions
    ]


def test_native_rust_pi_atr_trace_matches_across_utc_day_and_long_gap(tmp_path):
    _, preset, _ = _load_default_preset()
    params = _build_strategy_params(preset, CONTRACT_ID)
    python_strategy = _RecordingPiStrategy(params)
    python_strategy._hist = []
    python_strategy._hist_i = 0

    start = datetime(2026, 9, 14, 23, 0, tzinfo=timezone.utc)
    candles = []
    for index in range(28):
        timestamp = start + timedelta(minutes=5 * index)
        center = 5000.0 + index * 0.25
        candles.append(
            Candle(
                timestamp=timestamp,
                open=center,
                high=center + 1.0,
                low=center - 0.5,
                close=center + 0.25,
                volume=100 + index,
                symbol="MES",
                interval="1m",
            )
        )
    for timestamp, center, volume in (
        (datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc), 5015.0, 701),
        (datetime(2026, 9, 15, 6, 1, tzinfo=timezone.utc), 5016.0, 702),
        (datetime(2026, 9, 15, 6, 5, tzinfo=timezone.utc), 5017.0, 703),
    ):
        candles.append(
            Candle(
                timestamp=timestamp,
                open=center,
                high=center + 0.75,
                low=center - 0.25,
                close=center + 0.5,
                volume=volume,
                symbol="MES",
                interval="1m",
            )
        )

    fixture = {
        "schema": "ancsertpx.live-runtime-pi-replay.v1",
        "params": _native_params(python_strategy, params),
        "marks": [],
        "bars": [
            {
                "ts": as_utc(candle.timestamp).isoformat(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ],
        "trace_atr": True,
    }
    fixture_path = tmp_path / "pi-atr-utc-gap.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    native = _run_native(fixture_path)

    trace = native["atr_trace"]
    assert len(trace) == len(candles) > 0
    for bar_index, candle in enumerate(candles):
        assert python_strategy.evaluate(candle) is None
        _assert_atr_trace_point(trace[bar_index], python_strategy, bar_index)

    assert trace[0]["current_bucket_start_utc"] == "2026-09-14T23:00:00+00:00"
    assert trace[12]["current_bucket_start_utc"] == "2026-09-15T00:00:00+00:00"
    assert trace[28]["current_bucket_start_utc"] == "2026-09-15T06:00:00+00:00"
    assert trace[28]["completed_bars"] == 28
    assert trace[29]["current_volume"] == 1403
    assert trace[30]["current_bucket_start_utc"] == "2026-09-15T06:05:00+00:00"
    assert trace[30]["completed_bars"] == 29
    assert trace[28]["atr14"] is not None
