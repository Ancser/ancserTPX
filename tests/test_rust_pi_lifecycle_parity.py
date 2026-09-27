from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import MagicMock, Mock, patch

from backend.backtest.engine import BacktestConfig, BacktestEngine
from backend.db.models import Candle, Direction, StrategyParams, StrategyType, Trade, TradeSignal
from backend.live.engine import LiveTradingEngine
from backend.live.pi_listener import PiSignal
from backend.strategy.pi_signal import PiSignalStrategy
from backend.strategy.session_filter import MARKET_PHASE_FLATTEN, market_close_phase


ROOT = Path(__file__).resolve().parents[1]
RUST_WORKSPACE = ROOT / "research" / "rust_engine"
UTC = timezone.utc
CONTRACT = "CON.F.US.MNQ.Z26"


def _params(**overrides):
    values = {
        "strategy": "pi",
        "contract_id": CONTRACT,
        "contract_size": 1,
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
        "rr_ratio": 2.0,
        "tr_allowed_sessions": None,
    }
    values.update(overrides)
    return StrategyParams(**values)


def _strategy(monkeypatch, params=None):
    import backend.strategy.pi_signal as pi_signal_module

    monkeypatch.setattr(
        pi_signal_module,
        "_load_history",
        lambda replay_rows=None: [],
    )
    return PiSignalStrategy(params or _params())


def _candle(ts, *, price=100.0, high=None, low=None):
    return Candle(
        timestamp=datetime.fromisoformat(ts.replace("Z", "+00:00")),
        open=price,
        high=price + 0.5 if high is None else high,
        low=price - 0.5 if low is None else low,
        close=price,
        volume=10,
    )


def _config(strategy=None, *, account_id="123", contract_id=CONTRACT):
    strategy = strategy or PiSignalStrategy(_params())
    return {
        "strategy_mode": "pi",
        "long_only": strategy.pi_long_only,
        "long_kinds": list(strategy.pi_long_kinds),
        "short_kinds": list(strategy.pi_short_kinds),
        "short_levels": list(strategy.pi_short_levels)
        if strategy.pi_short_levels is not None else None,
        "continue_long_kinds": list(strategy.pi_continue_long_kinds),
        "continue_short_kinds": list(strategy.pi_continue_short_kinds),
        "continue_short_levels": list(strategy.pi_continue_short_levels)
        if strategy.pi_continue_short_levels is not None else None,
        "replacement_enabled": strategy.pi_lv2_replace_pi,
        "expected_account_id": account_id,
        "expected_contract_id": contract_id,
        "reopen_window_minutes": strategy.REOPEN_WINDOW_MINUTES,
    }


def _run_rust(fixture, tmp_path):
    cargo = shutil.which("cargo") or str(Path.home() / ".cargo" / "bin" / "cargo.exe")
    fixture_path = tmp_path / "pi-lifecycle.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    run = subprocess.run(
        [
            cargo, "run", "--offline", "--quiet", "-p", "ancsertpx-live-runtime",
            "--", "pi-lifecycle", str(fixture_path),
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


def _pi_source(direction="buy", kind="深蓝圈", level=2, *, continuation=False, replacement=False):
    return {
        "direction": direction,
        "kind": kind,
        "level": level,
        "continuation": continuation,
        "replacement": replacement,
    }


def _python_source(source):
    direction = {
        "buy": Direction.BUY,
        "long": Direction.BUY,
        "1": Direction.BUY,
        "sell": Direction.SELL,
        "short": Direction.SELL,
        "-1": Direction.SELL,
    }.get(str(source["direction"]).lower())
    meta = {"pi": {"kind": source["kind"], "level": source.get("level")}}
    if source.get("continuation"):
        meta["pi_continuation"] = {"reentries_for_source": 1}
    if source.get("replacement"):
        meta["pi_replacement"] = {"source_level": 2}
    return {"direction": direction, "meta": meta}


def test_native_pi_selectors_match_python_and_fresh_lv2_mark_gate(tmp_path, monkeypatch):
    strategy = _strategy(monkeypatch)
    sources = [
        ("selected_deep_blue", _pi_source()),
        ("unselected_kind", _pi_source(kind="淡蓝圈", level=1)),
        ("already_continued", _pi_source(continuation=True)),
        ("selected_short_purple", _pi_source("sell", "紫圈", 2)),
        ("wrong_short_level", _pi_source("sell", "紫圈", 1)),
        ("active_replacement_source", _pi_source("buy", "青π", 3)),
        ("prior_replacement_source", _pi_source("buy", "青π", 3, replacement=True)),
        ("selected_lv2_mark", _pi_source("buy", "深蓝圈", 2)),
        ("visual_only_mark", _pi_source("buy", "深蓝圈", None)),
        ("level_one_mark", _pi_source("buy", "深蓝圈", 1)),
        ("short_mark", _pi_source("sell", "深蓝圈", 2)),
    ]
    fixture = {
        "schema": "ancsertpx.live-runtime-pi-lifecycle.v1",
        "config": _config(strategy),
        "source_cases": [
            {"id": case_id, "source": source} for case_id, source in sources
        ],
    }
    native = _run_rust(fixture, tmp_path)
    results = {row["id"]: row for row in native["source_results"]}
    for case_id, source in sources:
        actual_source = _python_source(source)
        assert results[case_id]["continuation_enabled"] == strategy.continuation_enabled(actual_source)
        assert results[case_id]["replacement_source_active"] == strategy.replacement_source_active(actual_source)

    # Exercise the actual Python queue consumer for the four structured Level-2 cases.
    mark_rows = [
        ("selected_lv2_mark", _pi_source("buy", "深蓝圈", 2), True),
        ("visual_only_mark", _pi_source("buy", "深蓝圈", None), False),
        ("level_one_mark", _pi_source("buy", "深蓝圈", 1), False),
        ("short_mark", _pi_source("sell", "深蓝圈", 2), False),
    ]
    active = {
        "direction": Direction.BUY,
        "meta": {"pi": {"kind": "青π", "level": 3, "message_id": "active"}},
    }
    for index, (case_id, source, expected) in enumerate(mark_rows):
        candidate_strategy = _strategy(monkeypatch)
        candidate_strategy._atr_blend = Mock(return_value=2.0)
        timestamp = datetime(2026, 9, 21, 19, 0, tzinfo=UTC)
        mark = PiSignal(
            message_id=f"mark-{index}", ts=timestamp, equity="QQQ", future="MNQ",
            direction=1 if source["direction"] == "buy" else -1,
            kind=source["kind"], size="visual-size", pos=None,
            level=source["level"],
        )
        accepted = candidate_strategy.push(mark)
        selected = (
            candidate_strategy.take_lv2_replacement(
                _candle("2026-09-21T19:00:00Z"), active,
            )
            if accepted else None
        )
        assert (selected is not None) is expected, case_id
        assert results[case_id]["replacement_mark_selected"] is expected


def _backtest_engine(monkeypatch):
    import backend.strategy.pi_signal as pi_signal_module

    monkeypatch.setattr(
        pi_signal_module,
        "_load_history",
        lambda replay_rows=None: [],
    )
    return BacktestEngine(
        config=BacktestConfig(strategies=["trend"], symbol="MNQ"),
        strategy_params=_params(),
        record_equity=False,
    )


def _live_engine(tmp_path, params=None):
    with patch("backend.live.engine.EMAPMOSignalMessenger.from_env", return_value=MagicMock()):
        engine = LiveTradingEngine(
            MagicMock(), account_id=123, contract_id=CONTRACT,
            strategy_params=params or _params(),
        )
    engine._pi_reopen_state_file = str(tmp_path / "pi-reopen.json")
    return engine


def _arm_cases():
    source = _pi_source()
    pi = {
        "kind": "深蓝圈", "level": 2, "message_id": "source",
        "equity": "QQQ", "size": "Level 2", "pos": "upper",
    }
    position = {
        "source_entry": 100.0,
        "active_sl": 90.0,
        "active_tp": 120.0,
        "original_sl": 90.0,
        "original_tp": 120.0,
        "source_reason": "PI selected source",
    }
    return [
        {
            "id": "backtest_flatten_boundary",
            "surface": "backtest",
            "source": source,
            "pi": pi,
            "position": position,
            "bar": {"timestamp_utc": "2026-09-21T19:45:00Z", "high": 100.5, "low": 99.5, "close": 100.0},
        },
        {
            "id": "live_outer_flatten_trigger_with_prior_bar",
            "surface": "live",
            "arm_time_utc": "2026-09-21T19:45:00Z",
            "flat_time": "2026-09-21T19:45:00Z",
            "source": source,
            "pi": pi,
            "position": position,
            "bar": {"timestamp_utc": "2026-09-21T19:44:00Z", "high": 100.5, "low": 99.5, "close": 100.0},
        },
        {
            "id": "bracket_touched_on_flatten",
            "surface": "backtest",
            "source": source,
            "pi": pi,
            "position": position,
            "bar": {"timestamp_utc": "2026-09-21T19:45:00Z", "high": 100.5, "low": 90.0, "close": 100.0},
        },
        {
            "id": "outside_flatten_window",
            "surface": "backtest",
            "source": source,
            "pi": pi,
            "position": position,
            "bar": {"timestamp_utc": "2026-09-21T19:44:00Z", "high": 100.5, "low": 99.5, "close": 100.0},
        },
        {
            "id": "source_not_selected",
            "surface": "backtest",
            "source": _pi_source(kind="青π", level=3),
            "pi": {**pi, "kind": "青π", "level": 3},
            "position": position,
            "bar": {"timestamp_utc": "2026-09-21T19:45:00Z", "high": 100.5, "low": 99.5, "close": 100.0},
        },
        {
            "id": "zero_source_risk",
            "surface": "backtest",
            "source": source,
            "pi": pi,
            "position": {**position, "original_sl": 100.0},
            "bar": {"timestamp_utc": "2026-09-21T19:45:00Z", "high": 100.5, "low": 99.5, "close": 100.0},
        },
    ]


def _python_arm(case, monkeypatch, tmp_path):
    bar = case["bar"]
    candle = _candle(
        bar["timestamp_utc"], price=bar["close"], high=bar["high"], low=bar["low"],
    )
    source = case["source"]
    position = case["position"]
    side = Direction.BUY if source["direction"] == "buy" else Direction.SELL
    pi_meta = case.get("pi") or {
        "kind": source["kind"], "level": source.get("level"), "message_id": "source",
    }
    if source.get("continuation"):
        continuation_meta = {"reentries_for_source": 1}
    else:
        continuation_meta = None
    if case["surface"] == "backtest":
        engine = _backtest_engine(monkeypatch)
        meta = {"pi": pi_meta, "signal_reason": position["source_reason"]}
        if continuation_meta:
            meta["pi_continuation"] = continuation_meta
        engine._open_position = Trade(
            trade_id="source", strategy=StrategyType.TREND_FOLLOW,
            direction=side, entry_price=position["source_entry"],
            entry_time=candle.timestamp, sl_price=position["active_sl"],
            tp_price=position["active_tp"], original_sl_price=position["original_sl"],
            original_tp_price=position["original_tp"], contracts=1, point_value=2.0,
            contract_id=CONTRACT, meta=meta,
        )
        engine._arm_pi_reopen_ticket(candle)
        ticket = engine._pi_reopen_ticket
        return ticket

    engine = _live_engine(tmp_path / case["id"])
    if market_close_phase(datetime.fromisoformat(case["arm_time_utc"].replace("Z", "+00:00"))) != MARKET_PHASE_FLATTEN:
        return None
    signal = TradeSignal(
        strategy=StrategyType.TREND_FOLLOW, direction=side,
        entry_price=position["source_entry"], sl_price=position["active_sl"],
        tp_price=position["active_tp"], zone_id="source",
        reason=position["source_reason"], order_type="market", meta={"pi": pi_meta},
    )
    if continuation_meta:
        signal.meta["pi_continuation"] = continuation_meta
    signal.original_sl_price = position["original_sl"]
    signal.original_tp_price = position["original_tp"]
    signal.original_entry_price = position["source_entry"]
    engine._active_signal = signal
    engine._open_position = {"contractId": CONTRACT, "size": 1}
    engine._fill_price = position["source_entry"]
    flat_time = datetime.fromisoformat(case["flat_time"].replace("Z", "+00:00"))
    engine._arm_pi_reopen_ticket(candle, flat_time)
    return engine._pi_reopen_ticket


def test_native_pi_ticket_arming_matches_backtest_and_live_callers(tmp_path, monkeypatch):
    cases = _arm_cases()
    strategy = _strategy(monkeypatch)
    fixture = {
        "schema": "ancsertpx.live-runtime-pi-lifecycle.v1",
        "config": _config(strategy),
        "arm_cases": cases,
    }
    native = _run_rust(fixture, tmp_path)
    results = {row["id"]: row for row in native["arm_results"]}
    for case in cases:
        ticket = _python_arm(case, monkeypatch, tmp_path)
        result = results[case["id"]]
        assert result["armed"] == (ticket is not None), case["id"]
        if ticket is None:
            continue
        native_ticket = result["ticket"]
        pi = ticket.get("pi") or ticket.get("source", {})
        assert native_ticket["direction"] == ticket["direction"].lower()
        assert native_ticket["source_entry"] == ticket["source_entry"]
        assert native_ticket["original_sl"] == ticket["original_sl"]
        assert native_ticket["original_tp"] == ticket["original_tp"]
        expected_risk = (
            ticket.get("risk", abs(ticket["source_entry"] - ticket["original_sl"]))
            if case["surface"] == "backtest" else None
        )
        assert native_ticket.get("risk") == expected_risk
        assert native_ticket["flat_price"] == ticket["flat_price"]
        assert datetime.fromisoformat(
            native_ticket["flat_time"].replace("Z", "+00:00")
        ) == datetime.fromisoformat(ticket["flat_time"].replace("Z", "+00:00"))
        assert native_ticket["source_reason"] == ticket["source_reason"]
        assert native_ticket["pi"] == pi
        if case["surface"] == "live":
            assert native_ticket["version"] == ticket["version"]
            assert native_ticket["account_id"] == str(ticket["account_id"])
            assert native_ticket["contract_id"] == ticket["contract_id"]
        else:
            assert native_ticket.get("version") is None
            assert native_ticket.get("account_id") is None
            assert native_ticket.get("contract_id") is None


def _ticket(surface="backtest", *, account_id="123", contract_id=CONTRACT):
    return {
        "version": 1 if surface == "live" else None,
        "direction": "buy",
        "source_entry": 100.0,
        "original_sl": 90.0,
        "original_tp": 120.0,
        "risk": 10.0,
        "flat_price": 100.0,
        "flat_time": "2026-09-21T19:45:00Z",
        "pi": {"kind": "深蓝圈", "level": 2, "message_id": "source-1"},
        "source_reason": "PI selected source",
        "account_id": int(account_id) if surface == "live" else None,
        "contract_id": contract_id if surface == "live" else None,
    }


def _ticket_cases():
    def step(action, timestamp, *, surface="backtest", high=100.5, low=99.5, position_open=False, pending_order=False):
        return {
            "action": action,
            "surface": surface,
            "position_open": position_open,
            "pending_order": pending_order,
            "bar": {"timestamp_utc": timestamp, "high": high, "low": low, "close": 100.0},
        }

    same = "2026-09-21T22:05:00Z"
    return [
        {"id": "exact_five_minute_claim", "ticket": _ticket(), "steps": [step("update", same), step("claim", same)]},
        {"id": "late_expiry", "ticket": _ticket(), "steps": [step("update", "2026-09-21T22:06:00Z")]},
        {"id": "flat_stop_touch", "ticket": _ticket(), "steps": [step("update", "2026-09-21T20:15:00Z", low=89.5)]},
        {"id": "flat_target_touch", "ticket": _ticket(), "steps": [step("update", "2026-09-21T20:15:00Z", high=120.5)]},
        {
            "id": "short_flat_stop_touch",
            "ticket": {
                **_ticket(), "direction": "sell", "original_sl": 110.0,
                "original_tp": 80.0,
                "pi": {"kind": "紫圈", "level": 2},
            },
            "steps": [step("update", "2026-09-21T20:15:00Z", high=110.5)],
        },
        {
            "id": "short_flat_target_touch",
            "ticket": {
                **_ticket(), "direction": "sell", "original_sl": 110.0,
                "original_tp": 80.0,
                "pi": {"kind": "紫圈", "level": 2},
            },
            "steps": [step("update", "2026-09-21T20:15:00Z", low=79.5)],
        },
        {"id": "missed_same_day", "ticket": _ticket(), "steps": [step("update", "2026-09-22T22:00:00Z")]},
        {"id": "one_shot_claim", "ticket": _ticket(), "steps": [step("update", same), step("claim", same), step("claim", "2026-09-21T22:06:00Z")]},
        {"id": "before_reopen", "ticket": _ticket(), "steps": [step("update", "2026-09-21T21:59:00Z"), step("claim", "2026-09-21T21:59:00Z")]},
        {"id": "live_identity_mismatch", "ticket": _ticket("live", contract_id="CON.F.US.MNQ.U26"), "steps": [step("update", "2026-09-21T22:00:00Z", surface="live")]},
        {
            "id": "live_persisted_ticket_shape",
            "ticket": {
                key: value for key, value in _ticket("live").items()
                if key not in ("source", "risk")
            },
            "steps": [step("update", "2026-09-21T22:00:00Z", surface="live")],
        },
        {
            "id": "live_selector_changed",
            "ticket": {
                **_ticket("live"),
                "pi": {"kind": "淡蓝圈", "level": 1},
            },
            "steps": [step("update", "2026-09-21T22:00:00Z", surface="live")],
        },
        {"id": "live_position_not_flat", "ticket": _ticket("live"), "steps": [step("update", "2026-09-21T22:00:00Z", surface="live", position_open=True)]},
        {"id": "live_pending_order_blocks_claim", "ticket": _ticket("live"), "steps": [step("claim", "2026-09-21T22:00:00Z", surface="live", pending_order=True)]},
        {"id": "dst_reopen_claim", "ticket": {**_ticket(), "flat_time": "2026-03-09T19:45:00Z"}, "steps": [step("claim", "2026-03-09T22:05:00Z")]},
    ]


def _python_ticket_case(case, tmp_path, monkeypatch):
    surface = case["steps"][0]["surface"]
    ticket = case["ticket"]
    if surface == "backtest":
        engine = _backtest_engine(monkeypatch)
        engine._pi_reopen_ticket = {
            **ticket,
        }
        engine._pi_reopen_ticket.pop("source", None)
        log_messages = []
    else:
        engine = _live_engine(tmp_path / case["id"])
        engine._pi_reopen_ticket = {
            "version": 1,
            "account_id": int(ticket["account_id"]) if ticket["account_id"] else None,
            "contract_id": ticket["contract_id"],
            "direction": ticket["direction"],
            "source_entry": ticket["source_entry"],
            "original_sl": ticket["original_sl"],
            "original_tp": ticket["original_tp"],
            "flat_price": ticket["flat_price"],
            "flat_time": ticket["flat_time"],
            "pi": ticket["pi"],
            "source_reason": ticket["source_reason"],
        }
        log_messages = []
        engine._log_event = lambda message, *args, **kwargs: log_messages.append(message)

    out = []
    for row in case["steps"]:
        before = engine._pi_reopen_ticket is not None
        candle = _candle(
            row["bar"]["timestamp_utc"], high=row["bar"]["high"], low=row["bar"]["low"],
        )
        engine._open_position = ({"contractId": CONTRACT, "size": 1} if row.get("position_open") else None)
        engine._pending_order_id = 50 if row.get("pending_order") else None
        claimed = None
        if row["action"] == "update":
            engine._update_pi_reopen_ticket(candle)
        elif surface == "live":
            claimed = engine._take_pi_reopen_ticket(candle)
        else:
            engine._update_pi_reopen_ticket(candle)
            claimed = engine._take_pi_reopen_ticket(candle)

        if claimed is not None:
            status, reason = "claimed", None
        elif not before:
            status, reason = "empty", None
        elif engine._pi_reopen_ticket is not None:
            status, reason = "pending", None
        else:
            message = log_messages[-1] if log_messages else ""
            if surface == "backtest":
                event = engine.pi_reopen_events[-1] if engine.pi_reopen_events else {}
                message = str(event.get("reason", ""))
                if event.get("event") == "expired":
                    status = "expired"
                else:
                    status = "invalidated"
            else:
                status = "invalidated" if any(
                    token in message for token in ("identity", "position", "stop", "target", "selected")
                ) else "expired"
            reason = None
        out.append({"status": status, "reason": reason, "claimed": claimed is not None})
    return out, engine._pi_reopen_ticket is not None


def test_native_pi_ticket_transitions_match_python_backtest_and_live(tmp_path, monkeypatch):
    cases = _ticket_cases()
    strategy = _strategy(monkeypatch)
    fixture = {
        "schema": "ancsertpx.live-runtime-pi-lifecycle.v1",
        "config": _config(strategy),
        "ticket_cases": cases,
    }
    native = _run_rust(fixture, tmp_path)
    results = {row["id"]: row for row in native["ticket_results"]}
    for case in cases:
        expected_steps, expected_remaining = _python_ticket_case(case, tmp_path, monkeypatch)
        result = results[case["id"]]
        assert result["ticket_remaining"] is expected_remaining, case["id"]
        assert len(result["steps"]) == len(expected_steps)
        for index, (actual, expected) in enumerate(zip(result["steps"], expected_steps)):
            assert actual["status"] == expected["status"], {
                "case": case["id"], "step": index,
                "python": expected, "rust": actual,
            }
            assert (actual["claimed_ticket"] is not None) is expected["claimed"], {
                "case": case["id"], "step": index, "rust": actual,
            }

    exact_claim = results["exact_five_minute_claim"]["steps"][-1]["claimed_ticket"]
    assert exact_claim is not None, "a positive exact-five-minute claim is required"
    assert results["one_shot_claim"]["steps"][-1]["status"] == "empty"
    assert results["late_expiry"]["steps"][0]["status"] == "expired"
