"""Suite-wide isolation from the live machine's runtime files."""
from __future__ import annotations

import pytest

from backend.data import pi_live_audit


@pytest.fixture(autouse=True)
def _isolated_pi_live_audit(tmp_path, monkeypatch):
    """Point the PI live audit stream at a per-test file.

    1.5.1: every reader/writer in pi_live_audit defaults to AUDIT_PATH at call
    time, and that is the real runtime/logs/pi_live_signals.jsonl the running
    terminal appends to. Unisolated, one run of the listener tests appended
    ~16 fake rows (message ids 900/300/123456789, loop_ValueError) tagged with
    the real channel id — 143 fake loop_error rows had accumulated by
    2026-11-06 — and readers like load_message_ids() saw real live history.
    """
    monkeypatch.setattr(pi_live_audit, "AUDIT_PATH", tmp_path / "pi_live_signals.jsonl")
