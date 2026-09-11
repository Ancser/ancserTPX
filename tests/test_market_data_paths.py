from __future__ import annotations

import json
from pathlib import Path

from backend.data import market_data
from backend.data import market_data_sync


def test_default_layout_is_a_sibling_root(monkeypatch):
    monkeypatch.delenv(market_data.MARKET_DATA_ROOT_ENV, raising=False)
    monkeypatch.delenv(market_data.MARKET_DATA_BACKUP_ROOTS_ENV, raising=False)
    expected = Path(__file__).resolve().parents[2] / "ancserMarketData"
    assert market_data.configured_market_data_root() == expected.resolve()
    assert market_data.candle_store_dir() == (
        expected / "source" / "futures" / "continuous_1m"
    ).resolve()


def test_custom_roots_are_normalized_and_deduplicated(monkeypatch, tmp_path):
    primary = tmp_path / "primary"
    backup_a = tmp_path / "backup-a"
    backup_b = tmp_path / "backup-b"
    monkeypatch.setenv(market_data.MARKET_DATA_ROOT_ENV, str(primary))
    monkeypatch.setenv(
        market_data.MARKET_DATA_BACKUP_ROOTS_ENV,
        f"{backup_a};{backup_a};{backup_b};{primary}",
    )
    assert market_data.configured_market_data_root() == primary.resolve()
    assert market_data.configured_backup_roots() == (
        backup_a.resolve(), backup_b.resolve()
    )


def test_sync_is_atomic_and_verifiable(monkeypatch, tmp_path):
    primary = tmp_path / "primary"
    backup = tmp_path / "backup"
    monkeypatch.setenv(market_data.MARKET_DATA_ROOT_ENV, str(primary))
    monkeypatch.setenv(market_data.MARKET_DATA_BACKUP_ROOTS_ENV, str(backup))
    source = market_data.candle_store_dir()
    source.mkdir(parents=True)
    payload = source / "MNQ_accumulated_1m.meta.json"
    payload.write_text(json.dumps({"total_bars": 1}), encoding="utf-8")

    first = market_data_sync.sync_tree()
    assert first["status"] == "ok"
    mirrored = backup / "source" / "futures" / "continuous_1m" / payload.name
    assert mirrored.read_text(encoding="utf-8") == payload.read_text(encoding="utf-8")

    payload.write_text(json.dumps({"total_bars": 2}), encoding="utf-8")
    market_data_sync.sync_tree()
    assert mirrored.read_text(encoding="utf-8") == payload.read_text(encoding="utf-8")

    payload.unlink()
    market_data_sync.mirror_files([payload], delete_missing=True)
    assert not mirrored.exists()

    verified = market_data_sync.verify_tree(hashes=True)
    assert verified["destinations"][str(backup.resolve())]["ok"]


def test_orderflow_is_primary_only_and_excluded_from_tree_sync(monkeypatch, tmp_path):
    primary = tmp_path / "primary"
    backup = tmp_path / "backup"
    monkeypatch.setenv(market_data.MARKET_DATA_ROOT_ENV, str(primary))
    monkeypatch.setenv(market_data.MARKET_DATA_BACKUP_ROOTS_ENV, str(backup))
    raw = market_data.orderflow_root() / "day" / "sample.dbn.zst"
    derived = market_data.derived_path("orderflow", "mnq", "sample.json.gz")
    raw.parent.mkdir(parents=True)
    derived.parent.mkdir(parents=True)
    raw.write_bytes(b"raw")
    derived.write_bytes(b"derived")
    legacy_backup = backup / raw.relative_to(primary)
    legacy_backup.parent.mkdir(parents=True)
    legacy_backup.write_bytes(b"old backup retained but ignored")

    result = market_data_sync.sync_tree()

    assert result["status"] == "ok"
    assert legacy_backup.read_bytes() == b"old backup retained but ignored"
    assert not (backup / derived.relative_to(primary)).exists()
    assert market_data.backup_excluded(raw)
    assert market_data.backup_excluded(derived)
    verified = market_data_sync.verify_tree()
    assert verified["destinations"][str(backup.resolve())]["ok"]


def test_windows_installer_does_not_recreate_disabled_mirror_schedule():
    installer = (Path(__file__).resolve().parents[1] / "windows install.bat").read_text(
        encoding="utf-8",
    )
    assert 'schtasks /create /tn "ancserTPX MarketData Sync"' not in installer
    assert 'schtasks /change /tn "ancserTPX MarketData Sync" /disable' in installer
    assert "Hourly scheduled E: mirror is disabled" in installer
    assert 'schtasks /create /tn "ancserTPX Databento MBO Settlement"' in installer
    batch = (Path(__file__).resolve().parents[1] / "windows_databento_orderflow_settlement.bat").read_text(
        encoding="utf-8",
    )
    assert "databento_orderflow_settlement.py" in batch
    assert "--max-cost 0" in batch
    assert "ancserMarketData" in batch
    assert "E:\\" not in batch
