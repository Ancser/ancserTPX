from datetime import datetime, timezone

from scripts.volume_profile_context_study import (
    _delta_state,
    _flow_features,
    _join_gex,
)


def _bars(deltas, *, passive_side=0):
    rows = []
    for index, delta in enumerate(deltas):
        rows.append(
            {
                "epoch": 1_700_000_000 + index * 60,
                "volume": 100,
                "delta": delta,
                "ofi": delta,
                "imbalance_1_10_side": 0,
                "passive_rejection_side": passive_side,
            }
        )
    return rows


def test_delta_state_distinguishes_opposing_and_local_decay():
    assert _delta_state(1, -1, 1.0) == "opposing"
    assert _delta_state(1, 1, 0.5) == "aligned_decaying"
    assert _delta_state(1, 1, 1.5) == "aligned_accelerating"
    assert _delta_state(1, 0, 1.5) == "neutral"


def test_flow_features_use_only_the_completed_five_minute_window():
    bars = _bars([10, 10, 10, 10, 10, 10, 10, 10, 3, 3, 20, 20], passive_side=1)
    features = _flow_features(bars, 9, 1)
    assert features is not None
    assert features["mbo_delta_decay_ratio"] == 0.2
    assert features["mbo_delta_state"] == "aligned_decaying"
    assert features["mbo_consolidation_flow"] is True

    with_future = _flow_features(bars, 9, 1)
    bars[10]["delta"] = -10_000
    bars[11]["delta"] = -10_000
    assert with_future == _flow_features(bars, 9, 1)


def test_gex_join_rejects_snapshot_that_was_not_available_at_entry():
    snapshot_epoch = int(datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc).timestamp())
    rows = {
        "2026-08-07": {
            "oi": {
                "gex_label": "negative",
                "gex_state": -1,
                "gex_value_shift": "above",
                "gex_entry_location": "above",
                "gex_snapshot_epoch": snapshot_epoch,
            }
        }
    }
    before = _join_gex({"trade_date": "2026-08-07", "entry_epoch": snapshot_epoch - 60}, rows)
    after = _join_gex({"trade_date": "2026-08-07", "entry_epoch": snapshot_epoch}, rows)
    assert before["gex_available"] is False
    assert after["gex_available"] is True
    assert after["gex_oi_label"] == "negative"
