from backend.backtest.shadow_replay import _day_snapshot_id


def test_shadow_replay_uses_strategy_snapshot_without_trade_ledger():
    snapshots = {
        "old": {
            "account_id": "1",
            "created_at": "2026-07-16T16:00:00+00:00",
        },
        "current": {
            "account_id": "1",
            "created_at": "2026-07-17T16:00:00+00:00",
        },
        "other-account": {
            "account_id": "2",
            "created_at": "2026-07-18T16:00:00+00:00",
        },
    }

    assert _day_snapshot_id("2026-07-16", "1", snapshots) == "old"
    assert _day_snapshot_id("2026-07-17", "1", snapshots) == "current"
    assert _day_snapshot_id("2026-07-17", "2", snapshots) is None
