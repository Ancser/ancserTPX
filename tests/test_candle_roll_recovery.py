from datetime import datetime, timedelta
import multiprocessing
import os

import pytest

from backend.data import candle_store as cs, contract_ledger as ledger
from backend.db.models import Candle
from backend.timebase import UTC


def _hold_store_lock(path, acquired, release):
    from pathlib import Path
    from backend.data import candle_store
    candle_store.STORE_DIR = Path(path)
    with candle_store._store_transaction("MNQ", 1):
        acquired.set()
        release.wait(15)


def _crash_during_roll(path):
    from pathlib import Path
    from backend.data import candle_store
    candle_store.STORE_DIR = Path(path)
    with candle_store._store_transaction("MNQ", 1):
        candle_store._prepare_roll_transaction("MNQ", 1)
        candle_store._meta_path("MNQ", 1).write_text('{"contract_rolls": []}', encoding="utf-8")
        candle_store._store_path("MNQ", 1).write_bytes(b"partial")
        os._exit(77)


@pytest.fixture
def store(monkeypatch, tmp_path):
    monkeypatch.setattr(cs, "STORE_DIR", tmp_path)
    monkeypatch.setattr(cs, "mirror_files", lambda *args, **kwargs: None)
    cs.invalidate_cache()
    at = datetime(2026, 9, 10, tzinfo=UTC)
    candle = Candle(timestamp=at - timedelta(minutes=1), open=100, high=101,
                    low=99, close=100, volume=1, symbol="MNQ", interval="1m",
                    contract_id="CON.F.US.MNQ.U26", price_adjustment=0.0)
    cs.save([candle], "MNQ")
    meta = cs.load_meta("MNQ")
    meta["contract_rolls"] = ledger.rolls_to_meta([{
        "at": at - timedelta(days=90), "from": "M26", "to": "U26", "offset": 10.0, "basis": "test"
    }])
    cs.save_meta(meta, "MNQ")
    yield at
    cs.invalidate_cache()


def test_roll_write_failure_restores_original_and_can_retry(store, monkeypatch):
    at = store
    original_save = cs._save_locked
    def fail(*args, **kwargs):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(cs, "_save_locked", fail)
    with pytest.raises(OSError):
        cs.apply_contract_roll("MNQ", at, "CON.F.US.MNQ.U26", "CON.F.US.MNQ.Z26", 20, "test")
    assert cs.load("MNQ", use_cache=False)[0].close == 100
    assert cs.contract_rolls("MNQ")[-1]["to"] == "U26"
    assert not cs._roll_journal_path("MNQ", 1).exists()
    monkeypatch.setattr(cs, "_save_locked", original_save)
    assert cs.apply_contract_roll("MNQ", at, "CON.F.US.MNQ.U26", "CON.F.US.MNQ.Z26", 20, "test")["applied"]
    assert cs.load("MNQ", use_cache=False)[0].close == 120
    assert not cs.apply_contract_roll("MNQ", at, "CON.F.US.MNQ.U26", "CON.F.US.MNQ.Z26", 20, "test")["applied"]


def test_next_reader_recovers_after_interrupted_transaction(store):
    original_meta = cs.load_meta("MNQ")
    with cs._store_transaction("MNQ", 1):
        journal = cs._prepare_roll_transaction("MNQ", 1)
        cs._meta_path("MNQ", 1).write_text('{"contract_rolls": []}', encoding="utf-8")
        cs._store_path("MNQ", 1).write_bytes(b"incomplete")
    assert journal.exists()
    assert cs.load("MNQ", use_cache=False)[0].close == 100
    assert cs.load_meta("MNQ") == original_meta
    assert not journal.exists()


def test_invalid_backup_fails_closed(store):
    with cs._store_transaction("MNQ", 1):
        journal = cs._prepare_roll_transaction("MNQ", 1)
        import json
        item = next(x for x in json.loads(journal.read_text(encoding="utf-8"))["files"] if x["existed"])
        (cs.STORE_DIR / item["backup"]).write_bytes(b"bad")
    with pytest.raises(RuntimeError, match="backup"):
        cs.load("MNQ")
    assert journal.exists()


def test_store_lock_excludes_another_process(store):
    ctx = multiprocessing.get_context("spawn")
    acquired, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_store_lock, args=(str(cs.STORE_DIR), acquired, release))
    child.start()
    try:
        assert acquired.wait(10), "child did not acquire store lock"
        path = cs._store_path("MNQ", 1).with_suffix(".lock")
        with path.open("a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                with pytest.raises(OSError):
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                with pytest.raises(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        release.set()
        child.join(15)
    assert child.exitcode == 0


def test_process_crash_releases_lock_and_recovers_on_next_read(store):
    original_meta = cs.load_meta("MNQ")
    pending = Candle(timestamp=store - timedelta(seconds=30), open=105, high=106, low=104,
                     close=105, volume=1, symbol="MNQ", interval="1m",
                     contract_id="CON.F.US.MNQ.U26", price_adjustment=0.0)
    cs.append_pending([pending], "MNQ")
    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_crash_during_roll, args=(str(cs.STORE_DIR),))
    child.start()
    child.join(20)
    assert child.exitcode == 77
    assert cs._roll_journal_path("MNQ", 1).exists()
    assert cs.load("MNQ", use_cache=False)[0].close == 100
    assert cs.load_meta("MNQ") == original_meta
    with cs._store_transaction("MNQ", 1):
        assert cs._read_pending_locked("MNQ", 1)[0].close == 105
    assert not cs._roll_journal_path("MNQ", 1).exists()


@pytest.mark.parametrize("operation", ["save_meta", "_save_locked", "_write_pending_locked"])
def test_failure_after_each_roll_write_restores_store(store, monkeypatch, operation):
    before = cs.load_meta("MNQ")
    original = getattr(cs, operation)
    def fail_after_write(*args, **kwargs):
        result = original(*args, **kwargs)
        raise OSError("simulated interruption after write")
    monkeypatch.setattr(cs, operation, fail_after_write)
    with pytest.raises(OSError):
        cs.apply_contract_roll("MNQ", store, "CON.F.US.MNQ.U26", "CON.F.US.MNQ.Z26", 20, "test")
    assert cs.load("MNQ", use_cache=False)[0].close == 100
    assert cs.load_meta("MNQ") == before
