"""Contracts for the constrained prop-intraday tuning report."""

from types import SimpleNamespace

from scripts.prop_intraday_micro_tune import _result_row


def _empty_summary():
    stats = {"n": 0, "pnl": 0.0, "pf": 0.0, "max_dd": 0.0, "win": 0.0}
    return {
        "stats": stats,
        "slip_14t": stats,
        "verdict": "INSUFFICIENT_SAMPLE",
        "walk_forward": None,
        "monte_carlo": None,
        "monte_carlo_pass": False,
        "position_size": {},
    }


def test_result_rows_do_not_alias_the_callers_parameter_dict():
    params = {"risk_dollars": 100.0}
    result = SimpleNamespace(
        symbol="MNQ",
        variant="alias_contract",
        config={},
        diagnostics={},
        trades=(),
        summary=_empty_summary(),
    )

    row = _result_row(result, "test", params)
    row["logical_params"]["risk_dollars"] = 150.0

    assert params == {"risk_dollars": 100.0}
    assert row["logical_params"] == {"risk_dollars": 150.0}
