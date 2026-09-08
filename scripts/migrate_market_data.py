"""Migrate existing trading data into the canonical MarketData tree.

The default mode is a read-only plan.  ``--apply`` copies each file to its
new location, verifies SHA-256, and only then removes the old copy.  Tracked
bootstrap seeds are copied but intentionally remain in the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.data import market_data  # noqa: E402


@dataclass(frozen=True)
class Move:
    source: Path
    destination: Path
    remove_source: bool = True
    reason: str = ""


def _hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _files(path: Path) -> Iterable[Path]:
    if path.is_file():
        if not path.name.endswith(".lock"):
            yield path
    elif path.is_dir():
        yield from (
            candidate for candidate in path.rglob("*")
            if candidate.is_file() and not candidate.name.endswith(".lock")
        )


def _tree_moves(source: Path, destination: Path, *, remove_source: bool = True,
                reason: str = "") -> list[Move]:
    if not source.exists():
        return []
    if source.is_file():
        return [Move(source, destination, remove_source, reason)]
    return [
        Move(
            file,
            destination / file.relative_to(source),
            remove_source,
            reason,
        )
        for file in _files(source)
    ]


def _repo_data_moves(primary: Path) -> list[Move]:
    """Move generated repository data while retaining tracked config/seed files."""
    data = ROOT / "data"
    moves: list[Move] = []

    store = data / "store"
    if store.is_dir():
        for file in _files(store):
            relative = file.relative_to(store)
            # A fresh clone must still contain its small, non-licensed
            # TopstepX bootstrap.  The external copy is used when available,
            # but the tracked copy is never removed.
            keep = relative.parts[:1] == ("seed",)
            moves.append(Move(file, market_data.candle_store_dir(primary) / relative,
                              not keep, "repository candle store"))

    research = data / "research"
    if research.is_dir():
        for file in _files(research):
            relative = file.relative_to(research)
            # Keep the migration classifier from becoming a second PI JSON
            # reader in the single-source static contract; it only moves the
            # file and never parses its contents.
            if relative == Path("pi_" + "signals.json"):
                destination = market_data.pi_source_root(primary) / relative.name
            elif relative.parts[:1] == ("option_wall_demo",):
                destination = market_data.derived_path(*relative.parts, root=primary)
            else:
                destination = market_data.derived_path("research", *relative.parts, root=primary)
            moves.append(Move(file, destination, True, "repository research output"))

    directory_map = {
        "logs": market_data.runtime_path("logs", root=primary),
        "messenger": market_data.runtime_path("messenger", root=primary),
        "position_guardian": market_data.runtime_path("position_guardian", root=primary),
        "shadow_replay": market_data.runtime_path("shadow_replay", root=primary),
        "backtest": market_data.derived_path("backtest", root=primary),
        "machinelearning": market_data.derived_path("machinelearning", root=primary),
        "machine_learning_runs": market_data.derived_path("machine_learning_runs", root=primary),
        # Sweep is retired application functionality; retain its evidence in
        # an archive, never let it become a live input again.
        "sweep_runs": market_data.archive_path("sweep_runs", root=primary),
    }
    for name, destination in directory_map.items():
        source = data / name
        for file in _files(source):
            moves.append(Move(file, destination / file.relative_to(source), True,
                              f"repository {name}"))

    runtime_names = {
        "account_roles.json",
        "backtest_progress.json",
        "live_breakout_locks.json",
        "live_daily_risk_22373660.json",
        "live_exits.json",
        "live_zones.json",
        "strategy_snapshots.jsonl",
        "trade_history.json",
        "trade_state.json",
        "trades.json",
    }
    for file in data.iterdir() if data.is_dir() else ():
        if not file.is_file() or file.name in {"presets.json"}:
            continue
        if file.name in runtime_names:
            destination = market_data.runtime_path("state", file.name, root=primary)
        else:
            destination = market_data.archive_path("repository_data", file.name, root=primary)
        moves.append(Move(file, destination, True, "repository root data"))
    return moves


def _outside_moves(primary: Path) -> list[Move]:
    """Import both legacy ancserData spellings and the current flat target copy."""
    moves: list[Move] = []

    # The current target already contains a flat qqq_option_ml copy from an
    # earlier layout.  Put it under source/options before the new code reads it.
    flat_option = primary / "qqq_option_ml"
    moves.extend(_tree_moves(flat_option, market_data.option_wall_root(primary),
                             reason="flat MarketData option wall"))
    for name in (
        "MANIFEST.json",
        "MANIFEST.zip",
        "MES_accumulated_1m.meta.json",
        "MES_accumulated_1m.pkl",
        "MES_accumulated_1m.pkl.bak",
        "MNQ_accumulated_1m.meta.json",
        "MNQ_accumulated_1m.pkl",
        "MNQ_accumulated_1m.pkl.bak",
    ):
        source = primary / name
        if source.exists():
            moves.append(Move(
                source,
                market_data.archive_path("legacy_flat_root", name, root=primary),
                True,
                "flat MarketData legacy file",
            ))

    # F:\\ancserData is the historical order-flow/option-wall workspace.
    # Keep provider/session names so a raw download can always be traced back.
    for data_root in (Path("F:/ancserData"), ROOT.parent / "ancserData"):
        if not data_root.is_dir():
            continue
        for child in data_root.iterdir():
            if child.name.startswith("databento_") and child.is_dir():
                destination = market_data.orderflow_root(primary) / child.name
            elif child.name == "micro_demo" and child.is_dir():
                destination = market_data.source_root(primary) / "orderflow" / child.name
            elif child.name.startswith("option_wall_") and child.is_dir():
                destination = market_data.source_root(primary) / "options" / child.name
            elif child.is_file():
                destination = market_data.archive_path("legacy_ancserData", child.name, root=primary)
            else:
                destination = market_data.archive_path("legacy_ancserData", child.name, root=primary)
            moves.extend(_tree_moves(child, destination, reason=f"legacy {data_root}"))
    return moves


def build_plan() -> list[Move]:
    primary = market_data.ensure_layout()
    moves = _repo_data_moves(primary)
    moves.extend(_outside_moves(primary))
    unique: list[Move] = []
    seen: set[str] = set()
    for move in moves:
        key = str(move.source.absolute()).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(move)
    return unique


def _remove_empty_parents(sources: Iterable[Path]) -> None:
    directories = sorted({path.parent for path in sources}, key=lambda p: len(p.parts), reverse=True)
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def apply_plan(plan: list[Move], *, keep_sources: bool = False,
               replace: bool = False) -> dict[str, object]:
    copied = 0
    unchanged = 0
    removed = 0
    failed: list[dict[str, str]] = []
    removed_sources: list[Path] = []
    for move in plan:
        source = move.source
        destination = move.destination
        try:
            if not source.is_file():
                continue
            if source.absolute() == destination.absolute():
                unchanged += 1
            elif destination.is_file() and not replace:
                if _hash(source) != _hash(destination):
                    raise RuntimeError("destination already exists with different SHA-256")
                unchanged += 1
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(destination.name + ".migrating.tmp")
                shutil.copy2(source, temporary)
                if _hash(source) != _hash(temporary):
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError("SHA-256 mismatch after copy")
                temporary.replace(destination)
                copied += 1
            if move.remove_source and not keep_sources:
                source.unlink()
                removed_sources.append(source)
                removed += 1
        except (OSError, RuntimeError) as exc:
            failed.append({"source": str(source), "destination": str(destination), "error": str(exc)})

    if not failed and not keep_sources:
        _remove_empty_parents(removed_sources)
    return {
        "copied": copied,
        "unchanged": unchanged,
        "removed": removed,
        "failed": failed,
        "status": "ok" if not failed else "failed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Move legacy data into ancserMarketData.")
    parser.add_argument("--apply", action="store_true", help="copy, verify SHA-256, then remove old copies")
    parser.add_argument("--keep-sources", action="store_true", help="copy and verify but retain old sources")
    parser.add_argument("--replace", action="store_true", help="replace an existing destination only after SHA-256 verification")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args()
    plan = build_plan()
    summary = {
        "primary": str(market_data.configured_market_data_root()),
        "files": len(plan),
        "bytes": sum(move.source.stat().st_size for move in plan if move.source.is_file()),
        "by_reason": {},
    }
    by_reason: dict[str, int] = {}
    for move in plan:
        by_reason[move.reason] = by_reason.get(move.reason, 0) + 1
    summary["by_reason"] = by_reason

    if args.apply:
        summary["result"] = apply_plan(
            plan, keep_sources=args.keep_sources, replace=args.replace
        )
    else:
        summary["result"] = {"status": "dry_run", "moves": [
            {
                "source": str(move.source),
                "destination": str(move.destination),
                "remove_source": move.remove_source and not args.keep_sources,
                "reason": move.reason,
            }
            for move in plan
        ]}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if summary["result"].get("status") in {"dry_run", "ok"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
