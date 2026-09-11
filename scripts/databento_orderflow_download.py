"""Download entitled Databento MNQ order-flow data to the primary data disk.

The default source set is MBO plus the tiny definition/statistics/status
reference streams. MBO is the lossless full-book source used to derive MBP-10,
trade prints, TBBO, footprints, passive fills, and liquidity levels.
Downloading those derivative schemas again would multiply storage without
adding information.

Examples (``end`` is exclusive)::

    python scripts/databento_orderflow_download.py \
        --start 2026-08-08 --end 2026-09-08

    python scripts/databento_orderflow_download.py \
        --start 2026-08-08 --end 2026-09-08 --download

Every request is quoted before download. A non-zero quote must be below
``--max-cost``; the default is zero so a subscription regression cannot
silently fall through to usage-based billing.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATASET = "GLBX.MDP3"
DEFAULT_SCHEMAS = ("mbo", "definition", "statistics", "status")
ALL_RELEVANT_SCHEMAS = (
    "mbo", "mbp-10", "trades", "tbbo", "bbo-1s", "ohlcv-1m",
    "definition", "statistics", "status",
)
UTC = timezone.utc
_DOWNLOAD_LOCK_HANDLE = None


def _acquire_download_lock(path: Path) -> None:
    """Hold an OS lock for the process lifetime; the lock file may remain safely."""
    global _DOWNLOAD_LOCK_HANDLE
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError("another Databento order-flow download is already running") from exc
    _DOWNLOAD_LOCK_HANDLE = handle
    atexit.register(handle.close)


def _load_api_key() -> str:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DATABENTO_API_KEY is missing from the environment/.env")
    return key


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    temporary.replace(path)


def _sha256(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _instant(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) > 10**14:
            number /= 1_000_000_000
        return datetime.fromtimestamp(number, tz=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _base_symbol(raw_symbol: str) -> str:
    upper = raw_symbol.upper()
    return "MNQ" if "MNQ" in upper else upper


def _existing_inventory(source_root: Path, raw_symbol: str) -> dict[tuple[str, date, date], Path]:
    """Index complete DBN requests already present under the canonical root."""
    inventory: dict[tuple[str, date, date], Path] = {}
    for metadata_path in source_root.rglob("metadata.json"):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        query = payload.get("query") or payload.get("request") or {}
        schema = str(query.get("schema") or "")
        symbols = query.get("symbols") or []
        if isinstance(symbols, str):
            symbols = [symbols]
        if raw_symbol not in {str(symbol).upper() for symbol in symbols}:
            continue
        start = _instant(query.get("start"))
        end = _instant(query.get("end"))
        if not schema or start is None or end is None:
            continue
        files = sorted(metadata_path.parent.glob("*.dbn.zst"))
        if not files:
            files = sorted(metadata_path.parent.glob("*.dbn"))
        for path in files:
            if path.is_file() and path.stat().st_size > 0:
                inventory[(schema, start.date(), end.date())] = path
                break
    return inventory


def _days(start: date, end: date) -> Iterable[tuple[date, date]]:
    current = start
    while current < end:
        following = current + timedelta(days=1)
        yield current, min(following, end)
        current = following


def _request_ranges(schema: str, start: date, end: date) -> Iterable[tuple[date, date]]:
    # Definitions are tiny reference data. One range request is enough; market
    # event schemas are split by UTC day for retry/resume and cache building.
    if schema in {"definition", "statistics", "status"}:
        yield start, end
        return
    yield from _days(start, end)


def request_payload(
    schema: str,
    raw_symbol: str,
    start: date | str,
    end: date | str,
) -> dict[str, Any]:
    """Build one canonical Databento request used by download and settlement."""
    range_start = start if isinstance(start, date) else date.fromisoformat(str(start))
    range_end = end if isinstance(end, date) else date.fromisoformat(str(end))
    if range_end <= range_start:
        raise ValueError("request end must be later than start")
    return {
        "dataset": DATASET,
        "symbols": [str(raw_symbol).upper()],
        "schema": str(schema),
        "stype_in": "raw_symbol",
        "start": range_start.isoformat(),
        "end": range_end.isoformat(),
    }


def quote_request(client: Any, request: dict[str, Any]) -> tuple[int, float]:
    """Return the current record-count and usage quote without downloading."""
    count = int(client.metadata.get_record_count(**request))
    cost = float(client.metadata.get_cost(**request))
    if not math.isfinite(cost) or cost < 0:
        raise RuntimeError(
            f"Databento returned an invalid cost quote for {request.get('schema')} "
            f"{request.get('start')}: {cost}"
        )
    return count, cost


def _folder_name(symbol: str, schema: str, start: date, end: date) -> str:
    base = _base_symbol(symbol).lower()
    schema_slug = schema.replace("-", "")
    if end == start + timedelta(days=1):
        return f"databento_{start.isoformat()}_{base}_{schema_slug}"
    return f"databento_{start.isoformat()}_{end.isoformat()}_{base}_{schema_slug}"


def _file_name(schema: str, start: date, end: date) -> str:
    suffix = start.strftime("%Y%m%d")
    if end != start + timedelta(days=1):
        suffix += "-" + end.strftime("%Y%m%d")
    return f"glbx-mdp3-{suffix}.{schema.replace('-', '')}.dbn.zst"


def _archive_request_files(
    folder: Path,
    target: Path,
) -> list[tuple[Path, Path]]:
    """Move an old request aside on the same drive before a safe replacement."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    archived: list[tuple[Path, Path]] = []
    try:
        for candidate in (target, folder / "metadata.json", folder / "manifest.json"):
            if not candidate.is_file():
                continue
            backup = candidate.with_name(candidate.name + ".replaced-" + stamp)
            suffix = 1
            while backup.exists():
                backup = candidate.with_name(
                    candidate.name + f".replaced-{stamp}-{suffix}"
                )
                suffix += 1
            candidate.replace(backup)
            archived.append((candidate, backup))
    except Exception:
        _restore_archived_files(archived)
        raise
    return archived


def _restore_archived_files(archived: list[tuple[Path, Path]]) -> None:
    for original, backup in reversed(archived):
        if backup.is_file() and not original.exists():
            backup.replace(original)


def download_request(
    client: Any,
    request: dict[str, Any],
    source_root: Path,
    *,
    replace_existing: bool = True,
) -> tuple[Path, str]:
    """Download one request transactionally and return ``(path, sha256)``.

    A replacement never deletes the previous DBN/metadata/manifest.  It first
    renames them beside the source file, and restores them if the new transfer
    or manifest publication fails.  Those files remain under the excluded
    primary-only order-flow tree so the E: mirror is never involved.
    """
    schema = str(request["schema"])
    symbols = request.get("symbols") or []
    raw_symbol = str(symbols[0] if isinstance(symbols, list) else symbols).upper()
    range_start = date.fromisoformat(str(request["start"]))
    range_end = date.fromisoformat(str(request["end"]))
    folder = source_root / _folder_name(raw_symbol, schema, range_start, range_end)
    target = folder / _file_name(schema, range_start, range_end)
    if target.is_file() and not replace_existing:
        raise FileExistsError(f"request already exists: {target}")

    folder.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    try:
        client.timeseries.get_range(
            dataset=request["dataset"],
            symbols=[raw_symbol],
            schema=schema,
            stype_in=request.get("stype_in", "raw_symbol"),
            start=range_start.isoformat(),
            end=range_end.isoformat(),
            path=partial,
        )
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    if not partial.is_file() or partial.stat().st_size <= 0:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Databento returned no file for {schema} {range_start}")

    archived = _archive_request_files(folder, target) if replace_existing else []
    try:
        partial.replace(target)
        digest = _sha256(target)
        query = {
            key: value for key, value in request.items()
            if key not in {"record_count", "quoted_cost_usd"}
        }
        _atomic_json(folder / "metadata.json", {
            "version": 1,
            "query": query,
            "record_count": request.get("record_count", 0),
            "quoted_cost_usd": request.get("quoted_cost_usd", 0.0),
            "downloaded_at": datetime.now(UTC).isoformat(),
        })
        _atomic_json(folder / "manifest.json", {
            "version": 1,
            "files": [{
                "name": target.name,
                "bytes": target.stat().st_size,
                "sha256": digest,
            }],
        })
    except Exception:
        partial.unlink(missing_ok=True)
        for candidate in (target, folder / "metadata.json", folder / "manifest.json"):
            candidate.unlink(missing_ok=True)
        _restore_archived_files(archived)
        raise
    return target, digest


def _ensure_footprint_cache(
    source: Path,
    trade_date: date,
    symbol: str,
    *,
    force: bool = False,
    root: str | Path | None = None,
) -> Path:
    """Build a missing derived cache, including for an already-downloaded day."""
    from backend.data.orderflow import (
        CACHE_SCHEMA_VERSION,
        build_footprint_file,
        footprint_cache_path,
        read_footprint_cache,
    )

    output = footprint_cache_path(
        trade_date.isoformat(), _base_symbol(symbol), root=root,
    )
    if output.is_file() and output.stat().st_size > 0:
        try:
            version = int((read_footprint_cache(output).get("meta") or {}).get("schema_version", 0))
        except (OSError, ValueError, TypeError):
            version = 0
        if version == CACHE_SCHEMA_VERSION and not force:
            print(f"CACHE SKIP {output.name} | schema v{version}")
            return output
        print(f"CACHE REBUILD {output.name} | schema v{version} -> v{CACHE_SCHEMA_VERSION}")
    output, payload = build_footprint_file(
        source, trade_date.isoformat(), symbol=_base_symbol(symbol),
        output_path=output,
    )
    print(
        f"CACHE {output.name} | {len(payload['bars'])} RTH minute bars"
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="inclusive UTC date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="exclusive UTC date YYYY-MM-DD")
    parser.add_argument("--symbol", default="MNQU6", help="Databento raw symbol")
    parser.add_argument(
        "--schemas", nargs="+", default=list(DEFAULT_SCHEMAS),
        choices=ALL_RELEVANT_SCHEMAS,
    )
    parser.add_argument(
        "--download", action="store_true",
        help="perform the quoted download; without it this command only estimates",
    )
    parser.add_argument(
        "--max-cost", type=float, default=0.0,
        help="maximum total USD quote accepted (default 0: subscription-only)",
    )
    parser.add_argument(
        "--no-build-footprint", action="store_true",
        help="keep raw MBO only; normally each downloaded day also builds chart cache",
    )
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    if end <= start:
        parser.error("--end must be later than --start")

    import databento as db
    from backend.data import market_data

    primary = market_data.ensure_layout()
    _acquire_download_lock(market_data.runtime_path("databento_orderflow_download.lock"))
    source_root = market_data.orderflow_root(primary)
    inventory = _existing_inventory(source_root, args.symbol.upper())
    client = db.Historical(_load_api_key())
    requests: list[dict[str, Any]] = []
    total_cost = 0.0
    total_records = 0

    print(f"Databento {DATASET} {args.symbol.upper()} | {start} -> {end} (end exclusive)")
    print(f"Primary: {primary}")
    print("Order-flow backup: disabled (primary F: only)")
    for schema in dict.fromkeys(args.schemas):
        for range_start, range_end in _request_ranges(schema, start, end):
            key = (schema, range_start, range_end)
            existing = inventory.get(key)
            if existing:
                print(f"SKIP {schema:10s} {range_start} -> {range_end} | {existing.name}")
                continue
            request = request_payload(schema, args.symbol, range_start, range_end)
            count, cost = quote_request(client, request)
            if count <= 0:
                print(f"EMPTY {schema:9s} {range_start} -> {range_end}")
                continue
            request.update({"record_count": count, "quoted_cost_usd": cost})
            requests.append(request)
            total_cost += cost
            total_records += count
            print(
                f"QUOTE {schema:9s} {range_start} -> {range_end} | "
                f"{count:,} records | ${cost:.4f}"
            )

    print(
        f"Pending: {len(requests)} request(s), {total_records:,} records, "
        f"quoted ${total_cost:.4f}"
    )
    if total_cost > args.max_cost + 1e-9:
        print(
            f"REFUSED: quote ${total_cost:.4f} exceeds --max-cost ${args.max_cost:.4f}",
            file=sys.stderr,
        )
        return 2
    if not args.download:
        print("Estimate only; add --download to transfer data.")
        return 0

    # A previous run may have completed the raw transfer before cache building,
    # or the raw files may predate this production cache.  Make reruns repair
    # that state instead of treating SKIP as "nothing left to do".
    if not args.no_build_footprint:
        for (schema, range_start, range_end), existing in sorted(inventory.items()):
            if schema != "mbo" or range_end != range_start + timedelta(days=1):
                continue
            if start <= range_start < end:
                _ensure_footprint_cache(existing, range_start, args.symbol)

    downloaded = 0
    for request in requests:
        schema = str(request["schema"])
        range_start = date.fromisoformat(str(request["start"]))
        range_end = date.fromisoformat(str(request["end"]))
        print(f"DOWNLOAD {schema} {range_start} -> {range_end} ...", flush=True)
        target, digest = download_request(
            client, request, source_root, replace_existing=True,
        )
        print(
            f"SAVED {target.stat().st_size / 1_048_576:.1f} MiB | "
            f"sha256 {digest[:12]}... | primary only"
        )
        downloaded += 1

        if schema == "mbo" and not args.no_build_footprint:
            _ensure_footprint_cache(target, range_start, args.symbol)

    print(f"Complete: {downloaded} new source request(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
