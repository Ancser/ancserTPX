"""Static guards for the two-tier Glass sampling contract.

These checks intentionally protect the small architectural seams that are easy
to erase while tuning appearance. Real-browser coverage owns the rendered
result; this file makes recursion, copy growth, and heartbeat regressions fail
fast in the normal pytest suite.
"""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GLASS_JS = ROOT / "frontend" / "static" / "tpx-glass.js"
GLASS_CSS = ROOT / "frontend" / "static" / "tpx-glass.css"
APP_CSS = ROOT / "frontend" / "static" / "ancserTPX.css"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


def test_tiers_are_marked_before_stage_templates_are_cloned():
    js = _source(GLASS_JS)
    build = _slice(js, "function buildOpticalSurfaces()", "function alignOpticalCopy")
    assert build.index("markGlassTiers();") < build.index(
        'const stages = Array.from(document.querySelectorAll("[data-stage]"))'
    )
    assert build.index("markGlassTiers();") < build.index(
        "stageTemplates.set(stage, cloneOpticalSource(stage))"
    )

    marker = _slice(js, "function markGlassTiers()", "function sampleInCopy")
    assert 'element.dataset.optical === "precision"' in marker
    assert '? "2"' in marker and ': "1"' in marker
    # Tiering annotates existing roots; it must not create another compositor.
    for mutation in ("cloneNode", "createElement", ".append", ".prepend"):
        assert mutation not in marker


def test_precision_source_role_survives_copy_replacement():
    js = _source(GLASS_JS)
    build = _slice(js, "function buildOpticalSurfaces()", "function alignOpticalCopy")
    replace = _slice(
        js, "function replaceSurfaceStageCopy", "function repairVisiblePrecisionCopies"
    )
    assert 'stageCopy.classList.add("optical-tier-2-source")' in build
    assert '"optical-tier-2-source"' in replace
    # Adding the role is metadata on the existing copy, not another stage clone.
    assert "cloneOpticalSource" not in replace
    assert replace.count("template.cloneNode(true)") == 1


def test_only_tier_one_fallbacks_are_reexposed_to_precision():
    js = _source(GLASS_JS)
    css = _source(GLASS_CSS)
    assert 'source.classList.contains("optical-layer")' in js
    assert ".optical-world .optical-layer { display: none !important; }" in css
    assert (
        '.optical-stage-copy.optical-tier-2-source [data-glass-tier="1"]'
        in css
    )
    assert (
        '.optical-stage-copy.optical-tier-2-source [data-glass-tier="2"]'
        in css
    )
    assert '.optical-stage-copy [data-glass-tier="2"]' in css
    assert (
        '.optical-stage-copy:not(.optical-tier-2-source) '
        '[data-glass-tier="1"]'
        in css
    )
    # No unscoped Tier-1 visibility rule may expose nested Glass elsewhere.
    assert '\n[data-glass-tier="1"]' not in css


def test_precision_release_has_one_shot_active_hydration():
    js = _source(GLASS_JS)
    bind = _slice(js, "function bindMirrors", "/* Which stages actually")
    release = _slice(js, "function releaseMirrorBuffers", "function mirrorCanvases")
    mirror = _slice(js, "function mirrorCanvases", "function surfaceSpringActive")
    spring = _slice(js, "function runSpringLoop", "const surfaceFor")
    build = _slice(js, "function buildOpticalSurfaces()", "function alignOpticalCopy")

    assert 'needsActiveHydration: component === "precision"' in build
    assert 'surface.component === "precision"' in release
    assert "surface.needsActiveHydration = true" in release
    assert 'component === "precision" && syncTarget?.needsActiveHydration' in spring
    assert "const ready = mirrorCanvases(syncTarget, true)" in spring
    assert "if (ready)" in spring
    assert spring.index("if (ready)") < spring.index(
        "syncTarget.needsActiveHydration = false"
    )
    assert "return validSources > 0" in mirror
    assert 'surface.component === "precision"' in bind
    assert "surface.mirrorPairs.some" in bind
    assert "syncOpticalSurfaces(component, false, syncTarget)" in spring
    # The forced blit exists only in the guarded one-shot spring path.
    assert js.count("mirrorCanvases(syncTarget, true)") == 1


def test_workspace_recovery_repairs_only_missing_visible_precision_topology():
    js = _source(GLASS_JS)
    repair = _slice(
        js, "function repairVisiblePrecisionCopies", "let pendingRetargetTimer"
    )
    retarget = _slice(js, "function scheduleStageRetarget", "function rebuildStageClones")

    assert 'surface.component !== "precision"' in repair
    assert "const topologyComplete = bindMirrors(surface)" in repair
    assert "surface.stageCopy.offsetWidth > 0" in repair
    assert "surface.stageCopy.offsetHeight > 0" in repair
    assert "if (!topologyComplete || !copyLaidOut)" in repair
    assert repair.count("cloneOpticalSource(surface.stage)") == 1
    assert "replaceSurfaceStageCopy(surface, template)" in repair
    assert 'syncOpticalSurfaces("precision", false, surface)' in repair
    assert "repairVisiblePrecisionCopies();" in retarget
    assert "requestAnimationFrame(() =>" in retarget
    assert retarget.count("repairVisiblePrecisionCopies();") == 2
    assert "rebuildStageClones" not in repair
    # Ordinary workspace switches with complete topology must not deep-clone.
    assert repair.index("if (!topologyComplete || !copyLaidOut)") < repair.index(
        "cloneOpticalSource(surface.stage)"
    )


def test_tier_one_animated_style_only_mirrors_to_precision_copies():
    js = _source(GLASS_JS)
    helper = _slice(
        js, "function forEachMatchingPrecisionClone", "function mirrorTextMutation"
    )
    mutation = _slice(js, "function mirrorAttributeMutation", "function markStageDirty")
    assert 'surface.component !== "precision"' in helper
    assert 'classList.contains("optical-tier-2-source")' in helper
    assert "correspondingCloneNode" in helper
    assert "stageTemplates" not in helper
    assert "mirroredTierOneStyle" in mutation
    assert "forEachMatchingPrecisionClone(target" in mutation
    assert 'target.closest("[data-optical]")' in mutation
    assert mutation.index("forEachMatchingPrecisionClone(target") < mutation.index(
        'target.closest("[data-optical]")'
    )


def test_popup_switches_use_shared_geometry_without_compact_shrink():
    css = _source(GLASS_CSS)
    assert ".glass-switch.interacting .switch-thumb {" in css
    # The old popup-only pseudo-layer applied a 60% center transform.  It was
    # removed so chart/sweep rows use the ordinary switch track/thumb directly.
    assert 'data-glass-sampling="material-only"' not in css
    app_css = _source(APP_CSS)
    assert ".chart-layer-pop .glass-switch," in app_css
    assert ".sweep-model-pop .glass-switch" in app_css
    assert "width: 2.8333rem;" in app_css
    assert "height: 1.1667rem;" in app_css
    assert "position: absolute;" in app_css[app_css.index(
        ".chart-layer-pop .switch-thumb,"
    ):]
    assert "z-index: 5;" in app_css[app_css.index(
        ".chart-layer-pop .switch-thumb,"
    ):]


def test_switch_sampling_copy_excludes_the_thumb_paint():
    css = _source(GLASS_CSS)
    selector = (
        '.optical-stage-copy > .switch-thumb[data-optical="switch"],'
        '\n.optical-stage-copy .glass-switch > '
        '.switch-thumb[data-optical="switch"]'
    )
    assert selector in css
    rule = css[css.index(selector):css.index("}", css.index(selector))]
    assert "visibility: hidden !important;" in rule
    assert "opacity: 0 !important;" in rule
    assert "background: transparent !important;" in rule


def test_silent_same_state_switch_sync_cannot_leave_dark_interaction_stuck():
    js = _source(GLASS_JS)
    tactile = _slice(js, "function initTactileSwitch", "function startMirrorHeartbeat")
    assert "if (!changed && silent && !pointerActive)" in tactile
    assert "activity.target = 0;" in tactile
    assert "beginSwitchIdleReturn();" in tactile


def test_locale_uses_the_existing_tactile_switch_controller_once():
    js = _source(GLASS_JS)
    tactile = _slice(js, "function initTactileSwitch", "function startMirrorHeartbeat")
    switch_init = _slice(
        js,
        'liveAll(".glass-switch").forEach((track) => {',
        'const account = live(".glass-account")',
    )
    assert 'track.addEventListener("click", (event) => {' in tactile
    assert "handledPointerClick" in tactile
    assert "handledKeyboardClick" in tactile
    assert "event.isTrusted" in tactile
    assert "commit(committed ? 0 : 1);" in tactile
    assert 'track.id === "lang-toggle"' in switch_init
    assert "window.toggleLanguage?.()" in switch_init
    assert switch_init.index('track.id === "lang-toggle"') < switch_init.index(
        'track.id === "theme-switch"'
    )


def test_tier_one_controls_do_not_hide_the_pointer_lens():
    js = _source(GLASS_JS)
    css = _source(GLASS_CSS)
    blocker = _slice(js, "const blocksLens = (target)", "const apply = () =>")
    tier = "target.closest('[data-glass-tier=\"1\"]')"
    assert tier in blocker
    assert blocker.index(tier) < blocker.index("target.closest(interactiveSelector)")
    lens = _slice(js, "const lensSwitchSelector =", "const scale =")
    assert 'const lensSwitchSelector =' in lens
    assert 'target.closest(lensSwitchSelector)' in blocker
    assert 'syncLensCoveredSwitch(event.target)' in js
    assert 'precision-under-lens' in js
    assert '.chart-layer-pop .glass-switch.precision-under-lens' in css
    assert ".chart-lens * { pointer-events: none !important; }" in css


def test_edge_debug_is_a_root_attribute_only():
    js = _source(GLASS_JS)
    method = _slice(js, "setEdgeDebug(on)", "/* ── tuner surface")
    assert 'document.documentElement.dataset.glassEdgeDebug = "on"' in method
    assert "delete document.documentElement.dataset.glassEdgeDebug" in method
    for side_effect in ("schedule", "rebuild", "clone", "createElement"):
        assert side_effect not in method


def test_diagnostics_expose_sampling_state_without_mutation():
    js = _source(GLASS_JS)
    diagnostics = _slice(js, "get diagnostics()", "function boot()")
    for field in (
        "stageCopies",
        "dirtyStages",
        "surfaceStates",
        "component",
        "tier",
        "stage",
        "copyStage",
        "sourceWidth",
        "sourceHeight",
        "mirrorNeedsHydration",
    ):
        assert f"{field}:" in diagnostics
    for side_effect in ("schedule", "rebuildStageClones", "createElement"):
        assert side_effect not in diagnostics
