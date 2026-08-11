"""Cross-process ownership lease for a live trading engine.

The web and terminal runners can be launched independently.  A process-local
``_live_engines`` dictionary cannot stop both of them from polling Discord and
placing the same signal.  This module uses an OS advisory lock held for the
entire engine lifetime, keyed by account id.  The lock file is intentionally
kept after release; stale metadata is diagnostic only and never blocks a new
owner because the OS lock, not the file contents, is authoritative.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union


PathLike = Union[str, os.PathLike[str]]


class LiveEngineLease:
    """Hold one account's live-engine lock until :meth:`release` is called."""

    def __init__(self, account_id: int, path: Optional[PathLike] = None):
        self.account_id = int(account_id)
        if path is None:
            root = Path(__file__).resolve().parents[2]
            path = root / "data" / "logs" / f"live_engine_{self.account_id}.lock"
        self.path = Path(path)
        self._handle = None
        self._locked = False

    @property
    def held(self) -> bool:
        return self._locked

    def acquire(self) -> bool:
        """Try to claim the account without waiting.

        Returns ``False`` when another process already owns the account.  All
        filesystem errors are treated as a failed claim: starting a live engine
        without a verifiable ownership lock is unsafe.
        """
        if self._locked:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)

            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            self._handle = handle
            self._locked = True
            self._write_metadata()
            return True
        except (OSError, ValueError):
            try:
                handle.close()  # type: ignore[union-attr]
            except (NameError, AttributeError, OSError, ValueError):
                pass
            self._handle = None
            self._locked = False
            return False

    def _write_metadata(self) -> None:
        if not self._locked or self._handle is None:
            return
        metadata = {
            "pid": os.getpid(),
            "account_id": self.account_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        payload = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(payload)
        self._handle.flush()

    def release(self) -> None:
        """Release the OS lock; safe to call repeatedly."""
        handle = self._handle
        self._handle = None
        was_locked = self._locked
        self._locked = False
        if handle is None:
            return
        try:
            handle.seek(0)
            if was_locked:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            try:
                handle.close()
            except (OSError, ValueError):
                pass

    def __enter__(self) -> "LiveEngineLease":
        if not self.acquire():
            raise RuntimeError(f"live engine account {self.account_id} is already owned")
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

