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


def test_chart_layer_popup_contract_is_clone_safe_without_per_switch_optical_surfaces():
    start = HTML.index('<div id="chart-layer-pop"')
    end = HTML.index('<div id="chart-quick-btns"', start)
    popup = HTML[start:end]
    assert 'class="chart-layer-pop hidden"' in popup
    assert 'data-glass-scene="chart"' in popup
    assert 'data-glass-tier="1"' in popup
    assert 'data-glass-material="popup"' in popup
    assert popup.count('data-glass-material="local"') == 10
    assert popup.count('data-glass-sampling="material-only"') == 10
    assert "data-optical" not in popup

    assert "#chart-layer-pop" not in CSS
    assert ".chart-layer-pop {" in CSS
    assert ':root[data-glass-edge-debug="on"] .chart-layer-pop' in CSS
    popup_rule = CSS[CSS.index(".chart-layer-pop {"):CSS.index(".chart-layer-pop.hidden")]
    assert "border: 1px solid var(--glass-rim" in popup_rule
    assert "box-shadow: var(--glass-relief" in popup_rule
    assert "backdrop-filter: blur(20px) saturate(1.3)" in popup_rule
    assert ".chart-layer-pop::before" not in CSS
    assert ".chart-layer-pop::after" not in CSS
