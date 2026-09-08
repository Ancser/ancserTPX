"""Mirror the canonical MarketData tree to configured local destinations.

The primary tree is the source of truth.  A mirror is written with
copy-to-temporary-file then rename, so a stopped process cannot leave a
destination with a half-written pickle or compressed download.  Missing
backup drives are reported per destination and never make the primary store
unusable.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from backend.data import market_data
from backend.timebase import UTC


logger = logging.getLogger(__name__)
_THREAD_LOCK = threading.Lock()
_TRANSIENT_NAMES = {
    ".market_data_sync.lock",
    ".market_data_manifest.tmp",
}


def _is_transient(path: Path) -> bool:
    name = path.name
    return (
        name in _TRANSIENT_NAMES
        or name.endswith(".tmp")
        or name.endswith(".partial")
        or name.endswith(".part")
        or name.endswith(".lock")
        or ".syncing." in name
    )


def _files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file() and not _is_transient(path):
            yield path


@contextlib.contextmanager
def _process_lock(root: Path) -> Iterator[bool]:
    """Take a process and OS lock without deleting a stale lock file."""
    if not _THREAD_LOCK.acquire(blocking=False):
        yield False
        return

    handle = None
    locked = False
    lock_path = root / ".market_data_sync.lock"
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield True
    except (OSError, ValueError):
        yield False
    finally:
        if handle is not None:
            if locked and os.name == "nt":
                try:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, ValueError):
                    pass
            elif locked and os.name != "nt":
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (OSError, ValueError):
                    pass
            try:
                handle.close()
            except (OSError, ValueError):
                pass
        _THREAD_LOCK.release()


def _stable_stat(path: Path) -> os.stat_result | None:
    try:
        before = path.stat()
        after = path.stat()
    except OSError:
        return None
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return after


def _copy_atomic(source: Path, destination: Path) -> str:
    """Copy one stable file and return ``copied``, ``unchanged`` or ``failed``."""
    source_stat = _stable_stat(source)
    if source_stat is None:
        return "failed"
    try:
        existing = destination.stat()
        if (existing.st_size, existing.st_mtime_ns) == (
            source_stat.st_size, source_stat.st_mtime_ns
        ):
            return "unchanged"
    except OSError:
        pass

    temporary = destination.with_name(destination.name + ".syncing.tmp")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, temporary)
        after_copy = _stable_stat(source)
        if after_copy is None or (
            after_copy.st_size,
            after_copy.st_mtime_ns,
        ) != (source_stat.st_size, source_stat.st_mtime_ns):
            temporary.unlink(missing_ok=True)
            return "failed"
        temporary.replace(destination)
        return "copied"
    except OSError as exc:
        logger.warning("MarketData mirror failed: %s -> %s: %s", source, destination, exc)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return "failed"


def _copy_paths_locked(
    paths: Iterable[Path],
    destinations: tuple[Path, ...],
    *,
    delete_missing: bool = False,
) -> dict[str, object]:
    result: dict[str, object] = {
        "copied": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
        "files": [],
        "destinations": {},
    }
    files_report: list[str] = []
    destination_report: dict[str, dict[str, int | str]] = {}
    for destination in destinations:
        report: dict[str, int | str] = {
            "copied": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
        }
        destination_report[str(destination)] = report

    for source in paths:
        relative = market_data.relative_to_primary(source)
        if relative is None or _is_transient(source):
            result["skipped"] = int(result["skipped"]) + 1
            continue
        files_report.append(relative.as_posix())
        for destination in destinations:
            candidate = destination / relative
            if not source.is_file():
                if delete_missing:
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("MarketData mirror delete failed: %s: %s", candidate, exc)
                        report = destination_report[str(destination)]
                        report["failed"] = int(report["failed"]) + 1
                        result["failed"] = int(result["failed"]) + 1
                else:
                    result["skipped"] = int(result["skipped"]) + 1
                continue
            status = _copy_atomic(source, candidate)
            report = destination_report[str(destination)]
            report[status] = int(report[status]) + 1
            if status == "copied":
                result["copied"] = int(result["copied"]) + 1
            elif status == "unchanged":
                result["unchanged"] = int(result["unchanged"]) + 1
            else:
                result["failed"] = int(result["failed"]) + 1
    result["files"] = sorted(set(files_report))
    result["destinations"] = destination_report
    return result


def mirror_files(
    paths: Iterable[str | os.PathLike[str] | Path],
    destinations: Iterable[str | os.PathLike[str]] | None = None,
    *,
    delete_missing: bool = False,
) -> dict[str, object]:
    """Immediately mirror selected primary files, used after store writes.

    Paths outside the canonical MarketData root are ignored.  This keeps the
    candle-store unit tests hermetic and prevents a temporary test directory
    from ever being copied to a real backup drive.
    """
    primary = market_data.configured_market_data_root()
    targets = market_data.iter_backup_roots(destinations)
    normalized = [Path(path).absolute() for path in paths]
    with _process_lock(primary) as acquired:
        if not acquired:
            return {"status": "busy", "copied": 0, "unchanged": 0, "failed": 0}
        return _copy_paths_locked(normalized, targets, delete_missing=delete_missing)


def _write_manifest(primary: Path, files: list[Path]) -> Path:
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "primary_root": str(primary),
        "file_count": len(files),
        "files": [],
    }
    entries: list[dict[str, object]] = []
    for source in files:
        relative = source.relative_to(primary)
        try:
            stat = source.stat()
        except OSError:
            continue
        entries.append({
            "path": relative.as_posix(),
            "bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        })
    payload["files"] = entries
    path = market_data.manifest_path(primary)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def sync_tree(
    destinations: Iterable[str | os.PathLike[str]] | None = None,
) -> dict[str, object]:
    """Mirror every stable file in the primary tree to each destination."""
    primary = market_data.ensure_layout()
    targets = market_data.iter_backup_roots(destinations)
    with _process_lock(primary) as acquired:
        if not acquired:
            return {"status": "busy", "primary": str(primary), "destinations": {}}
        files = list(_files(primary))
        result = _copy_paths_locked(files, targets)
        manifest = _write_manifest(primary, files)
        # The manifest is generated after the file list is copied, so publish
        # it last.  It is small and gives both destinations a visible run ID.
        manifest_result = _copy_paths_locked([manifest], targets)
        result["manifest"] = str(manifest)
        result["manifest_sync"] = manifest_result
        result["status"] = "ok"
        result["primary"] = str(primary)
        return result


def prune_destination(
    destination: str | os.PathLike[str] | Path,
) -> dict[str, object]:
    """Remove mirror files absent from the primary tree.

    This is intentionally separate from normal syncing.  Scheduled runs never
    delete backup-only files; call it explicitly after a verified migration
    when an exact mirror is desired.
    """
    primary = market_data.configured_market_data_root()
    target = Path(destination).expanduser().resolve()
    allowed = {path.relative_to(primary).as_posix() for path in _files(primary)}
    removed: list[str] = []
    failed: list[str] = []
    if not target.is_dir():
        return {"destination": str(target), "removed": removed, "failed": failed}
    for path in target.rglob("*"):
        if not path.is_file() or _is_transient(path):
            continue
        relative = path.relative_to(target).as_posix()
        if relative in allowed:
            continue
        try:
            path.unlink()
            removed.append(relative)
        except OSError:
            failed.append(relative)
    for directory in sorted(
        (path for path in target.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    return {"destination": str(target), "removed": removed, "failed": failed}


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_tree(
    destinations: Iterable[str | os.PathLike[str]] | None = None,
    *,
    hashes: bool = False,
) -> dict[str, object]:
    """Compare primary and mirrors; hashes are optional because they read all bytes."""
    primary = market_data.configured_market_data_root()
    targets = market_data.iter_backup_roots(destinations)
    source_files = {
        path.relative_to(primary).as_posix(): path
        for path in _files(primary)
    }
    output: dict[str, object] = {
        "primary": str(primary),
        "hashes": hashes,
        "destinations": {},
    }
    for destination in targets:
        missing: list[str] = []
        different: list[str] = []
        extra: list[str] = []
        checked = 0
        for relative, source in source_files.items():
            candidate = destination / Path(relative)
            if not candidate.is_file():
                missing.append(relative)
                continue
            checked += 1
            try:
                left = _sha256(source) if hashes else (source.stat().st_size, source.stat().st_mtime_ns)
                right = _sha256(candidate) if hashes else (candidate.stat().st_size, candidate.stat().st_mtime_ns)
            except OSError:
                different.append(relative)
                continue
            if left != right:
                different.append(relative)
        if destination.is_dir():
            for candidate in _files(destination):
                if _is_transient(candidate):
                    continue
                relative = candidate.relative_to(destination).as_posix()
                if relative not in source_files:
                    extra.append(relative)
        output["destinations"][str(destination)] = {
            "available": destination.is_dir(),
            "checked": checked,
            "missing": missing,
            "different": different,
            "extra": extra,
            "ok": destination.is_dir() and not missing and not different and not extra,
        }
    return output
