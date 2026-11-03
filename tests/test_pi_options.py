"""PI options model: execution rules, backtest, account/alerts, and guards.

Real-data parity pins the backend to the published studies (RESEARCH_REPORT
§4.5-4.6); synthetic paths pin each rule, with a positive case for every
"does not trade" case.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from backend.options import pi_options as po
from backend.options import pi_options_account as acct

ET = po.NEW_YORK
UTC = timezone.utc


def _epoch(y, m, d, hh, mm):
    return int(datetime(y, m, d, hh, mm, tzinfo=ET).timestamp())


def _signal(points, *, strike="otm_3", tier="LV3", day=(2026, 7, 30), exit_day=(2026, 7, 31)):
    return {"symbol": "QQQ", "date": date(*day).isoformat(), "tier": tier,
            "entry_epoch": points[0][0], "exit_date": date(*exit_day).isoformat(),
            "expiration": date(*exit_day).isoformat(), "spot": 600.0,
            "strikes": {strike: 603.0}, "paths": {strike: [list(p) for p in points]}}


def _path(entry_ask=2.0, later=((1.5, 1.55),), cap_bid=3.0, exit_time=(15, 30)):
    """Entry minute, some later minutes, then the next-session exit minute."""
    t0 = _epoch(2026, 7, 30, 15, 0)
    points = [(t0, entry_ask - 0.05, entry_ask, True, True)]
    for i, (bid, ask) in enumerate(later, 1):
        points.append((t0 + 60 * i, bid, ask, True, True))
    points.append((_epoch(2026, 7, 31, *exit_time), cap_bid, cap_bid + 0.05, True, True))
    return points


# ── rules ──────────────────────────────────────────────────────────────────

def test_market_entry_holds_to_the_next_session_exit():
    out = po.simulate_signal(_signal(_path()), po.PiOptionSettings(contracts=None))
    assert out["status"] == "ok" and out["reason"] == "time"
    assert (out["fill"], out["exit"], out["contracts"]) == (2.0, 3.0, 5)      # $1,000 / $200
    assert out["net"] == pytest.approx((3.0 - 2.0) * 100 * 5 - 1.30 * 5)


def test_default_size_is_one_contract_per_signal_whatever_the_premium():
    cheap = po.simulate_signal(_signal(_path(entry_ask=0.5, cap_bid=1.0)), po.PiOptionSettings())
    rich = po.simulate_signal(_signal(_path(entry_ask=15.0, cap_bid=16.0)), po.PiOptionSettings())
    assert (cheap["contracts"], rich["contracts"]) == (1, 1)
    assert rich["net"] == pytest.approx((16.0 - 15.0) * 100 - 1.30)
    assert po.simulate_signal(_signal(_path()), po.PiOptionSettings(contracts=3))["contracts"] == 3
    with pytest.raises(ValueError):
        po.PiOptionSettings(contracts=0).validate()


def test_limit_fills_only_when_the_ask_trades_down_to_it():
    filled = po.simulate_signal(_signal(_path(later=((1.1, 1.15), (1.15, 1.19)))),
                                po.PiOptionSettings(entry_discount=0.4))
    assert filled["status"] == "ok" and filled["fill"] == 1.2
    missed = po.simulate_signal(_signal(_path(later=((1.5, 1.55),))),
                                po.PiOptionSettings(entry_discount=0.4))
    assert missed["status"] == "limit_not_filled"


def test_stop_sells_at_the_observed_bid_and_target_at_the_target():
    stop = po.simulate_signal(_signal(_path(later=((0.5, 0.55),), cap_bid=4.0)),
                              po.PiOptionSettings(stop=0.6))
    assert (stop["reason"], stop["exit"]) == ("sl", 0.5)                      # gap below 0.80
    target = po.simulate_signal(_signal(_path(later=((4.5, 4.55),))),
                                po.PiOptionSettings(target=1.0))
    assert (target["reason"], target["exit"]) == ("tp", 4.0)


def test_budget_below_one_contract_and_tier_filter_skip():
    rich = _signal(_path(entry_ask=15.0))
    out = po.run_backtest(po.PiOptionSettings(contracts=None, budget=1000), {"signals": [rich]})
    assert out["summary"]["skipped"] == {"premium_above_budget": 1}
    assert po.run_backtest(po.PiOptionSettings(), {"signals": [rich]})["summary"]["trades"] == 1
    lv2_only = po.run_backtest(po.PiOptionSettings(tiers=("LV2",)), {"signals": [_signal(_path())]})
    assert lv2_only["summary"]["signals"] == 0
    both = po.run_backtest(po.PiOptionSettings(), {"signals": [_signal(_path())]})
    assert both["summary"]["trades"] == 1                                      # positive control


def test_settings_reject_bad_values():
    with pytest.raises(ValueError):
        po.PiOptionSettings(strike="otm_99").validate()
    with pytest.raises(ValueError):
        po.PiOptionSettings(exit_time="12:00").validate()


# ── real data: the backend reproduces the published studies ────────────────
# The exit-grid study sized each signal to a $1,000 premium budget; the
# limit-entry study counted one contract per signal (the default size).

@pytest.mark.parametrize("args,expected", [
    (dict(strike="otm_3"), 25605),
    (dict(strike="atm", stop=0.6), 17613),
    (dict(strike="atm", exit_time="10:00"), 9353),
    (dict(strike="otm_10"), 57035),
    (dict(strike="otm_3", stop=0.4), 18098),
])
def test_real_cache_reproduces_the_exit_grid_study(args, expected):
    cache = po.load_cache()
    if cache is None:
        pytest.skip("PI option path cache not built on this machine")
    settings = po.PiOptionSettings(**args, contracts=None, budget=1000)
    assert round(po.run_backtest(settings, cache)["summary"]["total"]) == expected


def test_real_cache_one_contract_reproduces_the_limit_entry_market_row():
    cache = po.load_cache()
    if cache is None:
        pytest.skip("PI option path cache not built on this machine")
    summary = po.run_backtest(po.PiOptionSettings(strike="atm"), cache)["summary"]
    assert round(summary["total"]) == 8421                # RESEARCH_REPORT §4.5, market, per contract


def test_real_cache_reproduces_the_limit_entry_fill_counts():
    cache = po.load_cache()
    if cache is None:
        pytest.skip("PI option path cache not built on this machine")
    fills = [po.run_backtest(po.PiOptionSettings(strike="atm", entry_discount=d, contracts=None),
                             cache)["summary"]["trades"]
             for d in (0.2, 0.4, 0.6)]
    assert fills == [45, 38, 29]                      # 80% / 68% / 52% of 56 signals


# ── API ────────────────────────────────────────────────────────────────────

def test_endpoint_returns_backtest_shaped_payload(monkeypatch):
    from backend.api import routes

    monkeypatch.setattr(po, "load_cache", lambda path=None: {"signals": [_signal(_path())], "window": {}})
    body = asyncio.run(routes.backtest_pi_options(routes.PiOptionsBacktestRequest()))
    assert body["strategy"] == "pi_options"
    assert body["metrics"]["total_trades"] == 1
    row = body["trades"][0]
    assert row["symbol"].startswith("QQQ 603C") and row["size"] == 1 and row["exit_reason"] == "time"
    by_budget = asyncio.run(routes.backtest_pi_options(routes.PiOptionsBacktestRequest(contracts=None)))
    assert by_budget["trades"][0]["size"] == 5                                # $1,000 / $200
    with pytest.raises(HTTPException) as bad:
        asyncio.run(routes.backtest_pi_options(routes.PiOptionsBacktestRequest(strike="otm_99")))
    assert bad.value.status_code == 400


def test_endpoint_lists_every_signal_including_unfilled_limits(monkeypatch):
    from backend.api import routes

    fills = _signal(_path(later=((1.1, 1.15),)))                               # ask trades to 1.20
    misses = _signal(_path(later=((1.5, 1.55),)), day=(2026, 7, 29), exit_day=(2026, 7, 30))
    misses["entry_epoch"] -= 86400
    misses["paths"]["otm_3"] = [[p[0] - 86400, *p[1:]] for p in misses["paths"]["otm_3"]]
    monkeypatch.setattr(po, "load_cache", lambda path=None: {"signals": [fills, misses], "window": {}})
    body = asyncio.run(routes.backtest_pi_options(routes.PiOptionsBacktestRequest(entry_discount=0.4)))
    rows = body["option_rows"]
    assert [r["status"] for r in rows] == ["limit_not_filled", "filled"]      # by signal time
    missed, filled = rows
    assert (missed["signal_ask"], missed["limit"], missed["net"]) == (2.0, 1.2, 0.0)
    assert (filled["signal_ask"], filled["limit"], filled["entry_price"]) == (2.0, 1.2, 1.2)
    assert filled["entry_time"] > filled["signal_time"]                       # limit filled later
    assert sum(r["net"] for r in rows) == pytest.approx(body["options_summary"]["total"])
    assert len(body["trades"]) == 1                                           # metrics still count fills


def test_futures_paths_refuse_the_options_model():
    from backend.api import routes

    with pytest.raises(HTTPException) as bt:
        asyncio.run(routes.run_backtest(routes.BacktestRequest(strategy="pi_options")))
    assert bt.value.status_code == 400
    with pytest.raises(HTTPException) as live:
        asyncio.run(routes.live_start(routes.LiveStartRequest(account_id=1, strategy="pi_options")))
    assert live.value.status_code == 400 and "options account" in live.value.detail


# ── account: calendar, tickets, alerts ─────────────────────────────────────

def test_next_session_skips_weekends_holidays_and_good_friday():
    assert acct.next_session(date(2026, 9, 25)) == date(2026, 9, 28)          # Fri -> Mon
    assert acct.next_session(date(2027, 3, 25)) == date(2027, 3, 29)          # Good Friday 3/26
    assert acct.next_session(date(2026, 9, 4)) == date(2026, 9, 8)            # Labor Day
    assert acct.next_session(date(2026, 9, 28)) == date(2026, 9, 29)          # positive: plain day


def _row(kind="青π", equity="QQQ", direction=1, minute=(14, 5), day=(2026, 9, 28), mid="m1"):
    ts = datetime(*day, *minute, tzinfo=ET).astimezone(UTC)
    return {"event": "recorded", "equity": equity, "direction": direction,
            "side": "long" if direction > 0 else "short", "kind": kind,
            "ts": ts.isoformat(), "received_at": (ts + timedelta(seconds=80)).isoformat(),
            "message_id": mid}


def test_ticket_for_a_long_lv3_and_refusals():
    settings = po.PiOptionSettings()
    ticket = acct.build_ticket(_row(), settings)
    assert ticket["right"] == "CALL" and ticket["expiration"] == "2026-09-29"
    assert "spot + $3" in ticket["strike_rule"] and ticket["exit_at"].startswith("2026-09-29T15:30")
    assert "; 1 contract;" in ticket["text"] and ticket["contracts"] == 1
    assert "budget $1,000" in acct.build_ticket(_row(), po.PiOptionSettings(contracts=None))["text"]
    assert acct.build_ticket(_row(direction=-1, kind="粉π"), settings) is None      # short
    assert acct.build_ticket(_row(kind="淡蓝圈"), settings) is None                 # LV1
    assert acct.build_ticket(_row(minute=(9, 10)), settings) is None               # pre-market
    assert acct.build_ticket(_row(equity="SPX"), settings) is None


def test_runner_emits_one_fresh_ticket_per_symbol_and_day(monkeypatch, tmp_path):
    log = tmp_path / "tickets.jsonl"
    monkeypatch.setattr(acct, "tickets_path", lambda: log)     # never the real runtime log
    rows = [_row(mid="a"), _row(mid="b", minute=(14, 6)), _row(mid="c", equity="SPY"),
            _row(mid="d", minute=(13, 0))]                                     # d is stale
    now = datetime(2026, 9, 28, 14, 8, tzinfo=ET).astimezone(UTC)
    runner = acct.PiOptionsAlertRunner(reader=lambda: rows, now=lambda: now)
    runner.started_at = now - timedelta(minutes=30)
    new = runner.poll_once(po.PiOptionSettings())
    assert [t["symbol"] for t in new] == ["QQQ", "SPY"]
    assert runner.poll_once(po.PiOptionSettings()) == []                        # no repeats
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_account_saves_alert_mode_and_refuses_broker_modes(tmp_path):
    path = tmp_path / "account.json"
    saved = acct.save_account("alert", True, {"strike": "atm", "stop": 0.6}, path=path)
    assert saved["enabled"] is True and saved["settings"]["strike"] == "atm"
    assert json.loads(path.read_text())["settings"]["stop"] == 0.6
    assert saved["settings"]["contracts"] == 1                                 # default size
    assert acct.save_account("alert", True, {"contracts": None}, path=path)["settings"]["contracts"] is None
    assert acct.save_account("alert", True, {"contracts": 2}, path=path)["settings"]["contracts"] == 2
    for mode in ("paper", "live"):
        with pytest.raises(ValueError):
            acct.save_account(mode, True, {}, path=path)
