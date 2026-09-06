import numpy as np
import pandas as pd

import scripts.option_wall_candidate_robustness_study as study


def test_session_bootstrap_keeps_inactive_sessions_as_zero(monkeypatch):
    monkeypatch.setattr(study, "BOOTSTRAP_DRAWS", 100)
    frame = pd.DataFrame({
        "date": ["2026-01-02", "2026-01-04"],
        "pnl": [10.0, -2.0],
    })
    result = study._session_bootstrap(
        frame, ["2026-01-02", "2026-01-03", "2026-01-04"], seed=7,
    )
    assert result["sessions"] == 3
    assert result["active_sessions"] == 2


def test_temporal_thirds_use_session_clock_not_trade_count():
    frame = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-06"],
        "pnl": [10.0, 20.0],
    })
    result = study._temporal_thirds(
        frame, [f"2026-01-0{day}" for day in range(1, 7)],
    )
    assert result["third_1"]["trades"] == 1
    assert result["third_2"]["trades"] == 0
    assert result["third_3"]["trades"] == 1
