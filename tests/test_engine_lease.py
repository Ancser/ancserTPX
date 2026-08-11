from __future__ import annotations

import json
from pathlib import Path

from backend.live.engine_lease import LiveEngineLease


ROOT = Path(__file__).resolve().parents[1]


def test_account_lease_is_exclusive_and_reusable(tmp_path):
    path = tmp_path / "account.lock"
    first = LiveEngineLease(123, path)
    second = LiveEngineLease(123, path)

    assert first.acquire() is True
    assert first.held is True
    assert second.acquire() is False

    first.release()
    assert first.held is False
    # Metadata is only diagnostic; after release it remains readable and
    # identifies the previous owner without affecting the next claim.
    metadata = json.loads(path.read_text(encoding="utf-8"))
    assert metadata["account_id"] == 123
    assert second.acquire() is True
    second.release()
    second.release()  # idempotent cleanup


def test_different_accounts_can_run_in_parallel(tmp_path):
    one = LiveEngineLease(1, tmp_path / "one.lock")
    two = LiveEngineLease(2, tmp_path / "two.lock")
    assert one.acquire() is True
    assert two.acquire() is True
    one.release()
    two.release()


def test_web_start_reserves_the_account_before_async_warmup():
    source = (ROOT / "backend" / "api" / "routes.py").read_text(encoding="utf-8")
    assert "_live_start_locks" in source
    assert "Account {account_id} live engine start already in progress" in source
    assert "LiveEngineLease(int(req.account_id))" in source
