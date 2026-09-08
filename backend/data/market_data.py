"""Canonical filesystem layout for market data and local runtime artefacts.

The repository contains code and small bootstrap configuration.  Large or
machine-generated data belongs outside the repository so a clone stays small
and a data refresh cannot dirty the source tree.  The default primary root is
the sibling ``ancserMarketData`` directory; users can override it before
starting the application with ``ANCSER_MARKET_DATA_ROOT``.

On Windows the default mirror is ``E:\\ancserMarketData``.  Other machines can
set ``ANCSER_MARKET_DATA_BACKUP_ROOTS`` to one or more paths separated by the
platform path separator (``;`` on Windows, ``:`` on macOS/Linux).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MARKET_DATA_ROOT_ENV = "ANCSER_MARKET_DATA_ROOT"
MARKET_DATA_BACKUP_ROOTS_ENV = "ANCSER_MARKET_DATA_BACKUP_ROOTS"

# These are intentionally relative to the primary root.  Keeping the layout
# in one place prevents a new downloader from silently recreating data/ or an
# old sibling directory.
CANDLE_STORE_RELATIVE = Path("source") / "futures" / "continuous_1m"
OPTION_WALL_RELATIVE = Path("source") / "options" / "qqq_option_ml"
ORDERFLOW_RELATIVE = Path("source") / "orderflow" / "mnq_mbo"
PI_SOURCE_RELATIVE = Path("source") / "discord" / "pi"
DERIVED_RELATIVE = Path("derived")
RUNTIME_RELATIVE = Path("runtime")
ARCHIVE_RELATIVE = Path("archive")
MANIFEST_RELATIVE = Path("manifests") / "market_data_manifest.json"


def _absolute_path(value: str | os.PathLike[str], *, relative_to: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = relative_to / path
    return path.resolve()


def configured_market_data_root() -> Path:
    """Return the configured primary data root without creating it."""
    raw = os.environ.get(MARKET_DATA_ROOT_ENV, "").strip()
    return _absolute_path(raw, relative_to=PROJECT_ROOT) if raw else (
        PROJECT_ROOT.parent / "ancserMarketData"
    ).resolve()


def configured_backup_roots() -> tuple[Path, ...]:
    """Return deduplicated mirror roots, including unavailable drives.

    An unavailable E: drive is not an error at configuration time.  The sync
    command reports it and keeps the primary copy usable; this matters for
    portable clones and for laptops that temporarily have a drive detached.
    """
    raw = os.environ.get(MARKET_DATA_BACKUP_ROOTS_ENV, "").strip()
    if raw:
        values = [piece.strip() for piece in raw.split(os.pathsep) if piece.strip()]
    elif os.name == "nt":
        values = ["E:/ancserMarketData"]
    else:
        values = [str(PROJECT_ROOT.parent / "ancserMarketDataBackup")]

    primary = configured_market_data_root()
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = _absolute_path(value, relative_to=PROJECT_ROOT)
        key = os.path.normcase(str(path))
        if path == primary or key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


# Import-time constants are convenient for callers that need to display the
# configuration, while all path helpers below resolve environment overrides at
# call time.  Tests and long-running processes can therefore inspect either
# form without a hidden directory creation side effect.
MARKET_DATA_ROOT = configured_market_data_root()
BACKUP_ROOTS = configured_backup_roots()


def _root(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _absolute_path(root, relative_to=PROJECT_ROOT) if root is not None else configured_market_data_root()


def source_root(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / "source"


def candle_store_dir(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / CANDLE_STORE_RELATIVE


def option_wall_root(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / OPTION_WALL_RELATIVE


def orderflow_root(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / ORDERFLOW_RELATIVE


def pi_source_root(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / PI_SOURCE_RELATIVE


def derived_root(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / DERIVED_RELATIVE


def runtime_root(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / RUNTIME_RELATIVE


def archive_root(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / ARCHIVE_RELATIVE


def manifest_path(root: str | os.PathLike[str] | Path | None = None) -> Path:
    return _root(root) / MANIFEST_RELATIVE


def derived_path(*parts: str | os.PathLike[str], root: str | os.PathLike[str] | Path | None = None) -> Path:
    return derived_root(root).joinpath(*parts)


def runtime_path(*parts: str | os.PathLike[str], root: str | os.PathLike[str] | Path | None = None) -> Path:
    return runtime_root(root).joinpath(*parts)


def archive_path(*parts: str | os.PathLike[str], root: str | os.PathLike[str] | Path | None = None) -> Path:
    return archive_root(root).joinpath(*parts)


def repository_data_path(*parts: str | os.PathLike[str]) -> Path:
    """Return a small tracked configuration path, never a market-data path."""
    return PROJECT_ROOT.joinpath("data", *parts)


def repository_seed_dir() -> Path:
    """Tracked bootstrap seeds retained for a fresh clone."""
    return repository_data_path("store", "seed")


def ensure_layout(root: str | os.PathLike[str] | Path | None = None) -> Path:
    """Create the standard primary directories and return their root."""
    base = _root(root)
    for path in (
        source_root(base),
        candle_store_dir(base),
        option_wall_root(base),
        orderflow_root(base),
        pi_source_root(base),
        derived_root(base),
        runtime_root(base),
        archive_root(base),
        manifest_path(base).parent,
    ):
        path.mkdir(parents=True, exist_ok=True)
    return base


def relative_to_primary(path: str | os.PathLike[str] | Path) -> Path | None:
    """Return a path's canonical relative name, or ``None`` if it is external."""
    candidate = Path(path).absolute()
    try:
        return candidate.relative_to(configured_market_data_root().absolute())
    except ValueError:
        return None


def iter_backup_roots(values: Iterable[str | os.PathLike[str]] | None = None) -> tuple[Path, ...]:
    """Normalize an explicit destination list using the same dedupe rules."""
    if values is None:
        return configured_backup_roots()
    primary = configured_market_data_root()
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        path = _absolute_path(value, relative_to=PROJECT_ROOT)
        key = os.path.normcase(str(path))
        if path == primary or key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)
