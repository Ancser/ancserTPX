"""Presentation contracts for the 1.0.10 Glass/UI repair.

These are deliberately static contracts.  The real-browser smoke suite proves
paint/timing; these tests keep presentation-only edits from changing strategy
values, duplicating locale state, or turning popup controls into optical
surfaces that each allocate a stage clone.
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
HTML = (STATIC / "ancserTPX.html").read_text(encoding="utf-8")
JS = (STATIC / "ancserTPX.js").read_text(encoding="utf-8")
CSS = (STATIC / "ancserTPX.css").read_text(encoding="utf-8")
GLASS_CSS = (STATIC / "tpx-glass.css").read_text(encoding="utf-8")
GLASS_JS = (STATIC / "tpx-glass.js").read_text(encoding="utf-8")
SKIN_JS = (STATIC / "tpx-glass-skin.js").read_text(encoding="utf-8")


def _code(source: str) -> str:
    """Source with /* block comments */ removed.

    "X must not exist" guards have to ignore the comment that explains why X
    was removed, or keeping that explanation would fail the test.
    """
    return re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)

CANONICAL = [
    ("fade", "FADE"),
    ("sigma", "SIGMA"),
    ("factor", "FACTOR"),
    ("momentum", "MOMENTUM"),
    ("betafib", "BETAFIB"),
    ("pi", "PI"),
]


def _select_options(select_id: str) -> list[tuple[str, str]]:
    match = re.search(
        rf'<select\b[^>]*\bid="{re.escape(select_id)}"[^>]*>(.*?)</select>',
        HTML,
        re.DOTALL,
    )
    assert match, f"missing #{select_id}"
    return [
        (value, re.sub(r"\s+", " ", label).strip())
        for value, label in re.findall(
            r'<option\b[^>]*\bvalue="([^"]+)"[^>]*>(.*?)</option>',
            match.group(1),
            re.DOTALL,
        )
    ]


def _function_source(name: str) -> str:
    start = JS.index(f"function {name}(")
    brace = JS.index("{", start)
    depth = 0
    for index in range(brace, len(JS)):
        if JS[index] == "{":
            depth += 1
        elif JS[index] == "}":
            depth -= 1
            if depth == 0:
                return JS[start:index + 1]
    raise AssertionError(f"unterminated JS function {name}")


def test_model_selectors_keep_payload_values_and_show_only_canonical_identity():
    assert _select_options("strategy-bt") == CANONICAL
    assert _select_options("strategy-live") == CANONICAL


def test_strategy_descriptions_are_separate_and_follow_selection_and_language():
    for mode in ("bt", "live"):
        assert f'aria-describedby="strategy-desc-{mode}"' in HTML
        assert f'id="strategy-desc-{mode}" class="strategy-description"' in HTML
    for strategy, display in CANONICAL:
        assert re.search(
            rf"\b{strategy}:\s*\{{.*?displayName:\s*'{display}'.*?"
            rf"description:\s*\{{\s*en:\s*'[^']+'.*?zh:\s*'[^']+'",
            JS,
            re.DOTALL,
        )
    assert "syncStrategyDescription(mode);" in _function_source("_setStrategySelect")
    apply = _function_source("applyLanguage")
    assert "['bt', 'live'].forEach((mode) =>" in apply
    assert "syncStrategyDescription(mode);" in apply


def test_status_and_new_preset_names_use_canonical_identity_but_legacy_names_parse():
    assert "return strategyPresentation(value).displayName;" in _function_source("strategyDisplayName")
    assert "return strategyDisplayName((params || {}).strategy);" in _function_source("_namingModelFromParams")
    parser = _function_source("_presetNameMeta")
    for historical in ("TREND", "DAY ZONE", "DISTRIBUTION", "PMO", "BETA FIB"):
        assert historical in parser
    for _, canonical in CANONICAL:
        assert canonical in parser


def test_inline_prose_migrates_to_keyboard_and_pointer_help_but_validation_stays_visible():
    assert HTML.count("validation-hint") == 2
    migrate = _function_source("migrateInlineHelp")
    assert "querySelectorAll('.lbl-hint:not(.validation-hint)')" in migrate
    assert "source.nextElementSibling?.matches('.form-row')" in migrate
    assert "followingRow?.querySelector('label')" in migrate
    assert "label ? label.querySelector('.help-dot') : null" in migrate
    assert "label.insertBefore(dot, inlineHint || null)" in migrate
    assert "_attachInlineHelpSource(dot, source)" in migrate
    assert ".inline-help-source { display: none !important; }" in CSS
    assert ".validation-hint { display: inline; }" in CSS

    configure = _function_source("_configureHelpDot")
    for event in ("mouseenter", "mouseleave", "focus", "blur", "click", "keydown"):
        assert f"addEventListener('{event}'" in configure
    assert "event.key !== 'Escape'" in configure
    assert "document.createElement('button')" in _function_source("_newHelpDot")
    assert "role', 'tooltip'" in _function_source("getHelpTooltip")
    localized = _function_source("_localizedHelpTip")
    assert "return { en: value, zh: value }" in localized
    add = _function_source("addHelpDot")
    assert "const attr = 'data-tip-' + lang" in add
    show = _function_source("showHelpTooltip")
    assert "dot.getAttribute('data-tip-' + UI_LANG)" in show
    assert "dot.getAttribute('data-tip')" not in show


def test_language_switch_is_one_current_locale_thumb_on_the_existing_authority():
    assert HTML.count('id="lang-toggle"') == 1
    assert '<html lang="en">' in HTML
    assert (
        'id="lang-toggle" class="lang-toggle glass-switch topbar-lang" role="switch"'
        in HTML
    )
    assert 'data-locale="en" data-stage="switch"' in HTML
    assert (
        '<span class="optical-surface switch-thumb lang-thumb" '
        'data-optical="switch" aria-hidden="true">'
        in HTML
    )
    assert (
        '<span class="surface-content switch-state-icon lang-glyph">En</span>'
        in HTML
    )
    language_markup = HTML[HTML.index('id="lang-toggle"'):HTML.index('</button>', HTML.index('id="lang-toggle"'))]
    assert 'onclick="toggleLanguage()"' not in language_markup
    apply = _function_source("applyLanguage")
    assert "btn.dataset.locale = UI_LANG" in apply
    assert "btn.querySelector(':scope > .lang-thumb > .lang-glyph')" in apply
    assert "if (btn.tpxSetState) btn.tpxSetState(isZh)" in apply
    assert "else btn.classList.toggle('on', isZh)" in apply
    assert "btn.setAttribute('aria-checked', isZh ? 'true' : 'false')" in apply
    assert "glyph.textContent = isZh ? '中' : 'En'" in apply
    assert "document.documentElement.lang = UI_LANG === 'zh' ? 'zh-TW' : 'en'" in apply
    assert "btn.textContent" not in apply
    assert ".lang-toggle.glass-switch," in CSS
    assert ".lang-toggle.glass-switch.on {" in CSS
    assert "#lang-toggle.glass-switch" not in CSS


def test_language_and_theme_marks_share_the_existing_thumb_and_hide_while_moving():
    theme = SKIN_JS[SKIN_JS.index('themeTrack.id = "theme-switch"'):]
    theme = theme[:theme.index("right.appendChild(themeTrack)")]
    assert '"span", "surface-content switch-state-icon theme-state-icon"' in theme
    assert 'themeIcon.id = "theme-icon"' in theme
    assert 'themeIcon.textContent = "☾"' in theme
    assert "themeThumb.appendChild(themeIcon)" in theme
    assert "data-optical" not in theme

    assert ".switch-thumb > .switch-state-icon {" in GLASS_CSS
    assert (
        ".glass-switch.interacting .switch-thumb > .switch-state-icon { opacity: 0; }"
        in GLASS_CSS
    )
    assert ".theme-state-icon {" in GLASS_CSS
    assert 'icon.textContent = light ? "☀" : "☾"' in GLASS_JS


def test_pi_matrix_switches_use_the_real_optical_thumb_surface():
    """The PI matrix must not fall back to a plain, unpositioned thumb span."""
    for mode in ("bt", "live"):
        start = HTML.index(f'id="pi-params-{mode}"')
        end = HTML.index('class="pi-legacy-controls"', start)
        matrix = HTML[start:end]
        assert matrix.count('class="optical-surface switch-thumb"') == 6
        assert matrix.count('data-optical="switch"') == 6


def test_red_performance_threshold_mark_has_an_exclamation():
    # Amber and red warnings share one Unicode glyph; only the semantic wrapper color differs.
    assert "tpx-alert-mark" in JS
    assert "_alertMark" in JS
    assert "&#9888;" in JS
    assert ".tpx-alert-mark" in CSS
    assert ".tpx-alert-mark::before" not in CSS
    assert ".tpx-alert-mark::after" not in CSS
    assert "tpx-danger-mark" not in JS
    assert "tpx-danger-triangle" not in CSS
    assert "tpx-danger-exclamation" not in CSS


def test_live_pi_audit_overlay_is_read_only_and_backtest_replay_is_explicit():
    refresh = _function_source("refreshPiSignalMarkers")
    assert "API + '/pi/signals?'" in refresh
    # 1.0.10p: the limit alone is not the contract — `events=` has to be there
    # too. The listener writes a heartbeat row every poll, so an unfiltered
    # 2000-row window covered 11 hours and held 1 of the file's 12 signals.
    assert "/pi/signals/audit?limit=2000&events=received,recorded" in refresh
    assert "event.event !== 'received'" in refresh
    assert "event.event !== 'recorded'" in refresh
    assert "Preset acceptance never controls chart visibility." in refresh
    assert "activeTab === 'live'" in refresh
    assert "activeTab === 'backtest'" in refresh
    assert "Chart rendering is read-only" in refresh
    assert "explicitly runs a PI" in refresh
    assert "pi_history" not in refresh
    assert "activeTab !== 'live'" not in refresh


def test_parameter_source_is_english_and_pi_payload_values_are_unchanged():
    sidebar_start = HTML.index('<div class="sidebar">')
    sidebar_end = HTML.index('<!-- Main Content -->', sidebar_start)
    sidebar = re.sub(
        r"<!--.*?-->",
        "",
        HTML[sidebar_start:sidebar_end],
        flags=re.DOTALL,
    )
    assert not re.search(r"[\u3400-\u9fff]", sidebar)

    expected_pi_signal_set = [
        ("long_pi_only", "LONG ONLY · π LEVELS (RECOMMENDED)"),
        ("long_all", "LONG ONLY · ALL BLUE (INCLUDES LIGHT-BLUE CIRCLE)"),
        ("pi_only", "π LEVELS + DARK-BLUE CIRCLE (INCLUDES SHORTS)"),
        ("pi_strict", "PURE π ONLY (CYAN π / PINK π)"),
        ("all", "ALL BLUE/PURPLE (INCLUDES WEAK SIGNALS)"),
    ]
    for mode in ("bt", "live"):
        assert _select_options(f"pi-signal-set-{mode}") == expected_pi_signal_set
        assert _select_options(f"pi-long-only-{mode}") == [
            ("1", "LONG ONLY (RECOMMENDED)"),
            ("0", "LONG + SHORT"),
        ]

    for english in (
        "OBSERVATION WINDOW",
        "SIGNAL SET",
        "MAX SIGNAL AGE",
        "MOVE MIN",
        "ENTRY FIB",
        "SHORT TIME EXIT",
        "PI π / CIRCLES",
        "BETAFIB LEVELS",
    ):
        assert f"'{english}':" in JS


def test_pi_matrix_is_two_column_glass_switch_ui_and_keeps_legacy_wire_fields():
    for mode in ("bt", "live"):
        matrix = HTML[HTML.index(f'data-pi-matrix="{mode}"'):]
        matrix = matrix[:matrix.index("</div>", matrix.index("pi-matrix-note"))]
        assert f'id="pi-matrix-{mode}-long-pi"' in matrix
        assert f'id="pi-matrix-{mode}-short-pi"' in matrix
        assert f'id="pi-matrix-{mode}-long-level2"' in matrix
        assert f'id="pi-matrix-{mode}-long-level1"' in matrix
        assert f'id="pi-matrix-{mode}-short-level1"' in matrix
        assert f'id="pi-matrix-{mode}-short-level1" data-stage="switch" disabled' in matrix
        assert "class=\"glass-switch pi-matrix-switch" in matrix
        assert "LONG" in matrix and "SHORT" in matrix
        assert "LEVEL 2" in matrix and "LEVEL 1" in matrix
        assert "SHORT LEVEL 1/2 bubbles are recorded only" in matrix
        assert f'id="pi-signal-set-{mode}"' in HTML
        assert f'id="pi-long-only-{mode}"' in HTML

    assert "show('factor-params-' + mode, isFactor || isIntramom || isSessfib);" in JS
    assert "pi_long_kinds: piMatrix.pi_long_kinds" in JS
    assert "pi_short_kinds: piMatrix.pi_short_kinds" in JS


def test_requested_control_geometry_is_explicit_and_consistent():
    multiplier = CSS[CSS.index(".form-mult {"):CSS.index("}", CSS.index(".form-mult {"))]
    tuner = GLASS_CSS[
        GLASS_CSS.index(".glass-tuner .tuner-trigger {"):
        GLASS_CSS.index("}", GLASS_CSS.index(".glass-tuner .tuner-trigger {"))
    ]
    assert "font-size: .75rem" in multiplier
    assert "width: 2.625rem" in tuner
    assert "height: 2.625rem" in tuner


def test_retired_auto_center_is_absent_but_other_chart_tools_remain():
    for source in (HTML, JS, CSS, GLASS_CSS, GLASS_JS, SKIN_JS):
        for retired in (
            "btn-auto-center",
            "toggleAutoCenter",
            "_autoCenterProvider",
            "_autoCenterOn",
            "_acSpan",
            "_acMid",
            "_acOffset",
            "_acKickRAF",
            "_acEma",
            "_acBindDrag",
            "fabLatch",
            ".fab-action.latched",
            ".chart-sq-btn.active",
        ):
            assert retired not in source

    assert (
        'id="btn-scroll-latest" class="chart-sq-btn" '
        'title="Jump to the latest candle" onclick="scrollToLatest()"'
        in HTML
    )
    assert 'id="chart-layer-btn"' in HTML
    assert "chart.timeScale().scrollToRealTime()" in _function_source("scrollToLatest")
    assert "crosshair:" in JS
    assert "autoscaleInfoProvider" in _function_source("applyDefaultChartView")
    assert "const idx = _nearestBarIndex(sec)" in _function_source("_timeToXViaBars")


def test_chart_layer_popup_contract_uses_per_switch_optical_surfaces():
    start = HTML.index('<div id="chart-layer-pop"')
    end = HTML.index('<div id="chart-quick-btns"', start)
    popup = HTML[start:end]
    assert 'class="chart-layer-pop hidden"' in popup
    assert 'data-glass-scene="chart"' in popup
    assert 'data-glass-tier="1"' in popup
    assert 'data-glass-material="popup"' in popup
    assert popup.count('data-glass-material="local"') == 10
    # Repeated rows use the PI matrix's real optical thumb path, with the
    # ordinary switch geometry AND the ordinary switch optics.  1.0.10p: a
    # per-surface data-glass-shrink="0.20" override made these the only
    # switches on the page with their own sampling; the brief was parity with
    # the parameter switches, so shrink comes from settings.switch alone.
    assert 'data-glass-sampling="material-only"' not in popup
    assert popup.count('data-optical="switch"') == 10
    assert "data-glass-shrink" not in popup
    assert "dataset.glassShrink" not in _code(GLASS_JS)
    assert "const config = settings[surface.component];" in GLASS_JS

    assert "#chart-layer-pop" not in CSS
    assert ".chart-layer-pop {" in CSS
    assert ':root[data-glass-edge-debug="on"] .chart-layer-pop' in CSS
    popup_rule = CSS[CSS.index(".chart-layer-pop {"):CSS.index(".chart-layer-pop.hidden")]
    assert "border: 1px solid var(--glass-rim" in popup_rule
    assert "box-shadow: var(--glass-relief" in popup_rule
    assert "backdrop-filter: blur(20px) saturate(1.3)" in popup_rule
    assert ".chart-layer-pop::before" not in CSS
    assert ".chart-layer-pop::after" not in CSS
    assert ".chart-layer-pop .glass-switch," in CSS
    assert ".sweep-model-pop .glass-switch" in CSS
    # 1.0.10p: a thumb is the lens, never something to occlude.  Two rules
    # that hid it are gone for good -- one blanked the thumb inside every
    # stage copy, the other hid the LIVE thumb whenever Precision passed over
    # it.  `visibility` inherits, so the second also killed the .optical-layer
    # nested in the thumb: pressing a popup switch left a bare green pill.
    assert ('.optical-stage-copy > .switch-thumb[data-optical="switch"]'
            not in _code(GLASS_CSS))
    assert "precision-under-lens" not in _code(GLASS_JS)
    assert "precision-under-lens" not in _code(GLASS_CSS)
    assert "width: 2.8333rem;" in CSS
    assert "height: 1.1667rem;" in CSS
    layer_sync = _function_source("buildChartLayerMenu")
    assert "const current = tr.getAttribute('aria-checked') === 'true';" in layer_sync
    assert layer_sync.index("if (current === on) return;") < layer_sync.index("tr.tpxSetState(on)")


def test_switch_lens_material_and_backdrop_share_one_clock():
    """1.0.10p: the lens-up thumb is a PAIR (dark backdrop + optical layer).

    Both halves must be driven by --switch-glass, which apply() eases out on
    the spring.  Binding either half to the .interacting class instead splits
    them across two clocks: apply() drops the class the frame the thumb stops
    moving while the spring keeps running for ~70ms, so a class-triggered
    `background` transition repainted the thumb solid --bg after the lens had
    already faded -- a black blink on every release.
    """
    backdrop = GLASS_CSS[
        GLASS_CSS.index(".switch-thumb::before {"):
        GLASS_CSS.index(".glass-switch.interacting .switch-thumb {")
    ]
    assert "background: var(--bg);" in backdrop
    assert "opacity: var(--switch-glass, 0);" in backdrop

    # The class must no longer own the backdrop colour...
    interacting = GLASS_CSS[
        GLASS_CSS.index(".glass-switch.interacting .switch-thumb {"):
        GLASS_CSS.index(".glass-switch.interacting .switch-thumb .optical-layer")
    ]
    assert "background:" not in interacting
    # ...and the lens it pairs with reads the same variable.
    assert ".switch-thumb .optical-layer { opacity: var(--switch-glass, 0); }" in GLASS_CSS


def test_stage_clones_never_paint_the_lens_up_switch_material():
    """A clone has no .optical-layer, so it must never show the lens state.

    Stage copies are cloned in buildOpticalSurfaces() BEFORE optical layers
    are mounted, but mirrorAttributeMutation copies `class` and the inline
    --switch-glass into them verbatim.  Without this reset the Precision copy
    painted a solid --bg pill over the live, correctly-refracting thumb --
    which is what turned the CHART LAYERS switches black mid-drag.
    """
    assert ".optical-stage-copy .glass-switch,\n.optical-stage-copy.glass-switch {" in GLASS_CSS
    reset = GLASS_CSS[GLASS_CSS.index(".optical-stage-copy .glass-switch,"):]
    # !important is load-bearing: it has to beat the mirrored inline style.
    assert "--switch-glass: 0 !important;" in reset[:reset.index("}")]

    # Precision must also step aside while a switch is raising its own lens,
    # and that test has to precede the tier-1 bypass or the popup's own tier
    # mark wins and the clone is drawn on top anyway.
    start = GLASS_JS.index("const blocksLens = (target) => {")
    blocks = GLASS_JS[start:GLASS_JS.index("const apply = () => {", start)]
    assert 'if (target.closest(".glass-switch.interacting")) return true;' in blocks
    assert blocks.index(".glass-switch.interacting") < blocks.index('data-glass-tier="1"')


def test_sweep_model_dropdown_contract_uses_glass_switches_and_preserves_scope_names():
    start = HTML.index('<div class="sweep-action-row"')
    end = HTML.index('<div id="backtest-progress-wrap"', start)
    sweep = HTML[start:end]
    assert 'id="btn-sweep"' in sweep
    assert 'id="sweep-model-btn"' in sweep
    assert 'onclick="toggleSweepModelMenu()"' in sweep
    assert 'id="sweep-model-pop" class="sweep-model-pop hidden"' in sweep
    # 1.0.10p: derive the count from the markup instead of hardcoding it.
    # WHICH models are offered is owned by test_sweep_model_scope.py, which
    # checks them against the backend dispatch; pinning a number here as well
    # only means every model added or removed breaks an unrelated test.
    models = re.findall(r'data-sweep-model="([^"]+)"', sweep)
    assert "ALL" in models
    assert len(models) >= 2, "dropdown needs ALL plus at least one model"
    assert sweep.count('class="glass-switch sweep-model-switch') == len(models)
    # The trigger is intentionally a regular Sweep-style button. Only the
    # popup thumbs use optical sampling; the trigger must not inherit a
    # shrink lens or clone the sidebar behind its square affordance.
    assert 'sweep-model-trigger-glass' not in sweep
    assert sweep.count('data-optical="switch"') == len(models)
    assert sweep.count('data-stage="switch"') == len(models)
    assert 'data-optical="switch"' not in sweep.split('id="sweep-model-pop"', 1)[0].split('id="sweep-model-btn"', 1)[0]
    assert "height: 42px;" in CSS
    assert "function _sweepModelSelection()" in JS
    assert "const _mm = _sweepModelSelection();" in _function_source("runBacktestSweep")
    assert "sweep-model-scope-bt" not in JS
