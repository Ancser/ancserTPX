"""Process-local, record-only PI Discord service.

The trading listener belongs to a Live engine and must remain coupled to its
strategy queue.  This companion service is intentionally separate: it keeps a
durable audit/chart feed alive when the web page or terminal is connected but
no Live engine is running, and performs a bounded today/yesterday catch-up on
startup.  It never invokes a strategy callback.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from .pi_listener import PiListener

logger = logging.getLogger(__name__)


class PiAuditRecorder:
    """Own one record-only listener and a small pause/resume reference count."""

    def __init__(self, token: str, *, poll_seconds: float = 30.0) -> None:
        self._token = token
        self._poll_seconds = poll_seconds
        self._listener: Optional[PiListener] = None
        self._task: Optional[asyncio.Task] = None
        self._pause_depth = 0

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    @property
    def listener(self) -> Optional[PiListener]:
        return self._listener

    @property
    def task(self) -> Optional[asyncio.Task]:
        return self._task

    async def start(self) -> bool:
        """Start catch-up + continuous record-only polling if configured."""
        if not self.enabled or self._pause_depth:
            return False
        if self._task is not None and not self._task.done():
            return True

        listener = PiListener(
            self._token,
            lambda _sig: None,
            poll_seconds=self._poll_seconds,
            rate_limit_per_min=30,
            record_only=True,
        )
        self._listener = listener

        async def worker() -> None:
            try:
                await listener.backfill_recent(days=2)
                await listener.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - last-resort process guard
                logger.exception("[PI] record-only listener stopped: %s", exc)

        self._task = asyncio.create_task(worker(), name="pi-record-only")
        logger.info("[PI] record-only listener scheduled (today/yesterday catch-up)")
        return True

    async def stop(self) -> None:
        """Stop the worker without touching the durable audit file."""
        listener = self._listener
        task = self._task
        self._listener = None
        self._task = None
        if listener is not None:
            listener.stop()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def pause(self) -> bool:
        """Pause while a PI Live engine owns the real-time listener."""
        if not self.enabled:
            return False
        self._pause_depth += 1
        if self._pause_depth == 1:
            await self.stop()
        return True

    async def resume(self) -> bool:
        """Resume after the final PI Live engine releases the pause."""
        if not self.enabled:
            return False
        if self._pause_depth:
            self._pause_depth -= 1
        if self._pause_depth == 0:
            return await self.start()
        return True

    def health(self) -> dict:
        listener = self._listener
        health = listener.get_health() if listener is not None else {}
        health.update({
            "enabled": self.enabled,
            "paused": self._pause_depth > 0,
            "pause_depth": self._pause_depth,
            "task_alive": bool(self._task is not None and not self._task.done()),
            "record_only": True,
        })
        return health


_recorder: Optional[PiAuditRecorder] = None


def _get_recorder() -> PiAuditRecorder:
    global _recorder
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if _recorder is None or _recorder._token != token:
        _recorder = PiAuditRecorder(token)
    return _recorder


async def start_pi_recorder() -> bool:
    return await _get_recorder().start()


async def stop_pi_recorder() -> None:
    if _recorder is not None:
        await _recorder.stop()


async def pause_pi_recorder() -> bool:
    return await _get_recorder().pause()


async def resume_pi_recorder() -> bool:
    return await _get_recorder().resume()


def pi_recorder_health() -> dict:
    return _get_recorder().health()

