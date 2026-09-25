from scripts.pi_vp_option_evidence_audit import _first_pass_counts, _pi_inventory


def test_first_pass_counts_preserve_same_bar_ambiguity():
    labels = (
        "down_only",
        "down_first_then_up",
        "up_only",
        "up_first_then_down",
        "same_bar_ambiguous",
        "neither",
    )
    events = [
        {"horizons": {"1": {"valid": True, "first_pass_0_5": label}}}
        for label in labels
    ]

    counts = _first_pass_counts(events, 1)

    assert counts["down_first"] == 2
    assert counts["up_first"] == 2
    assert counts["same_bar_ambiguous"] == 1
    assert counts["neither_threshold_hit"] == 1


def test_first_pass_summary_ignores_incomplete_horizons():
    events = [
        {"horizons": {"5": {"valid": True, "first_pass_0_5": "up_only"}}},
        {"horizons": {"5": {"valid": False, "first_pass_0_5": "down_only"}}},
    ]

    counts = _first_pass_counts(events, 5)

    assert counts["up_first"] == 1
    assert counts["down_first"] == 0


def test_focus_deep_blue_mark_in_multi_mark_message_is_reported_as_filtered():
    raw = [{
        "id": "aggregate-20260702",
        "symbol": "QQQ",
        "ts": "2026-07-02T17:06:00+00:00",
        "marks": [
            {"kind": "淡蓝圈", "level": 1},
            {"kind": "深蓝圈", "level": 2},
        ],
    }]

    inventory = _pi_inventory([], raw)

    assert inventory["excluded_focus_qqq_level2_deepblue_rows"][0]["date_ny"] == "2026-07-02"
    assert inventory["excluded_focus_qqq_level2_deepblue_rows"][0]["canonical_exclusion_reason"] == "multi_mark_aggregate"
