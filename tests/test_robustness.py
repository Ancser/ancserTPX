"""Robustness evaluation moved out of the browser (1.0.10p).

These functions were only reachable through the rendered RESEARCH panel, so
pytest could not touch them and the sweep could not gate on them. The port has
to keep the numbers identical to the JS it replaces, so most of what is pinned
here is arithmetic, not plumbing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.backtest.robustness import (
    DEFAULT_SEED,
    PF_NO_LOSSES,
    evaluate,
    monte_carlo,
    monte_carlo_passes,
    monthly_pnl,
    series_stats,
    walk_forward,
)

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "frontend" / "static" / "ancserTPX.js").read_text(encoding="utf-8")


def _trades(pnls, start_day=1):
    """Trades one day apart so walk-forward's date split is predictable."""
    return [
        {"entry_time": f"2026-06-{start_day + i:02d}T15:00:00+00:00", "pnl": p}
        for i, p in enumerate(pnls)
    ]


# ── series stats ────────────────────────────────────────────────────────

def test_series_stats_matches_the_js_equity_walk():
    # gains 30, losses 20 -> PF 1.5; equity 10,-10,20,10 -> peak 20, dd 10
    stats = series_stats([10, -20, 30, -10, 10])
    assert stats["n"] == 5
    assert stats["pnl"] == pytest.approx(20.0)
    assert stats["pf"] == pytest.approx(50 / 30)
    assert stats["max_dd"] == pytest.approx(20.0)
    assert stats["win"] == pytest.approx(3 / 5)


def test_profit_factor_sentinel_replaces_infinity_when_nothing_loses():
    assert series_stats([5, 5])["pf"] == PF_NO_LOSSES
    assert series_stats([])["pf"] == 0.0
    assert series_stats([-5])["pf"] == 0.0


# ── monte carlo ─────────────────────────────────────────────────────────

def test_monte_carlo_needs_at_least_ten_trades():
    assert monte_carlo([1.0] * 9) is None
    assert monte_carlo([1.0] * 10) is not None


def test_monte_carlo_is_reproducible_unlike_the_math_random_version():
    """The whole point of moving it: the browser used Math.random().

    Same trades rendered twice gave different percentiles, so no Monte Carlo
    number in a report could be checked by anyone, including the next session.
    """
    pnls = [12, -8, 30, -14, 6, -3, 21, -9, 4, 17, -22, 8]
    a = monte_carlo(pnls, iters=200, seed=7)
    b = monte_carlo(pnls, iters=200, seed=7)
    assert a == b
    assert monte_carlo(pnls, iters=200, seed=8) != a


def test_monte_carlo_brackets_the_observed_result():
    """Positive assertion: the bootstrap must actually produce a distribution.

    Without this, every gate below would still pass on an implementation that
    returned a degenerate all-zeros result.
    """
    pnls = [50, -20, 40, -10, 60, -30, 25, -15, 35, -5, 45, -25]
    mc = monte_carlo(pnls, iters=500, seed=DEFAULT_SEED)
    assert mc["n"] == len(pnls)
    assert mc["pnl_p5"] < mc["pnl_p50"] < mc["pnl_p95"], "distribution collapsed"
    assert mc["dd_p50"] <= mc["dd_p95"]
    assert 0.0 <= mc["p_loss"] <= 1.0
    assert mc["pf_p5"] > 0


def test_monte_carlo_pass_requires_all_three_gates():
    base = {"p_loss": 0.01, "dd_p95": 500.0, "pf_p5": 1.4, "dd_threshold": 2000.0}
    assert monte_carlo_passes(base)
    assert not monte_carlo_passes({**base, "p_loss": 0.20})
    assert not monte_carlo_passes({**base, "dd_p95": 2500.0})
    assert not monte_carlo_passes({**base, "pf_p5": 0.9})
    assert not monte_carlo_passes(None)


# ── walk forward ────────────────────────────────────────────────────────

def test_walk_forward_splits_by_date_span_not_by_trade_count():
    """A strategy that dies halfway must fail, and equal-count cannot see that.

    Front-loaded trades: 8 in the first third, 1 in the last. Equal-count
    thirds would spread the healthy trades across all three segments and
    report a pass.
    """
    rows = [{"entry_time": f"2026-06-{d:02d}T15:00:00+00:00", "pnl": 100}
            for d in range(1, 9)]
    rows.append({"entry_time": "2026-06-30T15:00:00+00:00", "pnl": -400})
    wf = walk_forward(rows)
    assert [s["n"] for s in wf["segments"]] == [8, 0, 1]
    assert wf["pass"] is False


def test_walk_forward_passes_only_when_every_segment_is_profitable():
    good = walk_forward(_trades([50, -10, 40, -8, 60, -12, 30, -5, 45]))
    assert good["pass"] is True
    assert all(s["pnl"] > 0 for s in good["segments"])

    bad = walk_forward(_trades([50, -10, 40, -8, 60, -12, -90, -80, -70]))
    assert bad["pass"] is False


def test_walk_forward_declines_rather_than_guessing_on_thin_samples():
    assert walk_forward(_trades([1, 2, 3, 4, 5])) is None
    assert walk_forward([]) is None
    # entry_time missing on every row -> no usable dates
    assert walk_forward([{"pnl": 1} for _ in range(10)]) is None


def test_walk_forward_agrees_with_the_sweep_three_way_split():
    """sweep.py keeps its own inline split over day-keyed aggregates.

    Two implementations of one concept is what let the frontend and the sweep
    drift apart in the first place. They must at least agree on the boundary
    rule: `min(segments-1, offset * segments // span)`.
    """
    from backend.backtest import sweep
    src = sweep.__file__
    text = Path(src).read_text(encoding="utf-8")
    assert "seg = min(2, int(off * 3 / span_days))" in text, (
        "sweep.py's walk-forward split changed shape; re-check it against "
        "backend.backtest.robustness.walk_forward before trusting either"
    )


# ── normalisation + top level ───────────────────────────────────────────

def test_monthly_pnl_normalises_runs_of_different_length():
    two_months = monthly_pnl(6000, "2026-01-01T00:00:00+00:00",
                             "2026-03-01T00:00:00+00:00")
    seven_months = monthly_pnl(6000, "2026-01-01T00:00:00+00:00",
                               "2026-08-01T00:00:00+00:00")
    assert two_months > seven_months
    assert monthly_pnl(100, "2026-01-01T00:00:00+00:00",
                       "2026-01-01T06:00:00+00:00") is None


def test_evaluate_returns_every_panel_field_in_one_call():
    result = evaluate(_trades([50, -20, 40, -10, 60, -30, 25, -15, 35, -5, 45, -25]),
                      iters=200)
    assert result["trades"] == 12
    assert result["stats"]["n"] == 12
    assert result["monte_carlo"] is not None
    assert result["walk_forward"] is not None
    assert result["monthly_pnl"] is not None
    assert isinstance(result["monte_carlo_pass"], bool)


# ── the frontend must stop computing this itself ────────────────────────

def test_frontend_no_longer_owns_a_second_monte_carlo():
    """The port is only a win once the browser copy goes away.

    Leaving both means two implementations that drift — exactly the situation
    walk-forward was already in (sweep.py plus the panel).
    """
    code = re.sub(r"//[^\n]*", "", JS)
    for gone in ("function _robMonteCarlo(", "function _robWalkForward(",
                 "function _robSeriesStats(", "function _robMcPass(",
                 "function _robCachedMonteCarlo("):
        assert gone not in code, f"frontend still defines {gone!r}"
    # Math.random() was the reason no reported percentile could be rechecked.
    assert "Math.random()" not in code

    # Positive half: it must actually call the endpoint, or "no local copy"
    # would also pass on a panel that silently renders nothing.
    assert "'/research/robustness'" in JS
    assert "_robFetchBackend" in JS


def test_robustness_endpoint_returns_every_field_the_panel_reads():
    """The panel indexes these by name; a rename here blanks a card."""
    from fastapi.testclient import TestClient
    from backend.main import app

    trades = [
        {"entry_time": f"2026-06-{1 + i:02d}T15:00:00+00:00",
         "pnl": p, "size": 1, "symbol": "MNQ"}
        for i, p in enumerate([50, -20, 40, -10, 60, -30, 25, -15, 35, -5, 45, -25])
    ]
    with TestClient(app) as client:
        resp = client.post("/api/research/robustness",
                           json={"trades": trades, "iters": 200,
                                 "slip_levels": [1, 2, 4, 8]})
    assert resp.status_code == 200
    body = resp.json()

    assert body["trades"] == 12
    for key in ("pnl", "pf", "max_dd", "win", "n"):
        assert key in body["stats"]
    for key in ("pnl_p5", "pnl_p50", "pnl_p95", "p_loss",
                "dd_p50", "dd_p95", "p_dd_breach", "pf_p5"):
        assert key in body["monte_carlo"], key
    assert isinstance(body["monte_carlo_pass"], bool)
    assert len(body["walk_forward"]["segments"]) == 3
    assert body["span_months"] is not None
    assert body["monthly_pnl"] is not None
    assert [row["level"] for row in body["slip"]["levels"]] == [1, 2, 4, 8]
    assert body["slip"]["tick_value"] == pytest.approx(0.5)   # MNQ: $2 x 0.25


def test_robustness_endpoint_survives_an_empty_or_pnl_less_request():
    from fastapi.testclient import TestClient
    from backend.main import app

    with TestClient(app) as client:
        empty = client.post("/api/research/robustness", json={"trades": []})
        no_pnl = client.post("/api/research/robustness",
                             json={"trades": [{"entry_time": "2026-06-01T00:00:00Z"}]})
    for resp in (empty, no_pnl):
        assert resp.status_code == 200
        assert resp.json()["trades"] == 0
