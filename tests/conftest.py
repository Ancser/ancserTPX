"""Suite-wide isolation from the live machine's runtime files."""
from __future__ import annotations

import os

import pytest

# 1.5.1i: the Grok Bot webhook (backend/live/pi_grok_webhook.py) reads
# GROK_PI_WEBHOOK_* at call time, and several backend modules call
# load_dotenv(override=False) on import. With a real URL in .env, one run of
# the listener tests POSTed 5 fake PI signals (4 x QQQ cyan-pi longs) to the
# live Bot. Present-but-empty values make every forward a no-op AND stop any
# later load_dotenv from filling them in. Tests that exercise the webhook set
# their own value with monkeypatch.
for _grok_env in ("GROK_PI_WEBHOOK_URL", "GROK_PI_WEBHOOK_AUTHORIZATION", "GROK_PI_WEBHOOK_KEY"):
    os.environ[_grok_env] = ""

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
