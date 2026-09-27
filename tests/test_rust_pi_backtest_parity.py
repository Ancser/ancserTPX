from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from zoneinfo import ZoneInfo

from backend.backtest.engine import BacktestEngine
from backend.data import candle_store, pi_history
from backend.db.models import (
    BacktestConfig,
    Candle,
    Direction,
    StrategyParams,
    StrategyType,
    TradeSignal,
    get_commission_rt,
    get_fees_rt,
    get_point_value,
)
from backend.strategy.session_filter import MARKET_PHASE_FLATTEN, market_close_phase
from backend.terminal_live import _build_strategy_params, _load_default_preset
from backend.timebase import as_utc


ROOT = Path(__file__).resolve().parents[1]
RUST_WORKSPACE = ROOT / "research" / "rust_engine"
CONTRACT_MES = "CON.F.US.MES.Z26"
CONTRACT_MNQ = "CON.F.US.MNQ.Z26"
EXPECTED_PRESET_SHA256 = "4899a94b277bc96f51940ced0fb6e0c5a734edd822578b903912f00643b0e64c"
EXPECTED_PI_HISTORY_SHA256 = "0da002d25405144eb285f84c64f3320513574c12ba3efa26a79e32eba2c2ea38"


class _ScheduledPiStrategy:
    PENDING_TIMEOUT_CANDLES = 1

    def __init__(self, signals):
        self.signals = signals

    def reset(self):
        pass

    def observe(self, *_args):
        pass

    def evaluate(self, candle, *_args):
        return self.signals.get(as_utc(candle.timestamp))

    def notify_order_cancelled(self):
        pass

    def notify_trade_closed(self, _reason):
        pass


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _policy_json(policy):
    return {
        "model": policy.model,
        "max_hold_minutes": policy.max_hold_minutes,
        "hard_tp_enabled": policy.hard_tp_enabled,
        "trail_mode": policy.trail_mode.value,
        "trail_trigger_pct": policy.trail_trigger_pct,
        "trail_offset_ticks": policy.trail_offset_ticks,
        "trail_lock_pct": policy.trail_lock_pct,
        "ladder_trigger_r": policy.ladder_trigger_r,
        "ladder_gap_r": policy.ladder_gap_r,
    }


def _capture_python_oracle(name, engine, candles, *, point_value, commission, fees):
    entries = []
    operations = []
    index_by_position = {}
    execute_entry = engine._execute_entry
    evaluate_exit = engine._evaluate_active_exit

    def capture_entry(signal, candle):
        execute_entry(signal, candle)
        position = engine._open_position
        if position is None:
            return
        trade_index = len(entries)
        index_by_position[id(position)] = trade_index
        entries.append(
            {
                "entry_key": f"{trade_index}:{(position.meta.get('pi') or {}).get('message_id') or position.zone_id}",
                "timestamp": as_utc(position.entry_time).isoformat(),
                "direction": "buy" if position.direction == Direction.BUY else "sell",
                "entry_price": float(position.entry_price),
                "stop_loss": float(position.sl_price),
                "take_profit": float(position.tp_price),
                "quantity": int(position.contracts),
                "signal_reason": str(signal.reason or ""),
                "exit_policy": _policy_json(engine._active_exit_policy),
            }
        )

    def capture_exit(candle, *, held_minutes=None):
        position = engine._open_position
        decision = evaluate_exit(candle, held_minutes=held_minutes)
        if position is None:
            return decision
        operations.append(
            {
                "trade_index": index_by_position[id(position)],
                "timestamp": as_utc(candle.timestamp).isoformat(),
                "action": decision.action.value,
                "reason": decision.reason.value if decision.reason else None,
                "stop_price": decision.stop_price,
                "trail_triggered": bool(decision.state.trail_triggered),
                "ladder_max_r": float(decision.state.ladder_max_r),
                "ladder_lock_r": decision.state.ladder_lock_r,
            }
        )
        return decision

    engine._execute_entry = capture_entry
    engine._evaluate_active_exit = capture_exit
    result = engine.run(candles)
    assert entries, f"{name}: Python BacktestEngine produced no accepted entries"
    assert len(entries) == len(result.trades), (
        name,
        len(entries),
        len(result.trades),
    )

    bars = [
        {
            "timestamp": as_utc(candle.timestamp).isoformat(),
            "calendar_date": candle.timestamp.strftime("%Y-%m-%d"),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "flatten_window": market_close_phase(candle.timestamp) == MARKET_PHASE_FLATTEN,
        }
        for candle in candles
    ]
    scenario = {
        "name": name,
        "bars": bars,
        "entries": entries,
        "exit_operations": operations,
        "initial_capital": float(engine.config.initial_capital),
        "point_value": float(point_value),
        "commission_rt": float(commission),
        "fees_rt": float(fees),
        "max_daily_loss": float(engine.config.max_daily_loss),
        "force_exit_at_end": True,
    }
    expected = []
    for trade_index, trade in enumerate(result.trades):
        gross = (
            (trade.exit_price - trade.entry_price)
            if trade.direction == Direction.BUY
            else (trade.entry_price - trade.exit_price)
        ) * float(trade.point_value) * int(trade.contracts)
        expected.append(
            {
                "trade_index": trade_index,
                "entry_key": entries[trade_index]["entry_key"],
                "entry_time": as_utc(trade.entry_time).isoformat(),
                "exit_time": as_utc(trade.exit_time).isoformat(),
                "direction": "buy" if trade.direction == Direction.BUY else "sell",
                "entry_price": float(trade.entry_price),
                "original_stop_loss": float(trade.original_sl_price),
                "final_stop_loss": float(trade.sl_price),
                "take_profit": float(trade.tp_price),
                "exit_price": float(trade.exit_price),
                "quantity": int(trade.contracts),
                "point_value": float(trade.point_value),
                "gross_pnl": gross,
                "commission": float(trade.commission),
                "fees": float(trade.fees),
                "net_pnl": float(trade.pnl),
                "exit_reason": trade.exit_reason.value,
                "signal_reason": entries[trade_index]["signal_reason"],
            }
        )
    return scenario, expected


def _candle(ts, open_, high, low, close, symbol="MES"):
    return Candle(
        timestamp=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=100,
        symbol=symbol,
        interval="1m",
    )


def _signal(ts, *, direction="buy", entry=100.0, stop=98.0, target=104.0, key="synthetic"):
    return TradeSignal(
        strategy=StrategyType.TREND_FOLLOW,
        direction=Direction.BUY if direction == "buy" else Direction.SELL,
        entry_price=entry,
        sl_price=stop,
        tp_price=target,
        zone_id=key,
        reason=f"synthetic {key}",
        order_type="market",
        meta={"pi": {"message_id": key}},
    )


def _synthetic_scenario(name, candles, signals, *, params=None, contract_id=CONTRACT_MES, max_daily_loss=2000.0):
    strategy_params = params or StrategyParams(
        strategy="pi",
        contract_id=contract_id,
        contract_size=1,
        tr_allowed_sessions=None,
        tr_one_trade_per_session=False,
        one_trade_per_session_direction=False,
        pi_continue_long_kinds=[],
        pi_continue_short_kinds=[],
        pi_lv2_replace_pi=False,
    )
    strategy_params = deepcopy(strategy_params)
    strategy_params.tr_allowed_sessions = None
    strategy_params.tr_one_trade_per_session = False
    strategy_params.one_trade_per_session_direction = False
    strategy_params.pi_continue_long_kinds = []
    strategy_params.pi_continue_short_kinds = []
    strategy_params.pi_lv2_replace_pi = False
    contracts = int(strategy_params.contract_size)
    config = BacktestConfig(
        strategies=["pi"],
        symbol="MES" if ".MES." in contract_id else "MNQ",
        initial_capital=50000.0,
        commission_rt=get_commission_rt(contract_id),
        fees_rt=get_fees_rt(contract_id),
        max_daily_loss=max_daily_loss,
    )
    engine = BacktestEngine(config=config, strategy_params=strategy_params, record_equity=False)
    engine.trend_follow = _ScheduledPiStrategy(signals)
    candles = list(candles)
    last_signal_index = max(
        index
        for index, candle in enumerate(candles)
        if as_utc(candle.timestamp) in signals
    )
    while len(candles) - last_signal_index - 1 < 3:
        previous = candles[-1]
        timestamp = previous.timestamp + timedelta(minutes=1)
        price = float(previous.close)
        candles.append(_candle(timestamp, price, price + 0.25, price - 0.25, price, previous.symbol))
    scenario, expected = _capture_python_oracle(
        name,
        engine,
        candles,
        point_value=get_point_value(contract_id),
        commission=get_commission_rt(contract_id),
        fees=get_fees_rt(contract_id),
    )
    assert all(entry["quantity"] == contracts for entry in scenario["entries"])
    return scenario, expected


def _synthetic_scenarios():
    base = datetime(2026, 9, 24, 14, 0, tzinfo=ZoneInfo("UTC"))
    cases = []

    def add(name, candles, signals, **kwargs):
        cases.append(_synthetic_scenario(name, candles, signals, **kwargs))

    add(
        "entry_candle_stop_only",
        [_candle(base, 100.0, 105.0, 97.0, 101.0)],
        {as_utc(base): _signal(base, key="entry-stop")},
    )
    add(
        "entry_candle_target_only",
        [_candle(base, 100.0, 105.0, 99.0, 101.0)],
        {as_utc(base): _signal(base, key="entry-target-only")},
    )
    for name, bar_open in (
        ("both_hit_stop_nearer", 98.5),
        ("both_hit_target_nearer", 103.5),
        ("both_hit_tie_long", 101.0),
    ):
        add(
            name,
            [
                _candle(base, 100.0, 100.5, 99.5, 100.0),
                _candle(base + timedelta(minutes=1), bar_open, 105.0, 97.0, 101.0),
            ],
            {as_utc(base): _signal(base, key=name)},
        )
    add(
        "both_hit_tie_short",
        [
            _candle(base, 100.0, 100.5, 99.5, 100.0),
            _candle(base + timedelta(minutes=1), 99.0, 103.0, 95.0, 98.0),
        ],
        {as_utc(base): _signal(base, direction="sell", stop=102.0, target=96.0, key="tie-short")},
    )
    trailing = StrategyParams(
        strategy="pi",
        contract_id=CONTRACT_MES,
        contract_size=1,
        tr_allowed_sessions=None,
        tr_trail_enabled=True,
        tr_trail_trigger_pct=0.5,
        tr_trail_sl_ticks=2,
        tr_tp_ticks=40,
        pi_continue_long_kinds=[],
        pi_continue_short_kinds=[],
        pi_lv2_replace_pi=False,
    )
    add(
        "trail_move_then_stop",
        [
            _candle(base, 100.0, 100.5, 99.5, 100.0),
            _candle(base + timedelta(minutes=1), 100.0, 102.5, 99.5, 102.0),
            _candle(base + timedelta(minutes=2), 102.0, 103.5, 100.25, 101.0),
        ],
        {as_utc(base): _signal(base, key="trail" )},
        params=trailing,
    )
    timed = StrategyParams(
        strategy="pi",
        contract_id=CONTRACT_MES,
        contract_size=1,
        tr_allowed_sessions=None,
        pi_long_hold_min=1,
        pi_continue_long_kinds=[],
        pi_continue_short_kinds=[],
        pi_lv2_replace_pi=False,
    )
    add(
        "directional_time_exit",
        [
            _candle(base, 100.0, 100.5, 99.5, 100.0),
            _candle(base + timedelta(minutes=1), 100.0, 101.0, 99.0, 100.5),
        ],
        {as_utc(base): _signal(base, key="time-exit")},
        params=timed,
    )
    flatten_ts = datetime(2026, 9, 25, 19, 45, tzinfo=ZoneInfo("UTC"))
    add(
        "session_flatten",
        [
            _candle(flatten_ts - timedelta(minutes=1), 100.0, 100.5, 99.5, 100.0),
            _candle(flatten_ts, 100.0, 101.0, 99.0, 100.25),
        ],
        {as_utc(flatten_ts - timedelta(minutes=1)): _signal(flatten_ts, key="flatten")},
    )
    for name, contract_id, direction, end_price in (
        ("mes_quantity_costs", CONTRACT_MES, "buy", 104.0),
        ("mnq_quantity_costs", CONTRACT_MNQ, "sell", 96.0),
    ):
        params = StrategyParams(
            strategy="pi",
            contract_id=contract_id,
            contract_size=2,
            tr_allowed_sessions=None,
            pi_continue_long_kinds=[],
            pi_continue_short_kinds=[],
            pi_lv2_replace_pi=False,
        )
        side_signal = _signal(
            base,
            direction=direction,
            stop=98.0 if direction == "buy" else 102.0,
            target=104.0 if direction == "buy" else 96.0,
            key=name,
        )
        add(
            name,
            [
                _candle(base, 100.0, 100.5, 99.5, 100.0, "MES" if ".MES." in contract_id else "MNQ"),
                _candle(
                    base + timedelta(minutes=1),
                    100.0,
                    105.0 if end_price > 100 else 100.5,
                    95.0 if end_price < 100 else 99.0,
                    end_price,
                    "MES" if ".MES." in contract_id else "MNQ",
                ),
            ],
            {as_utc(base): side_signal},
            params=params,
            contract_id=contract_id,
        )

    daily = StrategyParams(
        strategy="pi",
        contract_id=CONTRACT_MES,
        contract_size=1,
        tr_allowed_sessions=None,
        pi_continue_long_kinds=[],
        pi_continue_short_kinds=[],
        pi_lv2_replace_pi=False,
    )
    add(
        "daily_loss_close_after_same_bar_reentry",
        [
            _candle(base, 100.0, 100.5, 99.5, 100.0),
            _candle(base + timedelta(minutes=1), 100.0, 100.5, 97.5, 98.0),
            _candle(base + timedelta(minutes=2), 98.0, 100.5, 97.5, 100.0),
        ],
        {
            as_utc(base): _signal(base, entry=100.0, stop=98.0, target=104.0, key="loss-first"),
            as_utc(base + timedelta(minutes=1)): _signal(
                base + timedelta(minutes=1), entry=98.0, stop=96.0, target=102.0, key="loss-second"
            ),
        },
        params=daily,
        max_daily_loss=10.0,
    )
    return cases


def _invoke_native(path):
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo.exe")
    return subprocess.run(
        [
            cargo,
            "run",
            "--offline",
            "-p",
            "ancsertpx-live-runtime",
            "--",
            "pi-backtest",
            str(path),
        ],
        cwd=RUST_WORKSPACE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        check=False,
    )


def _run_native(path):
    run = _invoke_native(path)
    assert run.returncode == 0, run.stderr[-8000:]
    return json.loads(run.stdout)


def _same_time(actual, expected):
    assert datetime.fromisoformat(actual) == datetime.fromisoformat(expected)


def _compare_scenario(actual, expected, scenario):
    assert actual["name"] == scenario["name"]
    assert actual["frozen_entries"] == len(expected)
    assert len(actual["trades"]) == len(expected), (actual["name"], actual["trades"], expected)
    assert actual["final_position_open"] is False
    for native_trade, python_trade in zip(actual["trades"], expected):
        for field in ("trade_index", "entry_key", "direction", "quantity", "exit_reason", "signal_reason"):
            assert native_trade[field] == python_trade[field], (actual["name"], field, native_trade, python_trade)
        for field in ("entry_time", "exit_time"):
            _same_time(native_trade[field], python_trade[field])
        for field in (
            "entry_price",
            "original_stop_loss",
            "final_stop_loss",
            "take_profit",
            "exit_price",
            "point_value",
            "gross_pnl",
            "commission",
            "fees",
            "net_pnl",
        ):
            assert abs(native_trade[field] - python_trade[field]) <= 1e-8, (
                actual["name"],
                field,
                native_trade[field],
                python_trade[field],
            )
    assert abs(actual["realized_net_pnl"] - sum(row["net_pnl"] for row in expected)) <= 1e-8
    expected_capital = scenario["initial_capital"] + sum(row["net_pnl"] for row in expected)
    assert abs(actual["ending_capital"] - expected_capital) <= 1e-8


def test_rust_pi_backtest_matches_python_engine_on_mes_and_synthetic_edges(tmp_path):
    preset_name, preset, _preset_source = _load_default_preset()
    assert preset_name == "PI 2MES BOTH BEST"
    assert _hash(ROOT / "data" / "presets.json") == EXPECTED_PRESET_SHA256
    assert _hash(pi_history.HIST_PATH) == EXPECTED_PI_HISTORY_SHA256

    params = deepcopy(_build_strategy_params(preset, CONTRACT_MES))
    # This ticket freezes ordinary entries; continuation and LV2 replacement
    # have their own parity slices and stay outside this fill/ledger cohort.
    params.pi_continue_long_kinds = []
    params.pi_continue_short_kinds = []
    params.pi_continue_short_levels = []
    params.pi_lv2_replace_pi = False
    config = BacktestConfig(
        strategies=["pi"],
        symbol="MES",
        initial_capital=50000.0,
        commission_rt=get_commission_rt(CONTRACT_MES),
        fees_rt=get_fees_rt(CONTRACT_MES),
    )
    engine = BacktestEngine(config=config, strategy_params=params, record_equity=False)
    strategy = engine.trend_follow
    assert strategy._hist, "canonical PI loader must provide marks"
    first_mark = min(timestamp for timestamp, _ in strategy._hist)
    last_mark = max(timestamp for timestamp, _ in strategy._hist)
    snapshot = candle_store.load_snapshot("MES", 1)
    assert snapshot.bars, f"MES candle store is empty: {snapshot.source_path}"
    start = first_mark - timedelta(days=30)
    end = last_mark + timedelta(minutes=3)
    candles = [
        candle
        for candle in snapshot.bars
        if start <= as_utc(candle.timestamp) <= end
    ]
    assert len(candles) > 10_000
    candles = sorted(candles, key=lambda candle: candle.timestamp)
    actual_scenario, actual_expected = _capture_python_oracle(
        "canonical_mes_ordinary_pi",
        engine,
        candles,
        point_value=get_point_value(CONTRACT_MES),
        commission=get_commission_rt(CONTRACT_MES),
        fees=get_fees_rt(CONTRACT_MES),
    )
    assert len(actual_expected) >= 10, "real MES cohort should exercise multiple ordinary fills"

    scenarios = [actual_scenario]
    expected = {actual_scenario["name"]: (actual_expected, actual_scenario)}
    synthetic = _synthetic_scenarios()
    for scenario, trades in synthetic:
        scenarios.append(scenario)
        expected[scenario["name"]] = (trades, scenario)

    fixture = {
        "schema": "ancsertpx.live-runtime-pi-backtest.v1",
        "scenarios": scenarios,
    }
    fixture_path = tmp_path / "pi-backtest-parity.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    native = _run_native(fixture_path)
    assert native["schema"] == "ancsertpx.live-runtime-pi-backtest-result.v1"
    assert len(native["scenarios"]) == len(scenarios)
    native_by_name = {row["name"]: row for row in native["scenarios"]}
    for scenario_result in native["scenarios"]:
        python_trades, source_scenario = expected[scenario_result["name"]]
        _compare_scenario(scenario_result, python_trades, source_scenario)

    assert native_by_name["entry_candle_stop_only"]["trades"][0]["exit_reason"] == "sl"
    assert native_by_name["entry_candle_stop_only"]["trades"][0]["exit_price"] == 98.0
    target_only_trade = native_by_name["entry_candle_target_only"]["trades"][0]
    assert target_only_trade["exit_reason"] == "flatten"
    assert datetime.fromisoformat(target_only_trade["exit_time"]) > datetime.fromisoformat(
        target_only_trade["entry_time"]
    )
    assert target_only_trade["exit_price"] == 101.0
    assert native_by_name["both_hit_stop_nearer"]["trades"][0]["exit_reason"] == "sl"
    assert native_by_name["both_hit_target_nearer"]["trades"][0]["exit_reason"] == "tp"
    assert native_by_name["both_hit_tie_long"]["trades"][0]["exit_reason"] == "sl"
    assert native_by_name["both_hit_tie_short"]["trades"][0]["exit_reason"] == "sl"
    trail_trade = native_by_name["trail_move_then_stop"]["trades"][0]
    assert trail_trade["exit_reason"] == "trail_sl"
    assert trail_trade["final_stop_loss"] == 100.5
    assert native_by_name["directional_time_exit"]["trades"][0]["exit_reason"] == "flatten"
    assert native_by_name["session_flatten"]["trades"][0]["exit_reason"] == "flatten"
    assert abs(native_by_name["mes_quantity_costs"]["trades"][0]["net_pnl"] - 37.52) < 1e-8
    assert abs(native_by_name["mnq_quantity_costs"]["trades"][0]["net_pnl"] - 13.52) < 1e-8
    daily_trades = native_by_name["daily_loss_close_after_same_bar_reentry"]["trades"]
    assert [trade["exit_reason"] for trade in daily_trades] == ["sl", "flatten"]
    assert abs(daily_trades[0]["net_pnl"] + 11.24) < 1e-8
    assert abs(daily_trades[1]["net_pnl"] - 8.76) < 1e-8

    # Negative control: removing a still-open position's shared-kernel event
    # must fail instead of silently holding the position or inventing an exit.
    trail_scenario = next(
        scenario for scenario in scenarios if scenario["name"] == "trail_move_then_stop"
    )
    broken_scenario = deepcopy(trail_scenario)
    broken_scenario["exit_operations"].pop(0)
    broken_path = tmp_path / "pi-backtest-missing-exit-operation.json"
    broken_path.write_text(
        json.dumps(
            {"schema": fixture["schema"], "scenarios": [broken_scenario]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rejected = _invoke_native(broken_path)
    assert rejected.returncode != 0
    assert "missing shared exit operation" in rejected.stderr

    duplicate_scenario = deepcopy(trail_scenario)
    duplicate_scenario["exit_operations"].append(
        deepcopy(duplicate_scenario["exit_operations"][0])
    )
    duplicate_path = tmp_path / "pi-backtest-duplicate-exit-operation.json"
    duplicate_path.write_text(
        json.dumps({"schema": fixture["schema"], "scenarios": [duplicate_scenario]}),
        encoding="utf-8",
    )
    duplicate = _invoke_native(duplicate_path)
    assert duplicate.returncode != 0
    assert "duplicate exit operation" in duplicate.stderr

    unused_scenario = deepcopy(trail_scenario)
    unused_scenario["exit_operations"].append(
        {
            "trade_index": 0,
            "timestamp": unused_scenario["bars"][-1]["timestamp"],
            "action": "none",
            "reason": None,
            "stop_price": None,
            "trail_triggered": True,
            "ladder_max_r": 0.0,
            "ladder_lock_r": None,
        }
    )
    unused_path = tmp_path / "pi-backtest-unused-exit-operation.json"
    unused_path.write_text(
        json.dumps({"schema": fixture["schema"], "scenarios": [unused_scenario]}),
        encoding="utf-8",
    )
    unused = _invoke_native(unused_path)
    assert unused.returncode != 0
    assert "exit operation(s) were not consumed" in unused.stderr

    output = {
        "preset": preset_name,
        "preset_sha256": EXPECTED_PRESET_SHA256,
        "pi_history_sha256": EXPECTED_PI_HISTORY_SHA256,
        "mes_candle_store": str(snapshot.source_path),
        "mes_store_version": snapshot.version,
        "cohort_start": actual_scenario["bars"][0]["timestamp"],
        "cohort_end": actual_scenario["bars"][-1]["timestamp"],
        "mes_bars": len(actual_scenario["bars"]),
        "ordinary_entries": len(actual_scenario["entries"]),
        "ordinary_closed_trades": len(actual_expected),
        "python_exit_operations": len(actual_scenario["exit_operations"]),
        "synthetic_scenarios": [item[0]["name"] for item in synthetic],
    }
    print(json.dumps(output, ensure_ascii=True))
