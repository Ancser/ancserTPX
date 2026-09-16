from types import SimpleNamespace

from scripts import price_action_databento_orderflow_study as study


def _flow_bar(epoch: int, *, index: int, current: bool = False) -> dict:
    return {
        "epoch": epoch + index * 60,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 20 if current else 10,
        "delta": 2 if current else 0,
        "ofi": 1 if current else 0,
        "cvd_state_side": 1 if current else 0,
        "cvd_5_delta": 5 if current else 0,
        "imbalance_1_10_side": 1 if current else 0,
        "passive_rejection_side": 1 if current else 0,
        "queue_imbalance": 0.30 if current else 0.0,
        "buy_ge_150": 2 if current else 0,
        "sell_ge_150": 0,
        "vwap_state": "above" if current else "unknown",
    }


def test_flow_window_requires_five_completed_consecutive_minutes():
    start = 1_800_000_000
    bars = [_flow_bar(start, index=index, current=index >= 30) for index in range(35)]
    day = {"bars": bars}

    window, index = study._flow_window(day, bars[-1]["epoch"])

    assert index == 34
    assert [bar["epoch"] for bar in window] == [start + index * 60 for index in range(30, 35)]

    missing = {"bars": [bar for bar in bars if bar["epoch"] != start + 32 * 60]}
    assert study._flow_window(missing, bars[-1]["epoch"]) is None


def test_flow_features_are_directional_and_ignore_future_minutes():
    start = 1_800_000_000
    bars = [_flow_bar(start, index=index, current=index >= 30) for index in range(35)]
    day = {"bars": bars}
    signal = SimpleNamespace(close=101.0, vwap=100.0)
    signal_epoch = bars[-1]["epoch"]

    before = study._flow_features(day, signal_epoch, signal, tick=0.25)

    # A large, opposite future minute must not alter a snapshot at minute 34.
    day["bars"].append({
        **_flow_bar(start, index=35, current=True),
        "delta": -10_000,
        "ofi": -10_000,
        "cvd_state_side": -1,
        "cvd_5_delta": -10_000,
    })
    after = study._flow_features(day, signal_epoch, signal, tick=0.25)

    assert before == after
    assert before["mbo_delta_side"] == 1
    assert before["mbo_cvd_side"] == 1
    assert before["mbo_volume_burst"] is True
    assert before["mbo_queue_side"] == 1
    assert before["pa_vwap_side"] == 1
    assert before["mbo_full_confirmation"] is True

    full = next(context for context in study.flow_context_catalog() if context.name == "mbo_full_confirmation")
    vwap_delta = next(context for context in study.flow_context_catalog() if context.name == "pa_vwap+mbo_delta")
    assert study.context_matches(full, before, 1)
    assert study.context_matches(vwap_delta, before, 1)
    assert not study.context_matches(full, before, -1)


def test_flow_context_screen_is_unique_and_predeclared():
    contexts = study.flow_context_catalog()
    names = [context.name for context in contexts]

    assert len(names) == len(set(names))
    assert names[0] == "baseline"
    assert "mbo_delta" in names
    assert "mbo_full_confirmation" in names
    assert any(len(context.gates) > 1 for context in contexts)
