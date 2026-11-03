"""Preset strategy names: migrate known aliases, never rewrite unknown ones.

2026-09-15: a server still running an older build loaded presets.json, did not
know "volume_profile", normalized it to "factor" and saved the file -- the
VOLUME PROFILE RESEARCH preset then always opened as FACTOR.  One shared
definition (backend.db.models.known_strategy_name) now serves request
dispatch, the web preset loader and the terminal preset loader.
"""
from __future__ import annotations

import copy

from backend.api import routes
from backend.db.models import SUPPORTED_STRATEGIES, known_strategy_name


def _doc(**presets):
    return {
        "presets": {name: dict(params) for name, params in presets.items()},
        "preset_schema": routes._PRESET_SCHEMA_VERSION,
        "last_used_bt": "default",
        "last_used_live": "default",
    }


def test_shared_definition_maps_aliases_and_retired_families():
    assert known_strategy_name("VP") == "volume_profile"
    assert known_strategy_name("sessfib") == "betafib"
    assert known_strategy_name("option_wall") == "optionwall"
    for retired in ("trend", "pmo", "mlc2"):
        assert known_strategy_name(retired) == "factor"
    assert known_strategy_name("tomorrow_model") is None
    assert all(known_strategy_name(name) == name for name in SUPPORTED_STRATEGIES)


def test_loader_keeps_supported_and_migrates_retired_names():
    data, _changed = routes._ensure_builtin_presets(_doc(
        VP={"strategy": "volume_profile", "contract_id": "CON.F.US.MNQ.Z26"},
        OLD={"strategy": "trend", "contract_id": "CON.F.US.MNQ.Z26"},
    ))
    assert data["presets"]["VP"]["strategy"] == "volume_profile"   # positive: kept
    assert data["presets"]["VP"]["tr_allowed_sessions"] == ["RTH"]
    assert data["presets"]["OLD"]["strategy"] == "factor"


def test_loader_never_rewrites_a_strategy_this_build_does_not_know():
    original = {"strategy": "tomorrow_model", "contract_id": "CON.F.US.MNQ.Z26", "x": 1}
    data, _changed = routes._ensure_builtin_presets(_doc(NEW=copy.deepcopy(original)))
    assert data["presets"]["NEW"]["strategy"] == "tomorrow_model"


def test_dispatch_still_runs_factor_for_an_unknown_request_value():
    assert routes._normalize_strategy_name("tomorrow_model") == "factor"
    assert routes._normalize_strategy_name("volume_profile") == "volume_profile"
