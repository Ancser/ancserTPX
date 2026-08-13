"""A PI sweep and a PI backtest must see the same signals.

1.0.10p. `/backtest` overlays the live audit on top of the archived PI file
(`routes.py` -> `load_replay_rows` -> `BacktestEngine(pi_replay_rows=...)`).
`run_pi_sweep` did not, so the same strategy with the same parameters saw a
different signal set depending on which button was pressed — the sweep stopped
at the last archival run of `scripts/pi_collect_history.py` while a single
backtest saw days more. Nothing failed; the sweep just quietly scored an older
world. Measured on 2026-08-12: 11 trades vs 12, PF 3.173 vs 3.259.

There is a second-order trap here. The sweep trims candles to the span where
signals exist, and if that span is measured from the archived file alone it
ends before any newer live mark — the overlay then loads correctly and still
contributes nothing, which is indistinguishable from "no new signals".
"""
from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta, timezone

from backend.backtest import sweep


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_run_one_forwards_replay_rows_to_the_engine():
    assert "pi_replay_rows" in inspect.signature(sweep._run_one).parameters
    body = _src(sweep._run_one)
    assert "pi_replay_rows=pi_replay_rows" in body, (
        "_run_one accepts the rows but does not hand them to BacktestEngine"
    )


def test_pi_sweep_resolves_the_overlay_and_passes_it_to_every_variant():
    body = _src(sweep.run_pi_sweep)
    assert "_pi_replay_overlay(" in body
    assert "pi_replay_rows=replay" in body, (
        "variants must run with the overlay, not just resolve it"
    )
    # Resolved once for the whole sweep, not per variant: 600 variants each
    # re-reading the audit file would dominate a 1.7s-per-variant run.
    assert body.count("_pi_replay_overlay(") == 1


def test_signal_window_is_sized_from_both_sources():
    """The window must be able to reach past the last archived signal."""
    assert "extra_stamps" in inspect.signature(sweep._pi_signal_window).parameters
    call = re.search(r"window = _pi_signal_window\(([^)]*)\)", _src(sweep.run_pi_sweep))
    assert call, "run_pi_sweep no longer sizes a window"
    assert call.group(1).strip() != "candles", (
        "window sized from the archived file only; live marks newer than the "
        "last archival run would fall outside it and score nothing"
    )

    overlay = _src(sweep.run_pi_sweep)
    assert overlay.index("_pi_replay_overlay(") < overlay.index("_pi_signal_window("), (
        "the overlay has to be resolved BEFORE the window, or the window "
        "cannot be widened to cover it"
    )


class _Bar:
    def __init__(self, ts):
        self.timestamp = ts


def test_window_widens_to_include_a_live_mark_past_the_archive():
    """Behavioural half: a stamp after the archive must widen the window."""
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    candles = [_Bar(base + timedelta(hours=i)) for i in range(24 * 30)]

    archived_only = sweep._pi_signal_window(candles)
    late = candles[-1].timestamp - timedelta(hours=2)
    with_live = sweep._pi_signal_window(candles, [late])

    # Positive assertion: the helper must actually be trimming something here,
    # otherwise "widened" below would be trivially true.
    assert len(archived_only) < len(candles), "window did not trim at all"
    assert len(with_live) >= len(archived_only)
    assert with_live[-1].timestamp >= late, (
        "a live mark past the archived span was left outside the window"
    )


def test_overlay_reads_the_full_range_not_the_window():
    """Asking the window for the overlay would be circular."""
    body = _src(sweep._pi_replay_overlay)
    assert "candles[0].timestamp" in body and "candles[-1].timestamp" in body
    assert "window" not in inspect.signature(sweep._pi_replay_overlay).parameters


def test_overlay_degrades_to_empty_rather_than_raising():
    """A missing or unreadable audit must not take the whole sweep down."""
    class _Broken:
        timestamp = "not-a-datetime"

    assert sweep._pi_replay_overlay([], object()) == []
    # Bad params -> load_replay_rows raises -> caught, sweep still runs.
    assert sweep._pi_replay_overlay([_Broken(), _Broken()], object()) == []
