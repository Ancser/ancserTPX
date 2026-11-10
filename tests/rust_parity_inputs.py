"""Frozen inputs shared by the Rust/Python PI parity tests.

Why this exists: both parity files used to call `_load_default_preset()` and
pin the SHA-256 of the whole `data/presets.json`. That function follows
`last_used_live`, and the file also stores UI state (`last_used_bt`,
`last_used_live`), so picking a different preset in the UI turned both parity
tests red without any engine change. Verified 2026-11-06: the content of
"PI 2MES BOTH BEST" is byte-identical in every commit from 07aaeac (1.2.3) to
62eb95a (1.5); only `last_used_live` moved (1.4), which is when they went red.

Now the cohort is selected by name and only that preset's content is pinned.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from backend import terminal_live

PARITY_PRESET = "PI 2MES BOTH BEST"
# sha256 of json.dumps(preset, sort_keys=True, ensure_ascii=False) for the
# raw entry in data/presets.json (before DEFAULT_PRESET_PARAMS are merged).
EXPECTED_PARITY_PRESET_SHA256 = "ff7ae23c64162e6eccbf5cf228baf9682d2442e87e4af167fa88718495c0450d"


def preset_content_sha256(raw: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def load_parity_preset() -> tuple[str, dict[str, Any]]:
    """Load PARITY_PRESET through the production loader, independent of UI state.

    Calls `_load_default_preset` itself (not a re-implementation of its
    DEFAULT_PRESET_PARAMS merge) with only `last_used_live` redirected, so the
    parity cohort sees exactly what a live terminal would load for this name.
    """
    data = terminal_live._load_presets_file()
    raw = (data.get("presets") or {}).get(PARITY_PRESET)
    assert isinstance(raw, dict), f"{PARITY_PRESET!r} is missing from data/presets.json"
    assert preset_content_sha256(raw) == EXPECTED_PARITY_PRESET_SHA256, (
        f"{PARITY_PRESET!r} content changed; re-freeze the parity cohort deliberately"
    )
    pinned = dict(data, last_used_live=PARITY_PRESET)
    with patch.object(terminal_live, "_load_presets_file", return_value=pinned):
        name, preset, source = terminal_live._load_default_preset()
    assert (name, source) == (PARITY_PRESET, "last_used_live")
    return name, preset


def require_local_parity_data() -> None:
    """Skip (not fail) where the private market data is absent, e.g. GitHub CI.

    The parity cohorts replay the real PI history and the full MES 1m store;
    neither is in git (CLAUDE.md: the 210 MB stores live in ancserMarketData).
    Without this, CI failed on FileNotFoundError / the tiny boot seed (0 bars)
    and the red run trained everyone to ignore CI. On the dev machine both
    files exist, so the tests still run there.
    """
    from backend.data import candle_store, pi_history

    if not Path(pi_history.HIST_PATH).is_file():
        pytest.skip("PI signal history (pi_signals.json) is not on this machine")
    if not candle_store._store_path("MES", 1).exists():
        pytest.skip("full MES 1m candle store is not on this machine (only the boot seed)")
