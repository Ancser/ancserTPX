"""Presentation contracts for the native UI surface.

These are deliberately static contracts.  The real-browser suites under
tests/ui prove paint/timing; these keep presentation-only edits from changing
strategy values.  Liquid Glass was removed for good in 1.3.0 (see
test_liquid_glass_is_fully_removed).
"""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "frontend" / "static"
HTML = (STATIC / "ancserTPX.html").read_text(encoding="utf-8")
JS = (STATIC / "ancserTPX.js").read_text(encoding="utf-8")
CSS = (STATIC / "ancserTPX.css").read_text(encoding="utf-8")
DESIGN = (STATIC / "ancserTPX-design.css").read_text(encoding="utf-8")
MESSENGER = (ROOT / "backend" / "live" / "emapmo_messenger.py").read_text(encoding="utf-8")


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
    ("optionwall", "OPTION WALL"),
    ("delta_absorption", "DELTA ABSORPTION"),
    ("volume_profile", "VOLUME PROFILE"),
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
    # PI OPTIONS is a Backtest-page model only: its live side is the separate
    # options account, never the futures engine selector.
    assert _select_options("strategy-bt") == CANONICAL + [("pi_options", "PI OPTIONS")]
    assert _select_options("strategy-live") == CANONICAL


def test_option_wall_primary_strict_controls_and_signal_date_scope_are_explicit():
    assert _select_options("option-wall-submodel-bt") == [
        ("primary_strict", "PRIMARY STRICT"),
    ]
    assert _select_options("option-wall-submodel-live") == [
        ("primary_strict", "PRIMARY STRICT"),
    ]
    assert '<option value="optionwall" disabled>OPTION WALL</option>' in HTML

    collect = _function_source("collectStrategyParams")
    for field in (
        "option_wall_submodel", "option_wall_side_mode",
        "option_wall_long_sl_atr", "option_wall_short_sl_atr",
        "option_wall_max_hold_min", "option_wall_max_trades_per_day",
    ):
        assert field in collect

    scope = _function_source("_scopeDatesForStrategy")
    assert "OPTION_WALL_SIGNAL_FIRST_DATE" in scope
    assert "startEl.dataset.signalScope" in scope
    assert "switchingFromAutoScopedDate" in scope


def test_model_controls_do_not_render_strategy_description_rows():
    for mode in ("bt", "live"):
        assert f'aria-describedby="strategy-desc-{mode}"' not in HTML
        assert f'id="strategy-desc-{mode}"' not in HTML
    assert "strategy-description" not in HTML
    assert "syncStrategyDescription" not in JS
    for strategy, display in CANONICAL:
        assert re.search(
            rf"\b{strategy}:\s*\{{.*?displayName:\s*'{display}'.*?"
            rf"description:\s*'[^']+'",
            JS,
            re.DOTALL,
        )


def test_volume_profile_controls_cover_the_edge_state_machine_and_atr_blend():
    for mode in ("bt", "live"):
        for control in (
            "vp-entry-mode", "vp-side", "vp-target", "vp-va", "vp-sl-atr",
            "vp-tp-atr", "vp-confirm", "vp-max-trades", "vp-break-buffer",
            "vp-touch", "vp-reclaim-buffer",
        ):
            assert f'id="{control}-{mode}"' in HTML
    collect = _function_source("collectStrategyParams")
    for field in (
        "vp_value_area_pct", "vp_entry_mode", "vp_target_mode", "vp_side_mode",
        "vp_sl_atr", "vp_tp_atr", "vp_confirm_bars",
        "vp_breakout_buffer_ticks", "vp_touch_tolerance_ticks",
        "vp_reclaim_buffer_ticks", "vp_max_trades_per_day",
    ):
        assert field in collect


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
    english = _function_source("_englishHelpTip")
    assert "return String(tip || '').trim();" in english
    assert "tip.zh" not in english
    add = _function_source("addHelpDot")
    assert "data-tip-en" in add
    assert "data-tip-zh" not in add
    show = _function_source("showHelpTooltip")
    assert "dot.getAttribute('data-tip-en')" in show
    assert "dot.getAttribute('data-tip')" not in show


def test_language_switch_is_removed_and_english_is_the_only_ui_locale():
    assert '<html lang="en">' in HTML
    assert 'id="lang-toggle"' not in HTML
    assert "toggleLanguage" not in JS
    assert "UI_LANG" not in JS
    assert "I18N_ZH" not in JS
    assert "tip.zh" not in JS
    assert "lang-toggle" not in CSS


def test_connection_rail_uses_identity_initial_and_provider_status_contract():
    assert 'id="connection-initial"' in HTML
    assert 'class="connection-icon connection-initial"' in HTML
    assert "<label>TOPSTEP USERNAME</label>" in HTML
    assert "<label>TOPSTEP API</label>" in HTML
    assert 'id="chart-provider-status"' in HTML
    for provider in ("discord", "topstep", "databento"):
        assert f'data-provider="{provider}"' in HTML
        assert f'id="latency-dot-{provider}"' in HTML
        assert f'id="latency-{provider}"' in HTML
    assert "@router.get(\"/connection/status\")" in (
        (ROOT / "backend" / "api" / "routes.py").read_text(encoding="utf-8")
    )
    assert "last_request_latency_ms" in (
        (ROOT / "backend" / "broker" / "topstepx.py").read_text(encoding="utf-8")
    )
    assert "last_fetch_latency_ms" in (
        (ROOT / "backend" / "live" / "pi_listener.py").read_text(encoding="utf-8")
    )
    assert "latency_ms" in (
        (ROOT / "backend" / "live" / "databento_orderflow.py").read_text(encoding="utf-8")
    )


def test_execute_trade_refresh_preserves_sidebar_position():
    refresh = _function_source("fetchAndDrawTradeHistory")
    assert "preserveSidebarScroll: executeTradesTabIsActive()" in refresh
    metrics = _function_source("renderMetrics")
    assert "function renderMetrics(m, backtestTrades, options)" in metrics
    assert "renderOptions.preserveSidebarScroll" in metrics
    assert "panel.scrollIntoView" in metrics


def test_trade_tables_keep_time_labels_on_one_line_without_why_column():
    assert "<th>ID</th>" not in HTML
    assert HTML.count('<th style="width:82px;">CONTRACT</th>') == 2
    assert '<th style="width:36px;">SIZE</th>' not in HTML
    assert '<th style="width:48px;">SYMBOL</th>' not in HTML
    assert HTML.count("<th>TIME</th>") == 2
    assert "<th>ENTRY TIME</th>" not in HTML
    assert "<th>EXIT TIME</th>" not in HTML
    assert "WHY" not in HTML
    assert HTML.count("<th>ENTRY</th>") == 2
    assert HTML.count("<th>EXIT</th>") == 2
    assert "ENTRY PRICE" not in HTML
    assert "EXIT PRICE" not in HTML

    assert ".trade-table-wrap table {" in CSS
    assert "min-width: 0;" in CSS
    assert ".trade-table-wrap th { white-space: nowrap; }" in CSS

    assert "colspan=\"11\"" not in JS
    assert 'colspan="9" style="text-align:center;color:var(--text2);padding:20px;">NO TRADE DATA' in JS
    assert 'colspan="9" style="text-align:center;color:var(--text2);padding:20px;">NO EXECUTE TRADE DATA' in JS
    assert "class=\"trade-contract-cell\"" in JS
    assert "String(symbol).replace(/^\\/+/, '')" in JS
    assert "TRADE_DISPLAY_TIME_ZONE = SYSTEM_TIME_ZONES.market" in JS
    assert "formatTradeTimeRange(t.entry_time, t.exit_time)" in JS
    assert "formatTradeDuration(t.entry_time, t.exit_time)" in JS
    assert "formatTradeTimeRangeMarkup" in JS
    assert "trade-time-range" in (STATIC / "ancserTPX-design.css").read_text(encoding="utf-8")
    assert 'data-btab="pnl"' not in HTML
    assert 'id="btab-pnl"' not in HTML
    assert "explainTrade" not in JS


def test_max_profit_slider_keeps_full_track():
    native = CSS[CSS.index("/* 1.0.9: PDPT 滑桿"):CSS.index("/* 1.0.10: CONTRACT")]
    assert "flex: 1 1 0%;" in native
    assert "width: auto;" in native
    assert "flex: 0 0 4.2em;" in native


def test_single_account_chart_shell_removes_minor_and_chart_legend():
    assert "ACCOUNT MINOR" not in HTML
    assert "live-acct-select-2" not in HTML
    assert "LIVE_MINOR_SLOT" not in JS
    assert "ACCOUNT MINOR" not in JS
    assert 'id="signal-legend"' not in HTML
    assert "signal-legend" not in JS
    assert "chart-signal-legend" not in CSS
    assert 'id="chart-watermark"' in HTML
    design = (ROOT / "frontend" / "static" / "ancserTPX-design.css").read_text(encoding="utf-8")
    assert "Orbitron" not in _code(design) and '--font-sans: "Inter"' in design
    assert "left: 12px;" in design
    assert "bottom: 34px;" in design
    assert "font-size: clamp(0.45rem, 1.2vw, 1.2rem);" in design


def test_motion_tokens_cover_fast_ui_and_one_second_theme_surfaces():
    design = (ROOT / "frontend" / "static" / "ancserTPX-design.css").read_text(encoding="utf-8")
    assert "--ui-motion-duration: 180ms;" in design
    assert "--ui-theme-duration: 150ms;" in design
    assert "html.theme-transitioning .bottom-panel" in design
    assert "html.theme-transitioning #chart-container" in design
    assert "html.theme-transitioning #calendar-view" in design
    assert "html.theme-transitioning .institution-panel" in design
    assert 'id="chart-theme-cover"' in HTML
    assert "function _startChartThemeCover()" in JS
    assert ".is-fading" in design
    assert ".chart-provider-status {" in design
    assert "flex-direction: column;" in design[design.index(".chart-provider-status {"):design.index(".chart-latency-row {")]
    assert "const APP_THEME_TRANSITION_MS = 150;" in JS
    assert "typeof renderPnlCurve === 'function'" in JS


def test_final_workspace_shell_uses_v2_palette_and_straight_edges():
    design = (ROOT / "frontend" / "static" / "ancserTPX-design.css").read_text(encoding="utf-8")
    assert "--surface-page: #0e1116;" in design
    assert "--surface-chart: #15181d;" in design
    assert "border-left: 3px solid var(--accent)" not in _code(design)
    assert "--accent: #64dcff;" in design
    assert "--ui-radius: 0;" in design
    assert ".panel-title::before { content: none !important; display: none !important; }" in design
    assert ".ui-switch .switch-thumb" in design


def test_pi_matrix_switches_use_the_positioned_switch_thumb():
    """Every PI matrix switch carries the standard positioned thumb."""
    for mode in ("bt", "live"):
        start = HTML.index(f'id="pi-params-{mode}"')
        # Keep this assertion scoped to the original PI entry matrix. The new
        # reopen-continuation matrix has its own six switch thumbs.
        end = HTML.index("pi-continuation-shell", start)
        matrix = HTML[start:end]
        assert matrix.count('class="switch-thumb"') == 6
        assert matrix.count('class="ui-switch pi-matrix-switch') == 6


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


def test_weekly_metric_card_shows_only_weekly_cv():
    start = JS.index("const weeklyVarItem = {")
    end = JS.index("    };", start) + len("    };")
    weekly = JS[start:end]
    assert "label: 'WEEKLY CV'" in weekly
    assert "wkCv.toFixed(2)" in weekly
    assert "wkStd" not in weekly
    assert "metric-real" not in weekly
    assert "% green" not in weekly
    assert "w)</span>" not in weekly


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
    assert "const seen = new Set();" in refresh
    assert "[chartTime, sym, event.kind].join('|')" in refresh
    assert "event.message_id || ''" not in refresh
    assert "pi_history" not in refresh
    assert "activeTab !== 'live'" not in refresh


def test_parameter_source_is_english_and_pi_payload_values_are_unchanged():
    sidebar_start = HTML.index('<div class="sidebar"')
    sidebar_end = HTML.index('<!-- Main Content -->', sidebar_start)
    sidebar = re.sub(
        r"<!--.*?-->",
        "",
        HTML[sidebar_start:sidebar_end],
        flags=re.DOTALL,
    )
    # UI-026 exception (user decision 2026-11-08): a hero button may carry its
    # Chinese name as a decorative watermark, only as the data-wm of an
    # aria-hidden .wm-clip. Every other piece of copy stays English.
    watermark = re.compile(r'<span class="wm-clip" data-wm="([^"]*)" aria-hidden="true"></span>')
    marks = watermark.findall(sidebar)
    assert marks == ["\u56de\u6e2c", "\u5be6\u76e4"]                     # positive: the exception is in use
    assert not re.search(r"[\u3400-\u9fff]", watermark.sub("", sidebar))

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
        "LONG TIME EXIT",
        "SHORT TIME EXIT",
        "PI",
    ):
        assert english in HTML


def test_pi_matrix_is_two_column_switch_ui_and_keeps_legacy_wire_fields():
    for mode in ("bt", "live"):
        start = HTML.index(f'data-pi-matrix="{mode}"')
        end = HTML.index('class="pi-legacy-controls"', start)
        matrix = HTML[start:end]
        assert f'id="pi-matrix-{mode}-long-pi"' in matrix
        assert f'id="pi-matrix-{mode}-short-pi"' in matrix
        assert f'id="pi-matrix-{mode}-long-level2"' in matrix
        assert f'id="pi-matrix-{mode}-long-level1"' in matrix
        assert f'id="pi-matrix-{mode}-short-level2"' in matrix
        assert f'id="pi-matrix-{mode}-short-level1"' in matrix
        assert "pi-matrix-switch-disabled" not in matrix
        assert " disabled" not in matrix
        assert "class=\"ui-switch pi-matrix-switch" in matrix
        assert "LONG" in matrix and "SHORT" in matrix
        assert "LEVEL 2" in matrix and "LEVEL 1" in matrix
        assert "pi-matrix-note" not in matrix
        assert "SHORT LEVEL 1/2 bubbles are recorded only" not in matrix
        assert f'id="pi-signal-set-{mode}"' in HTML
        assert f'id="pi-long-only-{mode}"' in HTML

    assert "show('factor-params-' + mode, isFactor || isIntramom || isSessfib);" in JS
    assert "pi_long_kinds: piMatrix.pi_long_kinds" in JS
    assert "pi_short_kinds: piMatrix.pi_short_kinds" in JS
    assert "pi_short_levels: piMatrix.pi_short_levels" in JS
    assert "pi_continue_long_kinds: piContinueMatrix.pi_continue_long_kinds" in JS
    assert "pi_continue_short_kinds: piContinueMatrix.pi_continue_short_kinds" in JS
    assert "pi_continue_short_levels: piContinueMatrix.pi_continue_short_levels" in JS
    assert "pi_lv2_replace_pi: _piLv2ReplaceEnabled(mode)" in JS
    assert "_setPiLv2ReplaceEnabled(mode, Boolean(p.pi_lv2_replace_pi))" in JS
    assert "onPiContinueMatrixProxy" in JS
    assert "onPiLv2ReplaceProxy" in JS

    for mode in ("bt", "live"):
        assert f'data-pi-continue-matrix="{mode}"' in HTML
        assert f'id="pi-continue-{mode}-long-level2"' in HTML
        assert f'id="pi-continue-{mode}-short-level2"' in HTML
        assert f'id="pi-reopen-gap-{mode}"' in HTML
        assert f'id="pi-lv2-replace-{mode}"' in HTML


def test_pi_matrix_light_mode_uses_light_surface_instead_of_dark_overlay():
    light_rule = DESIGN[
        DESIGN.index(':root[data-tpx-theme="apple-light"] .pi-signal-matrix {'):
        DESIGN.index("}", DESIGN.index(':root[data-tpx-theme="apple-light"] .pi-signal-matrix {'))
    ]
    assert "background: transparent;" in light_rule


def test_requested_control_geometry_is_explicit_and_consistent():
    multiplier = CSS[CSS.index(".form-mult {"):CSS.index("}", CSS.index(".form-mult {"))]
    assert "flex: 0 0 26px" in multiplier
    assert "justify-content: center" in multiplier
    assert "font-size: 1.5rem" in multiplier


def test_sidebar_is_25_percent_narrower_without_changing_mobile_stack():
    assert "width: 18.75rem;" in CSS
    assert "@media (max-width: 1200px) { .sidebar { width: 16.40625rem; } }" in CSS
    assert re.search(r"@media \(max-width: 900px\).*?\.sidebar\s*\{.*?width: 100%;", CSS, re.DOTALL)


def test_pi_directional_exits_use_matching_atr_options_and_two_half_column_rows():
    expected_atr = [
        ("1", "1 x ATR"), ("1.5", "1.5 x ATR"),
        ("2", "2 x ATR"), ("2.5", "2.5 x ATR"),
        ("3", "3 x ATR"), ("3.5", "3.5 x ATR"), ("4", "4 x ATR"),
    ]

    def options(select_id: str) -> list[tuple[str, str]]:
        match = re.search(
            rf'<select id="{re.escape(select_id)}"[^>]*>(.*?)</select>',
            HTML,
            flags=re.DOTALL,
        )
        assert match, f"missing select #{select_id}"
        return re.findall(
            r'<option value="([^"]+)"[^>]*>([^<]+)</option>',
            match.group(1),
        )

    for mode in ("bt", "live"):
        row_start = HTML.index(f'id="factor-sl-row-{mode}"')
        row_end = HTML.index(f'id="factor-hold-{mode}"', row_start)
        row = HTML[row_start:row_end]
        assert f'id="factor-sl-value-label-{mode}">SL INPUT<' in row
        assert f'id="pi-long-hold-group-{mode}"' in row
        assert f'id="pi-long-hold-{mode}"' in row
        assert f'id="pi-short-sl-group-{mode}"' in row
        assert f'id="pi-short-sl-{mode}"' in row
        assert f'id="pi-short-hold-group-{mode}"' in row
        assert f'id="pi-short-hold-{mode}"' in row
        assert options(f"factor-sl-value-{mode}") == expected_atr
        assert options(f"pi-short-sl-{mode}") == expected_atr
        assert options(f"pi-long-hold-{mode}") == [
            ("0", "0 (OFF)"), ("60", "60"), ("120", "120"), ("240", "240"),
        ]
        assert options(f"pi-short-hold-{mode}") == [
            ("0", "0 (OFF)"), ("60", "60"), ("120", "120"), ("240", "240"),
        ]
        assert re.search(
            rf'<select id="pi-long-hold-{mode}">\s*<option value="0" selected>',
            HTML,
        )
        assert HTML.count(f'id="pi-short-sl-{mode}"') == 1
        assert HTML.count(f'id="pi-long-hold-{mode}"') == 1
        assert HTML.count(f'id="pi-short-hold-{mode}"') == 1

        assert "slRow.classList.toggle('pi-dual-sl', isPi || isDelta)" in JS
    assert "longSlLabel.textContent = (isPi || isDelta) ? 'LONG SL' : 'SL INPUT'" in JS
    assert "pi_long_hold_min: _int('pi-long-hold-' + mode, 0)" in JS
    assert "pi_short_hold_min: _int('pi-short-hold-' + mode, 60)" in JS
    assert ".factor-sl-row.pi-dual-sl" in CSS
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in CSS
    assert '"long-sl long-time"' in CSS
    assert '"short-sl short-time"' in CSS


def test_retired_auto_center_is_absent_but_other_chart_tools_remain():
    for source in (HTML, JS, CSS, DESIGN):
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
    default_view = _function_source("applyDefaultChartView")
    assert "autoscaleInfoProvider: (baseImplementation) => _smoothChartAutoscale(baseImplementation)" in _function_source("initChart")
    assert "CHART_AUTOSCALE_HOLD_MS" in JS
    assert "CHART_AUTOSCALE_TRANSITION_MS" in JS
    assert "CHART_DEFAULT_SMOOTH_BARS" in default_view
    assert "smoothCenter" in default_view
    assert "_setChartPriceAutoScale(true)" in default_view
    assert "kineticScroll:" in _function_source("initChart")
    # 1.3.0: the library owns the whole 2D plot drag and its inertia; the old
    # per-axis lock and 1.10x horizontal controller were retired (UI-014).
    assert "mouse: true" in _function_source("initChart")
    assert "_setChartPriceAutoScale(false)" in _function_source("_routeChartPanMove")
    assert "CHART_PRICE_PAN_START_PX" in JS
    assert "_routeChartWheel" in _function_source("initChart")
    assert "_chartManualPriceRange" in _function_source("_smoothChartAutoscale")
    assert "_routeChartPanMove" in _function_source("initChart")
    assert "scrollToPosition" not in _function_source("_routeChartPanMove")
    time_scale = _function_source("initChart")
    assert "fixLeftEdge: false" in time_scale
    assert "fixRightEdge: false" in time_scale
    assert "lockVisibleTimeRangeOnResize: false" in time_scale
    assert "rightBarStaysOnScroll: false" in time_scale
    assert "applyDefaultChartView" not in _function_source("renderChart")
    assert "fitContent" not in _function_source("renderChart")
    assert "fetchAndShowChart('1m', true)" in _function_source("_ensureBacktestData")
    assert "const idx = _nearestBarIndex(sec)" in _function_source("_timeToXViaBars")


def test_chart_layer_popup_is_a_native_panel_of_shared_switches():
    start = HTML.index('<div id="chart-layer-pop"')
    end = HTML.index('<div id="chart-quick-btns"', start)
    popup = HTML[start:end]
    assert 'class="chart-layer-pop hidden"' in popup
    # One shared switch contract (CLAUDE.md, the 1.0.10o incident): no
    # per-popup attributes, selectors or forwarding proxies.
    assert len(re.findall(r'class="ui-switch(?: on)?"', popup)) == popup.count('data-switch-proxy="lp-') == 17
    assert popup.count('class="switch-thumb"') == 17
    assert "data-glass" not in popup and "optical" not in popup and "proxy-hidden" not in popup
    assert "#chart-layer-pop" not in CSS
    assert ".chart-layer-pop {" in CSS
    popup_rule = CSS[CSS.index(".chart-layer-pop {"):CSS.index(".chart-layer-pop.hidden")]
    assert "border: 0;" in popup_rule
    assert "box-shadow: none;" in popup_rule
    assert "backdrop-filter: none;" in popup_rule
    assert "-webkit-backdrop-filter: none;" in popup_rule
    assert ".chart-layer-pop::before" not in CSS
    assert ".chart-layer-pop::after" not in CSS
    assert ".chart-layer-pop .ui-switch {" in CSS
    assert "width: 2.8333rem;" in CSS
    assert "height: 1.1667rem;" in CSS
    layer_sync = _function_source("buildChartLayerMenu")
    assert "const current = tr.getAttribute('aria-checked') === 'true';" in layer_sync
    assert "if (current === on) return;" in layer_sync

def _strip_js_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", source)


def test_liquid_glass_is_fully_removed():
    """1.3.0: the user banned Liquid Glass for good.  No engine, skin, tuner,
    optical clones, spring setters or glass-named switch classes may return."""
    for name in ("tpx-glass.js", "tpx-glass.css", "tpx-glass-skin.js", "tpx-glass-tuner.js"):
        assert not (STATIC / name).exists(), name
    sources = {
        "html": re.sub(r"<!--.*?-->", "", HTML, flags=re.DOTALL),
        "js": _strip_js_comments(JS),
        "css": _code(CSS),
        "design": _code(DESIGN),
    }
    for label, source in sources.items():
        for token in ("tpx-glass", "TpxGlass", "tpxSetState", "optical-stage-copy",
                      "optical-surface", "data-optical", "data-glass", "glass-switch",
                      "glass-proxy", "glass-slider", "glassResample", "glass-sync",
                      "data-tpx-glass-skin"):
            assert token not in source, (label, token)
    assert "_apply_glass_lens" not in MESSENGER
    assert "specular" not in MESSENGER.lower()

def test_chart_layer_choices_are_restored_and_persisted_through_one_state_path():
    assert "const CHART_LAYER_STORAGE_KEY = 'ancserTPX.chartLayers'" in JS
    load = _function_source("_loadChartLayerPreferences")
    assert "typeof saved[layer.key] === 'boolean'" in load
    assert "Array.isArray(saved)" in load

    persist = _function_source("_persistChartLayerPreferences")
    assert "CHART_LAYERS.map" in persist
    assert "localStorage.setItem(CHART_LAYER_STORAGE_KEY" in persist

    seed = _function_source("_seedChartLayerMarkup")
    assert "track.classList.toggle('on', on)" in seed
    assert "track.setAttribute('aria-checked', String(on))" in seed
    assert JS.index("_seedChartLayerMarkup();") < JS.index("function toggleChartLayer(key, on)")

    toggle = _function_source("toggleChartLayer")
    assert "if (!(key in CHART_OVERLAYS)) return" in toggle
    assert toggle.index("CHART_OVERLAYS[key] = !!on") < toggle.index(
        "_persistChartLayerPreferences()"
    )


def test_delta_absorption_chart_evidence_is_split_and_model_scoped():
    for key, label in (
        ("delta_absorption", "DELTA ABSORPTION"),
        ("delta_value", "VAH / VAL (DELTA)"),
        ("delta_decay", "DELTA DECAY"),
        ("delta_stall", "PRICE NO-BREAK"),
    ):
        assert f"key: '{key}'" in JS
        assert label in HTML
        assert f'data-switch-proxy="lp-{key}"' in HTML

    draw = _function_source("drawDeltaAbsorptionOverlay")
    assert "_deltaChartModelActive()" in draw
    assert "data.value_areas" in draw
    assert "event.delta_decay" in draw
    assert "event.stalled" in draw
    assert "event.touched" in draw
    assert "clearDeltaAbsorptionOverlay()" in draw
    assert "VAH 70%" in draw
    assert "DELTA · NO MBO IN VISIBLE WINDOW" in _code(JS)
    assert "DELTA · LOADING MBO EVIDENCE" in _code(JS)
    assert "delta_overlay" in _code(JS)
    # A restored backtest installs its payload before the first candle load;
    # showCandleData must therefore perform the initial repaint as well as the
    # normal renderChart path.
    assert "drawDeltaAbsorptionOverlay();" in _function_source("showCandleData")
    assert "refreshDeltaAbsorptionOverlay" in _function_source("toggleChartLayer")
    assert "include_delta: '1'" in _function_source("refreshDeltaAbsorptionOverlay")


def test_option_wall_demo_is_an_opt_in_read_only_chart_layer():
    assert "{ key: 'optionwall', label: 'QQQ OPTION WALL',       on: false }" in JS
    assert 'data-switch-proxy="lp-optionwall" aria-checked="false"' in HTML
    assert "API + '/options-wall/demo?symbol=MNQ'" in _function_source("refreshOptionWallLayer")
    draw = _function_source("drawOptionWallOverlay")
    assert "call_wall_mnq" in draw
    assert "put_wall_mnq" in draw
    assert "gamma_flip_mnq" in draw
    assert "net_oi_gex_1pct" in draw
    assert "net_volume_gex_1pct" in draw
    assert "_optionWallVisibleWindow(visibleRange)" in draw
    assert "_optionWallSegmentEndTime(row, next)" in draw
    # Path rule (UI-023) lives in one pure function the browser test drives.
    assert "_optionWallSeriesPath(visiblePoints, spec.key)" in draw
    path = _function_source("_optionWallSeriesPath")
    assert "let drawing = false" in path
    assert "session !== currentSession" in path
    assert "Math.abs(lastX - x1) > 0.01" in path        # a time gap restarts
    assert "commands.push(['L', x2, y])" in path
    assert "CALL+PUT WALL" in draw
    assert "_optionWallsOverlap(row)" in draw
    assert "function _optionWallSameSession(row, next)" in JS
    assert "OPTION_WALL_MAX_GAP_SEC" not in JS
    assert "OPTION_WALL_OVERLAP_TOLERANCE = 0.25" in JS
    assert "const visiblePoints = []" in draw
    assert draw.count("_indicatorTimeToX(row.chartTime") == 1
    assert "visiblePoints.push({ x1, x2, session:" in draw


def test_chart_layer_names_keep_functional_names_without_visual_shape_suffixes():
    assert 'class="layer-name">EMAPMO</span>' in HTML
    assert 'class="layer-name">PI</span>' in HTML
    assert 'class="layer-name">QQQ OPTION WALL</span>' in HTML
    assert 'class="layer-name">FOOTPRINT</span>' in HTML
    assert 'class="layer-name">CVD</span>' in HTML
    assert 'class="layer-name">MREV</span>' in HTML
    assert 'class="layer-name">KDJMA</span>' in HTML
    assert 'class="layer-name">INTRAMOM</span>' in HTML
    assert 'class="layer-name">DAY ZONE</span>' in HTML
    for retired in (
        "EMAPMO ▲▼",
        "PI π / CIRCLES",
        "QQQ OPTION WALL / GEX",
        "FOOTPRINT / LEVEL 2",
        "CVD / DELTA",
        "MREV BUBBLES",
        "KDJMA DOTS",
        "INTRAMOM ARROWS",
        "DAY ZONE LEVELS",
    ):
        assert retired not in HTML


def test_pi_signal_overlay_has_one_shared_canvas_repaint_owner():
    redraw = _function_source("redrawAllOverlays")
    schedule = _function_source("scheduleChartOverlayRedraw")
    refresh = _function_source("refreshPiSignalMarkers")
    push = _function_source("pushPiSignalMarker")
    assert "drawIndicatorSignalOverlay()" in redraw
    assert "drawPiSignalOverlay()" not in redraw
    assert "drawIndicatorSignalOverlay()" in schedule
    assert "drawPiSignalOverlay()" not in schedule
    assert "scheduleChartOverlayRedraw();" in refresh
    assert "drawPiSignalOverlay();" not in refresh
    assert "scheduleChartOverlayRedraw();" in push
    assert "drawPiSignalOverlay();" not in push
    draw = _function_source("drawPiSignalOverlay")
    assert "const drawnMarks = new Set();" in draw
    assert "const markKey = [t, m.kind, sourceLevel].join('|');" in draw
    assert "if (drawnMarks.has(markKey)) continue;" in draw


def test_no_trade_visual_window_runs_from_1245pm_to_3pm_pacific():
    assert "{ startH: 15, startM: 45, endH: 18, endM: 0, label: 'NO TRADE' }" in JS
    assert "&notrade=2" in HTML


def test_prior_day_70_value_area_is_a_separate_three_line_chart_layer():
    assert "{ key: 'prevday70',label: 'PRIOR DAY 70% VAH/VAL/POC', on: false }" in JS
    assert 'data-switch-proxy="lp-prevday70" aria-checked="false"' in HTML
    assert "API + '/data/previous-day-value-areas'" in JS
    draw = _function_source("drawPreviousDayValueAreas")
    assert "vah_70" in draw
    assert "val_70" in draw
    assert "Number(area.poc)" in draw
    assert "Exactly three lines per displayed trade day" in draw


def test_retired_chart_value_area_layers_are_removed_but_strategy_values_remain():
    for retired in (
        "VAH/VAL/POC LINES",
        "SESSION VA",
        "BETAFIB LEVELS",
        "lp-zonelines",
        "lp-sessva",
        "lp-fib",
        "renderTfZones",
        "detect-zones",
        "betafib_levels",
    ):
        assert retired not in HTML
        assert retired not in JS
    assert "PRIOR DAY 70% VAH/VAL/POC" in HTML
    assert "PRIOR DAY 70% VAH/VAL/POC" in JS
    # BETAFIB and SESSION remain valid execution parameters/models; only the
    # retired chart overlays and their chart-only transport were removed.
    assert 'value="betafib"' in HTML
    assert 'value="session"' in HTML
    assert "factor_session_va_filter" in JS


def test_left_history_paging_does_not_recompute_signals_or_duplicate_canvas_work():
    history = _function_source("loadOlderChartHistory")
    assert "older.concat(currentRaw)" in history
    assert "_chartHistoryApplying = true" in history
    assert "scheduleChartOverlayRedraw()" in history
    assert "refreshIndicatorSignalMarkers" not in history
    assert "refreshPiSignalMarkers" not in history
    assert "drawSessionDividers()" not in history

    init = _function_source("initChart")
    assert "scheduleChartOverlayRedraw()" in init
    assert "_vpRafId" not in init
    sync = _function_source("startOverlaySync")
    assert "scheduleChartOverlayRedraw()" in sync
    assert "drawOptionWallOverlay()" not in sync


def test_cross_model_sweep_controls_and_result_view_are_removed():
    for token in (
        'id="btn-sweep"',
        'id="sweep-model-btn"',
        'id="sweep-model-pop"',
        'data-sweep-model=',
        'data-btab="presets"',
        'id="btab-presets"',
        "runBacktestSweep",
        "renderSweepTable",
        "loadSweepResults",
        "saveSweepPreset",
    ):
        assert token not in HTML
        assert token not in JS
        assert token not in CSS

    assert 'id="preset-bt"' in HTML
    assert 'id="preset-live"' in HTML
