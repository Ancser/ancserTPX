"""Canonical clock, browser-display, and rolling-contract contracts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import re

from backend.api.routes import get_config
from backend.db.models import StrategyParams, current_quarterly_contract_id
from backend.timebase import CHICAGO, NEW_YORK, UTC, topstep_trade_date, utc_now


ROOT = Path(__file__).resolve().parents[1]
BACKEND_PY = tuple(ROOT.glob("backend/**/*.py"))
FRONTEND_JS = ROOT / "frontend" / "static" / "ancserTPX.js"
SYSTEM_JS = ROOT / "frontend" / "static" / "tpx-system.js"
TOPSTEP_JS = ROOT / "frontend" / "static" / "topstep-eval.js"
HTML = ROOT / "frontend" / "static" / "ancserTPX.html"


def test_utc_is_not_new_york_and_new_york_offsets_follow_dst():
    winter = datetime(2026, 1, 15, 12, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 12, tzinfo=UTC)

    assert winter.astimezone(NEW_YORK).utcoffset() == timedelta(hours=-5)
    assert summer.astimezone(NEW_YORK).utcoffset() == timedelta(hours=-4)
    assert winter.astimezone(NEW_YORK).tzinfo != UTC


def test_topstep_trade_date_uses_the_single_chicago_1700_boundary():
    before = datetime(2026, 7, 15, 21, 59, tzinfo=UTC)  # 16:59 CDT
    after = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)    # 17:00 CDT

    assert before.astimezone(CHICAGO).hour == 16
    assert topstep_trade_date(before) == "2026-07-15"
    assert topstep_trade_date(after) == "2026-07-16"


def test_backend_uses_canonical_timezone_constructors_and_aware_utc_now():
    constructors: dict[str, list[str]] = {}
    for path in BACKEND_PY:
        text = path.read_text(encoding="utf-8")
        hits = re.findall(r'ZoneInfo\("America/[^\"]+"\)|timezone\.utc|datetime\.utcnow\(', text)
        if hits:
            constructors[path.relative_to(ROOT).as_posix()] = hits

    assert constructors == {
        "backend/timebase.py": [
            "timezone.utc",
            'ZoneInfo("America/New_York")',
            'ZoneInfo("America/Chicago")',
            'ZoneInfo("America/Los_Angeles")',
        ]
    }
    assert utc_now().tzinfo is UTC


def test_config_publishes_the_canonical_clock_and_contract_manifests():
    config = asyncio.run(get_config())

    assert config["time_zones"] == {
        "data": "UTC",
        "market": "America/New_York",
        "topstep": "America/Chicago",
        "pi_source": "America/Los_Angeles",
    }
    assert config["front_month_contracts"] == {
        symbol: current_quarterly_contract_id(symbol)
        for symbol in ("MNQ", "ENQ", "MES")
    }
    assert config["contract_specs"]["MNQ"]["point_value"] == 2.0
    assert config["contract_specs"]["MNQ"]["tick_size"] == 0.25


def test_production_defaults_roll_instead_of_naming_a_fixed_expiry():
    before_roll = datetime(2026, 9, 9, tzinfo=UTC)
    on_roll = datetime(2026, 9, 10, tzinfo=UTC)

    assert current_quarterly_contract_id("MNQ", before_roll).endswith(".U26")
    assert current_quarterly_contract_id("MNQ", on_roll).endswith(".Z26")
    assert StrategyParams().contract_id == current_quarterly_contract_id("MNQ")

    product_text = "\n".join([
        FRONTEND_JS.read_text(encoding="utf-8"),
        HTML.read_text(encoding="utf-8"),
    ])
    assert not re.search(r"CON\.F\.US\.(?:MNQ|ENQ)\.[HMUZ]\d{2}", product_text)


def test_browser_has_one_boot_manifest_and_only_shifts_utc_for_display():
    system_js = SYSTEM_JS.read_text(encoding="utf-8")
    app_js = FRONTEND_JS.read_text(encoding="utf-8")
    topstep_js = TOPSTEP_JS.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")

    for name in ("America/New_York", "America/Chicago", "America/Los_Angeles"):
        assert name in system_js
        assert name not in app_js
        assert name not in topstep_js
    assert "Object.assign(SYSTEM_TIME_ZONES, cfg.time_zones || {})" in app_js
    assert "Object.assign(FRONT_MONTH_CONTRACTS, cfg.front_month_contracts || {})" in app_js
    assert "getTimezoneOffset()" in app_js
    assert (
        html.index('src="/static/tpx-system.js')
        < html.index('src="/static/topstep-eval.js')
        < html.index('src="/static/ancserTPX.js')
    )
