from datetime import datetime, timedelta, timezone

from backend.db.models import Direction, StrategyParams, StrategyType, TradeSignal
from scripts.strategy_long_term_audit import (
    PiDirectionalExitOverlay,
    build_rth_regimes,
    classify_result,
    compact_stats,
)


def _robustness(*, pf=1.2, stress_pf=1.1, wf=True, mc=True, n=100, pnl=1000):
    return {
        "baseline": {
            "n": n,
            "pnl": pnl,
            "pf": pf,
            "win_rate": 0.5,
            "max_dd": 500,
            "avg_trade": pnl / n if n else 0,
        },
        "stress_14t": {
            "n": n,
            "pnl": pnl - 100,
            "pf": stress_pf,
            "win_rate": 0.5,
            "max_dd": 600,
        },
        "walk_forward": {"pass": wf, "segments": []},
        "monte_carlo_pass": mc,
    }


def test_compact_stats_reports_net_pnl_and_drawdown():
    result = compact_stats([{"pnl": 10}, {"pnl": -4}, {"pnl": 6}])
    assert result["n"] == 3
    assert result["pnl"] == 12
    assert result["pf"] == 4
    assert result["max_dd"] == 4


def test_rth_regime_volatility_is_unknown_until_prior_history_exists():
    start = datetime(2020, 1, 2, 14, 30, tzinfo=timezone.utc)
    candles = []
    for day in range(22):
        ts = start + timedelta(days=day)
        candles.extend(
            [
                type("C", (), {
                    "timestamp": ts,
                    "open": 100.0,
                    "high": 110.0,
                    "low": 99.0,
                    "close": 109.0,
                })(),
                type("C", (), {
                    "timestamp": ts + timedelta(minutes=1),
                    "open": 109.0,
                    "high": 111.0,
                    "low": 100.0,
                    "close": 108.0,
                })(),
            ]
        )
    regimes = build_rth_regimes(candles)
    assert len(regimes) == 22
    assert next(iter(regimes.values()))["volatility"] == "unknown"
    assert list(regimes.values())[-1]["volatility"] in {"low", "normal", "high"}
    assert list(regimes.values())[-1]["trend"] == "up"


def test_classification_prioritizes_coverage_and_then_edge_failures():
    limited, limited_reasons = classify_result(
        key="pi",
        robustness=_robustness(),
        yearly={"2026": {"n": 100, "pnl": 1000}},
        regimes={},
        coverage={"span_months": 2},
    )
    assert limited == "COVERAGE_LIMITED"
    assert limited_reasons

    failed, failed_reasons = classify_result(
        key="factor",
        robustness=_robustness(pf=0.9, stress_pf=0.7, wf=False, mc=False),
        yearly={"2020": {"n": 100, "pnl": -100}},
        regimes={},
        coverage=None,
    )
    assert failed == "FAIL_NO_EDGE"
    assert len(failed_reasons) >= 3


def test_pi_overlay_attaches_the_shared_asymmetric_time_policy():
    class Inner:
        PENDING_TIMEOUT_CANDLES = 1

        def evaluate(self, candle, zones=None, is_mature=True):
            return TradeSignal(
                strategy=StrategyType.TREND_FOLLOW,
                direction=Direction.SELL,
                entry_price=100,
                sl_price=101,
                tp_price=98,
                zone_id="test",
                reason="test",
            )

    params = StrategyParams(strategy="factor", contract_id="CON.F.US.MNQ.Z26")
    overlay = PiDirectionalExitOverlay(Inner(), params)
    signal = overlay.evaluate(None)
    assert signal.exit_policy is not None
    assert signal.exit_policy.model == "pi"
    assert signal.exit_policy.max_hold_minutes == 60

