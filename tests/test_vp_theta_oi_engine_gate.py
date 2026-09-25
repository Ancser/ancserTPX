from __future__ import annotations

import csv
import gzip
from datetime import datetime, timezone
from types import SimpleNamespace

from scripts.vp_theta_oi_engine_gate import (
    CausalOIProvider,
    OI_RULE,
    _rule_allows,
    install_gate,
)
from scripts.vp_theta_oi_5yr_study import FEATURE_FIELDS


def _complete_context() -> dict:
    return {
        "oi_chain_completeness": "complete_same_date_chain",
        "oi_snapshot_date_et": "2026-03-05",
        "oi_publication_max_utc": "2026-03-05T10:30:40+00:00",
        "oi_unsigned_total": 6_000_000.0,
        "oi_put_call_ratio": 1.2,
        "oi_near_expiry_share_calendar_dte_le_1": 0.07,
        "oi_strike_hhi": 0.02,
    }


def test_frozen_oi_rule_needs_all_gate_features_and_complete_chain() -> None:
    assert _rule_allows(_complete_context())
    incomplete = _complete_context()
    incomplete["oi_chain_completeness"] = "partial_same_date_chain"
    assert not _rule_allows(incomplete)
    missing = _complete_context()
    missing["oi_put_call_ratio"] = None
    assert not _rule_allows(missing)


def test_gate_rejection_leaves_later_candidate_available() -> None:
    first = SimpleNamespace(timestamp=datetime(2026, 3, 5, 13, 30, tzinfo=timezone.utc))
    second = SimpleNamespace(timestamp=datetime(2026, 3, 5, 13, 31, tzinfo=timezone.utc))

    class Provider:
        def context_at(self, timestamp):
            return {
                **_complete_context(),
                "oi_unsigned_total": 8_000_000.0
                if timestamp == first.timestamp
                else 6_000_000.0,
            }

    class Strategy:
        def __init__(self):
            self.evaluated = 0

        @staticmethod
        def _candidate_set(candle):
            return [("buy", "val", "range_rejection", "range")]

        def evaluate(self, candle, zones=None, is_mature=True):
            self.evaluated += 1
            return SimpleNamespace(meta={})

    strategy = Strategy()
    counts = install_gate(
        strategy,
        Provider(),
        active_start=first.timestamp.date(),
        active_end=first.timestamp.date(),
    )
    assert strategy.evaluate(first) is None
    assert strategy.evaluated == 0
    signal = strategy.evaluate(second)
    assert signal is not None
    assert signal.meta["research_oi_gate"]["rule"] == OI_RULE["name"]
    assert strategy.evaluated == 1
    assert counts["rejected_rule"] == 1
    assert counts["signals_materialized"] == 1


def test_causal_provider_marks_later_same_date_rows_partial(tmp_path) -> None:
    path = tmp_path / "month=2026-03.csv.gz"
    rows = [
        {
            "symbol": "QQQ",
            "timestamp": "2026-03-05T11:30:00.000Z",
            "expiration": "2026-03-06",
            "strike": "500",
            "right": "CALL",
            "open_interest": "100",
        },
        {
            "symbol": "QQQ",
            "timestamp": "2026-03-05T11:30:00.000Z",
            "expiration": "2026-03-06",
            "strike": "500",
            "right": "PUT",
            "open_interest": "50",
        },
        {
            "symbol": "QQQ",
            "timestamp": "2026-03-05T20:15:00.000Z",
            "expiration": "2026-03-20",
            "strike": "510",
            "right": "CALL",
            "open_interest": "0",
        },
    ]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    provider = CausalOIProvider(tmp_path, start=datetime(2026, 3, 1).date(), end=datetime(2026, 3, 5).date())
    rth = provider.context_at(datetime(2026, 3, 5, 13, 30, tzinfo=timezone.utc))
    after_later_publication = provider.context_at(
        datetime(2026, 3, 5, 20, 20, tzinfo=timezone.utc)
    )
    assert rth["oi_chain_completeness"] == "partial_same_date_chain"
    assert rth["oi_unsigned_total"] == 150.0
    assert not _rule_allows(rth)
    assert after_later_publication["oi_chain_completeness"] == "complete_same_date_chain"
    assert after_later_publication["oi_unsigned_total"] == 150.0
    assert all(field in after_later_publication for field in FEATURE_FIELDS)
