from __future__ import annotations

from scripts import orderflow_filter_exit_study as study
from scripts import orderflow_pi_relationship_study as relationship


def _trade(net_pnl: float, points: float, reason: str = "time", bars_held: int = 1):
    return {
        "net_pnl": net_pnl,
        "directional_points": points,
        "exit_reason": reason,
        "bars_held": bars_held,
    }


def test_metrics_reports_net_pf_drawdown_and_qualification():
    result = study._metrics(
        [_trade(10.0, 5.0, "tp"), _trade(-5.0, -2.5, "sl"), _trade(5.0, 2.5)],
        day_count=2,
    )

    assert result["net_pnl"] == 10.0
    assert result["max_drawdown"] == -5.0
    assert result["max_drawdown_abs"] == 5.0
    assert result["pf"] == 3.0
    assert result["win_rate"] == 0.6667
    assert result["qualified"] is True


def test_size_band_is_side_specific_and_reads_v5_cells():
    # Serialized v5 row: tick, buy, sell, passive bid/ask, max bid/ask,
    # refill bid/ask, then buy 50/100/150 and sell 50/100/150 quantities.
    cell = [80000, 0, 0, 0, 0, 0, 0, 0, 0, 11, 22, 33, 44, 55, 66]
    bar = {"cells": [cell]}

    assert study._band_qty(bar, 1, "50_99") == 44
    assert study._band_qty(bar, 1, "100_149") == 55
    assert study._band_qty(bar, -1, "50_99") == 11
    assert study._band_qty(bar, -1, "150_plus") == 33


def test_simulate_resolves_stop_and_time_exit_from_completed_ohlc():
    stop = study._simulate(
        [{"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}],
        0, 1, 100.0, 99.0, 103.0,
    )
    timed = study._simulate(
        [
            {"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.25},
            {"open": 100.25, "high": 101.0, "low": 100.0, "close": 100.75},
        ],
        0, 1, 100.0, None, None,
    )

    assert stop == {"exit_price": 99.0, "exit_reason": "sl", "bars_held": 1}
    assert timed == {"exit_price": 100.75, "exit_reason": "time", "bars_held": 2}


def test_simulate_stops_at_book_only_bar_after_rth_ohlc():
    result = study._simulate(
        [{"open": 100.0, "high": 101.0, "low": 99.5, "close": 100.25},
         {"open": None, "high": None, "low": None, "close": None}],
        0, 1, 100.0, None, None,
    )

    assert result == {"exit_price": 100.25, "exit_reason": "time", "bars_held": 1}


def test_enrichment_keeps_book_only_tail_without_inventing_ohlc():
    day = {
        "date": "2026-09-07", "schema_version": 5,
        "bars": [
            {"epoch": 1788800000, "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.5, "buy": 2, "sell": 1, "cells": []},
            {"epoch": 1788800060, "open": None, "high": None, "low": None,
             "close": None, "buy": 0, "sell": 0, "cells": []},
        ],
    }

    relationship._enrich_day(day, {})

    assert day["bars"][0]["ohlc_valid"] is True
    assert day["bars"][1]["ohlc_valid"] is False
    assert day["bars"][1]["near_day_high_hindsight"] is False
    assert relationship._candidate_events(day["bars"][1], []) == []


def test_combined_exit_policy_uses_atr_for_long_and_time_for_short():
    assert study._levels(100.0, 1, 2.0, "long_atr_short_time") == (92.0, 124.0)
    assert study._levels(100.0, -1, 2.0, "long_atr_short_time") == (None, None)


def test_position_occupancy_resets_for_each_trading_date():
    rows = []
    for day, epoch in (("2026-08-10", 1_000), ("2026-08-11", 86_400 + 1_000)):
        rows.append({
            "family": "mbo_turnover", "rule": "test", "filter": "none",
            "date": day, "signal_epoch": epoch, "entry_index": 0,
            "direction": 1, "pi_message_id": None,
            "bars": [{"epoch": epoch, "open": 100.0, "high": 100.5,
                      "low": 99.5, "close": 100.25}],
        })

    summary, trades = study._run_variant(rows, "time_60m", [], day_count=2)

    assert summary["trades"] == 2
    assert [trade["date"] for trade in trades] == ["2026-08-10", "2026-08-11"]
