"""Notify-only helper: POST new PI Discord events to a Grok Bot webhook.

This never places orders and must not share Discord credentials. If the
webhook URL is unset, every call is a no-op so live trading is unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_URL_ENV = "GROK_PI_WEBHOOK_URL"
_AUTH_ENV = "GROK_PI_WEBHOOK_AUTHORIZATION"
_KEY_ENV = "GROK_PI_WEBHOOK_KEY"
_TIMEOUT_SEC = 5


def notify_pi_webhook(payload: dict[str, Any]) -> None:
    url = os.getenv(_URL_ENV, "").strip()
    if not url:
        return
    auth = os.getenv(_AUTH_ENV, "").strip()
    key = os.getenv(_KEY_ENV, "").strip()
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
            resp.read()
    except Exception as exc:
        # Never log the URL, authorization, or payload contents.
        logger.warning("[PI] grok webhook notify failed: %s", type(exc).__name__)
