from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

from backend.db.models import Candle, Direction, StrategyParams
from backend.live.pi_listener import PiSignal
from backend.strategy.pi_signal import PiSignalStrategy
from backend.timebase import topstep_trade_date


ROOT = Path(__file__).resolve().parents[1]
RUST_WORKSPACE = ROOT / "research" / "rust_engine"
UTC = timezone.utc
CONTRACT = "CON.F.US.MNQ.Z26"


def _params(**overrides):
    values = {
        "strategy": "pi",
        "contract_id": CONTRACT,
        "contract_size": 2,
        "pi_long_only": False,
        "pi_long_kinds": ["青π", "深蓝圈"],
        "pi_short_kinds": ["粉π", "紫圈"],
        "pi_short_levels": [2],
        "pi_continue_long_kinds": ["深蓝圈"],
        "pi_continue_short_kinds": ["紫圈"],
        "pi_continue_short_levels": [2],
        "pi_lv2_replace_pi": True,
        "pi_reopen_max_gap_r": 1.0,
        "factor_sl_value": 1.0,
        "pi_short_sl_value": 2.5,
        "rr_ratio": 2.0,
        "factor_max_trades_per_day": 3,
        "factor_side_mode": "all",
        "pi_max_signal_age_min": 5,
    }
    values.update(overrides)
    return StrategyParams(**values)


def _strategy(monkeypatch, params):
    import backend.strategy.pi_signal as pi_signal_module

    monkeypatch.setattr(
        pi_signal_module, "_load_history", lambda replay_rows=None: [],
    )
    return PiSignalStrategy(params)


def _python_candle(row):
    return Candle(
        timestamp=datetime.fromisoformat(row["ts"].replace("Z", "+00:00")),
        open=row["open"], high=row["high"], low=row["low"],
        close=row["close"], volume=row.get("volume", 10),
    )


def _rust_params(strategy, params):
    from backend.db.models import get_tick_size

    return {
        "future": strategy.pi_future,
        "contract_size": params.contract_size,
        "tick_size": get_tick_size(params.contract_id),
        "timeframe_minutes": strategy.tf_minutes,
        "sl_atr": strategy.sl_atr,
        "rr_ratio": strategy.rr,
        "max_trades_per_day": strategy.max_trades_per_day,
        "long_only": strategy.pi_long_only,
        "long_kinds": list(strategy.pi_long_kinds),
        "short_kinds": list(strategy.pi_short_kinds),
        "short_levels": strategy.pi_short_levels,
        "short_sl": strategy.pi_short_sl,
        "max_signal_age_minutes": strategy.pi_max_age_min,
        "side_mode": params.factor_side_mode,
        "exit": {
            "long_hold_minutes": strategy.pi_long_hold,
            "short_hold_minutes": strategy.pi_short_hold,
            "trail_enabled": bool(params.tr_trail_enabled),
            "trail_trigger_pct": float(params.tr_trail_trigger_pct),
            "trail_offset_ticks": int(params.tr_trail_sl_ticks),
            "tp_ticks": int(params.tr_tp_ticks),
            "exit_mode": params.tr_exit_mode,
        },
    }


def _lifecycle(strategy):
    return {
        "strategy_mode": "pi",
        "long_only": strategy.pi_long_only,
        "long_kinds": list(strategy.pi_long_kinds),
        "short_kinds": list(strategy.pi_short_kinds),
        "short_levels": strategy.pi_short_levels,
        "continue_long_kinds": list(strategy.pi_continue_long_kinds),
        "continue_short_kinds": list(strategy.pi_continue_short_kinds),
        "continue_short_levels": strategy.pi_continue_short_levels,
        "replacement_enabled": strategy.pi_lv2_replace_pi,
        "expected_account_id": "123",
        "expected_contract_id": CONTRACT,
        "reopen_window_minutes": strategy.REOPEN_WINDOW_MINUTES,
    }


def _run_rust(fixture, tmp_path):
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo.exe")
    fixture_path = tmp_path / "pi-signal-builders.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    run = subprocess.run(
        [
            cargo, "run", "--offline", "--quiet", "-p", "ancsertpx-live-runtime",
            "--", "pi-signal-builders", str(fixture_path),
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


def _source(direction, kind, level, *, continuation=False, replacement=False):
    return {
        "direction": direction,
        "kind": kind,
        "level": level,
        "continuation": continuation,
        "replacement": replacement,
    }


def _ticket(direction="buy", *, flat_price=100.0, source_kind=None, source_level=None):
    source_kind = source_kind or ("深蓝圈" if direction == "buy" else "紫圈")
    source_level = source_level or 2
    return {
        "version": 1,
        "direction": direction,
        "source_entry": 100.0,
        "original_sl": 90.0 if direction == "buy" else 110.0,
        "original_tp": 120.0 if direction == "buy" else 80.0,
        "risk": 10.0,
        "flat_price": flat_price,
        "flat_time": "2026-09-21T19:45:00+00:00",
        "pi": {
            "message_id": "source-1", "equity": "QQQ",
            "kind": source_kind, "level": source_level, "size": "Level 2",
        },
        "source_reason": "PI QQQ 深蓝圈/Level 2",
        "account_id": 123,
        "contract_id": CONTRACT,
    }


def _bar(ts, close):
    return {
        "ts": ts,
        "open": close,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": 10,
    }


def _case(case_id, action, *, ts, close, atr=2.0, trades=0, ticket=None,
          gap=1.0, mark=None, active=None, active_pi=None):
    return {
        "id": case_id,
        "action": action,
        "candle": _bar(ts, close),
        "atr_blend": atr,
        "trades_used_today": trades,
        "max_gap_r": gap,
        "ticket": ticket,
        "mark": mark,
        "active_source": active,
        "active_pi": active_pi or {},
    }


def _mark(*, ts, kind="深蓝圈", level=2, direction=1, future="MNQ", size="Level 2"):
    return {
        "ts": ts,
        "message_id": "lv2-1",
        "equity": "QQQ",
        "future": future,
        "direction": direction,
        "kind": kind,
        "size": size,
        "level": level,
        "pos": "upper",
    }


def _python_build(case, strategy, params):
    candle = _python_candle(case["candle"])
    ts = candle.timestamp
    trade_date = topstep_trade_date(ts)
    strategy._daily[trade_date] = case["trades_used_today"]
    strategy._atr_blend = Mock(return_value=case["atr_blend"])
    if case["action"] == "reopen":
        ticket = case["ticket"]
        if ticket is None:
            return None, False, False, case["trades_used_today"]
        signal = strategy.build_reopen_continuation(
            ticket, candle, max_gap_r=case["max_gap_r"],
        )
        return signal, True, False, case["trades_used_today"] + int(signal is not None)

    active_source = case["active_source"]
    mark = case["mark"]
    if active_source is None:
        return None, False, False, case["trades_used_today"]
    active = {
        "direction": Direction.BUY if active_source["direction"] == "buy" else Direction.SELL,
        "meta": {"pi": case["active_pi"]},
    }
    if active_source.get("continuation"):
        active["meta"]["pi_continuation"] = {"reentries_for_source": 1}
    if active_source.get("replacement"):
        active["meta"]["pi_replacement"] = {"source_level": 2}
    if mark is None:
        return None, False, False, case["trades_used_today"]
    pushed = strategy.push(PiSignal(
        message_id=mark["message_id"],
        ts=datetime.fromisoformat(mark["ts"].replace("Z", "+00:00")),
        equity=mark["equity"], future=mark["future"], direction=mark["direction"],
        kind=mark["kind"], size=mark["size"], pos=mark["pos"], level=mark["level"],
    ))
    if not pushed:
        return None, False, False, case["trades_used_today"]
    if not strategy.replacement_source_active(active):
        return None, False, False, case["trades_used_today"]
    signal = strategy.take_lv2_replacement(candle, active)
    return signal, False, True, case["trades_used_today"] + int(signal is not None)


def _assert_signal_parity(case, actual, expected, expected_consumed, expected_mark_consumed, trades_after, params, strategy):
    case_id = case["id"]
    assert actual["ticket_consumed"] is expected_consumed, case_id
    assert actual["mark_consumed"] is expected_mark_consumed, case_id
    assert actual["trades_used_today_after"] == trades_after, case_id
    rust_signal = actual["signal"]
    if expected is None:
        assert rust_signal is None, {"case": case_id, "rust": rust_signal}
        return
    assert rust_signal is not None, case_id
    assert rust_signal["direction"] == expected.direction.value
    assert rust_signal["entry"] == expected.entry_price
    assert rust_signal["stop_loss"] == expected.sl_price
    assert rust_signal["take_profit"] == expected.tp_price
    assert rust_signal["quantity"] == params.contract_size
    assert rust_signal["reason"] == expected.reason
    assert rust_signal["zone_id"] == expected.zone_id
    assert rust_signal["trade_date"] == topstep_trade_date(_python_candle(case["candle"]).timestamp)
    assert rust_signal["atr_blend"] == case["atr_blend"]
    assert rust_signal["meta"] == expected.meta
    expected_width = case["atr_blend"]
    if expected.direction == Direction.SELL and strategy.sl_atr > 0:
        expected_width *= strategy.pi_short_sl / strategy.sl_atr
    assert rust_signal["risk_width"] == expected_width
    assert abs(expected.entry_price - expected.sl_price) == expected_width * strategy.sl_atr


def test_native_reopen_and_lv2_replacement_signals_match_python(tmp_path, monkeypatch):
    params = _params()
    strategy = _strategy(monkeypatch, params)
    source_ticket = _ticket()
    short_ticket = _ticket("sell", flat_price=100.0)
    active_pi = {"kind": "青π", "level": 3, "message_id": "active-1", "equity": "SPY"}
    active_source = _source("buy", "青π", 3)
    now = "2026-09-21T22:00:00Z"
    cases = [
        _case("long_reopen_exact_gap", "reopen", ts=now, close=110.0, ticket=source_ticket),
        _case("long_reopen_gap_over_cap", "reopen", ts=now, close=110.25, ticket=source_ticket),
        _case("long_reopen_rounding_tie", "reopen", ts=now, close=100.125, ticket=source_ticket),
        _case("short_reopen", "reopen", ts=now, close=90.0, ticket=short_ticket),
        _case("reopen_atr_not_warm", "reopen", ts=now, close=100.0, atr=None, ticket=source_ticket),
        _case("reopen_selector_rejected", "reopen", ts=now, close=100.0,
              ticket={
                  **source_ticket,
                  "source": _source("buy", "青π", 3),
                  "pi": {**source_ticket["pi"], "kind": "青π", "level": 3},
              }),
        _case("reopen_zero_source_risk", "reopen", ts=now, close=100.0,
              ticket={**source_ticket, "original_sl": 100.0, "risk": 0.0}),
        _case("reopen_daily_cap", "reopen", ts=now, close=100.0, trades=3, ticket=source_ticket),
        _case("replacement_fresh_lv2", "replacement", ts=now, close=100.125,
              mark=_mark(ts=now), active=active_source, active_pi=active_pi),
        _case("replacement_stale_mark", "replacement", ts=now, close=100.0,
              mark=_mark(ts="2026-09-21T21:54:00Z"), active=active_source, active_pi=active_pi),
        _case("replacement_exact_age_limit", "replacement", ts=now, close=100.0,
              mark=_mark(ts="2026-09-21T21:55:00Z"), active=active_source, active_pi=active_pi),
        _case("replacement_atr_not_warm", "replacement", ts=now, close=100.0,
              atr=None, mark=_mark(ts=now), active=active_source, active_pi=active_pi),
        _case("replacement_visual_level_only", "replacement", ts=now, close=100.0,
              mark=_mark(ts=now, level=None, size="Level 2"), active=active_source, active_pi=active_pi),
        _case("replacement_level_one", "replacement", ts=now, close=100.0,
              mark=_mark(ts=now, level=1), active=active_source, active_pi=active_pi),
        _case("replacement_unselected_kind", "replacement", ts=now, close=100.0,
              mark=_mark(ts=now, kind="淡蓝圈", level=2), active=active_source, active_pi=active_pi),
        _case("replacement_wrong_future", "replacement", ts=now, close=100.0,
              mark=_mark(ts=now, future="MES"), active=active_source, active_pi=active_pi),
        _case("replacement_bad_active_source", "replacement", ts=now, close=100.0,
              mark=_mark(ts=now), active=_source("buy", "深蓝圈", 2),
              active_pi={"kind": "深蓝圈", "level": 2, "message_id": "active-2"}),
        _case("replacement_continuation_source", "replacement", ts=now, close=100.0,
              mark=_mark(ts=now), active=_source("buy", "青π", 3, continuation=True),
              active_pi=active_pi),
        _case("replacement_prior_replacement_source", "replacement", ts=now, close=100.0,
              mark=_mark(ts=now), active=_source("buy", "青π", 3, replacement=True),
              active_pi=active_pi),
        _case("replacement_daily_cap", "replacement", ts=now, close=100.0, trades=3,
              mark=_mark(ts=now), active=active_source, active_pi=active_pi),
    ]
    fixture = {
        "schema": "ancsertpx.live-runtime-pi-signal-builders.v1",
        "params": _rust_params(strategy, params),
        "lifecycle": _lifecycle(strategy),
        "cases": cases,
    }
    native = _run_rust(fixture, tmp_path)
    results = {row["id"]: row for row in native["results"]}

    for case in cases:
        case_strategy = _strategy(monkeypatch, params)
        expected, ticket_used, mark_used, trades_after = _python_build(case, case_strategy, params)
        _assert_signal_parity(
            case, results[case["id"]], expected, ticket_used, mark_used,
            trades_after, params, case_strategy,
        )
