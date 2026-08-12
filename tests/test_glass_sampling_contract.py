"""Static guards for the two-tier Glass sampling contract.

These checks intentionally protect the small architectural seams that are easy
to erase while tuning appearance. Real-browser coverage owns the rendered
result; this file makes recursion, copy growth, and heartbeat regressions fail
fast in the normal pytest suite.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GLASS_JS = ROOT / "frontend" / "static" / "tpx-glass.js"
GLASS_CSS = ROOT / "frontend" / "static" / "tpx-glass.css"
APP_CSS = ROOT / "frontend" / "static" / "ancserTPX.css"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    """Source with /* block comments */ removed.

    This file is full of "X must not exist" guards, and this codebase
    deliberately keeps a comment naming X wherever X was deleted, so the next
    person does not reintroduce it.  Matching those guards against raw text
    makes the explanation itself trip the assertion, which pushes people to
    delete the explanation.  Negative assertions read this instead.
    """
    return re.sub(r"/\*.*?\*/", "", _source(path), flags=re.DOTALL)


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


def test_switch_sampling_copy_keeps_the_thumb_but_never_its_lens_state():
    """1.0.10p: a copy shows the control AS DRAWN, at rest.

    Two opposite mistakes are covered here.  Blanking the thumb inside every
    stage copy deletes it from Precision's magnified image, where the copy is
    the only thing being shown.  Letting the copy inherit the lens-up state is
    worse: clones are built before optical layers are mounted, so `interacting`
    plus a mirrored --switch-glass paints the dark backdrop with no lens behind
    it -- a solid --bg pill drawn over the live thumb.
    """
    css = _source(GLASS_CSS)
    assert ('.optical-stage-copy > .switch-thumb[data-optical="switch"]'
            not in _code(GLASS_CSS))

    selector = (
        ".optical-stage-copy .glass-switch,"
        "\n.optical-stage-copy.glass-switch {"
    )
    assert selector in css
    rule = css[css.index(selector):css.index("}", css.index(selector))]
    assert "--switch-glass: 0 !important;" in rule

    resting = (
        ".optical-stage-copy .glass-switch.interacting > .switch-thumb,"
        "\n.optical-stage-copy.glass-switch.interacting > .switch-thumb {"
    )
    assert resting in css
    resting_rule = css[css.index(resting):css.index("}", css.index(resting))]
    assert "background: var(--switch-thumb-color);" in resting_rule


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
    assert ".chart-lens * { pointer-events: none !important; }" in css

    # 1.0.10p: ...but a control raising its OWN lens is the exception, and it
    # has to be tested before the tier-1 bypass or the popup's tier mark wins.
    # Precision would otherwise paint a still clone of the switch on top of
    # the live refraction.  The occlusion machinery that used to paper over
    # this (hiding the live thumb under the lens) is gone: `visibility`
    # inherits, so it took the thumb's own .optical-layer with it.
    interacting = 'target.closest(".glass-switch.interacting")'
    assert interacting in blocker
    assert blocker.index(interacting) < blocker.index(tier)
    js_code = _code(GLASS_JS)
    css_code = _code(GLASS_CSS)
    assert "lensSwitchSelector" not in js_code
    assert "syncLensCoveredSwitch" not in js_code
    assert "precision-under-lens" not in js_code
    assert "precision-under-lens" not in css_code


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
