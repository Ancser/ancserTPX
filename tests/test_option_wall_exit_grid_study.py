import numpy as np

from scripts.option_wall_exit_grid_study import (
    CURRENT_LONG,
    CURRENT_SHORT,
    LONG_SL_GRID,
    LONG_TP_GRID,
    SHORT_SL_GRID,
    SHORT_TP_GRID,
    _basic_metrics,
    _monthly_plateau_walk_forward,
    _neighbor_lists,
    _side_configs,
)


def test_exit_grid_contains_current_asymmetric_three_r_and_smaller_levels():
    long_configs = _side_configs(LONG_SL_GRID, LONG_TP_GRID)
    short_configs = _side_configs(SHORT_SL_GRID, SHORT_TP_GRID)
    assert CURRENT_LONG in long_configs
    assert CURRENT_SHORT in short_configs
    assert min(LONG_SL_GRID) < CURRENT_LONG[0]
    assert min(SHORT_SL_GRID) < CURRENT_SHORT[0]
    assert (4.0, None) in long_configs
    assert (1.5, None) in short_configs


def test_basic_metrics_keeps_chronological_drawdown_and_profit_factor():
    metrics = _basic_metrics(np.array([10.0, -5.0, -8.0, 20.0]))
    assert metrics["net_pnl"] == 17.0
    assert metrics["pf"] == 30.0 / 13.0
    assert metrics["max_drawdown"] == -13.0


def test_neighbors_are_bounded_and_include_self():
    coordinates = list(np.ndindex(2, 2, 2, 2))
    groups = _neighbor_lists(coordinates, (2, 2, 2, 2))
    assert len(groups) == 16
    assert all(len(group) == 16 for group in groups)
    assert all(index in group for index, group in enumerate(groups))


def test_monthly_walk_forward_does_not_select_before_prior_month_requirement():
    dates = np.asarray([
        "2026-01-05", "2026-02-05", "2026-03-05", "2026-04-05", "2026-05-05",
    ])
    matrix = np.asarray([
        [1.0, 1.0, 1.0, 2.0, 2.0],
        [-1.0, -1.0, -1.0, 100.0, 100.0],
    ])
    rows = [
        {"long_sl_atr": 1.0, "long_tp_atr": None, "short_sl_atr": 1.0, "short_tp_atr": None},
        {"long_sl_atr": 2.0, "long_tp_atr": None, "short_sl_atr": 2.0, "short_tp_atr": None},
    ]
    result = _monthly_plateau_walk_forward(
        matrix, dates, rows, [np.array([0]), np.array([1])], current_index=0,
        prior_months=3,
    )
    assert [row["test_month"] for row in result["selected_by_month"]] == ["2026-04", "2026-05"]
    assert result["selected_by_month"][0]["long_sl_atr"] == 1.0
