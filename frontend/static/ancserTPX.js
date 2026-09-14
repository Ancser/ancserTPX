// ============================================
// ancserTPX Frontend
// ============================================

// Auto-detect port from current page URL (supports dynamic launcher ports)
const API = window.location.origin + '/api';
const SYSTEM_RUNTIME = window.ANCSER_SYSTEM;
let MARKET_CLOCK_VERSION = SYSTEM_RUNTIME.marketClockVersion;
const SYSTEM_TIME_ZONES = SYSTEM_RUNTIME.timeZones;
const FRONT_MONTH_CONTRACTS = SYSTEM_RUNTIME.frontMonthContracts;
const SYSTEM_CONTRACT_SPECS = SYSTEM_RUNTIME.contractSpecs;

// The production shell has two deliberate palettes.  Keep the preference in
// the browser so a reload never flashes back to the old skin or changes the
// user's choice underneath an open chart.
const APP_THEME_STORAGE_KEY = 'ancserTPXTheme';
const APP_THEME_GRAPHITE = 'graphite';
const APP_THEME_APPLE_LIGHT = 'apple-light';
const APP_THEME_TRANSITION_MS = 500;
let _themeTransitionTimer = null;
let _chartThemeCoverTimer = null;
let _pnlThemeCopyTimer = null;

function _normaliseAppTheme(theme) {
    return String(theme || '').toLowerCase() === APP_THEME_APPLE_LIGHT
        ? APP_THEME_APPLE_LIGHT
        : APP_THEME_GRAPHITE;
}

function _storedAppTheme() {
    try { return _normaliseAppTheme(localStorage.getItem(APP_THEME_STORAGE_KEY)); }
    catch (_) { return APP_THEME_GRAPHITE; }
}

function _appThemeColor(name, fallback) {
    const value = getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim();
    return value || fallback;
}

function _refreshChartTheme() {
    if (!chart) return;

    const grid = _appThemeColor('--chart-grid', 'rgba(100, 220, 255, 0.03)');
    const crosshair = _appThemeColor('--chart-crosshair', 'rgba(100, 220, 255, 0.2)');
    chart.applyOptions({
        layout: {
            background: { type: 'solid', color: _appThemeColor('--surface-chart', '#08090d') },
            textColor: _appThemeColor('--ink-muted', '#556178'),
        },
        grid: {
            vertLines: { color: grid },
            horzLines: { color: grid },
        },
        crosshair: {
            vertLine: { color: crosshair },
            horzLine: { color: crosshair },
        },
        rightPriceScale: { borderColor: _appThemeColor('--line-subtle', 'rgba(100, 220, 255, 0.08)') },
        timeScale: { borderColor: _appThemeColor('--line-subtle', 'rgba(100, 220, 255, 0.08)') },
    });

    if (candleSeries) {
        const up = _appThemeColor('--candle-up', '#888888');
        const down = _appThemeColor('--candle-down', '#555555');
        candleSeries.applyOptions({
            upColor: up,
            borderUpColor: up,
            wickUpColor: up,
            downColor: down,
            borderDownColor: down,
            wickDownColor: down,
        });
    }
    if (typeof scheduleChartOverlayRedraw === 'function') scheduleChartOverlayRedraw();

    // The Research PNL curve is a canvas too. Repaint it after the theme token
    // swap when its view is visible so its labels and palette follow the same
    // transition instead of retaining the previous theme's pixels.
    const researchView = document.getElementById('calendar-view');
    if (researchView && !researchView.classList.contains('hidden')
        && typeof renderPnlCurve === 'function') {
        const schedule = window.requestAnimationFrame || ((callback) => setTimeout(callback, 16));
        schedule(() => {
            if (!researchView.classList.contains('hidden')) renderPnlCurve();
        });
    }
}

function _startChartThemeCover() {
    const cover = document.getElementById('chart-theme-cover');
    if (!cover) return;

    if (_chartThemeCoverTimer !== null) {
        clearTimeout(_chartThemeCoverTimer);
        _chartThemeCoverTimer = null;
    }

    // Lightweight Charts repaints its opaque canvas synchronously.  Freeze the
    // old chart color for one frame, repaint underneath it, then reveal the new
    // chart over the same half-second theme phase as the surrounding UI.
    cover.style.setProperty('--chart-theme-cover-color', _appThemeColor('--surface-chart', '#08090d'));
    cover.classList.remove('is-fading');
    cover.classList.add('is-active');

    const schedule = window.requestAnimationFrame || ((callback) => setTimeout(callback, 16));
    schedule(() => {
        if (!cover.isConnected) return;
        cover.classList.remove('is-active');
        cover.classList.add('is-fading');
        _chartThemeCoverTimer = setTimeout(() => {
            cover.classList.remove('is-fading');
            _chartThemeCoverTimer = null;
        }, APP_THEME_TRANSITION_MS + 40);
    });
}

function _startResearchPnlThemeCrossfade() {
    const host = document.getElementById('pnl-curve-body');
    const canvas = host && host.querySelector('#pnl-curve-canvas');
    if (!host || !canvas || !canvas.width || !canvas.height) return;

    if (_pnlThemeCopyTimer !== null) {
        clearTimeout(_pnlThemeCopyTimer);
        _pnlThemeCopyTimer = null;
    }
    host.querySelectorAll('.pnl-curve-theme-copy').forEach(copy => copy.remove());

    // Canvas pixels do not participate in CSS color transitions. Keep the old
    // curve above the host while the new theme redraws underneath, then fade
    // that bitmap out over the same half-second phase as the other surfaces.
    const copy = document.createElement('canvas');
    copy.className = 'pnl-curve-theme-copy';
    copy.width = canvas.width;
    copy.height = canvas.height;
    copy.setAttribute('aria-hidden', 'true');
    try { copy.getContext('2d').drawImage(canvas, 0, 0); } catch (_) { return; }
    host.appendChild(copy);

    const schedule = window.requestAnimationFrame || ((callback) => setTimeout(callback, 16));
    schedule(() => {
        if (!copy.isConnected) return;
        copy.classList.add('is-fading');
        _pnlThemeCopyTimer = setTimeout(() => {
            copy.remove();
            _pnlThemeCopyTimer = null;
        }, APP_THEME_TRANSITION_MS + 40);
    });
}

function applyAppTheme(theme, persist = true) {
    const next = _normaliseAppTheme(theme);
    const root = document.documentElement;
    const previous = _normaliseAppTheme(root.dataset.tpxTheme);
    const changed = previous !== next;
    if (changed) {
        _startChartThemeCover();
        _startResearchPnlThemeCrossfade();
        root.classList.add('theme-transitioning');
        if (_themeTransitionTimer) clearTimeout(_themeTransitionTimer);
        _themeTransitionTimer = setTimeout(() => {
            root.classList.remove('theme-transitioning');
            _themeTransitionTimer = null;
        }, APP_THEME_TRANSITION_MS);
    }
    root.dataset.tpxTheme = next;
    root.style.colorScheme = next === APP_THEME_APPLE_LIGHT ? 'light' : 'dark';
    if (persist) {
        try { localStorage.setItem(APP_THEME_STORAGE_KEY, next); } catch (_) {}
    }
    const toggle = document.getElementById('theme-toggle');
    if (toggle) {
        toggle.dataset.theme = next;
        toggle.setAttribute(
            'aria-label',
            next === APP_THEME_APPLE_LIGHT
                ? 'Use deep-blue dark theme'
                : 'Use Apple Light theme',
        );
        toggle.title = next === APP_THEME_APPLE_LIGHT
            ? 'Switch to deep-blue'
            : 'Switch to Apple Light';
    }
    _refreshChartTheme();
}

function setAppTheme(theme) {
    applyAppTheme(theme, true);
}

function toggleAppTheme() {
    const current = _normaliseAppTheme(document.documentElement.dataset.tpxTheme);
    setAppTheme(current === APP_THEME_APPLE_LIGHT ? APP_THEME_GRAPHITE : APP_THEME_APPLE_LIGHT);
}

window.setAppTheme = setAppTheme;
window.toggleAppTheme = toggleAppTheme;
document.addEventListener('DOMContentLoaded', () => applyAppTheme(_storedAppTheme(), false));

const SIDEBAR_COLLAPSED_STORAGE_KEY = 'ancserTPX.sidebarCollapsed';

function applySidebarState(collapsed, persist = true) {
    const next = Boolean(collapsed);
    const body = document.body;
    const toggle = document.getElementById('sidebar-toggle');
    if (body) body.classList.toggle('sidebar-collapsed', next);
    if (toggle) {
        toggle.dataset.collapsed = next ? 'true' : 'false';
        toggle.setAttribute('aria-expanded', String(!next));
        toggle.setAttribute('aria-label', next ? 'Expand parameter panel' : 'Collapse parameter panel');
        toggle.title = next ? 'Expand parameter panel' : 'Collapse parameter panel';
        const sr = toggle.querySelector('.sr-only');
        if (sr) sr.textContent = next ? 'Expand parameter panel' : 'Collapse parameter panel';
    }
    if (persist) {
        try { localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, next ? '1' : '0'); } catch (_) {}
    }
}

function toggleSidebar() {
    const collapsed = document.body?.classList.contains('sidebar-collapsed');
    applySidebarState(!collapsed);
}

function _storedSidebarCollapsed() {
    try { return localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === '1'; } catch (_) { return false; }
}

window.toggleSidebar = toggleSidebar;
document.addEventListener('DOMContentLoaded', () => applySidebarState(_storedSidebarCollapsed(), false));

// Same-origin Web control protection. The backend sets a port-scoped,
// SameSite=Strict CSRF cookie on GET; only this origin can read it and copy it
// into the custom header required by every mutating /api request.
const ANCSERTPX_CSRF_HEADER = 'X-AncserTPX-CSRF';
const ANCSERTPX_WEB_PORT = window.location.port || (window.location.protocol === 'https:' ? '443' : '80');
const ANCSERTPX_CSRF_COOKIE = 'ancsertpx_csrf_' + ANCSERTPX_WEB_PORT;
const _ancserNativeFetch = window.fetch.bind(window);

function _ancserCookie(name) {
    const prefix = encodeURIComponent(name) + '=';
    for (const item of String(document.cookie || '').split(';')) {
        const value = item.trim();
        if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
    }
    return '';
}

window.fetch = function ancserSecureFetch(input, init) {
    const options = init ? { ...init } : {};
    const requestMethod = String(
        options.method || (input && input.method) || 'GET'
    ).toUpperCase();
    const mutating = ['POST', 'PUT', 'PATCH', 'DELETE'].includes(requestMethod);
    let target = null;
    try {
        target = new URL(
            (input && input.url) ? input.url : String(input),
            window.location.href,
        );
    } catch (e) {}
    if (mutating && target && target.origin === window.location.origin &&
            target.pathname.startsWith('/api/')) {
        const headers = new Headers(
            options.headers || ((input && input.headers) ? input.headers : undefined)
        );
        const csrf = _ancserCookie(ANCSERTPX_CSRF_COOKIE);
        if (csrf) headers.set(ANCSERTPX_CSRF_HEADER, csrf);
        options.headers = headers;
        options.credentials = 'same-origin';
    }
    return _ancserNativeFetch(input, options);
};
// Far-past anchor for full-range fetches. The paginated backend walks back from
// today to the contract's earliest bar; this just has to predate any NQ/MNQ data.
const FULL_RANGE_START = '2008-01-01';
// CONNECT (and auto-connect on boot) only loads this many days of recent history
// so startup is fast (account list + chart + GO LIVE ready in ~1s). The full
// multi-year range is fetched lazily on the first backtest / Machine Learning
// action via _ensureBacktestData().
const CONNECT_WARMUP_DAYS = 14;
// Cap how many recent 1m bars get pulled into the chart. Full-range data can be
// hundreds of thousands of bars; rendering them all blocks the main thread and
// freezes the tab. Backtest / ML use the complete backend dataset regardless.
const CHART_MAX_CANDLES = 60000;
// Default framing keeps the latest 200-bar smooth window around the middle of
// the viewport instead of anchoring the view to the newest candle.
const CHART_DEFAULT_SMOOTH_BARS = 200;
const CHART_DEFAULT_VISIBLE_HOURS = 18;
const CHART_PAN_SENSITIVITY = 1.10;
const CHART_PAN_DIRECTION_THRESHOLD_PX = 5;
// Lightweight Charts' native mouse inertia is driven by the uncorrected
// horizontal drag.  Once the 10% sensitivity correction is applied, that
// native animation can briefly snap back to its old offset at mouse-up.  Keep
// vertical dragging native, but run a small horizontal inertia from the
// corrected position instead.
const CHART_PAN_INERTIA_DECAY_MS = 220;
const CHART_PAN_INERTIA_MIN_SPEED = 0.00075;
// Keep risk/reward fill intensity stable across zoom and repaint cycles.
const CHART_RISK_REWARD_GREEN_ALPHA = 0.105;
const CHART_RISK_REWARD_RED_ALPHA = 0.115;
// The chart starts with a recent slice, then asks the append-only local store
// for an older page when the user pans to the left edge.  This keeps CONNECT
// fast without making the visible history stop at the warm-up window.
const CHART_HISTORY_PAGE_SIZE = 5000;
const CHART_HISTORY_TRIGGER_BARS = 120;
const CHART_AUTOSCALE_HOLD_MS = 140;
const CHART_AUTOSCALE_TRANSITION_MS = 220;
let chart = null;
let candleSeries = null;
let _rawCandleBuffer = []; // [{time(unix), open, high, low, close, volume}]
let _chartHistoryLoading = false;
let _chartHistoryApplying = false;
let _chartHistoryExhausted = false;
let _chartHistorySuppressUntil = 0;
let _chartOverlayRafId = null;
let _chartResizeRafId = null;
let _chartLastSize = '';
let _chartPanCorrectionRafId = null;
let _chartPanLastX = null;
let _chartPanInertiaRafId = null;
let _chartPanInertiaPosition = null;
let _chartPanInertiaVelocity = 0;
let _chartAutoscaleRafId = null;
let _chartAutoscaleReleaseTimer = null;
let _chartAutoscaleHoldUntil = 0;
let _chartAutoscaleHoldRange = null;
let _chartAutoscaleTarget = null;
let _chartAutoscaleEase = null;
// coordinateToPrice() can briefly expose Lightweight Charts' raw/default
// range while a wheel gesture is being processed.  Keep the last range that
// this provider actually presented so the next gesture never starts from
// that transient range.
let _chartAutoscalePresentedRange = null;
let _chartAutoscaleInitialised = false;
let _chartAutoscaleAnimateNext = false;
let _chartPanGesture = null;
let backtestData = null;
let currentAccount = null;
let allAccounts = [];
const CONNECTION_STATUS_POLL_INTERVAL_MS = 2500;
let _connectionStatusPollTimer = null;
let _connectionStatusProbeInFlight = false;
let _topstepConnectInProgress = false;

// -- Strategy Params & Presets ----------------------

function defaultContractId() {
    return FRONT_MONTH_CONTRACTS.MNQ || 'MNQ';
}

function contractRootFromId(contractId) {
    const raw = String(contractId || '').trim().toUpperCase();
    if (!raw) return '';

    const parts = raw.split('.');
    let root = (parts.length >= 5 && parts[0] === 'CON' && parts[1] === 'F')
        ? parts[3]
        : raw;
    const short = /^([A-Z]+?)[FGHJKMNQUVXZ]\d{2}$/.exec(root);
    if (short) root = short[1];

    // ProjectX calls the E-mini Nasdaq product ENQ, while the user-facing
    // label remains NQ. Keep one canonical value in the controls.
    return root === 'NQ' ? 'ENQ' : root;
}

function contractUiValue(contractId) {
    const raw = String(contractId || '').trim().toUpperCase();
    const root = contractRootFromId(raw);
    return FRONT_MONTH_CONTRACTS[root] ? root : raw;
}

// The backend owns front-month calculation. The browser stores only a bare
// product root, so a rollover can never leave an expiry-looking option behind.
function refreshContractOptions() {
    document.querySelectorAll('[data-contract-root]').forEach(option => {
        const root = String(option.dataset.contractRoot || '').toUpperCase();
        option.value = root;
    });
    const cidInput = document.getElementById('contract-id');
    if (cidInput) {
        const raw = String(cidInput.value || 'MNQ').toUpperCase();
        cidInput.value = contractUiValue(raw) || 'MNQ';
    }
}
document.addEventListener('DOMContentLoaded', refreshContractOptions);

const DEFAULT_STRATEGY_PARAMS = {
    market_clock_version: MARKET_CLOCK_VERSION,
    // 1.0.9: TREND 已移除,預設策略改為 factor
    strategy: 'factor',
    tp_ticks: 200,
    sl_ticks: 50,
    trail_sl_ticks: 10,
    trail_sl_pct: 0.05,
    trail_trigger_pct: 0.30,
    trail_enabled: true,
    tr_tp_ticks: 200,
    tr_sl_ticks: 50,
    tr_trail_sl_ticks: 10,
    tr_trail_sl_pct: 0.05,
    tr_trail_trigger_pct: 0.30,
    tr_trail_enabled: true,
    tr_full_tp_lock: 0,
    candle_seconds: 60,
    contract_id: defaultContractId(),
    contract_size: 3,
    value_area_pct: 0.80,
    area_timeframe: '15m',
    tr_overlap_trade_tf: 'merged',
    rr_ratio: 2,
    full_tp_lock: 0,
    breakout_confirm_bars: 7,
    one_trade_per_session_direction: true,
    tr_one_trade_per_session: true,
    tr_allowed_sessions: ['ASIA'],
    // Zone stability is enabled by default; keep this flag for future experiments.
    skip_zone_stability: false,
    conf_band_ticks: 4,
    conf_min_distinct_tf: 2,
    conf_rr: 1.0,
    conf_wait_minutes: 1,
    conf_base_minutes: 1,
    conf_min_prob: 0.65,
    conf_ev_floor: null,
    conf_rr_grid: null,
    conf_use_scorer: true,
    conf_enable_breakout: false,
    conf_max_risk_ticks: null,
    conf_sl_reference_tf: 'largest',
    conf_allowed_sessions: ['ASIA'],
    conf_trail_trigger_pct: 0.50,
    conf_trail_lock_pct: 0.05,
    conf_full_tp_lock: 0,
    conf_session_limit: true,
    sigma_window_minutes: 15,
    sigma_method: 'std',
    sigma_entry_mode: 'blind',
    sigma_accept_mode: 'none',
    sigma_start: 1.0,
    sigma_max: 3.0,
    sigma_target_mode: 'half',
    sigma_stop_span: 1.0,
    sigma_accept_sigma: 2.0,
    sigma_accept_bars: 2,
    factor_timeframe_minutes: 5,
    factor_signal_family: 'emapmo',
    factor_side_mode: 'long_only',
    factor_pmo_signal_mode: 'early',
    factor_session_va_filter: 'off',
    factor_sl_rule: 'atr_blend',
    factor_tp_rule: 'atr_blend',
    factor_sl_value: 2.5,
    factor_tp_value: 2.0,
    factor_max_hold_bars: 0,   // 1.0.9: HOLD 5m system removed → SL/TP-only exits
    factor_max_trades_per_day: 3,
    factor_warmup_bars: 150,
    // Optional source levels for short circle signals.  null preserves the
    // legacy kind-only behavior; [] intentionally disables both levels.
    pi_short_levels: null,
    pi_short_sl_value: 2.5,
    pi_long_hold_min: 0,
    pi_short_hold_min: 60,
    delta_window: 5,
    delta_baseline_window: 30,
    delta_strength_multiplier: 1.0,
    delta_weakening_ratio: 0.70,
    delta_stall_ticks: 1,
    delta_value_lookback: 10,
    delta_value_touch_ticks: 0,
    delta_source: 'whole',
    delta_gate: 'location',
    delta_pattern: 'absorption',
    delta_side_mode: 'all',
    delta_require_profile: true,
    option_wall_submodel: 'primary_strict',
    option_wall_side_mode: 'all',
    option_wall_long_sl_atr: 4.0,
    option_wall_short_sl_atr: 1.5,
    option_wall_max_hold_min: 60,
    option_wall_max_trades_per_day: 3,
    // 1.0.8: 移除 mlc2_* 預設(ml_consolidation_v2 已刪除)
};

const _appliedStrategyParamsByMode = {
    bt: Object.assign({}, DEFAULT_STRATEGY_PARAMS),
    live: Object.assign({}, DEFAULT_STRATEGY_PARAMS),
};
const MODIFIED_PRESET_VALUE = '__unsaved_modified__';
const _loadedPresetNameByMode = { bt: '', live: '' };
let _presetDirtyTrackingBound = false;

const MNQ_SIZE_CHOICES = [1, 2, 3, 5, 10];  // 1.0.8: sizing choices
const TRAIL_TICK_STEP = 5;
const TRAIL_SL_PCT_CHOICES = [0.05, 0.10, 0.20, 0.30, 0.40, 0.50];
const TRAIL_TRIGGER_PCT_CHOICES = [0, 0.30, 0.50, 0.70];

function contractSymbolFromId(contractId) {
    const cid = (contractId || '').toUpperCase();
    const m = /^CON\.F\.US\.([A-Z]+)\./.exec(cid);
    if (m) return m[1] === 'ENQ' ? 'NQ' : m[1];
    if (['MNQ', 'NQ', 'MES', 'GC', 'MGC', 'ZL'].includes(cid)) return cid;
    return 'NQ';
}

function contractLabelFromId(contractId) {
    return contractSymbolFromId(contractId);
}

function displaySymbolFromTrade(t) {
    if (t && t.symbol) return String(t.symbol).startsWith('/') ? t.symbol : '/' + t.symbol;
    if (t && t.contract_id) return '/' + contractLabelFromId(t.contract_id);
    const liveContract = document.getElementById('contract-live')?.value;
    const btContract = document.getElementById('contract-bt')?.value;
    return '/' + contractLabelFromId(liveContract || btContract || DEFAULT_STRATEGY_PARAMS.contract_id);
}

function positionSideMeta(pos) {
    const raw = pos?.side ?? pos?.positionSide ?? pos?.position_side ?? pos?.Side;
    const text = String(raw ?? '').toLowerCase();
    const num = Number(raw);
    // TopstepX / ProjectX position payload uses `type`: 1 = Long, 2 = Short.
    const t = Number(pos?.type ?? pos?.Type ?? pos?.positionType);
    let isLong = num === 0 || text === 'long' || text === 'buy';
    let isShort = num === 1 || num === 2 || text === 'short' || text === 'sell';
    if (t === 1) { isLong = true; isShort = false; }
    else if (t === 2) { isShort = true; isLong = false; }
    return {
        label: isLong ? 'LONG' : (isShort ? 'SHORT' : String(raw ?? '?')),
        isLong: isLong,
    };
}

function positionQty(pos) {
    const n = Number(pos?.size ?? pos?.quantity ?? pos?.qty ?? 1);
    return Number.isFinite(n) ? Math.abs(n) : '?';
}

function positionAvgText(pos) {
    const raw = pos?.averagePrice ?? pos?.avgPrice ?? pos?.avg_price ?? pos?.price ?? pos?.fillPrice;
    const n = Number(raw);
    return Number.isFinite(n) ? n.toFixed(2) : '?';
}

function positionContractLabel(pos, fallbackContractId) {
    const cid = pos?.contractId || pos?.contract_id || pos?.ContractId || fallbackContractId;
    return contractLabelFromId(cid);
}

function pointValueForContract(contractId) {
    const sym = contractSymbolFromId(contractId);
    if (sym === 'MNQ') return 2;
    if (sym === 'MES') return 5;
    if (sym === 'GC') return 100;
    if (sym === 'MGC') return 10;
    if (sym === 'ZL') return 600;
    return 20;
}

function tickDollarValue(contractId, size) {
    const sym = contractSymbolFromId(contractId);
    const tickSize = sym === 'ZL' ? 0.01 : ((sym === 'GC' || sym === 'MGC') ? 0.10 : 0.25);
    return pointValueForContract(contractId) * tickSize * normalizeContractSize(contractId, size);
}

function fmtPct(pct) {
    const n = Math.round(Math.abs(pct) * 100);
    if (pct > 0) return '+' + n + '% TP';
    if (pct < 0) return '-' + n + '% SL';
    return 'BE';
}

function fmtSigned(n, suffix) {
    if (n > 0) return '+' + n + suffix;
    if (n < 0) return '-' + Math.abs(n) + suffix;
    return '0' + suffix;
}

function fmtMoney(n) {
    const abs = Math.abs(n);
    const text = abs >= 10 ? abs.toFixed(0) : abs.toFixed(2);
    if (n > 0) return '+$' + text;
    if (n < 0) return '-$' + text;
    return '$0';
}

function normalizeAreaPctValue(value) {
    return 80;
}

function floorTicksToStep(ticks) {
    const n = Math.abs(Number(ticks) || 0);
    return Math.floor(n / TRAIL_TICK_STEP) * TRAIL_TICK_STEP;
}

function trailTicksFromPct(pct, slTicks, tpTicks, triggerPct) {
    const sl = Math.abs(parseInt(slTicks, 10) || 0);
    const tp = Math.abs(parseInt(tpTicks, 10) || 0);
    const p = Math.max(0.05, Math.min(0.50, parseFloat(pct) || 0.05));
    const trigger = parseFloat(triggerPct) || 0;
    if (trigger <= 0) return 0;
    let ticks = floorTicksToStep(tp * p);
    const maxPositive = Math.max(0, floorTicksToStep(tp * trigger) - TRAIL_TICK_STEP);
    if (ticks > maxPositive) ticks = maxPositive;
    return Math.max(0, Math.min(tp, ticks));
}

function trailPctFromTicks(ticks, slTicks, tpTicks) {
    const t = parseInt(ticks, 10) || 0;
    const sl = Math.abs(parseInt(slTicks, 10) || 0);
    const tp = Math.abs(parseInt(tpTicks, 10) || 0);
    const raw = tp ? Math.max(0.05, t / tp) : 0.05;
    return TRAIL_SL_PCT_CHOICES.reduce((best, pct) =>
        Math.abs(pct - raw) < Math.abs(best - raw) ? pct : best, TRAIL_SL_PCT_CHOICES[0]);
}

function allowedSizesForContract(contractId) {
    return contractSymbolFromId(contractId) === 'MNQ' ? MNQ_SIZE_CHOICES : [1];
}

function normalizeContractSize(contractId, size) {
    const allowed = allowedSizesForContract(contractId);
    const n = parseInt(size, 10);
    return allowed.includes(n) ? n : allowed[0];
}

function syncSizeOptions(mode, wantedSize) {
    const cidEl = document.getElementById('contract-' + mode);
    const sizeEl = document.getElementById('size-' + mode);
    if (!cidEl || !sizeEl) return;
    const wanted = wantedSize != null ? wantedSize : sizeEl.value;
    const allowed = allowedSizesForContract(cidEl.value);
    const normalized = normalizeContractSize(cidEl.value, wanted);
    sizeEl.innerHTML = '';
    allowed.forEach(size => {
        const opt = document.createElement('option');
        opt.value = String(size);
        opt.textContent = String(size);
        sizeEl.appendChild(opt);
    });
    sizeEl.value = String(normalized);
}

function updateTrailBounds(mode, preferredPct) {
    // preferredPct: number -> preferred trail pct; omitted -> read current select value
    var preferredTrailPct = (preferredPct != null) ? preferredPct : null;

    const slEl = document.getElementById('sl-ticks-' + mode);
    const tpEl = document.getElementById('tp-ticks-' + mode);
    const trailPctEl = document.getElementById('trail-sl-pct-' + mode);
    const trailTicksEl = document.getElementById('trail-sl-ticks-' + mode);
    const triggerEl = document.getElementById('trail-trigger-pct-' + mode);
    const pctSpan = document.getElementById('trail-sl-pct-' + mode + '-val');
    const valSpan = document.getElementById('trail-sl-ticks-' + mode + '-val');
    if (!slEl || !tpEl || !trailPctEl || !trailTicksEl || !triggerEl) return;

    const sl = Math.abs(parseInt(slEl.value, 10) || 0);
    const tp = Math.abs(parseInt(tpEl.value, 10) || 0);
    const cidEl = document.getElementById('contract-' + mode);
    const sizeEl = document.getElementById('size-' + mode);
    const contractId = (cidEl && cidEl.value) || DEFAULT_STRATEGY_PARAMS.contract_id;
    const size = sizeEl ? sizeEl.value : DEFAULT_STRATEGY_PARAMS.contract_size;
    const tickValue = tickDollarValue(contractId, size);

    const parsedTrigger = parseFloat(triggerEl.value);
    const triggerSelected = Number.isFinite(parsedTrigger) ? parsedTrigger : DEFAULT_STRATEGY_PARAMS.trail_trigger_pct;
    triggerEl.innerHTML = '';
    TRAIL_TRIGGER_PCT_CHOICES.forEach(pct => {
        const ticks = floorTicksToStep(tp * pct);
        const opt = document.createElement('option');
        opt.value = pct === 0 ? '0' : pct.toFixed(2);
        opt.textContent = pct === 0
            ? 'OFF'
            : Math.round(pct * 100) + '% (' + ticks + 't / ' + fmtMoney(ticks * tickValue) + ')';
        triggerEl.appendChild(opt);
    });
    triggerEl.value = TRAIL_TRIGGER_PCT_CHOICES.includes(triggerSelected)
        ? (triggerSelected === 0 ? '0' : triggerSelected.toFixed(2))
        : DEFAULT_STRATEGY_PARAMS.trail_trigger_pct.toFixed(2);
    const triggerPct = parseFloat(triggerEl.value) || 0;
    const trailEnabled = triggerPct > 0;

    const oldPct = preferredTrailPct != null ? preferredTrailPct : parseFloat(trailPctEl.value);
    const choices = trailEnabled
        ? TRAIL_SL_PCT_CHOICES.filter(pct => pct <= 0.50 && pct < triggerPct - 1e-9)
        : [0];
    const wantedPct = Number.isFinite(oldPct) ? oldPct : DEFAULT_STRATEGY_PARAMS.trail_sl_pct;
    const selectedPct = choices.includes(wantedPct)
        ? wantedPct
        : choices.reduce((best, pct) =>
            Math.abs(pct - wantedPct) < Math.abs(best - wantedPct) ? pct : best, choices[0]);

    trailPctEl.innerHTML = '';
    choices.forEach(pct => {
        const ticks = trailTicksFromPct(pct, sl, tp, triggerPct);
        const opt = document.createElement('option');
        opt.value = pct.toFixed(2);
        opt.textContent = trailEnabled
            ? fmtPct(pct) + ' (' + fmtSigned(ticks, 't') + ' / ' + fmtMoney(ticks * tickValue) + ')'
            : 'OFF';
        trailPctEl.appendChild(opt);
    });
    trailPctEl.value = selectedPct.toFixed(2);
    const trailTicks = trailTicksFromPct(selectedPct, sl, tp, triggerPct);
    trailTicksEl.value = String(trailTicks);
    trailPctEl.disabled = !trailEnabled;
    trailPctEl.style.opacity = trailEnabled ? '1' : '0.4';
    if (pctSpan) {
        pctSpan.textContent = trailEnabled ? fmtPct(selectedPct) : 'OFF';
        pctSpan.style.opacity = trailEnabled ? '1' : '0.4';
    }
    if (valSpan) {
        valSpan.textContent = trailEnabled
            ? fmtSigned(trailTicks, 't') + ' / ' + fmtMoney(trailTicks * tickValue)
            : '0t / ' + fmtMoney(0);
        valSpan.style.opacity = trailEnabled ? '1' : '0.4';
    }
}

// RR ratio selector: SL is auto (lowest-volume node); TP = RR x SL distance.
// We keep a nominal hidden tp-ticks (= RR x fallback SL ticks) so the trailing-SL
// display math (which is expressed as a fraction of TP) keeps working.
function onRrChange(mode) {
    const rrEl = document.getElementById('rr-ratio-' + mode);
    const slEl = document.getElementById('sl-ticks-' + mode);
    const tpEl = document.getElementById('tp-ticks-' + mode);
    const lbl = document.getElementById('rr-ratio-' + mode + '-val');
    const rr = rrEl ? (parseFloat(rrEl.value) || 2) : 2;
    const slTicks = slEl ? (Math.abs(parseInt(slEl.value, 10)) || 50) : 50;
    if (tpEl) tpEl.value = String(rr * slTicks);
    if (lbl) lbl.textContent = '1:' + (Number.isInteger(rr) ? String(rr) : rr.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
    updateTrailBounds(mode);
}

// SINGLE vs OVERLAP method. Overlap reveals the timeframe multi-select and uses
// the merged synthetic zone (avg VAH/VAL/POC) only when all selected TFs' value
// areas overlap. Single uses one AREA TF zone.
// Timeframe checkbox changed → keep the strategy's execution selection in sync.
function onTfSelectionChange(mode) {
    enforceSessionTfExclusive(mode);   // 1.0.8: SESSION 與其他 TF 互斥
    updateOverlapTradeTfControl(mode);
}

// 1.0.8: SESSION(0.15.5 式整段 session 生長區間)勾選時,其他 TF 全部
// 取消勾選並鎖灰;取消 SESSION 時解鎖,若無任何 TF 被選則回落 15m。
function enforceSessionTfExclusive(mode) {
    const boxes = Array.from(document.querySelectorAll('.overlap-tf-chk-' + mode));
    const sess = boxes.find(c => c.value === 'session');
    if (!sess) return;
    const others = boxes.filter(c => c !== sess);
    if (sess.checked) {
        others.forEach(c => { c.checked = false; c.disabled = true; });
    } else {
        others.forEach(c => { c.disabled = false; });
        if (!others.some(c => c.checked)) {
            const fallback = others.find(c => c.value === '15m');
            if (fallback) fallback.checked = true;
        }
    }
}

function readOverlapTfCombo(mode) {
    const order = ['15m', '30m', '1h', '4h', 'session'];  // 1.0.8: +session
    const checked = Array.from(document.querySelectorAll('.overlap-tf-chk-' + mode))
        .filter(c => c.checked).map(c => c.value);
    return order.filter(tf => checked.includes(tf));
}

function setOverlapTfCombo(mode, combo) {
    const set = new Set(Array.isArray(combo) ? combo : []);
    document.querySelectorAll('.overlap-tf-chk-' + mode).forEach(c => {
        c.checked = set.has(c.value);
    });
    enforceSessionTfExclusive(mode);   // 1.0.8: preset 載入路徑也要互斥
}

function normalizeTrendOverlapTradeTf(value) {
    return String(value || '').trim().toLowerCase() === 'smallest' ? 'smallest' : 'merged';
}

function trendTfUsage(params) {
    const p = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params || {});
    const combo = Array.isArray(p.tf_combo) ? p.tf_combo.filter(Boolean) : [];
    const method = (p.method || (combo.length >= 2 ? 'overlap' : 'single')).toLowerCase();
    const tfs = (method === 'overlap' && combo.length >= 2)
        ? combo
        : [String(p.area_timeframe || combo[0] || '15m')];
    const isOverlap = method === 'overlap' && tfs.length >= 2;
    const tradeMode = normalizeTrendOverlapTradeTf(p.tr_overlap_trade_tf);
    const trade = isOverlap
        ? (tradeMode === 'smallest' ? tfs[0] : 'merged:' + tfs.join('+'))
        : tfs[0];
    return {
        judge: tfs.join('+'),
        overlap: isOverlap ? tfs.join('+') : 'OFF',
        trade: trade,
    };
}

function trendTfUsageText(params) {
    const u = trendTfUsage(params);
    return 'judge=' + u.judge + ' overlap=' + u.overlap + ' trade=' + u.trade;
}

function updateOverlapTradeTfControl(mode) {
    const sel = document.getElementById('tr-overlap-trade-tf-' + mode);
    const hint = document.getElementById('tr-overlap-trade-hint-' + mode);
    if (!sel && !hint) return;
    const tfs = readOverlapTfCombo(mode);
    const isOverlap = tfs.length >= 2;
    if (sel) {
        sel.disabled = !isOverlap;
        const mergedOpt = Array.from(sel.options).find(o => o.value === 'merged');
        const smallestOpt = Array.from(sel.options).find(o => o.value === 'smallest');
        if (mergedOpt) mergedOpt.textContent = isOverlap ? ('Merged overlap (' + tfs.join('+') + ')') : 'Merged overlap';
        if (smallestOpt) smallestOpt.textContent = isOverlap ? ('Smallest selected TF (' + tfs[0] + ')') : 'Smallest selected TF';
    }
    const params = {
        method: isOverlap ? 'overlap' : 'single',
        tf_combo: isOverlap ? tfs : [],
        area_timeframe: tfs[0] || '15m',
        tr_overlap_trade_tf: sel ? sel.value : 'merged',
    };
    const u = trendTfUsage(params);
    if (hint) {
        hint.textContent = 'JUDGE ' + u.judge + ' / OVERLAP ' + u.overlap + ' / TRADE ' + u.trade;
        hint.style.color = isOverlap ? 'var(--text2)' : 'var(--text3)';
    }
}

function onOverlapTradeTfChange(mode) {
    const sel = document.getElementById('tr-overlap-trade-tf-' + mode);
    const value = normalizeTrendOverlapTradeTf(sel && sel.value);
    if (!_appliedStrategyParamsByMode[mode]) {
        _appliedStrategyParamsByMode[mode] = Object.assign({}, DEFAULT_STRATEGY_PARAMS);
    }
    _appliedStrategyParamsByMode[mode].tr_overlap_trade_tf = value;
    if (sel) sel.value = value;
    updateOverlapTradeTfControl(mode);
}

function normalizeStrategyName(value) {
    const v = String(value || '').trim().toLowerCase();
    // 1.0.10: 獨立 PMO 策略已移除 —— 舊設定落到 factor(等價的 emapmo 家族),
    // 與後端 _normalize_strategy_name 的行為一致。
    if (v === 'pmo') return 'factor';
    if (v === 'pi') return 'pi';          // 1.0.10: 外部 Discord 訊號
    if (v === 'optionwall' || v === 'option_wall') return 'optionwall';
    if (v === 'delta' || v === 'delta_va' || v === 'delta+va' || v === 'delta_absorb' || v === 'delta_absorption') return 'delta_absorption';
    if (v === 'sigma') return 'sigma';
    if (v === 'fade') return 'fade';   // 1.0.8: DAY ZONE 前日VA回歸
    if (v === 'factor') return 'factor';
    // 1.0.9: 改名相容 —— 舊 preset 存的是 intramom / sessfib
    if (v === 'momentum' || v === 'intramom' || v === 'claudefib') return 'momentum';   // 1.0.9: MOMENTUM 日內動能延續
    if (v === 'betafib' || v === 'sessfib') return 'betafib';     // 1.0.9: SESSFIB 夜盤 Fib 回撤(觀察用)
    // 1.0.8: 移除 ml_consolidation_v2 (mlc2) 策略映射
    // 1.0.9: TREND 已移除 —— 舊 preset 的 'trend' 一律落到 factor,
    // 否則 _setChoice 找不到選項會自己補一個死選項回下拉選單。
    return 'factor';
}

// Stable strategy identity is deliberately separate from the display copy.
// Values stay unchanged because they are part of preset and API compatibility.
const STRATEGY_PRESENTATION = Object.freeze({
    fade: {
        displayName: 'FADE',
        description: 'Previous-day value-area mean reversion.',
    },
    sigma: {
        displayName: 'SIGMA',
        description: 'Rolling-distribution resting fade.',
    },
    factor: {
        displayName: 'FACTOR',
        description: 'EMAPMO / KDJMA / MREV factor signals.',
    },
    momentum: {
        displayName: 'MOMENTUM',
        description: 'Intraday momentum continuation.',
    },
    betafib: {
        displayName: 'BETAFIB',
        description: 'Overnight Fibonacci retracement (observation only).',
    },
    pi: {
        displayName: 'PI',
        description: 'External Discord signal routing.',
    },
    optionwall: {
        displayName: 'OPTION WALL',
        description: 'Causal QQQ Option Wall / Gamma signals mapped to MNQ (historical replay).',
    },
    delta_absorption: {
        displayName: 'DELTA ABSORPTION',
        description: 'Completed MBO Delta change at the previous RTH 70% value area.',
    },
});

function strategyPresentation(value) {
    return STRATEGY_PRESENTATION[normalizeStrategyName(value)] || STRATEGY_PRESENTATION.factor;
}

function _setStrategySelect(mode, strategy) {
    const normalized = normalizeStrategyName(strategy);
    const el = document.getElementById('strategy-' + mode);
    if (el) {
        if (!Array.from(el.options || []).some(o => o.value === normalized)) {
            const opt = document.createElement('option');
            opt.value = normalized;
            opt.textContent = strategyDisplayName(normalized);
            el.appendChild(opt);
        }
        el.value = normalized;
    }
    if (!_appliedStrategyParamsByMode[mode]) {
        _appliedStrategyParamsByMode[mode] = Object.assign({}, DEFAULT_STRATEGY_PARAMS);
    }
    _appliedStrategyParamsByMode[mode].strategy = normalized;
    return normalized;
}

function _selectedPresetName(mode) {
    const sel = document.getElementById('preset-' + mode);
    const name = sel ? String(sel.value || '') : '';
    return (name && name !== 'default' && name !== MODIFIED_PRESET_VALUE) ? name : '';
}

function _selectedPresetParams(mode) {
    const name = _selectedPresetName(mode);
    return name ? (((_presetsCache && _presetsCache.presets) || {})[name] || null) : null;
}

function reconcilePresetStrategyForDispatch(mode, params, context) {
    const presetName = _selectedPresetName(mode);
    const preset = _selectedPresetParams(mode);
    if (!preset || !params) return params;
    const presetStrategy = normalizeStrategyName(preset.strategy);
    const payloadStrategy = normalizeStrategyName(params.strategy);
    if (presetStrategy !== payloadStrategy) {
        params.strategy = presetStrategy;
        if (!_appliedStrategyParamsByMode[mode]) {
            _appliedStrategyParamsByMode[mode] = Object.assign({}, DEFAULT_STRATEGY_PARAMS);
        }
        _appliedStrategyParamsByMode[mode].strategy = presetStrategy;
        _setStrategySelect(mode, presetStrategy);
        updateStrategyParamVisibility(mode);
        log((context || 'PARAMS') + ': repaired model from selected preset "' +
            _presetDisplayName(presetName) + '" (' + payloadStrategy + ' -> ' + presetStrategy + ')', 'warn');
    }
    return params;
}

function strategyDisplayName(value) {
    return strategyPresentation(value).displayName;
}

function strategyIdPrefix(kind) {
    return '';
}

function _paramControlGroup(id) {
    const el = document.getElementById(id);
    return el ? (el.closest('.form-group') || el) : null;
}

function _showParamControl(id, on) {
    const grp = _paramControlGroup(id);
    if (grp) grp.style.display = on ? '' : 'none';
}

function _setParamControlDisabled(id, off, title) {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = off;
    const grp = el.closest('.form-group');
    if (grp) {
        grp.style.opacity = off ? '0.35' : '';
        grp.title = off ? (title || '') : '';
    }
}

// ML (confluence) uses a completely different parameter set than TREND.
// When ML is selected we hide every trend-only control — TREND box, AREA %,
// CONFIRM, and the TIMEFRAMES picker — and reveal only the ML params actually
// used by the confluence engine (min prob / rr / band / min distinct tf).
function updateStrategyParamVisibility(mode) {
    const strategy = normalizeStrategyName(
        (document.getElementById('strategy-' + mode) || {}).value
    );
    const isML = strategy === 'confluence';
    const isTrend = strategy === 'trend';
    const isFade = strategy === 'fade';   // 1.0.8
    const isSigma = strategy === 'sigma';
    const isFactor = strategy === 'factor';
    const isDelta = strategy === 'delta_absorption';
    const show = (id, on) => {
        const el = document.getElementById(id);
        if (el) el.style.display = on ? '' : 'none';
    };
    const showControl = (id, on) => _showParamControl(id + '-' + mode, on);
    // trend-only controls — hidden in ML mode.
    show('tr-params-' + mode, !isML);
    show('overlap-tf-row-' + mode, !isML && isTrend);
    show('tr-overlap-trade-row-' + mode, !isML && isTrend);
    showControl('area-pct', !isML && (isTrend || isFade));
    showControl('confirm-bars', !isML && isTrend);
    showControl('tr-exit-mode', !isML && (isTrend || isFactor || isDelta));
    showControl('rr-ratio', !isML && (isTrend || isFactor || isDelta));
    showControl('trail-trigger-pct', !isML && (isTrend || isFactor || isDelta));
    showControl('trail-sl-pct', !isML && (isTrend || isFactor || isDelta));
    // ML Confluence params — shown only in confluence mode
    show('ml-params-' + mode, isML);
    if (isML) onRrModeChange(mode);
    if (!isML) {
        updateOverlapTradeTfControl(mode);
        updateTrailBounds(mode);
        enforceFadeTfLock(mode, isFade);   // 1.0.8: fade → 鎖 DAY
        onExitModeChange(mode);            // 1.0.8: 依 exit mode 灰化
    }
    // 1.0.9: fade 進場模式列僅 fade 顯示;唯讀 SL 模型顯示依策略/fade 子模式更新
    show('fade-entry-mode-row-' + mode, isFade);
    // 1.0.9: INTRAMOM 沿用 FACTOR 區塊的 SL/方向/RR 控制項,所以兩者一起顯示;
    // 但 EMAPMO 專屬的因子族/訊號模式/門檻滑桿在 INTRAMOM 下無意義。
    const isIntramom = strategy === 'momentum';
    // 1.0.9: SESSFIB 與 INTRAMOM 同樣沿用 FACTOR 區塊的 SL/方向/RR,
    // 但 EMAPMO 專屬的因子族/訊號模式/VA 濾網對兩者都無意義。
    const isSessfib = strategy === 'betafib';
    // 1.0.10: PI 沿用 FACTOR 區塊的 SL/TP/方向控制項(多單用),
    // 但 EMAPMO 專屬的族/訊號模式/VA 濾網對它無意義。
    const isPi = strategy === 'pi';
    const isOptionWall = strategy === 'optionwall';
    // PI has its own directional signal matrix.  The FACTOR entry controls
    // (SIDE / SIGNAL MODE / VA FILTER) used to remain visible here as well,
    // which made PI show two overlapping direction controls.  PI still uses
    // the shared FACTOR exit/risk block below, but not its entry block.
    show('factor-params-' + mode, isFactor || isIntramom || isSessfib);
    show('delta-params-' + mode, isDelta);
    show('momentum-params-' + mode, isIntramom);
    show('betafib-params-' + mode, isSessfib);
    // 1.0.10: MODEL SETTINGS 已拆成 ENTRY / EXIT 兩段。
    // factor-params-* 只留進場側(族/方向/訊號/VA),SL 錨點搬到 factor-exit-*,
    // BETAFIB 的 fib 層級搬到 betafib-exit-*,兩者的顯示條件與各自的進場區塊相同。
    show('factor-exit-' + mode, isFactor || isIntramom || isSessfib || isPi || isDelta);
    show('pi-params-' + mode, isPi);
    show('pi-exit-' + mode, isPi);
    show('option-wall-params-' + mode, isOptionWall);
    show('pi-short-sl-group-' + mode, isPi || isDelta);
    show('pi-long-hold-group-' + mode, isPi || isDelta);
    show('pi-short-hold-group-' + mode, isPi || isDelta);
    const slRow = document.getElementById('factor-sl-row-' + mode);
    if (slRow) slRow.classList.toggle('pi-dual-sl', isPi || isDelta);
    const longSlLabel = document.getElementById('factor-sl-value-label-' + mode);
    if (longSlLabel) {
        // PI uses factor_sl_value for longs and pi_short_sl_value for shorts.
        // Keep the generic label for every other strategy sharing this control.
        longSlLabel.textContent = (isPi || isDelta) ? 'LONG SL' : 'SL INPUT';
    }
    show('betafib-exit-' + mode, isSessfib);
    showControl('tp-cap-usd', !isOptionWall);
    showControl('factor-max-trades', !isOptionWall);
    showControl('tr-session-limit', !isOptionWall);
    showControl('tr-allowed-sessions', !isOptionWall && !isDelta);
    ['factor-family-', 'factor-pmo-mode-', 'factor-va-filter-'].forEach((id) => {
        const el = document.getElementById(id + mode);
        const row = el && el.closest ? el.closest('.form-group') : null;
        if (row) row.style.display = (isIntramom || isSessfib || isPi || isDelta) ? 'none' : '';
    });
    syncEmapmoThresholdRow(mode);   // 1.0.9: 門檻滑桿只在 EMAPMO 顯示
    let slText;
    if (isFade) {
        const fem = _mlSelectValue('fade-entry-mode-' + mode, 'limit');
        slText = (fem === 'or15')
            ? 'OR15: entry ± 0.2× previous-day VA width (two-sided false break · TP 1× width)'
            : 'DAY ZONE: previous-day VAL - 120 tick fixed buffer';
    } else if (isSigma) {
        slText = 'DISTRIBUTION: preset rolling sigma SL / center TP';
    } else if (isFactor) {
        slText = 'FACTOR: completed 5m signal, market entry; side/signal/SL/TP use FACTOR controls';
    } else if (isOptionWall) {
        slText = 'OPTION WALL: hourly causal signal · completed 5m ATR blend · no hard TP · 60m max';
    } else if (isDelta) {
        slText = 'DELTA ABSORPTION: completed 1m MBO Delta + prior RTH 70% VA touch';
    } else {
        slText = 'TREND: lowest-volume node between POC and VAH/VAL for SL';
    }
    let entryMode = 'market';
    if (isFade) {
        const femNow = _mlSelectValue('fade-entry-mode-' + mode, 'limit');
        entryMode = femNow === 'limit' ? 'limit' : 'market';
        slText = femNow === 'or15'
            ? 'DAY ZONE OR15: completed opening-range false-break candle, market entry'
            : 'DAY ZONE: previous-day value-area level timing; entry type is model-defined';
    } else if (isSigma) {
        slText = 'DISTRIBUTION: rolling distribution band timing; market entry after model signal';
    } else if (isFactor) {
        slText = 'FACTOR: completed 5m factor signal; live/backtest use last completed candle only';
    } else if (isOptionWall) {
        slText = 'OPTION WALL: PRIMARY STRICT hourly signal; market entry on MNQ historical replay';
    } else if (isDelta) {
        slText = 'DELTA ABSORPTION: completed 1m MBO bar; market entry after Delta + VA condition';
    } else {
        slText = 'TREND: completed candle + value-area breakout confirmation; market entry';
    }
    const slEl = document.getElementById('sl-model-display-' + mode);
    if (slEl) slEl.value = slText;
    const entryEl = document.getElementById('entry-mode-' + mode);
    if (entryEl) {
        entryEl.value = entryMode;
        entryEl.disabled = true;
        entryEl.title = 'Entry type is fixed by the selected model engine, so changing it here would make backtest/live inconsistent.';
        const grp = entryEl.closest('.form-group');
        if (grp) {
            grp.style.opacity = '0.55';
            grp.title = entryEl.title;
        }
    }
    syncFactorRiskControls(mode);
}

// 1.0.8: DAY ZONE = 固定用「前一整個交易日」水位 — TF 群組顯示 DAY(鎖定),
// 其他 TF 取消勾選並鎖灰;離開 fade 還原(無選擇時回落 15m)。
function enforceFadeTfLock(mode, isFade) {
    const dayChip = document.getElementById('tf-day-chip-' + mode);
    const boxes = Array.from(document.querySelectorAll('.overlap-tf-chk-' + mode));
    if (dayChip) dayChip.style.display = isFade ? '' : 'none';
    if (isFade) {
        boxes.forEach(c => { c.checked = false; c.disabled = true; });
    } else {
        boxes.forEach(c => { c.disabled = false; });
        enforceSessionTfExclusive(mode);
        if (!boxes.some(c => c.checked)) {
            const fallback = boxes.find(c => c.value === '15m');
            if (fallback) fallback.checked = true;
        }
    }
}

// 1.0.8: TP 切換 — ladder 用不到 RR RATIO / TRAIL TP TRIGGER / TRAIL SL
// (TP 移除、階梯常數固定 2R/1R),灰化不可改項;切回 tp 還原。
function onExitModeChange(mode) {
    const sel = document.getElementById('tr-exit-mode-' + mode);
    const strategy = normalizeStrategyName(
        (document.getElementById('strategy-' + mode) || {}).value
    );
    if (strategy !== 'trend' && strategy !== 'factor' && strategy !== 'delta_absorption') return;
    const isLadder = !!(sel && sel.value === 'ladder');
    const setGroupVisible = (id, on) => {
        const el = document.getElementById(id);
        if (!el) return;
        const grp = el.closest('.form-group');
        if (grp) grp.style.display = on ? '' : 'none';
        el.disabled = !on;
    };
    const ladderOption = sel && sel.querySelector
        ? sel.querySelector('option[value="ladder"]')
        : null;
    if (ladderOption) {
        ladderOption.hidden = strategy === 'delta_absorption';
        ladderOption.disabled = strategy === 'delta_absorption';
    }
    if (strategy === 'delta_absorption' && sel && sel.value === 'ladder') {
        sel.value = 'tp';
    }
    // RR/trailing controls are not used by LADDER.  Keep the behavior the
    // same for legacy models and include Delta so its PI-style exit block
    // cannot get stuck with stale ladder controls.
    setGroupVisible('rr-ratio-' + mode, !isLadder);
    setGroupVisible('trail-trigger-pct-' + mode, !isLadder);
    setGroupVisible('trail-sl-pct-' + mode, !isLadder);
    const ladderRow = document.getElementById('ladder-ratio-row-' + mode);
    if (ladderRow) ladderRow.style.display = isLadder ? '' : 'none';
}

function _factorRiskOptionList(rule, kind) {
    if (rule === 'range15_pct') {
        return [
            ['0.10', '10% of 15m area'],
            ['0.15', '15% of 15m area'],
            ['0.20', '20% of 15m area'],
            ['0.50', '50% of 15m area'],
            ['0.75', '75% of 15m area'],
        ];
    }
    // 1.0.10: FIB LEVEL 的 SL 不是倍數,而是 fib 層級本身(由 SL fib 那個下拉決定),
    // 所以 SL INPUT 在此模式下沒有意義 —— 給一個明確的佔位而不是誤導性的 ATR 倍數。
    if (rule === 'fib') {
        return [['0', 'Determined by SL fib']];
    }
    // DAILY ATR 與 ATR/ATR BLEND 同樣是倍數。PI 多空兩側共用 1–4、0.5 step。
    return [
        ['1', '1 x ATR'], ['1.5', '1.5 x ATR'],
        ['2', '2 x ATR'], ['2.5', '2.5 x ATR'],
        ['3', '3 x ATR'], ['3.5', '3.5 x ATR'], ['4', '4 x ATR'],
    ];
}

function onFactorRiskAnchorChange(mode, kind) {
    const ruleEl = document.getElementById('factor-' + kind + '-rule-' + mode);
    const valEl = document.getElementById('factor-' + kind + '-value-' + mode);
    if (!ruleEl || !valEl) return;
    const current = valEl.value;
    const options = _factorRiskOptionList(ruleEl.value, kind);
    valEl.innerHTML = '';
    let found = false;
    options.forEach(([value, label]) => {
        const opt = document.createElement('option');
        opt.value = value;
        opt.textContent = label;
        if (String(current) === value) found = true;
        valEl.appendChild(opt);
    });
    if (!found && current !== '') {
        const opt = document.createElement('option');
        opt.value = current;
        opt.textContent = ['atr', 'atr_blend', 'daily'].includes(ruleEl.value)
            ? current + ' x ATR'
            : current;
        valEl.appendChild(opt);
    }
    if (current !== '') valEl.value = current;
    // 1.0.10: SL ANCHOR 已併入 BETAFIB 的基準選擇 —— 選到 FIB LEVEL 時要
    // 把 fib 層級(SL fib / TP fib)那一列叫出來,那組才是該模式下的實際 TP。
    if (kind === 'sl' && typeof onBetafibBasisChange === 'function') onBetafibBasisChange(mode);
}

function syncFactorRiskControls(mode) {
    onFactorRiskAnchorChange(mode, 'sl');
}

function onRrModeChange(mode) {
    const sel = document.getElementById('conf-rrmode-' + mode);
    const isGrid = !!(sel && sel.value === 'grid');
    const rrEl = document.getElementById('conf-rr-' + mode);
    const gridEl = document.getElementById('conf-rrgrid-' + mode);
    if (rrEl) rrEl.style.display = isGrid ? 'none' : '';
    if (gridEl) gridEl.style.display = isGrid ? '' : 'none';
    updateMlParamSummary(mode);
}

function _mlSelectValue(id, fallback) {
    const el = document.getElementById(id);
    return el ? el.value : fallback;
}

// 1.0.10: SL/TP fib 層級只有 risk_basis="fib" 用得到,其他基準下顯示它們
// 會讓人以為改了有效 —— 實際上 atr_blend/daily 的 TP 是 rr_ratio 決定的。
function onBetafibBasisChange(mode) {
    // 1.0.10: 改讀合併後的 SL ANCHOR(原本的 betafib-basis-* 下拉已移除)
    const basis = _mlSelectValue('factor-sl-rule-' + mode, 'atr_blend');
    const row = document.getElementById('betafib-fiblevels-' + mode);
    if (row) row.style.display = (basis === 'fib') ? '' : 'none';
}

// BETAFIB entry window stores "start,end" in New York wall-clock hours.
// Empty means the full overnight window; midnight (0) remains a valid hour.
function _betafibWin(mode, idx) {
    const el = document.getElementById('betafib-window-' + mode);
    const raw = el ? String(el.value || '') : '';
    if (!raw) return null;
    const parts = raw.split(',');
    if (parts.length !== 2) return null;
    const n = parseInt(parts[idx], 10);
    return Number.isFinite(n) ? n : null;
}

function _fmtMlProb(v) {
    const n = parseFloat(v);
    if (!Number.isFinite(n) || n <= 0) return 'OFF';
    return n.toFixed(2);
}

function _fmtMlEv(v) {
    if (v === '' || v == null) return 'OFF';
    const n = parseFloat(v);
    if (!Number.isFinite(n)) return 'OFF';
    return '≥' + (Number.isInteger(n) ? String(n) : n.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
}

function _clampConfRr(value, fallback) {
    const n = parseFloat(value);
    const rr = Number.isFinite(n) ? n : (fallback != null ? fallback : 1.0);
    return Math.max(1, Math.min(6, Math.round(rr * 4) / 4));
}

function _fmtConfRr(value) {
    const rr = _clampConfRr(value, 1.0);
    return Number.isInteger(rr) ? String(rr) : rr.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

function _confRrSelectValue(value) {
    return _clampConfRr(value, 1.0).toFixed(2);
}

function normalizeAllowedSessions(value) {
    if (value == null) return null;
    const raw = Array.isArray(value)
        ? value
        : String(value).replace(/\|/g, ',').replace(/\+/g, ',').split(',');
    const order = ['ASIA', 'EURO', 'PRE', 'RTH', 'AH'];
    const set = new Set(raw.map(v => String(v || '').trim().toUpperCase()).filter(Boolean));
    if (!set.size || set.has('ALL') || set.has('*')) return null;
    const arr = order.filter(code => set.has(code));
    return arr.length ? arr : null;
}

function allowedSessionsLabel(value) {
    const arr = normalizeAllowedSessions(value);
    return arr ? arr.join('+') : 'ALL';
}

function allowedSessionsSelectValue(value) {
    const arr = normalizeAllowedSessions(value);
    return arr ? arr.join(',') : '';
}

function updateMlParamSummary(mode) {
    const el = document.getElementById('ml-param-summary-' + mode);
    if (!el) return;
    const rr = _clampConfRr(_mlSelectValue('conf-rr-' + mode, '1'), 1.0);
    const band = parseInt(_mlSelectValue('conf-band-' + mode, '4'), 10) || 4;
    const minTf = parseInt(_mlSelectValue('conf-mintf-' + mode, '2'), 10) || 2;
    const maxRisk = parseInt(_mlSelectValue('conf-maxrisk-' + mode, '0'), 10) || 0;
    const slRef = _mlSelectValue('conf-slref-' + mode, 'largest') === 'smallest' ? 'smallest' : 'largest';
    const trigger = parseFloat(_mlSelectValue('conf-trail-trigger-' + mode, '0')) || 0;
    const lockPct = parseFloat(_mlSelectValue('conf-trail-lock-' + mode, '0.05')) || 0.05;
    const sessionOn = _mlSelectValue('conf-session-limit-' + mode, '1') !== '0';
    const marketSession = allowedSessionsLabel(_mlSelectValue('conf-allowed-sessions-' + mode, 'ASIA'));
    const prob = _fmtMlProb(_mlSelectValue('conf-minprob-' + mode, '0.65'));
    const ev = _fmtMlEv(_mlSelectValue('conf-evfloor-' + mode, ''));
    const risk = maxRisk > 0 ? (maxRisk + 't') : 'OFF';
    const trail = trigger > 0
        ? ('trail ' + Math.round(trigger * 100) + '% → SL +' + Math.round(lockPct * 100) + '% TP')
        : 'trail OFF';
    el.innerHTML =
        'PARAMS: 1m base · wait 1m · B' + band + ' · ' + minTf + 'TF · RR1:' + _fmtConfRr(rr) +
        ' · minProb ' + prob + ' · EV ' + ev + ' · maxRisk ' + risk + ' · breakout off.<br>' +
        'RISK: SLref ' + slRef + ' · ' + trail + ' · session limit ' + (sessionOn ? 'ON' : 'OFF') +
        ' · market ' + marketSession + ' · size follows top selector.';
}

// 1.0.10: PI 的訊號只有 2026-06-11 之後(Discord 頻道全部歷史就這麼多),
// 用預設的 FULL_RANGE_START(2008)回測等於白掃 233 萬根、載入 10 秒起跳,
// 而 2026-06 之前一筆訊號都沒有。切到 PI 時自動把起始日縮到訊號範圍。
const PI_SIGNAL_FIRST_DATE = '2026-06-01';
const OPTION_WALL_SIGNAL_FIRST_DATE = '2025-12-01';

function _scopeDatesForStrategy(mode, strategy) {
    if (mode !== 'bt') return;
    const startEl = document.getElementById('start-date');
    if (!startEl) return;
    const firstDate = strategy === 'pi'
        ? PI_SIGNAL_FIRST_DATE
        : (strategy === 'optionwall' ? OPTION_WALL_SIGNAL_FIRST_DATE : '');
    const previousScope = startEl.dataset.signalScope || '';
    const previousFirstDate = previousScope === 'pi'
        ? PI_SIGNAL_FIRST_DATE
        : (previousScope === 'optionwall' ? OPTION_WALL_SIGNAL_FIRST_DATE : '');
    if (firstDate) {
        const switchingFromAutoScopedDate = previousFirstDate
            && previousScope !== strategy
            && startEl.value === previousFirstDate;
        if (startEl.value < firstDate || switchingFromAutoScopedDate) {
            if (!startEl.dataset.signalPrev) startEl.dataset.signalPrev = startEl.value;
            startEl.value = firstDate;
            log(strategyDisplayName(strategy) + ' signals begin on ' + firstDate
                + ' — start date shortened automatically to skip the empty range.', 'info');
        }
        startEl.dataset.signalScope = strategy;
    } else if (startEl.dataset.signalPrev) {
        startEl.value = startEl.dataset.signalPrev;
        delete startEl.dataset.signalPrev;
        delete startEl.dataset.signalScope;
    }
}

function onStrategyChange(mode) {
    const el = document.getElementById('strategy-' + mode);
    const strat = el ? el.value : DEFAULT_STRATEGY_PARAMS.strategy;
    const normalized = normalizeStrategyName(strat);
    const applied = _appliedStrategyParamsByMode[mode] || {};
    const setValue = (id, value) => {
        const control = document.getElementById(id);
        if (control) control.value = value;
    };
    // First manual selection starts from the researched candidate's PI-style
    // exit profile.  Do not overwrite a Delta configuration that was loaded
    // from a saved preset or already customized by the user.
    if (normalized === 'delta_absorption' && applied._deltaDefaultsApplied !== true) {
        setValue('factor-sl-value-' + mode, '4');
        setValue('pi-short-sl-' + mode, '1.5');
        setValue('rr-ratio-' + mode, '3');
        setValue('pi-long-hold-' + mode, '0');
        setValue('pi-short-hold-' + mode, '60');
        setValue('tr-exit-mode-' + mode, 'tp');
        setValue('trail-trigger-pct-' + mode, '0');
        applied._deltaDefaultsApplied = true;
        applied.factor_sl_value = 4;
        applied.rr_ratio = 3;
        applied.pi_short_sl_value = 1.5;
        applied.pi_long_hold_min = 0;
        applied.pi_short_hold_min = 60;
        applied.trail_trigger_pct = 0;
        applied.trail_enabled = false;
        onRrChange(mode);
        updateTrailBounds(mode);
    }
    _setStrategySelect(mode, normalized);
    updateStrategyParamVisibility(mode);
    _scopeDatesForStrategy(mode, normalized);
}

// Read the ML (confluence) parameter block for a panel into a params object,
// or null when the panel is not in ML mode. Base is always 1m (standardized).
function collectConfluenceParams(mode) {
    const stratEl = document.getElementById('strategy-' + mode);
    if (normalizeStrategyName(stratEl && stratEl.value) !== 'confluence') return null;
    const fv = (id, fb) => {
        const el = document.getElementById(id);
        const n = el ? parseFloat(el.value) : NaN;
        return Number.isNaN(n) ? fb : n;
    };
    const iv = (id, fb) => {
        const el = document.getElementById(id);
        const n = el ? parseInt(el.value, 10) : NaN;
        return Number.isNaN(n) ? fb : n;
    };
    // EV-priority gate floor: blank input => null (legacy win-prob gate); a
    // number (incl. 0) => admit setups with EV>=floor.
    const ovf = (id) => {
        const el = document.getElementById(id);
        if (!el || el.value === '' || el.value == null) return null;
        const n = parseFloat(el.value);
        return Number.isNaN(n) ? null : n;
    };
    // Variable-RR grid: "1.0,1.5,2.0" => [1,1.5,2]; blank => null (fixed RR).
    const rgv = (id) => {
        const el = document.getElementById(id);
        if (!el || !el.value) return null;
        const arr = el.value.split(',').map(s => parseFloat(s)).filter(n => !Number.isNaN(n) && n > 0);
        return arr.length ? arr : null;
    };
    // RR mode: "fixed" => single conf_rr; "grid" => variable RR (EV picks best
    // per signal). Only emit conf_rr_grid when the mode is "grid".
    const rrModeEl = document.getElementById('conf-rrmode-' + mode);
    const rrMode = rrModeEl ? rrModeEl.value : 'fixed';
    return {
        conf_band_ticks: fv('conf-band-' + mode, 4.0),
        conf_min_distinct_tf: iv('conf-mintf-' + mode, 2),
        conf_rr: _clampConfRr(fv('conf-rr-' + mode, 1.0), 1.0),
        conf_wait_minutes: 1,
        conf_base_minutes: 1,
        conf_min_prob: fv('conf-minprob-' + mode, 0.65),
        conf_ev_floor: ovf('conf-evfloor-' + mode),
        conf_rr_grid: null,
        conf_use_scorer: true,
        conf_enable_breakout: (function () {
            // BREAKOUT control removed from the UI (prior research showed it is redundant at
            // the optimal RR). Default OFF → momentum+reversion only.
            const el = document.getElementById('conf-breakout-' + mode);
            return el ? el.value === '1' : false;
        })(),
        conf_max_risk_ticks: iv('conf-maxrisk-' + mode, 0) || null,
        conf_sl_reference_tf: (_mlSelectValue('conf-slref-' + mode, 'largest') === 'smallest') ? 'smallest' : 'largest',
        conf_allowed_sessions: normalizeAllowedSessions(
            _mlSelectValue('conf-allowed-sessions-' + mode, 'ASIA')
        ),
        // STYLE: optional exit-policy (break-even / trail / lock). All-OFF == original.
        conf_trail_trigger_pct: fv('conf-trail-trigger-' + mode, 0.50),
        conf_trail_lock_pct: fv('conf-trail-lock-' + mode, 0.05),
        conf_full_tp_lock: iv('conf-fulltplock-' + mode, 0),
        conf_session_limit: (function () {
            const el = document.getElementById('conf-session-limit-' + mode);
            return el ? el.value === '1' : true;
        })(),
    };
}


function collectStrategyParams(mode) {
    const applied = Object.assign(
        {},
        DEFAULT_STRATEGY_PARAMS,
        _appliedStrategyParamsByMode[mode] || {}
    );
    const _int = (id, fallback) => {
        const el = document.getElementById(id);
        if (!el) return fallback || 0;
        const n = parseInt(el.value, 10);
        return Number.isNaN(n) ? (fallback || 0) : n;
    };
    const _float = (id, fallback) => {
        const el = document.getElementById(id);
        if (!el) return fallback;
        const n = parseFloat(el.value);
        return Number.isNaN(n) ? fallback : n;
    };
    const _val = (id) => { const el = document.getElementById(id); return el ? el.value : ''; };
    const _paramVal = (idBase, key, fallback) => {
        const el = document.getElementById(idBase + '-' + mode);
        if (el) return el.value;
        return applied[key] != null ? applied[key] : fallback;
    };
    const _paramNum = (idBase, key, fallback) => {
        const n = Number(_paramVal(idBase, key, fallback));
        return Number.isFinite(n) ? n : fallback;
    };
    const _paramInt = (idBase, key, fallback) => {
        const n = parseInt(_paramVal(idBase, key, fallback), 10);
        return Number.isNaN(n) ? fallback : n;
    };
    const _allowedSessionsFromSelect = (idBase, key, fallback) => {
        const el = document.getElementById(idBase + '-' + mode);
        if (el) return normalizeAllowedSessions(el.value);
        const source = Object.prototype.hasOwnProperty.call(applied, key) ? applied[key] : fallback;
        return normalizeAllowedSessions(source);
    };
    const cidEl = document.getElementById('contract-' + mode);
    const sizeEl = document.getElementById('size-' + mode);
    const contractId = (cidEl && cidEl.value) || DEFAULT_STRATEGY_PARAMS.contract_id;
    const uiStrategy = normalizeStrategyName(_val('strategy-' + mode));
    const strategy = normalizeStrategyName(applied.strategy || uiStrategy);
    if (strategy !== uiStrategy) _setStrategySelect(mode, strategy);
    const clampTicks = (v, fallback) => Math.max(50, Math.min(200, v || fallback));

    const readLeg = (kind) => {
        const prefix = strategyIdPrefix(kind);
        const d = DEFAULT_STRATEGY_PARAMS;
        const slTicks = clampTicks(_int(prefix + 'sl-ticks-' + mode, d[kind + '_sl_ticks'] || d.sl_ticks), d[kind + '_sl_ticks'] || d.sl_ticks);
        const tpTicks = clampTicks(_int(prefix + 'tp-ticks-' + mode, d[kind + '_tp_ticks'] || d.tp_ticks), d[kind + '_tp_ticks'] || d.tp_ticks);
        const triggerPct = _float(prefix + 'trail-trigger-pct-' + mode, d[kind + '_trail_trigger_pct'] || d.trail_trigger_pct);
        const trailPct = _float(prefix + 'trail-sl-pct-' + mode, d[kind + '_trail_sl_pct'] || d.trail_sl_pct);
        const trailTicks = trailTicksFromPct(trailPct, slTicks, tpTicks, triggerPct);
        const trailTicksEl = document.getElementById(prefix + 'trail-sl-ticks-' + mode);
        if (trailTicksEl) trailTicksEl.value = String(trailTicks);
        return {
            tp_ticks: tpTicks,
            sl_ticks: slTicks,
            trail_sl_ticks: trailTicks,
            trail_sl_pct: trailPct,
            trail_trigger_pct: triggerPct,
            trail_enabled: triggerPct > 0,
            full_tp_lock: _int(prefix + 'full-tp-lock-' + mode, 0),
        };
    };

    const tr = readLeg('tr');
    const primary = tr;
    // Timeframe selection alone decides single vs overlap:
    //   1 TF  → single (tf_combo empty, area_timeframe = that TF)
    //   2+ TF → overlap (tf_combo = selected TFs, area_timeframe = first/smallest)
    const selTfs = readOverlapTfCombo(mode);
    const tfs = selTfs.length ? selTfs : ['15m'];
    const method = tfs.length >= 2 ? 'overlap' : 'single';
    const tfCombo = method === 'overlap' ? tfs : [];
    const factorSessionVaFilter = String(_paramVal('factor-va-filter', 'factor_session_va_filter', 'off')) === 'outside' ? 'outside' : 'off';
    const areaTimeframe = (strategy === 'factor' && factorSessionVaFilter === 'outside') ? 'session' : tfs[0];
    const overlapTradeEl = document.getElementById('tr-overlap-trade-tf-' + mode);
    const overlapTradeTf = normalizeTrendOverlapTradeTf(
        (overlapTradeEl && overlapTradeEl.value) || applied.tr_overlap_trade_tf
    );
    const _factorFamily = (v) => {
        const s = String(v || 'emapmo').toLowerCase();
        return ['emapmo', 'momentum_reversion', 'icefishball'].includes(s) ? s : 'emapmo';
    };
    const _factorSide = (v) => {
        const s = String(v || 'long_only').toLowerCase();
        return ['all', 'long_only', 'short_only'].includes(s) ? s : 'long_only';
    };
    const _factorRule = (v, fallback) => {
        const s = String(v || fallback || 'atr').toLowerCase();
        return ['atr', 'atr_blend', 'range15_pct'].includes(s) ? s : fallback;
    };
    const rrRaw = Math.max(0.1, Math.min(6, parseFloat(_mlSelectValue('rr-ratio-' + mode, '2')) || 2));
    const rrInt = Math.max(1, Math.min(6, parseInt(_mlSelectValue('rr-ratio-' + mode, '2'), 10) || 2));
    // 1.0.10: SL ANCHOR 是**單一**下拉,依策略寫進不同欄位:
    //   BETAFIB → betafib_risk_basis (atr_blend / daily / fib)
    //   其他    → factor_sl_rule     (atr / atr_blend / range15_pct)
    // 兩個白名單各自過濾,所以選了 daily/fib 時 factor_sl_rule 會安全退回
    // atr_blend,選了 atr/range15_pct 時 betafib_risk_basis 退回 atr_blend。
    // 這樣同一份 preset 在切換策略時不會帶著對方看不懂的值。
    const slAnchorRaw = String(_paramVal('factor-sl-rule', 'factor_sl_rule', 'atr_blend') || '').toLowerCase();
    const betafibBasis = ['atr_blend', 'daily', 'fib'].includes(slAnchorRaw) ? slAnchorRaw : 'atr_blend';
    const factorSlRule = _factorRule(slAnchorRaw, 'atr_blend');
    const factorSlValue = _paramNum('factor-sl-value', 'factor_sl_value', 2.5) || 2.5;
    // 1.0.9: HOLD 5m-candle system removed — FACTOR exits are SL/TP only, always,
    // for every current and future preset. Pinned 0 regardless of any stored value.
    const factorHoldBars = 0;
    const dailyMaxTrades = Math.max(0, _paramInt('factor-max-trades', 'factor_max_trades_per_day', 3));
    const piMatrix = strategy === 'pi'
        ? _piMatrixPayload(mode)
        : { pi_long_kinds: null, pi_short_kinds: null, pi_short_levels: null };
    const _deltaChoice = (idBase, key, fallback, allowed) => {
        const raw = String(_paramVal(idBase, key, fallback) || '').toLowerCase();
        return allowed.includes(raw) ? raw : fallback;
    };
    const deltaPattern = _deltaChoice(
        'delta-pattern', 'delta_pattern', 'absorption', ['absorption', 'exhaustion', 'both']);
    const deltaSide = _deltaChoice(
        'delta-side', 'delta_side_mode', 'all', ['all', 'long_only', 'short_only']);
    const deltaSource = _deltaChoice(
        'delta-source', 'delta_source', 'whole', ['whole', 'outside']);
    const deltaGate = _deltaChoice(
        'delta-gate', 'delta_gate', 'location', ['raw', 'location', 'reclaim', 'reclaim_vwap']);
    const params = {
        market_clock_version: MARKET_CLOCK_VERSION,
        strategy: strategy,
        method: method,
        tf_combo: tfCombo,
        tp_ticks: primary.tp_ticks,
        sl_ticks: primary.sl_ticks,
        trail_sl_ticks: primary.trail_sl_ticks,
        trail_sl_pct: primary.trail_sl_pct,
        trail_trigger_pct: primary.trail_trigger_pct,
        trail_enabled: primary.trail_enabled,
        tr_tp_ticks: tr.tp_ticks,
        tr_sl_ticks: tr.sl_ticks,
        tr_trail_sl_ticks: tr.trail_sl_ticks,
        tr_trail_sl_pct: tr.trail_sl_pct,
        tr_trail_trigger_pct: tr.trail_trigger_pct,
        tr_trail_enabled: tr.trail_enabled,
        tr_full_tp_lock: tr.full_tp_lock,
        candle_seconds: 60,   // 1m bars platform-wide
        contract_id: contractId,
        contract_size: normalizeContractSize(contractId, sizeEl ? sizeEl.value : 3),
        value_area_pct: _float('area-pct-' + mode, 0.80),
        area_timeframe: areaTimeframe,
        tr_overlap_trade_tf: overlapTradeTf,
        rr_ratio: rrInt,
        full_tp_lock: primary.full_tp_lock,
        one_trade_per_session_direction: true,
        tr_one_trade_per_session: _int('tr-session-limit-' + mode, 1) === 1,
        tr_allowed_sessions: _allowedSessionsFromSelect('tr-allowed-sessions', 'tr_allowed_sessions', ['ASIA']),
        skip_zone_stability: false,
        breakout_confirm_bars: Math.max(1, Math.min(10, _int('confirm-bars-' + mode, 7))),
        // 1.0.8: 出場模式(tp | ladder)+ 日虧斷路器(0=OFF)
        tr_exit_mode: (_mlSelectValue('tr-exit-mode-' + mode, 'tp') === 'ladder') ? 'ladder' : 'tp',
        tr_daily_loss_stop: Math.max(0, Math.min(6, _int('tr-daily-stop-' + mode, 0))),
        tr_daily_win_stop: Math.max(0, Math.min(6, _int('tr-daily-win-stop-' + mode, 0))),  // 1.0.9: FULL WIN LOCK
        // 1.0.9: prevRV 波動閘 + fade 進場模式
        fade_entry_mode: (function (m) { return (m === 'rejection' || m === 'or15') ? m : 'limit'; })(_mlSelectValue('fade-entry-mode-' + mode, 'limit')),  // 1.0.9: +or15
        sigma_window_minutes: parseInt(applied.sigma_window_minutes != null ? applied.sigma_window_minutes : 15, 10) || 15,
        sigma_method: String(applied.sigma_method || 'std') === 'mad' ? 'mad' : 'std',
        sigma_entry_mode: String(applied.sigma_entry_mode || 'blind') === 'reject' ? 'reject' : 'blind',
        sigma_accept_mode: ['none', 'filter', 'switch'].includes(String(applied.sigma_accept_mode || 'none')) ? String(applied.sigma_accept_mode || 'none') : 'none',
        sigma_start: Number(applied.sigma_start != null ? applied.sigma_start : 1.0) || 1.0,
        sigma_max: Number(applied.sigma_max != null ? applied.sigma_max : 3.0) || 3.0,
        sigma_target_mode: ['inner1', 'half', 'center'].includes(String(applied.sigma_target_mode || 'half')) ? String(applied.sigma_target_mode || 'half') : 'half',
        sigma_stop_span: Number(applied.sigma_stop_span != null ? applied.sigma_stop_span : 1.0) || 1.0,
        sigma_accept_sigma: Number(applied.sigma_accept_sigma != null ? applied.sigma_accept_sigma : 2.0) || 2.0,
        sigma_accept_bars: parseInt(applied.sigma_accept_bars != null ? applied.sigma_accept_bars : 2, 10) || 2,
        factor_timeframe_minutes: parseInt(applied.factor_timeframe_minutes != null ? applied.factor_timeframe_minutes : 5, 10) || 5,
        factor_signal_family: _factorFamily(_paramVal('factor-family', 'factor_signal_family', 'emapmo')),
        factor_side_mode: _factorSide(_paramVal('factor-side', 'factor_side_mode', 'long_only')),
        factor_pmo_signal_mode: ['normal', 'early', 'both'].includes(String(_paramVal('factor-pmo-mode', 'factor_pmo_signal_mode', 'early'))) ? String(_paramVal('factor-pmo-mode', 'factor_pmo_signal_mode', 'early')) : 'early',
        factor_session_va_filter: factorSessionVaFilter,
        // 1.0.9: EMAPMO 進場門檻滑桿 → early(SIG)門檻的縮放係數。
        // 1.0 = 原始 -0.100;非 EMAPMO 家族送 1.0(引擎端等同不套用)。
        factor_pmo_early_scale: _emapmoThresholdScale(mode),
        // 1.0.9: INTRAMOM 專屬(觀察窗 / 進場時);其餘沿用 factor_* 與 rr_ratio
        momentum_first_minutes: _int('momentum-first-' + mode, 30) || 30,
        momentum_entry_hour: _int('momentum-hour-' + mode, 18) || 18,
        // 1.0.9: SESSFIB 專屬。fib 級別可調 —— 0.618 是 576 變體掃描中
        // 唯一通過 G0–G4 的進場位;0.786 太淺(94% 夜盤都會觸及)。
        betafib_entry_fib: _float('betafib-entry-' + mode, 0.618),
        betafib_anchor: _mlSelectValue('betafib-anchor-' + mode, 'hl'),
        betafib_risk_basis: betafibBasis,   // 1.0.10: 來自合併後的 SL ANCHOR
        betafib_min_move_pct: _float('betafib-minpct-' + mode, 0),
        // 1.0.10: PI 外部訊號
        pi_signal_set: _mlSelectValue('pi-signal-set-' + mode, 'long_pi_only'),
        pi_long_only: _mlSelectValue('pi-long-only-' + mode, '1') === '1',
        pi_long_kinds: piMatrix.pi_long_kinds,
        pi_short_kinds: piMatrix.pi_short_kinds,
        pi_short_levels: piMatrix.pi_short_levels,
        pi_max_signal_age_min: _int('pi-max-age-' + mode, 5),
        pi_short_sl_value: _float('pi-short-sl-' + mode, 2.5),
        pi_long_hold_min: _int('pi-long-hold-' + mode, 0),
        pi_short_hold_min: _int('pi-short-hold-' + mode, 60),
        delta_pattern: deltaPattern,
        delta_side_mode: deltaSide,
        delta_source: deltaSource,
        delta_gate: deltaGate,
        delta_window: Math.max(1, Math.min(30, _paramInt('delta-window', 'delta_window', 5))),
        delta_baseline_window: Math.max(2, Math.min(240, _paramInt('delta-baseline', 'delta_baseline_window', 30))),
        delta_strength_multiplier: Math.max(0.1, Math.min(10, _paramNum('delta-strength', 'delta_strength_multiplier', 1.0))),
        delta_weakening_ratio: Math.max(0.1, Math.min(1, _paramNum('delta-weakening', 'delta_weakening_ratio', 0.70))),
        delta_stall_ticks: Math.max(0, Math.min(20, _paramInt('delta-stall', 'delta_stall_ticks', 1))),
        delta_value_lookback: Math.max(2, Math.min(120, _paramInt('delta-lookback', 'delta_value_lookback', 10))),
        delta_value_touch_ticks: Math.max(0, Math.min(20, _paramInt('delta-touch', 'delta_value_touch_ticks', 0))),
        delta_require_profile: _paramVal('delta-require-profile', 'delta_require_profile', '1') === '1',
        option_wall_submodel: _mlSelectValue('option-wall-submodel-' + mode, 'primary_strict'),
        option_wall_side_mode: _mlSelectValue('option-wall-side-' + mode, 'all'),
        option_wall_long_sl_atr: _float('option-wall-long-sl-' + mode, 4.0),
        option_wall_short_sl_atr: _float('option-wall-short-sl-' + mode, 1.5),
        option_wall_max_hold_min: _int('option-wall-hold-' + mode, 60),
        option_wall_max_trades_per_day: _int('option-wall-max-trades-' + mode, 3),
        // 1.0.10: 腿幅上限 + 進場時窗 + fib 基準的 SL/TP 層級。
        // The single select stores New York-local "start,end" hours.
        betafib_max_move_pct: _float('betafib-maxpct-' + mode, 0),
        betafib_entry_start_hour: _betafibWin(mode, 0),
        betafib_entry_end_hour: _betafibWin(mode, 1),
        betafib_sl_fib: _float('betafib-slfib-' + mode, 0.75),
        betafib_tp_fib: _float('betafib-tpfib-' + mode, 0.90),
        // 1.0.9: 單筆風險/獲利寬度上限(ticks/口);0 → null = 不限
        // 1.0.9: 讀 hidden(價距 ticks)—— 唯一真相,滑桿與文字都從它衍生
        max_profit_ticks: capTicks(mode) || null,
        factor_sl_rule: factorSlRule,
        factor_tp_rule: factorSlRule,
        factor_sl_value: factorSlValue,
        factor_tp_value: Number((factorSlValue * rrRaw).toFixed(6)),
        factor_max_hold_bars: factorHoldBars,
        factor_max_trades_per_day: dailyMaxTrades,
        factor_warmup_bars: parseInt(applied.factor_warmup_bars != null ? applied.factor_warmup_bars : 150, 10) || 150,
    };
    // 1.0.8: 移除 ml_consolidation_v2 (mlc2) 參數區塊
    return params;
}

function applyStrategyParams(mode, params) {
    const p = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params);
    const _set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
    const _setChoice = (id, val, label) => {
        const el = document.getElementById(id);
        if (!el) return;
        const s = String(val);
        if (!el.options) {
            el.value = s;
            return;
        }
        if (!Array.from(el.options).some(o => o.value === s)) {
            const opt = document.createElement('option');
            opt.value = s;
            opt.textContent = label || s;
            el.appendChild(opt);
        }
        el.value = s;
    };
    const _setVal = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    const _ticks = (val, fallback) => Math.max(50, Math.min(200, parseInt(val != null ? val : fallback, 10) || fallback));
    p.strategy = normalizeStrategyName(p.strategy);
    _appliedStrategyParamsByMode[mode] = Object.assign({}, p);
    // A saved Delta preset already owns its exit values.  Keep the manual
    // candidate initializer from replacing a customized preset when the user
    // switches away and back to this model.
    if (p.strategy === 'delta_absorption') {
        _appliedStrategyParamsByMode[mode]._deltaDefaultsApplied = true;
    }
    _setStrategySelect(mode, p.strategy);
    _set('area-pct-' + mode, (p.value_area_pct != null ? Number(p.value_area_pct) : 0.80).toFixed(2));
    _set('tr-overlap-trade-tf-' + mode, normalizeTrendOverlapTradeTf(p.tr_overlap_trade_tf));
    const cidEl = document.getElementById('contract-' + mode);
    if (cidEl) {
        // Older saved presets contain a resolved expiry such as U26. It is
        // historical input, not a selectable UI contract; show its root and
        // let the backend resolve the current front month on execution.
        const wanted = contractUiValue(p.contract_id || DEFAULT_STRATEGY_PARAMS.contract_id);
        if (!Array.from(cidEl.options).some(o => o.value === wanted)) {
            const opt = document.createElement('option');
            opt.value = wanted; opt.textContent = wanted;
            cidEl.appendChild(opt);
        }
        cidEl.value = wanted;
    }
    syncSizeOptions(mode, p.contract_size != null ? p.contract_size : DEFAULT_STRATEGY_PARAMS.contract_size);

    const writeLeg = (kind) => {
        const prefix = strategyIdPrefix(kind);
        const d = DEFAULT_STRATEGY_PARAMS;
        const tpTicks = _ticks(p[kind + '_tp_ticks'] != null ? p[kind + '_tp_ticks'] : p.tp_ticks, d[kind + '_tp_ticks'] || d.tp_ticks);
        const slTicks = _ticks(p[kind + '_sl_ticks'] != null ? p[kind + '_sl_ticks'] : p.sl_ticks, d[kind + '_sl_ticks'] || d.sl_ticks);
        _set(prefix + 'tp-ticks-' + mode, tpTicks);
        _setVal(prefix + 'tp-ticks-' + mode + '-val', tpTicks);
        _set(prefix + 'sl-ticks-' + mode, slTicks);
        _setVal(prefix + 'sl-ticks-' + mode + '-val', slTicks);

        const enabledKey = kind + '_trail_enabled';
        const triggerKey = kind + '_trail_trigger_pct';
        const triggerPct = p[enabledKey] === false
            ? 0
            : (p[triggerKey] != null
                ? parseFloat(p[triggerKey])
                : (p.trail_trigger_pct != null ? parseFloat(p.trail_trigger_pct) : (d[triggerKey] || d.trail_trigger_pct)));
        const trailPctKey = kind + '_trail_sl_pct';
        const trailTicksKey = kind + '_trail_sl_ticks';
        const trailPct = p[trailPctKey] != null
            ? parseFloat(p[trailPctKey])
            : (p.trail_sl_pct != null
                ? parseFloat(p.trail_sl_pct)
                : trailPctFromTicks(
                    p[trailTicksKey] != null ? p[trailTicksKey] : (p.trail_sl_ticks != null ? p.trail_sl_ticks : d[trailTicksKey]),
                    slTicks,
                    tpTicks
                ));
        _set(prefix + 'trail-trigger-pct-' + mode, triggerPct === 0 ? '0' : triggerPct.toFixed(2));
        updateTrailBounds(mode, trailPct);
        _set(prefix + 'full-tp-lock-' + mode, String(p[kind + '_full_tp_lock'] != null ? p[kind + '_full_tp_lock'] : (p.full_tp_lock || 0)));
        const sessionKey = kind + '_one_trade_per_session';
        _set(kind + '-session-limit-' + mode, (p[sessionKey] != null ? p[sessionKey] : true) ? '1' : '0');
    };

    writeLeg('tr');

    let rrVal = Math.max(1, Math.min(6, parseFloat(p.rr_ratio != null ? p.rr_ratio : 2) || 2));
    if (p.strategy === 'factor') {
        const slRule = String(p.factor_sl_rule || '').toLowerCase();
        const tpRule = String(p.factor_tp_rule || '').toLowerCase();
        const slValue = Number(p.factor_sl_value);
        const tpValue = Number(p.factor_tp_value);
        if (slRule && tpRule && slRule === tpRule && Number.isFinite(slValue) && slValue > 0 && Number.isFinite(tpValue) && tpValue > 0) {
            rrVal = Math.max(0.1, Math.min(6, tpValue / slValue));
        }
    }
    const rrText = (Number.isInteger(rrVal) ? String(rrVal) : rrVal.toFixed(2).replace(/0+$/, '').replace(/\.$/, ''));
    _setChoice('rr-ratio-' + mode, rrText, rrText + ' x SL');
    onRrChange(mode);

    _set('confirm-bars-' + mode, String(p.breakout_confirm_bars != null ? p.breakout_confirm_bars : 7));

    // 1.0.8: 出場模式 + 日虧斷路器
    _set('tr-exit-mode-' + mode, String(p.tr_exit_mode || 'tp') === 'ladder' ? 'ladder' : 'tp');
    _set('tr-daily-stop-' + mode, String(Math.max(0, Math.min(6, parseInt(p.tr_daily_loss_stop != null ? p.tr_daily_loss_stop : 0, 10) || 0))));
    _set('tr-daily-win-stop-' + mode, String(Math.max(0, Math.min(6, parseInt(p.tr_daily_win_stop != null ? p.tr_daily_win_stop : 0, 10) || 0))));  // 1.0.9: FULL WIN LOCK
    // 1.0.9: prevRV 波動閘 + fade 進場模式
    _set('fade-entry-mode-' + mode, (function (m) { return (m === 'rejection' || m === 'or15') ? m : 'limit'; })(String(p.fade_entry_mode || 'limit')));  // 1.0.9: +or15

    // FACTOR params restored when the preset uses the factor strategy.
    const _factorFamilyValue = (v) => {
        const s = String(v || 'emapmo').toLowerCase();
        return ['emapmo', 'icefishball', 'momentum_reversion'].includes(s) ? s : 'emapmo';
    };
    const _factorSideValue = (v) => {
        const s = String(v || 'long_only').toLowerCase();
        return ['all', 'long_only', 'short_only'].includes(s) ? s : 'long_only';
    };
    const _factorPmoValue = (v) => {
        const s = String(v || 'early').toLowerCase();
        return ['normal', 'early', 'both'].includes(s) ? s : 'early';
    };
    const _factorRuleValue = (v, fallback) => {
        const s = String(v || fallback).toLowerCase();
        return ['atr', 'atr_blend', 'range15_pct'].includes(s) ? s : fallback;
    };
    _setChoice('factor-family-' + mode, _factorFamilyValue(p.factor_signal_family));
    _setChoice('factor-side-' + mode, _factorSideValue(p.factor_side_mode));
    _setChoice('factor-pmo-mode-' + mode, _factorPmoValue(p.factor_pmo_signal_mode));
    _setChoice('factor-va-filter-' + mode, String(p.factor_session_va_filter || 'off') === 'outside' ? 'outside' : 'off');
    _setEmapmoThreshold(mode, p.factor_pmo_early_scale);
    _set('momentum-first-' + mode, String(p.momentum_first_minutes != null ? p.momentum_first_minutes : 30));
    _set('momentum-hour-' + mode, String(p.momentum_entry_hour != null ? p.momentum_entry_hour : 18));
    _set('betafib-entry-' + mode, String(p.betafib_entry_fib != null ? p.betafib_entry_fib : 0.618));
    _set('betafib-anchor-' + mode, String(p.betafib_anchor || 'hl'));
    // 1.0.10: BETAFIB preset 的 SL 錨點存在 betafib_risk_basis,要回填到合併後的
    // SL ANCHOR;非 BETAFIB 的 preset 則由下方的 factor-sl-rule 回填,不能互相蓋掉。
    if (String(p.strategy || '').toLowerCase() === 'betafib') {
        _setChoice('factor-sl-rule-' + mode, String(p.betafib_risk_basis || 'atr_blend'));
    }
    _set('betafib-minpct-' + mode, String(p.betafib_min_move_pct != null ? p.betafib_min_move_pct : 0));
    // 1.0.10: PI
    _set('pi-signal-set-' + mode, String(p.pi_signal_set || 'long_pi_only'));
    // 1.0.10: 預設只做多 —— 舊 preset 沒有這個欄位時要落在 '1',不是 '0'
    _set('pi-long-only-' + mode, (p.pi_long_only === undefined ? true : p.pi_long_only) ? '1' : '0');
    // Render the new matrix from either explicit kind arrays or the legacy
    // signal-set fields.  Old presets therefore keep their exact behavior.
    if (Array.isArray(p.pi_long_kinds) || Array.isArray(p.pi_short_kinds) ||
            Array.isArray(p.pi_short_levels)) {
        const state = _piMatrixStateEmpty();
        const longKinds = new Set(Array.isArray(p.pi_long_kinds) ? p.pi_long_kinds : []);
        const shortKinds = new Set(Array.isArray(p.pi_short_kinds) ? p.pi_short_kinds : []);
        const explicitShortLevels = Array.isArray(p.pi_short_levels);
        const shortLevels = new Set(explicitShortLevels
            ? p.pi_short_levels.map(Number).filter((level) => level === 1 || level === 2)
            : []);
        ['long', 'short'].forEach((side) => {
            ['pi', 'level2', 'level1'].forEach((level) => {
                const kind = PI_MATRIX_KIND_BY_SIDE_LEVEL[side][level];
                if (side === 'short' && level !== 'pi') {
                    // New payloads select short circles by the source's
                    // numeric Level N.  Legacy payloads only had 紫圈, so
                    // keep both circle rows selected when levels are absent.
                    state[side][level] = explicitShortLevels
                        ? shortLevels.has(PI_MATRIX_LEVEL_BY_SIDE_LEVEL[side][level])
                        : Boolean(kind && shortKinds.has(kind));
                } else {
                    state[side][level] = Boolean(kind && (side === 'long' ? longKinds : shortKinds).has(kind));
                }
            });
        });
        if (p.pi_long_only) state.short = { pi: false, level2: false, level1: false };
        _piMatrixWriteState(mode, state);
    } else {
        _piMatrixSyncFromLegacy(mode);
    }
    _set('pi-max-age-' + mode, String(p.pi_max_signal_age_min != null ? p.pi_max_signal_age_min : 5));
    const piShortSl = String(p.pi_short_sl_value != null ? p.pi_short_sl_value : 2.5);
    _setChoice('pi-short-sl-' + mode, piShortSl, piShortSl + ' x ATR');
    _setChoice('pi-long-hold-' + mode, String(p.pi_long_hold_min != null ? p.pi_long_hold_min : 0));
    _setChoice('pi-short-hold-' + mode, String(p.pi_short_hold_min != null ? p.pi_short_hold_min : 60));
    _setChoice('delta-pattern-' + mode, String(p.delta_pattern || 'absorption'));
    _setChoice('delta-side-' + mode, String(p.delta_side_mode || 'all'));
    _setChoice('delta-source-' + mode, String(p.delta_source || 'whole'));
    _setChoice('delta-gate-' + mode, String(p.delta_gate || 'location'));
    _setChoice('delta-window-' + mode, String(p.delta_window != null ? p.delta_window : 5));
    _setChoice('delta-baseline-' + mode, String(p.delta_baseline_window != null ? p.delta_baseline_window : 30));
    _setChoice('delta-strength-' + mode, String(p.delta_strength_multiplier != null ? p.delta_strength_multiplier : 1));
    _setChoice('delta-weakening-' + mode, String(p.delta_weakening_ratio != null ? p.delta_weakening_ratio : 0.70));
    _setChoice('delta-stall-' + mode, String(p.delta_stall_ticks != null ? p.delta_stall_ticks : 1));
    _setChoice('delta-lookback-' + mode, String(p.delta_value_lookback != null ? p.delta_value_lookback : 10));
    _setChoice('delta-touch-' + mode, String(p.delta_value_touch_ticks != null ? p.delta_value_touch_ticks : 0));
    _setChoice('delta-require-profile-' + mode, p.delta_require_profile === false ? '0' : '1');
    _setChoice('option-wall-submodel-' + mode, String(p.option_wall_submodel || 'primary_strict'));
    _setChoice('option-wall-side-' + mode, String(p.option_wall_side_mode || 'all'));
    _setChoice('option-wall-long-sl-' + mode, String(p.option_wall_long_sl_atr != null ? p.option_wall_long_sl_atr : 4.0));
    _setChoice('option-wall-short-sl-' + mode, String(p.option_wall_short_sl_atr != null ? p.option_wall_short_sl_atr : 1.5));
    _setChoice('option-wall-hold-' + mode, String(p.option_wall_max_hold_min != null ? p.option_wall_max_hold_min : 60));
    _setChoice('option-wall-max-trades-' + mode, String(p.option_wall_max_trades_per_day != null ? p.option_wall_max_trades_per_day : 3));
    // 1.0.10
    _set('betafib-maxpct-' + mode, String(p.betafib_max_move_pct != null ? p.betafib_max_move_pct : 0));
    _set('betafib-window-' + mode,
        (p.betafib_entry_start_hour == null || p.betafib_entry_end_hour == null)
            ? '' : (p.betafib_entry_start_hour + ',' + p.betafib_entry_end_hour));
    _set('betafib-slfib-' + mode, String(p.betafib_sl_fib != null ? p.betafib_sl_fib : 0.75));
    _set('betafib-tpfib-' + mode, String(p.betafib_tp_fib != null ? p.betafib_tp_fib : 0.90));
    onBetafibBasisChange(mode);   // 套用 preset 後同步 fib 層級列的顯示
    _scopeDatesForStrategy(mode, normalizeStrategyName(p.strategy));
    // 1.0.9: preset 存的是價距 ticks —— 直接寫進唯一真相,
    // setCapTicks 會把滑桿位置與文字一起重畫,兩者不可能不同步。
    setCapTicks(mode, parseInt(p.max_profit_ticks != null ? p.max_profit_ticks : 0, 10) || 0);
    updateRiskCapHint(mode);
    // 1.0.10: BETAFIB 的 SL 錨點在上面已由 betafib_risk_basis 回填,這裡不能再蓋回去。
    if (String(p.strategy || '').toLowerCase() !== 'betafib') {
        _setChoice('factor-sl-rule-' + mode, _factorRuleValue(p.factor_sl_rule, 'atr_blend'));
    }
    _setChoice('factor-sl-value-' + mode, String(p.factor_sl_value != null ? p.factor_sl_value : 2.5));
    syncFactorRiskControls(mode);
    _setChoice('factor-hold-' + mode, '0');   // 1.0.9: HOLD 5m system removed → always OFF (SL/TP-only)
    _setChoice('factor-max-trades-' + mode, String(p.factor_max_trades_per_day != null ? p.factor_max_trades_per_day : (p.pmo_max_trades_per_day != null ? p.pmo_max_trades_per_day : 3)));

    // ML confluence params restored when the preset uses the ML strategy.
    const _prob = (v) => {
        const n = Number(v);
        if (!Number.isFinite(n) || n <= 0) return '0';
        return n.toFixed(2);
    };
    _set('conf-minprob-' + mode, _prob(p.conf_min_prob != null ? p.conf_min_prob : 0.65));
    _set('conf-rr-' + mode, _confRrSelectValue(p.conf_rr != null ? Number(p.conf_rr) : 1.0));
    _set('conf-band-' + mode, String(parseInt(p.conf_band_ticks != null ? p.conf_band_ticks : 4, 10)));
    _set('conf-mintf-' + mode, String(p.conf_min_distinct_tf != null ? p.conf_min_distinct_tf : 2));
    _set('conf-evfloor-' + mode, (p.conf_ev_floor == null ? '' : String(p.conf_ev_floor)));
    // RR mode: a saved grid => variable mode; otherwise fixed. Format the grid
    // with one decimal so it matches the <option> values (e.g. "1.0,1.5,...").
    _set('conf-rrmode-' + mode, 'fixed');
    _set('conf-breakout-' + mode, (p.conf_enable_breakout === false) ? '0' : '1');
    _set('conf-maxrisk-' + mode, String(p.conf_max_risk_ticks != null ? p.conf_max_risk_ticks : 0));
    _set('conf-slref-' + mode, p.conf_sl_reference_tf === 'smallest' ? 'smallest' : 'largest');
    // STYLE: optional exit-policy (break-even / trail / lock). All-OFF == original.
    // OFF option value is "0" (not "0.00"); only non-zero needs the 2-decimal form.
    const _pct = (v) => { const n = Number(v) || 0; return n === 0 ? '0' : n.toFixed(2); };
    _set('conf-trail-trigger-' + mode, _pct(p.conf_trail_trigger_pct));
    _syncTrailTriggerBtn(mode);
    _set('conf-trail-lock-' + mode, _pct(p.conf_trail_lock_pct));
    _set('conf-fulltplock-' + mode, String(parseInt(p.conf_full_tp_lock != null ? p.conf_full_tp_lock : 0, 10)));
    _set('conf-session-limit-' + mode, (p.conf_session_limit === false) ? '0' : '1');
    // 1.0.9 FIX: a preset that stores `null` allowed-sessions means ALL sessions
    // (dropdown value ""). The old `!= null ? : ['ASIA']` collapsed that ALL back
    // to ASIA-only on reload, so a saved "ALL sessions" preset silently traded
    // only ASIA → different backtest than the in-memory params it was saved from.
    // Pass the value straight through (missing field still defaults to ASIA via
    // DEFAULT_STRATEGY_PARAMS merged into `p`).
    _set('conf-allowed-sessions-' + mode, allowedSessionsSelectValue(p.conf_allowed_sessions));
    _set('tr-allowed-sessions-' + mode, allowedSessionsSelectValue(p.tr_allowed_sessions));
    if (p.conf_model_name) {
        _pendingPresetModelByMode[mode] = p.conf_model_name;
        _selectModelFromPreset(mode, p.conf_model_name);
    }
    onRrModeChange(mode);

    // Timeframe checkboxes: overlap → tf_combo; single → [area_timeframe].
    const tfCombo = Array.isArray(p.tf_combo) ? p.tf_combo.filter(Boolean) : [];
    const method = (p.method || (tfCombo.length >= 2 ? 'overlap' : 'single')).toLowerCase();
    const selectedTfs = (method === 'overlap' && tfCombo.length >= 2)
        ? tfCombo
        : [String(p.area_timeframe || '15m')];
    setOverlapTfCombo(mode, selectedTfs);
    updateOverlapTradeTfControl(mode);

    // Preset model is authoritative. Re-apply it after trend helper refreshes so
    // FACTOR/DAY ZONE/DISTRIBUTION presets cannot leave stale TREND-only UI.
    _setStrategySelect(mode, p.strategy);
    updateStrategyParamVisibility(mode);
    updateMlParamSummary(mode);
}

// Kept for older event hooks; trigger OFF now controls the disabled state.
function onTrailToggle(mode) {
    updateTrailBounds(mode);
}


// ── Preset system (server-side JSON) ──
let _presetsCache = { presets: {}, last_used_bt: 'default', last_used_live: 'default', fixed_presets: [] };

function isFixedPreset(name) {
    return Array.isArray(_presetsCache.fixed_presets) && _presetsCache.fixed_presets.includes(name);
}

const PRESET_MODEL_ORDER = [
    'FADE', 'SIGMA', 'FACTOR', 'MOMENTUM', 'BETAFIB', 'PI', 'OPTION WALL', 'DELTA ABSORPTION',
    // Historical names remain sortable and parseable; new names never emit them.
    'TREND', 'DAY ZONE', 'DISTRIBUTION', 'PMO', 'BETA FIB',
];

function _presetNameMeta(name) {
    const raw = String(name || '');
    const fixed = /\s+\*$/.test(raw);
    const s = raw.replace(/\s+\*$/, '').trim();
    const compactDated = s.match(/^(\d{4})\s+(FADE|SIGMA|FACTOR|MOMENTUM|BETAFIB|PI|OPTION WALL|DELTA ABSORPTION|TREND|DAY ZONE|DISTRIBUTION|PMO|BETA FIB)\s+#(\d+)\s*(.*)$/i);
    const dottedDated = s.match(/^(\d{2}\.\d{2})(?:\s+(\d{2}:\d{2}))?\s+(FADE|SIGMA|FACTOR|MOMENTUM|BETAFIB|PI|OPTION WALL|DELTA ABSORPTION|TREND|DAY ZONE|DISTRIBUTION|PMO|BETA FIB)\s+#(\d+)\s*(.*)$/i);
    const legacy = s.match(/^(FADE|SIGMA|FACTOR|MOMENTUM|BETAFIB|PI|OPTION WALL|DELTA ABSORPTION|TREND|DAY ZONE|DISTRIBUTION|PMO|BETA FIB)\s+#(\d+)\s*(.*)$/i);
    if (compactDated) {
        return {
            fixed,
            date: compactDated[1],
            time: '',
            model: compactDated[2].toUpperCase(),
            num: parseInt(compactDated[3], 10) || 0,
            tail: compactDated[4] || '',
        };
    }
    if (dottedDated) {
        return {
            fixed,
            date: dottedDated[1].replace('.', ''),
            time: dottedDated[2] || '',
            model: dottedDated[3].toUpperCase(),
            num: parseInt(dottedDated[4], 10) || 0,
            tail: dottedDated[5] || '',
        };
    }
    if (legacy) {
        return {
            fixed,
            date: '',
            time: '',
            model: legacy[1].toUpperCase(),
            num: parseInt(legacy[2], 10) || 0,
            tail: legacy[3] || '',
        };
    }
    return { fixed, date: '', time: '', model: '', num: 0, tail: s };
}

function _presetSortRank(name) {
    const meta = _presetNameMeta(name);
    const idx = PRESET_MODEL_ORDER.indexOf(meta.model);
    return idx >= 0 ? (idx + 1) * 10 : 100;
}

function _presetDateSortValue(meta) {
    if (!meta || !meta.date) return -1;
    const compact = String(meta.date || '').replace('.', '');
    const parts = compact.length === 4
        ? [compact.slice(0, 2), compact.slice(2, 4)]
        : String(meta.date || '').split('.');
    const t = (meta.time || '00:00').split(':');
    const month = parseInt(parts[0], 10) || 0;
    const day = parseInt(parts[1], 10) || 0;
    const hour = parseInt(t[0], 10) || 0;
    const minute = parseInt(t[1], 10) || 0;
    return (((month * 32) + day) * 24 + hour) * 60 + minute;
}

function _presetDisplayName(name) {
    const meta = _presetNameMeta(name);
    if (!meta.model) return String(name || '');
    const dt = (meta.date || '----').padEnd(4, ' ');
    const model = meta.model.padEnd(12, ' ');
    const num = ('#' + meta.num).padEnd(4, ' ');
    return dt + ' ' + model + num + meta.tail + (meta.fixed ? ' *' : '');
}

function _comparePresetNames(a, b) {
    const ma = _presetNameMeta(a);
    const mb = _presetNameMeta(b);
    const ra = _presetSortRank(a);
    const rb = _presetSortRank(b);
    if (ra !== rb) return ra - rb;
    const da = _presetDateSortValue(ma);
    const db = _presetDateSortValue(mb);
    if (da !== db) return db - da;
    if (ma.num !== mb.num) return ma.num - mb.num;
    return String(a).localeCompare(String(b), undefined, { numeric: true });
}

function _namingDatePrefix(d) {
    const dt = d instanceof Date ? d : new Date();
    const mm = String(dt.getMonth() + 1).padStart(2, '0');
    const dd = String(dt.getDate()).padStart(2, '0');
    return mm + dd;
}

function _namingModelFromParams(params) {
    return strategyDisplayName((params || {}).strategy);
}

/* PI entry controls -------------------------------------------------------
 *
 * The engine historically exposed the five named `pi_signal_set` presets
 * plus `pi_long_only`.  Keep those fields as the compatibility wire format,
 * but let the UI present the actual signal kinds as a small LONG/SHORT
 * matrix.  The optional kind arrays are understood by PiSignalStrategy and
 * leave old presets untouched when they are absent.
 */
const PI_MATRIX_KIND_BY_SIDE_LEVEL = Object.freeze({
    long: Object.freeze({ pi: '青π', level2: '深蓝圈', level1: '淡蓝圈' }),
    // Both short circle rows use the same visual kind.  Their source Level N
    // is carried separately in pi_short_levels so Level 1 and Level 2 can be
    // selected independently instead of being treated as one disabled row.
    short: Object.freeze({ pi: '粉π', level2: '紫圈', level1: '紫圈' }),
});

const PI_MATRIX_LEVEL_BY_SIDE_LEVEL = Object.freeze({
    long: Object.freeze({ pi: 3, level2: 2, level1: 1 }),
    short: Object.freeze({ pi: 3, level2: 2, level1: 1 }),
});

const PI_MATRIX_SET_STATE = Object.freeze({
    long_pi_only: Object.freeze({ long: Object.freeze({ pi: true, level2: true, level1: false }), short: Object.freeze({ pi: false, level2: false, level1: false }) }),
    long_all: Object.freeze({ long: Object.freeze({ pi: true, level2: true, level1: true }), short: Object.freeze({ pi: false, level2: false, level1: false }) }),
    pi_only: Object.freeze({ long: Object.freeze({ pi: true, level2: true, level1: false }), short: Object.freeze({ pi: true, level2: false, level1: false }) }),
    pi_strict: Object.freeze({ long: Object.freeze({ pi: true, level2: false, level1: false }), short: Object.freeze({ pi: true, level2: false, level1: false }) }),
    // ALL includes both short circle levels.  Their duplicate visual kind is
    // de-duplicated on the legacy wire field and preserved by levels.
    all: Object.freeze({ long: Object.freeze({ pi: true, level2: true, level1: true }), short: Object.freeze({ pi: true, level2: true, level1: true }) }),
});

const PI_MATRIX_SET_ORDER = Object.freeze(['long_pi_only', 'long_all', 'pi_only', 'pi_strict', 'all']);

function _piMatrixSwitchId(mode, side, level) {
    return 'pi-matrix-' + mode + '-' + side + '-' + level;
}

function _piMatrixStateEmpty() {
    return {
        long: { pi: false, level2: false, level1: false },
        short: { pi: false, level2: false, level1: false },
    };
}

function _piMatrixCloneState(state) {
    const out = _piMatrixStateEmpty();
    ['long', 'short'].forEach((side) => {
        ['pi', 'level2', 'level1'].forEach((level) => {
            out[side][level] = Boolean(state && state[side] && state[side][level]);
        });
    });
    return out;
}

function _piMatrixStateForSet(signalSet, longOnly) {
    const set = PI_MATRIX_SET_STATE[String(signalSet || 'long_pi_only').toLowerCase()]
        || PI_MATRIX_SET_STATE.long_pi_only;
    const state = _piMatrixCloneState(set);
    if (longOnly) {
        state.short = { pi: false, level2: false, level1: false };
    }
    return state;
}

function _piMatrixStateDistance(a, b) {
    let distance = 0;
    ['long', 'short'].forEach((side) => {
        ['pi', 'level2', 'level1'].forEach((level) => {
            if (Boolean(a?.[side]?.[level]) !== Boolean(b?.[side]?.[level])) distance += 1;
        });
    });
    return distance;
}

function _piMatrixNearestSet(state) {
    let best = 'long_pi_only';
    let bestDistance = Infinity;
    PI_MATRIX_SET_ORDER.forEach((name) => {
        const distance = _piMatrixStateDistance(state, PI_MATRIX_SET_STATE[name]);
        if (distance < bestDistance) {
            best = name;
            bestDistance = distance;
        }
    });
    return best;
}

function _piMatrixStateKinds(state, side) {
    const kinds = PI_MATRIX_KIND_BY_SIDE_LEVEL[side];
    if (!kinds) return [];
    const out = [];
    ['pi', 'level2', 'level1'].forEach((level) => {
        const kind = kinds[level];
        if (state?.[side]?.[level] && kind && !out.includes(kind)) out.push(kind);
    });
    return out;
}

function _piMatrixStateLevels(state, side) {
    const levels = PI_MATRIX_LEVEL_BY_SIDE_LEVEL[side];
    if (!levels || side !== 'short') return [];
    return ['level1', 'level2']
        .filter((level) => state?.[side]?.[level] && Number.isFinite(levels[level]))
        .map((level) => levels[level]);
}

function _piMatrixReadState(mode) {
    const state = _piMatrixStateEmpty();
    let found = false;
    ['long', 'short'].forEach((side) => {
        ['pi', 'level2', 'level1'].forEach((level) => {
            const el = document.getElementById(_piMatrixSwitchId(mode, side, level));
            if (!el) return;
            found = true;
            state[side][level] = el.classList.contains('on') || el.getAttribute('aria-checked') === 'true';
        });
    });
    return found ? state : null;
}

function _piMatrixWriteState(mode, state) {
    ['long', 'short'].forEach((side) => {
        ['pi', 'level2', 'level1'].forEach((level) => {
            const el = document.getElementById(_piMatrixSwitchId(mode, side, level));
            if (!el) return;
            const on = Boolean(state?.[side]?.[level]);
            if (el.tpxSetState) el.tpxSetState(on);
            else {
                el.classList.toggle('on', on);
                el.setAttribute('aria-checked', String(on));
            }
        });
    });
}

function _piMatrixDispatchChange(el) {
    if (!el) return;
    try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
}

function _piMatrixSyncLegacy(mode, state) {
    const next = _piMatrixCloneState(state);
    const setName = _piMatrixNearestSet(next);
    const signalSet = document.getElementById('pi-signal-set-' + mode);
    const longOnly = document.getElementById('pi-long-only-' + mode);
    if (signalSet && signalSet.value !== setName) {
        signalSet.value = setName;
        _piMatrixDispatchChange(signalSet);
    }
    // The explicit kinds are authoritative; this flag keeps old consumers
    // and status/preset displays semantically aligned with the matrix.
    const hasShort = _piMatrixStateKinds(next, 'short').length > 0;
    if (longOnly) {
        const nextValue = hasShort ? '0' : '1';
        if (longOnly.value !== nextValue) {
            longOnly.value = nextValue;
            _piMatrixDispatchChange(longOnly);
        }
    }
    return { setName, state: next };
}

function _piMatrixSyncFromLegacy(mode) {
    const signalSet = document.getElementById('pi-signal-set-' + mode);
    const longOnly = document.getElementById('pi-long-only-' + mode);
    const state = _piMatrixStateForSet(
        signalSet ? signalSet.value : 'long_pi_only',
        longOnly ? longOnly.value === '1' : true,
    );
    _piMatrixWriteState(mode, state);
    return state;
}

function onPiMatrixProxy(mode, side, level) {
    const state = _piMatrixReadState(mode);
    if (!state) return;
    _piMatrixSyncLegacy(mode, state);
}

function _piMatrixPayload(mode) {
    const state = _piMatrixReadState(mode);
    if (!state) return { pi_long_kinds: null, pi_short_kinds: null, pi_short_levels: null };
    _piMatrixSyncLegacy(mode, state);
    return {
        pi_long_kinds: _piMatrixStateKinds(state, 'long'),
        pi_short_kinds: _piMatrixStateKinds(state, 'short'),
        pi_short_levels: _piMatrixStateLevels(state, 'short'),
    };
}

function _normalizeNamingModel(model) {
    const value = String(model || '').trim().toUpperCase();
    if (['FADE', 'SIGMA', 'FACTOR', 'MOMENTUM', 'BETAFIB', 'PI', 'DELTA ABSORPTION'].includes(value)) return value;
    if (value === 'DAY ZONE') return 'FADE';
    if (value === 'DISTRIBUTION') return 'SIGMA';
    if (value === 'BETA FIB') return 'BETAFIB';
    if (value === 'TREND' || value === 'PMO') return 'FACTOR';
    return 'FACTOR';
}

function _sanitizePresetPurpose(value, fallback) {
    const clean = String(value || '').replace(/\s+/g, '').trim();
    return (clean || fallback || 'Manual').slice(0, 12);
}

function _nextPresetNumber(model, datePrefix) {
    const a = _normalizeNamingModel(model);
    const prefix = datePrefix + ' ' + a + ' #';
    let maxN = 0;
    Object.keys((_presetsCache && _presetsCache.presets) || {}).forEach((name) => {
        if (!String(name).startsWith(prefix)) return;
        const m = String(name).match(/#(\d+)/);
        if (m) maxN = Math.max(maxN, parseInt(m[1], 10) || 0);
    });
    return maxN + 1;
}

function _contractPresetToken(params) {
    const p = params || {};
    const label = contractLabelFromId(p.contract_id || DEFAULT_STRATEGY_PARAMS.contract_id);
    const size = normalizeContractSize(
        p.contract_id || DEFAULT_STRATEGY_PARAMS.contract_id,
        p.contract_size != null ? p.contract_size : DEFAULT_STRATEGY_PARAMS.contract_size
    );
    return label + 'x' + size;
}

function _probToken(value) {
    const n = Number(value || 0);
    return n > 0 ? ('P' + n.toFixed(2).replace(/0+$/, '').replace(/\.$/, '')) : 'POFF';
}

function buildPresetParamToken(params) {
    const p = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params || {});
    if (normalizeStrategyName(p.strategy) === 'optionwall') {
        const side = String(p.option_wall_side_mode || 'all').toUpperCase();
        return [
            String(p.option_wall_submodel || 'primary_strict').toUpperCase(),
            side,
            'LSL' + Number(p.option_wall_long_sl_atr || 4),
            'SSL' + Number(p.option_wall_short_sl_atr || 1.5),
            'H' + Number(p.option_wall_max_hold_min || 60),
            'D' + Number(p.option_wall_max_trades_per_day || 0),
            'RTH',
            _contractPresetToken(p),
        ].join(' ');
    }
    if (normalizeStrategyName(p.strategy) === 'sigma') {
        const market = allowedSessionsLabel(p.tr_allowed_sessions != null ? p.tr_allowed_sessions : ['RTH']);
        return [
            'DISTRIBUTION',
            'Roll' + (parseInt(p.sigma_window_minutes != null ? p.sigma_window_minutes : 15, 10) || 15),
            String(p.sigma_method || 'std').toUpperCase(),
            String(p.sigma_entry_mode || 'blind'),
            'Accept' + String(p.sigma_accept_mode || 'none'),
            'TP' + String(p.sigma_target_mode || 'half'),
            'SL' + String(p.sigma_stop_span != null ? p.sigma_stop_span : 1),
            market,
            _contractPresetToken(p),
        ].join(' ');
    }
    if (normalizeStrategyName(p.strategy) === 'factor') {
        const market = allowedSessionsLabel(p.tr_allowed_sessions != null ? p.tr_allowed_sessions : null);
        const va = String(p.factor_session_va_filter || 'off') === 'outside' ? 'VA80OUT' : 'VAOFF';
        const fam = String(p.factor_signal_family || 'emapmo').toLowerCase();
        const famLabel = fam === 'icefishball'
            ? 'KDJMA'
            : (fam === 'momentum_reversion' ? 'MREV' : 'EMAPMO');
        const side = String(p.factor_side_mode || 'all').toLowerCase();
        const sideLabel = side === 'all' ? 'both' : side;
        return [
            'FACTOR',
            famLabel,
            sideLabel,
            String(p.factor_pmo_signal_mode || 'normal'),
            va,
            String(p.factor_timeframe_minutes || 5) + 'm',
            'SL' + String(p.factor_sl_rule || 'atr_blend') + String(p.factor_sl_value != null ? p.factor_sl_value : ''),
            'TP' + String(p.factor_tp_rule || 'atr_blend') + String(p.factor_tp_value != null ? p.factor_tp_value : ''),
            String(p.tr_exit_mode || 'tp').toUpperCase(),
            'H' + (Number(p.factor_max_hold_bars || 0) > 0 ? String(p.factor_max_hold_bars) : 'OFF'),
            market,
            _contractPresetToken(p),
        ].join(' ');
    }
    if (normalizeStrategyName(p.strategy) === 'delta_absorption') {
        const side = String(p.delta_side_mode || 'all').toUpperCase();
        const pattern = String(p.delta_pattern || 'absorption').toUpperCase();
        const source = String(p.delta_source || 'whole').toUpperCase();
        const gate = String(p.delta_gate || 'location').toUpperCase();
        return [
            'DELTA', pattern, side, source, gate,
            'W' + Number(p.delta_window != null ? p.delta_window : 5),
            'B' + Number(p.delta_baseline_window != null ? p.delta_baseline_window : 30),
            'S' + Number(p.delta_strength_multiplier != null ? p.delta_strength_multiplier : 1),
            'VA' + (p.delta_require_profile === false ? 'OPT' : 'REQ'),
            'SL' + Number(p.factor_sl_value != null ? p.factor_sl_value : 4),
            'SSL' + Number(p.pi_short_sl_value != null ? p.pi_short_sl_value : 1.5),
            'RR' + Number(p.rr_ratio != null ? p.rr_ratio : 3),
            'RTH',
            _contractPresetToken(p),
        ].join(' ');
    }
    if (normalizeStrategyName(p.strategy) === 'confluence') {
        const risk = p.conf_max_risk_ticks != null && Number(p.conf_max_risk_ticks) > 0
            ? ('R' + Number(p.conf_max_risk_ticks))
            : 'ROFF';
        const trailPct = Number(p.conf_trail_trigger_pct || 0);
        const lockPct = Number(p.conf_trail_lock_pct != null ? p.conf_trail_lock_pct : 0.05);
        const trail = trailPct > 0
            ? ('Trail' + Math.round(trailPct * 100) + 'L' + Math.round(lockPct * 100))
            : 'TrailOFF';
        const sessionLimit = p.conf_session_limit === false ? 'SesOFF' : 'SesON';
        const market = allowedSessionsLabel(p.conf_allowed_sessions != null ? p.conf_allowed_sessions : ['ASIA']);
        const slRef = p.conf_sl_reference_tf === 'smallest' ? 'SLsmall' : 'SLlarge';
        return [
            _contractPresetToken(p),
            'RR1:' + _fmtConfRr(p.conf_rr != null ? p.conf_rr : 1.0),
            _probToken(p.conf_min_prob),
            risk,
            slRef,
            'W1m',
            trail,
            sessionLimit,
            market,
            'B' + Number(p.conf_band_ticks != null ? p.conf_band_ticks : 4),
            'TF' + Number(p.conf_min_distinct_tf != null ? p.conf_min_distinct_tf : 2),
        ].join(' ');
    }
    // 1.0.8: 移除 ml_consolidation_v2 (mlc2) preset 標籤
    const vaPct = Math.round((p.value_area_pct != null ? Number(p.value_area_pct) : 0.80) * 100);
    const rr = Math.max(1, Math.min(6, parseInt(p.rr_ratio != null ? p.rr_ratio : 2, 10) || 2));
    const tfCombo = Array.isArray(p.tf_combo) ? p.tf_combo.filter(Boolean) : [];
    const method = (p.method || (tfCombo.length >= 2 ? 'overlap' : 'single')).toLowerCase();
    const tfs = (method === 'overlap' && tfCombo.length >= 2)
        ? tfCombo
        : (tfCombo.length ? [tfCombo[0]] : [p.area_timeframe || '15m']);
    const confirm = Math.max(1, Math.min(10, parseInt(p.breakout_confirm_bars != null ? p.breakout_confirm_bars : 7, 10) || 7));
    const market = allowedSessionsLabel(p.tr_allowed_sessions != null ? p.tr_allowed_sessions : ['ASIA']);
    const overlapTrade = method === 'overlap' && p.tr_overlap_trade_tf === 'smallest' ? 'TradeSmall' : '';
    return ['TR' + vaPct, tfs.join('/'), overlapTrade, 'RR1:' + rr, 'C' + confirm, market, _contractPresetToken(p)]
        .filter(Boolean).join(' ');
}

function suggestedPresetPurpose(params) {
    const p = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params || {});
    if (normalizeStrategyName(p.strategy) === 'sigma') return 'Distribution';
    if (normalizeStrategyName(p.strategy) === 'factor') return 'Icefishball';
    if (normalizeStrategyName(p.strategy) === 'optionwall') return 'Primary Strict';
    if (normalizeStrategyName(p.strategy) === 'delta_absorption') return 'Absorption';
    if (normalizeStrategyName(p.strategy) === 'confluence') {
        const risk = Number(p.conf_max_risk_ticks || 0);
        const prob = Number(p.conf_min_prob || 0);
        const rr = Number(p.conf_rr || 0);
        if (prob >= 0.6) return 'Lowest DD';
        if (risk <= 50) return 'Highest PNL';
        if (rr >= 2.75) return 'Robust Test';
        if (rr <= 1.75) return 'Best Calmar';
        return 'Manual Test';
    }
    return 'Manual Test';
}

function buildPresetName(params, purpose, model) {
    const px = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params || {});
    const day = _namingDatePrefix();
    const a = _normalizeNamingModel(model || _namingModelFromParams(px));
    const use = _sanitizePresetPurpose(purpose, suggestedPresetPurpose(px));
    const tail = ' ' + use + ' ' + buildPresetParamToken(px);
    // 1.0.9 FIX: #N 唯一性保證 — 同名(同日同模型同參數 token)重存會直接覆蓋舊 preset,
    // 造成「命名重複」假象;現在往上找第一個未被占用的 #N。
    let n = _nextPresetNumber(a, day);
    const exists = (nm) => !!((_presetsCache && _presetsCache.presets) || {})[nm];
    let name = day + ' ' + a + ' #' + n + tail;
    let guard = 0;
    while (exists(name) && guard++ < 99) {
        n += 1;
        name = day + ' ' + a + ' #' + n + tail;
    }
    return name;
}

async function fetchPresets() {
    try {
        const resp = await fetch(API + '/presets');
        if (resp.ok) _presetsCache = await resp.json();
    } catch(e) { /* server offline, use cache */ }
    return _presetsCache;
}

function requestPresetName(mode, defaultName) {
    try {
        if (typeof window.prompt === 'function') {
            return Promise.resolve(window.prompt('Preset name:', defaultName));
        }
    } catch (e) {
        // Some embedded browsers disable prompt(); fall back to an inline editor.
    }
    return new Promise(resolve => {
        const sel = document.getElementById('preset-' + mode);
        const group = sel ? sel.closest('.form-group') : null;
        const actionRow = group ? group.nextElementSibling : null;
        const anchor = actionRow || group || sel;
        let wrap = document.getElementById('preset-save-inline-' + mode);
        if (!wrap) {
            wrap = document.createElement('div');
            wrap.id = 'preset-save-inline-' + mode;
            wrap.className = 'action-row';
            wrap.style.marginTop = '6px';
            const input = document.createElement('input');
            input.id = 'preset-save-name-' + mode;
            input.type = 'text';
            input.style.flex = '1';
            input.style.minWidth = '0';
            input.style.background = '#05070b';
            input.style.border = '1px solid rgba(100,220,255,0.24)';
            input.style.color = 'var(--white)';
            input.style.fontFamily = 'inherit';
            input.style.fontSize = '10px';
            input.style.padding = '7px 8px';
            const ok = document.createElement('button');
            ok.type = 'button';
            ok.className = 'btn btn-outline btn-mini';
            ok.textContent = 'OK';
            const cancel = document.createElement('button');
            cancel.type = 'button';
            cancel.className = 'btn btn-outline btn-mini';
            cancel.textContent = 'CANCEL';
            wrap.appendChild(input);
            wrap.appendChild(ok);
            wrap.appendChild(cancel);
            if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(wrap, anchor.nextSibling);
            else document.body.appendChild(wrap);
        }
        const input = document.getElementById('preset-save-name-' + mode);
        const buttons = wrap.querySelectorAll('button');
        const ok = buttons[0];
        const cancel = buttons[1];
        const finish = (value) => {
            wrap.style.display = 'none';
            resolve(value);
        };
        input.value = defaultName;
        wrap.style.display = 'flex';
        input.focus();
        input.select();
        ok.onclick = () => finish(input.value);
        cancel.onclick = () => finish(null);
        input.onkeydown = (ev) => {
            if (ev.key === 'Enter') finish(input.value);
            if (ev.key === 'Escape') finish(null);
        };
    });
}

async function savePreset(mode) {
    const params = reconcilePresetStrategyForDispatch(mode, collectStrategyParams(mode), 'SAVE PRESET');
    const confParams = collectConfluenceParams(mode);
    if (false && confParams) {
        const modelSel = document.getElementById('conf-model-' + mode);
        Object.assign(params, confParams, {
            strategy: 'confluence',
            conf_model_name: (modelSel && modelSel.value) || _activeModelName || null,
        });
    }
    const defaultName = buildPresetName(params, suggestedPresetPurpose(params));
    const name = await requestPresetName(mode, defaultName);
    if (!name || !name.trim()) return;
    try {
        const saveResp = await fetch(API + '/presets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), params: params }),
        });
        if (!saveResp.ok) throw new Error('HTTP ' + saveResp.status);
        _presetsCache.presets = _presetsCache.presets || {};
        _presetsCache.presets[name.trim()] = params;
        await fetch(API + '/presets/use', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), mode: mode }),
        });
        await fetchPresets();
        _presetsCache.presets = _presetsCache.presets || {};
        if (!_presetsCache.presets[name.trim()]) _presetsCache.presets[name.trim()] = params;
        refreshPresetDropdowns();
        _setPresetDropdownValue(mode, name.trim());
        _loadedPresetNameByMode[mode] = name.trim();
        log('Preset "' + name.trim() + '" saved', 'success');
    } catch(e) {
        log('Preset save error: ' + e.message, 'error');
    }
}

async function loadPreset(mode) {
    const sel = document.getElementById('preset-' + mode);
    const name = sel.value;
    if (name === 'default') {
        applyStrategyParams(mode, DEFAULT_STRATEGY_PARAMS);
        _loadedPresetNameByMode[mode] = '';
    } else if (_presetsCache.presets[name]) {
        const presetParams = _presetsCache.presets[name];
        applyStrategyParams(mode, presetParams);
        _loadedPresetNameByMode[mode] = name;
        log('Preset "' + name + '" loaded', 'info');
    }
    // Record last used
    try {
        await fetch(API + '/presets/use', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name, mode: mode }),
        });
    } catch(e) { /* ignore */ }
}

function _setPresetDropdownValue(mode, name) {
    const sel = document.getElementById('preset-' + mode);
    if (!sel || !name) return;
    if (name === MODIFIED_PRESET_VALUE) {
        _ensureModifiedPresetOption(sel);
        sel.value = MODIFIED_PRESET_VALUE;
        return;
    }
    if (!Array.from(sel.options || []).some(o => o.value === name)) {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = _presetDisplayName(isFixedPreset(name) ? name + ' *' : name);
        sel.appendChild(opt);
    }
    sel.value = name;
}

function _ensureModifiedPresetOption(sel) {
    if (!sel) return;
    if (!Array.from(sel.options || []).some(o => o.value === MODIFIED_PRESET_VALUE)) {
        const opt = document.createElement('option');
        opt.value = MODIFIED_PRESET_VALUE;
        opt.textContent = 'UNSAVED MODIFIED';
        opt.disabled = true;
        sel.insertBefore(opt, sel.options && sel.options.length ? sel.options[0] : null);
    }
}

function markPresetModified(mode) {
    const sel = document.getElementById('preset-' + mode);
    if (!sel || sel.value === 'default' || sel.value === MODIFIED_PRESET_VALUE) return;
    _loadedPresetNameByMode[mode] = sel.value;
    _setPresetDropdownValue(mode, MODIFIED_PRESET_VALUE);
}

function initPresetDirtyTracking() {
    if (_presetDirtyTrackingBound) return;
    _presetDirtyTrackingBound = true;
    ['bt', 'live'].forEach(mode => {
        const root = document.getElementById(mode === 'bt' ? 'backtest-config-panel' : 'live-settings-panel');
        if (!root) return;
        const handler = (ev) => {
            if (!ev || !ev.target) return;
            if (ev.target.id === 'preset-' + mode) return;
            markPresetModified(mode);
        };
        root.addEventListener('change', handler, true);
        root.addEventListener('input', handler, true);
    });
}

function syncMainAccountPresetToPanels(silent) {
    const slotSel = document.getElementById('live-acct-preset-' + LIVE_MAIN_SLOT);
    const name = (slotSel && slotSel.value) || '';
    const preset = name ? (((_presetsCache && _presetsCache.presets) || {})[name]) : null;
    if (!name || !preset) return false;
    _setPresetDropdownValue('bt', name);
    _setPresetDropdownValue('live', name);
    applyStrategyParams('bt', preset);
    applyStrategyParams('live', preset);
    _loadedPresetNameByMode.bt = name;
    _loadedPresetNameByMode.live = name;
    if (!silent) {
        log('ACCOUNT MAIN preset applied to Backtest/Live panels: ' + _presetDisplayName(name), 'info');
    }
    return true;
}

function refreshPresetDropdowns() {
    const names = Object.keys(_presetsCache.presets || {}).sort(_comparePresetNames);
    ['bt', 'live'].forEach(function(mode) {
        const sel = document.getElementById('preset-' + mode);
        if (!sel) return;
        const current = sel.value;
        sel.innerHTML = '<option value="default">Default</option>';
        names.forEach(function(n) {
            const opt = document.createElement('option');
            opt.value = n;
            opt.textContent = _presetDisplayName(isFixedPreset(n) ? n + ' *' : n);
            sel.appendChild(opt);
        });
        if (current === MODIFIED_PRESET_VALUE) {
            _setPresetDropdownValue(mode, MODIFIED_PRESET_VALUE);
            return;
        }
        if (current && (current === 'default' || _presetsCache.presets[current])) {
            sel.value = current;
        }
    });
}

async function deletePreset(mode) {
    const sel = document.getElementById('preset-' + mode);
    const name = sel.value;
    if (name === 'default') {
        log('Cannot delete default preset', 'warn');
        return;
    }
    try {
        const resp = await fetch(API + '/presets/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const result = await resp.json();
        if (!result.success || !result.deleted) throw new Error('Preset not found: ' + name);
        await fetchPresets();
        refreshPresetDropdowns();
        // Reset both dropdowns to default if they were showing deleted preset
        ['bt', 'live'].forEach(m => {
            const s = document.getElementById('preset-' + m);
            if (s && s.value === name) { s.value = 'default'; applyStrategyParams(m, DEFAULT_STRATEGY_PARAMS); }
        });
        log('Preset "' + name + '" deleted', 'info');
    } catch(e) {
        log('Preset delete error: ' + e.message, 'error');
    }
}

async function initPresets() {
    await fetchPresets();
    refreshPresetDropdowns();
    // Auto-load last used preset for each mode
    const lastBt = _presetsCache.last_used_bt || 'default';
    const lastLive = _presetsCache.last_used_live || 'default';
    const btSel = document.getElementById('preset-bt');
    const liveSel = document.getElementById('preset-live');
    if (btSel) {
        btSel.value = lastBt;
        if (lastBt !== 'default' && _presetsCache.presets[lastBt]) {
            applyStrategyParams('bt', _presetsCache.presets[lastBt]);
            _loadedPresetNameByMode.bt = lastBt;
        } else {
            applyStrategyParams('bt', DEFAULT_STRATEGY_PARAMS);
            _loadedPresetNameByMode.bt = '';
        }
    }
    if (liveSel) {
        liveSel.value = lastLive;
        if (lastLive !== 'default' && _presetsCache.presets[lastLive]) {
            applyStrategyParams('live', _presetsCache.presets[lastLive]);
            _loadedPresetNameByMode.live = lastLive;
        } else {
            applyStrategyParams('live', DEFAULT_STRATEGY_PARAMS);
            _loadedPresetNameByMode.live = '';
        }
    }
}

// TRAIL TP TRIGGER is a binary OFF/50% toggle button backed by a hidden input
// (id conf-trail-trigger-<mode>) so the backend payload reads exactly as before.
function _syncTrailTriggerBtn(mode) {
    const inp = document.getElementById('conf-trail-trigger-' + mode);
    const btn = document.getElementById('conf-trail-trigger-btn-' + mode);
    if (!inp || !btn) return;
    const on = (parseFloat(inp.value) || 0) > 0;
    btn.textContent = on ? '50%' : 'OFF';
    btn.classList.toggle('on', on);
}
function toggleTrailTrigger(mode) {
    const inp = document.getElementById('conf-trail-trigger-' + mode);
    if (!inp) return;
    const on = (parseFloat(inp.value) || 0) > 0;
    inp.value = on ? '0' : '0.5';
    _syncTrailTriggerBtn(mode);
    updateMlParamSummary(mode);
}
document.addEventListener('DOMContentLoaded', () => {
    ['bt', 'live'].forEach((mode) => {
        _syncTrailTriggerBtn(mode);
        updateMlParamSummary(mode);
    });
});

document.addEventListener('DOMContentLoaded', initPresets);

// ════════════════════════════════════════════════════════════════════════
// Immutable model registry. Training appends a version; selecting one copies it
// to the canonical active scorer shared by backtest and live.
// ════════════════════════════════════════════════════════════════════════
let _modelRegistry = [];   // cached list from GET /confluence/models
let _activeModelName = '';
const _pendingPresetModelByMode = { bt: '', live: '' };

function _fmtModelLabel(m) {
    const labelActive = m.active ? '● ' : '';
    const labelOos = (m.oos_auc != null) ? ` · AUC ${Number(m.oos_auc).toFixed(2)}` : '';
    return `${labelActive}${m.name}${labelOos}`;

    const day = String(m.trained_at || m.name || '').slice(0, 10).replace(/-/g, '');
    const trainer = String(m.trainer || 'codex').toUpperCase();
    const active = m.active ? '● ' : '';
    const desc = m.description ? ` · ${m.description}` : '';
    const oos = (m.oos_auc != null) ? ` · AUC ${Number(m.oos_auc).toFixed(2)}` : '';
    return `${active}${day} · ${trainer} · RR${Number(m.rr).toFixed(0)}${desc}${oos}`;
}

// 1.0.9: confluence/ML 已整批移除(後端模組、路由、模型檔都刪了),
// 這裡保留空殼是為了讓其餘啟動流程不必改動 —— 呼叫端仍會叫它,但不再
// 打已不存在的 /confluence/models(否則每次載入都會噴 404)。
// 見 docs/1.0.9_DELETE_LIST.md。
async function loadModelRegistry() {
    _modelRegistry = [];
    _activeModelName = '';
}

function _populateModelSelect(mode) {
    const sel = document.getElementById('conf-model-' + mode);
    if (!sel) return;
    sel.innerHTML = '';
    if (!_modelRegistry.length) {
        const option = new Option('NO TRAINED MODELS', '');
        option.disabled = true;
        option.selected = true;
        sel.add(option);
        return;
    }
    _modelRegistry.forEach((m) => {
        const option = new Option(_fmtModelLabel(m), m.name);
        option.dataset.rr = m.rr;
        option.dataset.band = m.band;
        option.dataset.tf = m.min_distinct_tf;
        option.dataset.brk = m.breakout ? '1' : '0';
        option.dataset.trained = m.trained ? '1' : '0';
        sel.add(option);
    });
    const activeExists = _modelRegistry.some(m => m.name === _activeModelName);
    const preferred = _pendingPresetModelByMode[mode];
    const preferredExists = preferred && _modelRegistry.some(m => m.name === preferred);
    sel.value = preferredExists ? preferred : (activeExists ? _activeModelName : _modelRegistry[0].name);
    const m = _modelRegistry.find(model => model.name === sel.value) || _modelRegistry[0];
    _applyModelCombo(mode, m.rr, m.band, m.min_distinct_tf, m.breakout);
}

// Mirror a combo's params into the MODEL panel fields (shared by select+retrain).
function _applyModelCombo(mode, rr, band, tf, brk) {
    const _set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    _set('conf-band-' + mode, String(Math.round(band)));
    _set('conf-mintf-' + mode, String(tf));
    _set('conf-rrmode-' + mode, 'fixed');
    _set('conf-breakout-' + mode, brk ? '1' : '0');
    if (typeof onRrModeChange === 'function') onRrModeChange(mode);
    updateMlParamSummary(mode);
}

function _selectModelFromPreset(mode, name) {
    if (!name) return false;
    const sel = document.getElementById('conf-model-' + mode);
    if (!sel) return false;
    const model = _modelRegistry.find(m => m.name === name);
    if (!model || !Array.from(sel.options).some(o => o.value === name)) return false;
    sel.value = name;
    _applyModelCombo(mode, model.rr, model.band, model.min_distinct_tf, model.breakout);
    return true;
}

async function activatePresetModel(mode, name, opts) {
    if (!name) return;
    const silent = !!(opts && opts.silent);
    _pendingPresetModelByMode[mode] = name;
    try {
        const resp = await fetch(API + '/confluence/models/activate', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.success) {
            if (!silent) log('Preset model activate failed: ' + (data.detail || ('HTTP ' + resp.status)), 'warn');
            return;
        }
        _activeModelName = name;
        await loadModelRegistry();
        _selectModelFromPreset(mode, name);
        if (!silent) log('Preset model active: ' + name, 'success');
    } catch (e) {
        if (!silent) log('Preset model activate failed: ' + e, 'warn');
    }
}

async function onModelSelect(mode) {
    const sel = document.getElementById('conf-model-' + mode);
    if (!sel || !sel.value) return;
    const opt = sel.options[sel.selectedIndex];
    const name = sel.value;
    const rr = Number(opt.dataset.rr), band = Number(opt.dataset.band);
    const tf = Number(opt.dataset.tf), brk = opt.dataset.brk === '1';
    _pendingPresetModelByMode[mode] = name;
    _applyModelCombo(mode, rr, band, tf, brk);
    if (opt.dataset.trained !== '1') {
        log('This model has not been trained yet.', 'warn');
        return;
    }
    try {
        const resp = await fetch(API + '/confluence/models/activate', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.success) {
            _activeModelName = name;
            await loadModelRegistry();
            const runRr = parseFloat((document.getElementById('conf-rr-' + mode) || {}).value || '1');
            log(`Model activated: ${name} (band ${Math.round(band)} · ${tf}TF · runtime RR 1:${_fmtConfRr(runRr)})`, 'success');
        } else {
            log('Model activation failed: ' + (data.detail || ('HTTP ' + resp.status)), 'error');
        }
    } catch (e) { log('Model activation failed: ' + e, 'error'); }
}

async function retrainModel(mode) {
    const sel = document.getElementById('conf-model-' + mode);
    const trainer = 'codex';
    const descriptionEl = document.getElementById('conf-description-' + mode);
    const rr = parseFloat((document.getElementById('conf-rr-' + mode) || {}).value || '1');
    const band = parseInt((document.getElementById('conf-band-' + mode) || {}).value || '4', 10);
    const tf = parseInt((document.getElementById('conf-mintf-' + mode) || {}).value || '2', 10);
    const brk = false;
    const lw = 1;
    const minProb = String((document.getElementById('conf-minprob-' + mode) || {}).value || '0.65');
    const description = String((descriptionEl || {}).value || '').trim()
        || `RR${_fmtConfRr(rr)} B${band} TF${tf} prob${minProb} ui retrain`;
    if (!confirm(`Create a new model version\ntrainer = ${trainer.toUpperCase()}\ndescription = ${description}\nHistorical data must already be loaded. Training may take some time.`)) return;
    log(`Training new model version · ${trainer.toUpperCase()} · ${description}…`, 'info');
    try {
        const resp = await fetch(API + '/confluence/models/retrain', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ trainer, description, rr, band_ticks: band,
                                   min_distinct_tf: tf, enable_breakout: brk,
                                   loss_weight: lw, activate: true }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.success) {
            log(`✓ ${data.name} training complete · n=${data.n_samples} win=${(data.win_rate * 100).toFixed(0)}% `
                + `oos=${Number(data.oos_auc).toFixed(2)} · activated`, 'success');
            await loadModelRegistry();
            if (sel) sel.value = data.name;
            if (descriptionEl) descriptionEl.value = '';
        } else {
            log('Training failed: ' + (data.detail || ('HTTP ' + resp.status)), 'error');
        }
    } catch (e) { log('Training failed: ' + e, 'error'); }
}


// -- Connection Dropdown ----------------------------

function toggleConnDropdown(forceState) {
    const trigger = document.getElementById('conn-trigger');
    const panel = document.getElementById('conn-panel');
    const shouldOpen = forceState !== undefined ? forceState : !panel.classList.contains('open');
    panel.classList.toggle('open', shouldOpen);
    trigger.classList.toggle('open', shouldOpen);
}

// Close dropdown when clicking outside
document.addEventListener('click', (e) => {
    const wrap = document.querySelector('.conn-dropdown-wrap');
    if (wrap && !wrap.contains(e.target)) {
        toggleConnDropdown(false);
    }
});

// -- Compact, accessible parameter help ---------------------------------

let _activeHelpDot = null;
let _inlineHelpSeq = 0;

function _updateHelpDotLabel(dot) {
    if (!dot) return;
    dot.setAttribute('aria-label', 'Show parameter help');
    dot.title = 'Parameter help';
}

function _configureHelpDot(dot) {
    if (!dot || dot.dataset.helpReady === 'true') return dot;
    dot.dataset.helpReady = 'true';
    dot.addEventListener('mouseenter', () => showHelpTooltip(dot));
    dot.addEventListener('mouseleave', () => hideHelpTooltip(false));
    dot.addEventListener('focus', () => showHelpTooltip(dot));
    dot.addEventListener('blur', () => hideHelpTooltip(false));
    dot.addEventListener('click', (event) => {
        event.stopPropagation();
        if (_activeHelpDot === dot && dot.dataset.helpPinned === 'true') {
            closeHelpTooltip();
            return;
        }
        dot.dataset.helpPinned = 'true';
        showHelpTooltip(dot);
    });
    dot.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape') return;
        event.preventDefault();
        event.stopPropagation();
        closeHelpTooltip();
    });
    _updateHelpDotLabel(dot);
    return dot;
}

function _newHelpDot() {
    const dot = document.createElement('button');
    dot.type = 'button';
    dot.className = 'help-dot';
    dot.textContent = '?';
    dot.setAttribute('aria-expanded', 'false');
    dot.setAttribute('aria-describedby', 'global-help-tooltip');
    return _configureHelpDot(dot);
}

function _englishHelpTip(tip) {
    return String(tip || '').trim();
}

function addHelpDot(label, tip) {
    if (!label || !tip) return null;
    let dot = label.querySelector('.help-dot');
    if (!dot) {
        dot = _newHelpDot();
        label.appendChild(dot);
    }
    const value = _englishHelpTip(tip);
    if (value) {
        const existing = dot.getAttribute('data-tip-en') || '';
        if (!existing.includes(value)) {
            dot.setAttribute('data-tip-en', existing ? existing + '\n' + value : value);
        }
    }
    return dot;
}

function getHelpTooltip() {
    let tip = document.getElementById('global-help-tooltip');
    if (!tip) {
        tip = document.createElement('div');
        tip.id = 'global-help-tooltip';
        tip.className = 'help-tooltip';
        tip.setAttribute('role', 'tooltip');
        document.body.appendChild(tip);
    }
    return tip;
}

function showHelpTooltip(dot) {
    if (_activeHelpDot && _activeHelpDot !== dot) {
        _activeHelpDot.dataset.helpPinned = 'false';
        _activeHelpDot.setAttribute('aria-expanded', 'false');
    }
    const chunks = [];
    const registered = dot ? dot.getAttribute('data-tip-en') : '';
    if (registered) chunks.push(registered);
    const sourceIds = dot ? String(dot.getAttribute('data-help-sources') || '').split(',').filter(Boolean) : [];
    sourceIds.forEach((id) => {
        const source = document.getElementById(id);
        const value = source ? String(source.textContent || '').trim() : '';
        if (value && !chunks.includes(value)) chunks.push(value);
    });
    const text = chunks.join('\n');
    if (!text) return;
    const tip = getHelpTooltip();
    tip.textContent = text;
    tip.style.visibility = 'hidden';
    tip.classList.add('open');
    const rect = dot.getBoundingClientRect();
    const pad = 10;
    const tipW = tip.offsetWidth;
    const tipH = tip.offsetHeight;
    const top = Math.max(pad, Math.min(
        rect.top + rect.height / 2 - tipH / 2,
        window.innerHeight - tipH - pad));
    const left = Math.max(pad, Math.min(
        rect.right + pad,
        window.innerWidth - tipW - pad));
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
    tip.style.visibility = 'visible';
    dot.setAttribute('aria-expanded', 'true');
    _activeHelpDot = dot;
}

function hideHelpTooltip(force) {
    if (!force && _activeHelpDot && _activeHelpDot.dataset.helpPinned === 'true') return;
    const tip = document.getElementById('global-help-tooltip');
    if (tip) tip.classList.remove('open');
    if (_activeHelpDot) _activeHelpDot.setAttribute('aria-expanded', 'false');
    _activeHelpDot = null;
}

function closeHelpTooltip() {
    if (_activeHelpDot) _activeHelpDot.dataset.helpPinned = 'false';
    hideHelpTooltip(true);
}

function _attachInlineHelpSource(dot, source) {
    if (!dot || !source) return;
    if (!source.id) source.id = 'inline-help-source-' + (++_inlineHelpSeq);
    const ids = String(dot.getAttribute('data-help-sources') || '').split(',').filter(Boolean);
    if (!ids.includes(source.id)) ids.push(source.id);
    dot.setAttribute('data-help-sources', ids.join(','));
    source.classList.add('inline-help-source');
    source.setAttribute('aria-hidden', 'true');
}

function migrateInlineHelp() {
    document.querySelectorAll('.lbl-hint:not(.validation-hint)').forEach((source) => {
        /* Subgroup prose sits immediately before its first .form-row rather
           than inside a label. Route that prose into the first field's one
           inline help button; otherwise it becomes an orphan `?` on a row of
           its own. Keep the old direct fallback for truly unmappable prose. */
        const followingRow = source.nextElementSibling?.matches('.form-row')
            ? source.nextElementSibling
            : null;
        const label = source.closest('label') || followingRow?.querySelector('label');
        let dot = label ? label.querySelector('.help-dot') : null;
        if (!dot) {
            dot = _newHelpDot();
            if (label) {
                const inlineHint = label.querySelector(':scope > .lbl-hint:not(.validation-hint)');
                label.insertBefore(dot, inlineHint || null);
            } else {
                source.parentNode.insertBefore(dot, source);
            }
        }
        _attachInlineHelpSource(dot, source);
    });
}

function decorateParamHelpDots() {
    // Applied to BOTH backtest (-bt) and live (-live) panels.
    const shared = {
        'strategy': 'FADE / SIGMA / FACTOR / MOMENTUM / BETAFIB / PI.\nSelect a model; its English description appears below the selector.',
        'contract': 'Futures contract used for data and orders. Bare symbols automatically resolve to the current front month.',
        'size': 'Number of contracts per trade.',
        'preset': 'Load or save the current parameter set.',
        // ML CONFLUENCE
        'conf-minprob': 'ML win-probability gate.',
        'conf-rrmode': 'Fixed reward:risk from 1:1 to 1:6.',
        'conf-band': 'Confluence band in ticks.',
        'conf-mintf': 'Minimum distinct timeframes required.',
        'conf-evfloor': 'Expected-value gate.',
        'conf-maxrisk': 'Max allowed stop distance in ticks.',
        'conf-trail-trigger': 'Trail trigger for moving stop after partial progress toward TP.',
        'conf-session-limit': 'Live-style duplicate-entry lock.',
        'conf-allowed-sessions': 'Market segment filter.',
        'overlap-tf': 'Timeframes feeding confluence.',
        // TREND
        'tr-overlap-trade-tf': 'TREND overlap trade zone: merged = synthetic averaged overlap; smallest = trade the smallest selected timeframe zone.',
        'area-pct': 'TREND range-area threshold.',
        'confirm-bars': 'Confirmation bars after breakout.',
        'rr-ratio': 'Take-profit to stop-loss ratio.',
        'trail-trigger-pct': 'Trail trigger percentage.',
        'trail-sl-pct': 'Where the stop moves after trigger.',
        'full-tp-lock': 'Blocks new entries after daily profit target.',
        'tr-session-limit': 'One filled opportunity per zone/direction per session.',
        'tr-allowed-sessions': 'Trend market segment filter.',
    };
    const standalone = {
        'username': 'Topstep / ProjectX login email.',
        'apikey': 'ProjectX API key.',
    };
    const apply = (id, tip) => {
        const el = document.getElementById(id);
        const group = el ? el.closest('.form-group') : null;
        addHelpDot(group ? group.querySelector('label') : null, tip);
    };
    Object.entries(shared).forEach(([base, tip]) => {
        apply(base + '-bt', tip);
        apply(base + '-live', tip);
    });
    Object.entries(standalone).forEach(([id, tip]) => apply(id, tip));
    migrateInlineHelp();
    getHelpTooltip();
}

document.addEventListener('click', (event) => {
    if (!event.target.closest || !event.target.closest('.help-dot')) closeHelpTooltip();
});

// -- Init ------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    initChart();
    initPresetDirtyTracking();
    decorateParamHelpDots();
    checkHealth();
    setInterval(checkHealth, HEALTH_CHECK_INTERVAL_MS);
    const envConfigReady = loadEnvConfig();
    updateClock();
    setInterval(updateClock, 1000);

    ['username', 'apikey'].forEach(id => {
        const input = document.getElementById(id);
        if (!input) return;
        input.addEventListener('input', () => {
            if (id === 'username') updateConnectionInitial(input.value);
            _refreshConnectionState({ credentialsChanged: true });
        });
    });

    updateConnectionInitial(document.getElementById('username')?.value || '');
    startConnectionStatusMonitor();

    // Full-range mode: no manual dates. END = today, START = far past so the
    // paginated fetch walks back to the contract's earliest available bar.
    const today = new Date();
    document.getElementById('start-date').value = FULL_RANGE_START;
    document.getElementById('end-date').value = today.toISOString().slice(0, 10);
    ['bt', 'live'].forEach(mode => {
        syncSizeOptions(mode);
        updateTrailBounds(mode);
        updateStrategyParamVisibility(mode);
    });

    // Auto-connect after env config loads
    setTimeout(async () => {
        try { await envConfigReady; } catch (e) {}
        const username = document.getElementById('username').value.trim();
        const connectionReady = _refreshConnectionState() !== 'error';
        if (username && connectionReady) {
            connectAPI();
        } else {
            // No .env config — open dropdown for manual entry
            toggleConnDropdown(true);
        }
    }, 500);

    // Tab switching (BACKTEST / LIVE MONITOR)
    document.querySelectorAll('.tab').forEach(t => {
        t.onclick = () => {
            document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
            t.classList.add('active');
            const tab = t.dataset.tab;
            const backtestPanels = document.getElementById('backtest-config-panel');
            const metricsPanel = document.getElementById('metrics-panel');
            const livePanel = document.getElementById('live-panel');
            const liveTopBar = document.getElementById('live-top-bar');
            const calView = document.getElementById('calendar-view');
            const mainEl = document.querySelector('.main');
            // 1.0.9: 切到非 account 分頁 → 停掉帳號頁狀態輪詢
            // 1.0.9: 切到非 live 分頁 → 停掉兩帳號槽輪詢
            if (tab !== 'live' && _liveSlotInterval) { clearInterval(_liveSlotInterval); _liveSlotInterval = null; }
            if (tab !== 'live') _cancelLivePoll(_liveSlotsPollState);
            // Calendar / Account are full-page overlays; any other tab restores .main.
            if (tab === 'calendar') {
                if (mainEl) mainEl.style.display = 'none';
                if (calView) calView.classList.remove('hidden');
                liveTopBar.style.display = 'none';
                renderCalendar();   // 1.0.9: robustness 面板由 renderCalendar 末端刷新
                return;
            }
            if (mainEl) mainEl.style.display = '';
            if (calView) calView.classList.add('hidden');
            if (tab === 'backtest') {
                backtestPanels.classList.remove('hidden');
                // 1.0.9: 切回來時重新算一次標籤。滑桿的 value 在切頁時不會變,
                // 但標籤 span 是 JS 寫上去的,任何一次 applyStrategyParams 都可能
                // 把它蓋回 OFF —— 送出值一直是對的,只有顯示會騙人。
                try { refreshCapsForContract('bt'); } catch (e) {}
                if (metricsPanel.style.display === 'block') metricsPanel.classList.remove('hidden');
                livePanel.classList.add('hidden');
                liveTopBar.style.display = 'none';
                refreshPiSignalMarkers();
            } else if (tab === 'live') {
                backtestPanels.classList.add('hidden');
                try { refreshCapsForContract('live'); } catch (e) {}
                metricsPanel.classList.add('hidden');
                livePanel.classList.remove('hidden');
                if (_liveInterval || _liveStatusInterval) liveTopBar.style.display = 'block';
                updateLiveTopBar();
                // Browser timers/fetches may have been throttled while Research,
                // Backtest, or a hidden tab was active.  Cancel the old generation
                // and query the now-visible Live destination immediately.
                pollLiveStatus({ restart: true });
                pollLiveSlots({ restart: true });
                // 1.0.9: 兩帳號槽 —— 填入 + 每 2s 更新各槽狀態
                try { initLiveSlots(); } catch (e) {}
                if (!_liveSlotInterval) _liveSlotInterval = setInterval(pollLiveSlots, 2000);
                // Live PI audit rows are separate from the immutable history
                // used by backtest.  Refresh them when this tab becomes visible.
                refreshPiSignalMarkers();
            }
        };
    });
    document.querySelectorAll('.bottom-tab').forEach(t => {
        t.onclick = () => {
            document.querySelectorAll('.bottom-tab').forEach(x => x.classList.remove('active'));
            t.classList.add('active');
            const tab = t.dataset.btab;
            ['trades', 'execute', 'log'].forEach(id => {
                const panel = document.getElementById('btab-' + id);
                if (panel) panel.classList.toggle('hidden', id !== tab);
            });
            _animateBottomPane(document.getElementById('btab-' + tab));
            if (tab === 'log') scrollSystemLogToBottom();
            if (tab === 'execute') {
                revealNewestExecuteTrade();
                refreshVisibleExecuteTrades(true);
            }
            glassResample();   // 1.0.10 #1:面板剛換,取樣還是舊分頁的內容
        };
    });
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) return;
        const active = document.querySelector('.tab.active');
        if (active && (active.dataset.tab === 'live' || active.dataset.tab === 'backtest')) {
            refreshPiSignalMarkers();
        }
        if (active && active.dataset.tab === 'live') {
            pollLiveStatus({ restart: true });
            pollLiveSlots({ restart: true });
            refreshVisibleExecuteTrades(true);
        }
    });
});

async function loadEnvConfig() {
    try {
        const resp = await fetch(API + '/config');
        const cfg = await resp.json();
        updateConnectionInitial(cfg.username || '');
        Object.assign(SYSTEM_TIME_ZONES, cfg.time_zones || {});
        Object.assign(FRONT_MONTH_CONTRACTS, cfg.front_month_contracts || {});
        Object.assign(SYSTEM_CONTRACT_SPECS, cfg.contract_specs || {});
        MARKET_CLOCK_VERSION = cfg.market_clock_version || MARKET_CLOCK_VERSION;
        DEFAULT_STRATEGY_PARAMS.market_clock_version = MARKET_CLOCK_VERSION;
        DEFAULT_STRATEGY_PARAMS.contract_id = defaultContractId();
        Object.values(_appliedStrategyParamsByMode).forEach(params => {
            params.market_clock_version = MARKET_CLOCK_VERSION;
            if (!params.contract_id || /^[A-Z]+$/.test(params.contract_id)) {
                params.contract_id = defaultContractId();
            }
        });
        refreshContractOptions();
        const apiKeyInput = document.getElementById('apikey');
        if (apiKeyInput) {
            apiKeyInput.dataset.configured = (
                cfg.has_api_key || (cfg.env_loaded && cfg.api_key_preview)
            ) ? '1' : '0';
        }

        if (cfg.env_loaded) {
            document.getElementById('username').value = cfg.username;
            document.getElementById('apikey').placeholder = cfg.api_key_preview + ' (from .env)';
            document.getElementById('apikey').value = '';
            const contractInput = document.getElementById('contract-id');
            if (contractInput) {
                contractInput.value = contractUiValue(
                    cfg.contract_id || defaultContractId()
                ) || 'MNQ';
            }
            log('.env loaded: username=' + cfg.username + ', key=' + cfg.api_key_preview, 'success');
            log('Credentials from .env -- click CONNECT to fetch data', 'info');

            // Accounts loaded after connect, not here
        } else {
            log('.env not configured -- enter credentials manually', 'warn');
        }
        _refreshConnectionState({ credentialsChanged: true });
    } catch(e) {
        log('Could not load .env config: ' + e.message, 'warn');
        setStatus('err', 'CONFIG ERROR');
    }
}

// -- Account Switcher --

async function loadAccounts() {
    try {
        const resp = await fetch(API + '/accounts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const data = await resp.json();
        if (!data.success || !data.accounts || data.accounts.length === 0) return;

        allAccounts = data.accounts;

        // Default to practice
        const practice = data.accounts.find(a => a.is_practice);
        currentAccount = (data.accounts.find(a => a.is_main) || practice || data.accounts[0]);
        updateAccountBadge();
        setTimeout(_focusMainLiveAccount, 0);
        try { initLiveSlots(); } catch (e) {}   // 1.0.9: 填入兩帳號槽

        // Populate live monitor account dropdown
        const liveSelect = document.getElementById('live-account-select');
        if (liveSelect) {
            liveSelect.innerHTML = '';
            data.accounts.forEach(acc => {
                const opt = document.createElement('option');
                opt.value = acc.id;
                const label = acc.is_practice ? 'PRACTICE' : 'FUNDED';
                opt.textContent = acc.name + ' [$' + acc.balance.toLocaleString(undefined, {maximumFractionDigits: 0}) + '] ' + label;
                liveSelect.appendChild(opt);
            });
            // Restore saved account selection, fallback to practice
            const savedAccId = localStorage.getItem('ancser_live_account_id');
            if (savedAccId && data.accounts.find(a => a.id == savedAccId)) {
                liveSelect.value = savedAccId;
            } else if (practice) {
                liveSelect.value = practice.id;
            }
            document.getElementById('btn-go-live').disabled = false;
            onLiveAccountSwitch();
        }

        log('Accounts loaded: ' + data.accounts.map(a => a.name).join(', '), 'success');
    } catch(e) {
        // silently fail
    }
}

let liveAccount = null;
const LIVE_MAIN_SLOT = 1;

function liveSlotLabel(slot) {
    return 'ACCOUNT MAIN';
}

function _liveSlotAccountId(slot) {
    const el = document.getElementById('live-acct-select-' + slot);
    const id = parseInt((el && el.value) || '', 10);
    return Number.isFinite(id) ? id : 0;
}

function getMainLiveAccountId() {
    return _liveSlotAccountId(LIVE_MAIN_SLOT);
}

function getMainLiveAccount() {
    const slotEl = document.getElementById('live-acct-select-' + LIVE_MAIN_SLOT);
    const id = getMainLiveAccountId();
    if (id) return (allAccounts || []).find(a => Number(a.id) === Number(id)) || null;
    if (!slotEl) return currentAccount || liveAccount || null;
    return null;
}

function _renderLiveAccountInfo(acc, capitalOverride) {
    const info = document.getElementById('live-account-info');
    if (!info) return;
    if (!acc) { info.innerHTML = ''; return; }
    const isPractice = !!acc.is_practice || String(acc.account_type || '').toLowerCase() === 'practice';
    const type = isPractice ? 'PRACTICE' : 'FUNDED';
    const capital = capitalOverride != null ? Number(capitalOverride) : Number(acc.balance || 0);
    info.innerHTML = '<span style="color:var(--text2);">' + type + '</span> | Balance: <span style="color:var(--green);">$' +
        capital.toLocaleString(undefined, {maximumFractionDigits: 0}) + '</span>';
    if (!isPractice) info.innerHTML += '<br><span style="color:var(--red);">WARNING: FUNDED ACCOUNT</span>';
}

function _focusMainLiveAccount() {
    const acc = getMainLiveAccount();
    if (!acc) return null;
    liveAccount = acc;
    currentAccount = acc;
    updateAccountBadge();
    _renderLiveAccountInfo(acc);
    const legacy = document.getElementById('live-account-select');
    if (legacy) {
        const hasOption = Array.from(legacy.options || []).some(o => String(o.value) === String(acc.id));
        if (hasOption) legacy.value = String(acc.id);
    }
    return acc;
}

function getMainLivePresetParams(fallback) {
    const sel = document.getElementById('live-acct-preset-' + LIVE_MAIN_SLOT);
    const name = (sel && sel.value) || '';
    const presets = (_presetsCache && _presetsCache.presets) || {};
    return (name && presets[name]) ? presets[name] : (fallback || {});
}

function onLiveAccountSwitch() {
    const focused = _focusMainLiveAccount();
    if (focused) return;
    const id = parseInt(document.getElementById('live-account-select').value);
    liveAccount = allAccounts.find(a => a.id === id) || null;
    // Save selection
    if (id) localStorage.setItem('ancser_live_account_id', id);
    // Sync header badge
    currentAccount = liveAccount;
    updateAccountBadge();

    _renderLiveAccountInfo(liveAccount);
}

let _liveInterval = null;
let _liveStatusInterval = null;
let _liveStartInProgress = false;
const LIVE_STATUS_TIMEOUT_MS = 3000;
const _liveStatusPollState = {
    generation: 0,
    inFlight: null,
    controller: null,
    lastGood: null,
    lastGoodAccountId: null,
};
const _liveSlotsPollState = {
    generation: 0,
    inFlight: null,
    controller: null,
    lastGood: null,
};

function _cancelLivePoll(state) {
    state.generation += 1;
    if (state.controller) state.controller.abort();
    state.controller = null;
    state.inFlight = null;
}

async function _fetchJsonWithTimeout(url, controller) {
    const timer = setTimeout(() => controller.abort(), LIVE_STATUS_TIMEOUT_MS);
    try {
        const response = await fetch(url, { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return await response.json();
    } finally {
        clearTimeout(timer);
    }
}

function _runBoundedLivePoll(state, url, options, onSuccess, onFailure) {
    const restart = !!(options && options.restart);
    if (restart) _cancelLivePoll(state);
    if (state.inFlight) return state.inFlight;

    const generation = ++state.generation;
    const controller = new AbortController();
    state.controller = controller;
    let task = null;
    task = (async () => {
        try {
            const payload = await _fetchJsonWithTimeout(url, controller);
            if (generation !== state.generation) return;
            await onSuccess(payload);
        } catch (error) {
            // A restarted/aborted older request must never overwrite the newer
            // account or workspace.  A timeout on the current request is real
            // uncertainty and is rendered as STATUS STALE below.
            if (generation === state.generation) onFailure(error);
        } finally {
            if (state.inFlight === task) {
                state.inFlight = null;
                state.controller = null;
            }
        }
    })();
    state.inFlight = task;
    return task;
}

function getMarketSession() {
    // NQ CME Globex — all times converted to ET (New York)
    // EDT (Mar-Nov): ET = UTC-4, CDT (Mar-Nov): CT = UTC-5
    // EST (Nov-Mar): ET = UTC-5, CST (Nov-Mar): CT = UTC-6
    // Schedule in ET:
    //   盤前: 18:00 - 09:30 ET (next day)
    //   開盤: 09:30 - 16:00 ET
    //   盤後: 16:00 - 17:00 ET
    //   維護: 17:00 - 18:00 ET
    //   休市: Fri 17:00 - Sun 18:00
    const now = new Date();
    const local = _newYorkParts(now.getTime());
    const dayCodes = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
    const etMinutes = local.hour * 60 + local.minute;
    const etDay = dayCodes[local.weekday];

    // Weekend: Sat all day, Sun before 18:00 ET, Fri after 17:00 ET
    if (etDay === 6) return { label: 'CLOSED', color: 'var(--text3)' };
    if (etDay === 0 && etMinutes < 18 * 60) return { label: 'CLOSED', color: 'var(--text3)' };
    if (etDay === 5 && etMinutes >= 17 * 60) return { label: 'CLOSED', color: 'var(--text3)' };

    // 維護 17:00-18:00 ET
    if (etMinutes >= 17 * 60 && etMinutes < 18 * 60) {
        return { label: 'CLOSED', color: 'var(--text3)' };
    }

    // 1.0.8: 市場開盤時,改用與全 App 一致的交易盤段分類 (ASIA/EURO/PRE/RTH/AH),
    // 而非舊的 NORMAL/AFTER/PRE 市場狀態,避免與策略 allowed_sessions / 圖表底色命名衝突。
    const code = getSessionCodeFromDate(now);
    const color = SESSION_BADGE_COLORS[code] || 'var(--amber)';
    return { label: code, color };
}

// 1.0.8: 交易盤段徽章配色 (對齊 getSessionCodeFromDate 的 5 個代碼)
const SESSION_BADGE_COLORS = {
    ASIA: 'var(--amber)',
    EURO: 'var(--cyan)',
    PRE:  '#c491ff',
    RTH:  'var(--green)',
    AH:   'var(--text3)',
};

function updateLiveTopBar() {
    const session = getMarketSession();
    const el = document.getElementById('lv-session');
    if (el) { el.textContent = session.label; el.style.color = session.color; }
    const elM = document.getElementById('lv-market-session');
    if (elM) { elM.textContent = session.label; elM.style.color = session.color; }

    // Strategy label
    const stratEl = document.getElementById('lv-strategy');
    if (stratEl) {
        const s = (window._liveStrategyName || '--').toUpperCase();
        stratEl.textContent = s;
    }

    const topAccount = getMainLiveAccount() || currentAccount;
    if (topAccount) {
        document.getElementById('lv-capital').textContent = '$' + Number(topAccount.balance || 0).toLocaleString(undefined, {maximumFractionDigits: 0});
    }
}

async function goLive() {
    if (!liveAccount) { log('Select an account first', 'warn'); return; }

    // Lock button immediately to prevent double-click
    const goBtn = document.getElementById('btn-go-live');
    if (goBtn.disabled) return;
    goBtn.disabled = true;
    _liveStartInProgress = true;
    const stopBtn = document.getElementById('btn-stop-live');
    if (stopBtn) stopBtn.disabled = true;
    const flattenBtn = document.getElementById('btn-flatten');
    if (flattenBtn) flattenBtn.disabled = true;

    const statusEl = document.getElementById('live-status-text');
    statusEl.style.color = 'var(--amber)';
    statusEl.textContent = 'STARTING...';
    log('GO LIVE: account=' + liveAccount.name + ' (' + (liveAccount.is_practice ? 'practice' : 'FUNDED') + ')', 'info');
    const stratParams = collectStrategyParams('live');

    // v1.0.6: ML (confluence, explainable) is selected via the STRATEGY dropdown.
    // No shadow mode in live — practice account places real orders.
    const confParams = collectConfluenceParams('live');
    if (false && confParams) {
        stratParams.strategy = 'confluence';
        stratParams.conf_shadow = false;
        Object.assign(stratParams, confParams);
        const gateTxt = (stratParams.conf_ev_floor != null)
            ? ('EV≥' + stratParams.conf_ev_floor + ' (EV priority)')
            : ('minProb=' + stratParams.conf_min_prob);
        const rrTxt = (Array.isArray(stratParams.conf_rr_grid) && stratParams.conf_rr_grid.length)
            ? ('rrGrid=' + stratParams.conf_rr_grid.join('/') + ' (EV selection)')
            : ('rr=' + stratParams.conf_rr);
        log('ML CONFLUENCE: LIVE (places orders) base=1m ' + gateTxt
            + ' ' + rrTxt + ' band=' + stratParams.conf_band_ticks
            + ' minTF=' + stratParams.conf_min_distinct_tf
            + ' SLref=' + (stratParams.conf_sl_reference_tf || 'largest')
            + ' market=' + allowedSessionsLabel(stratParams.conf_allowed_sessions), 'info');
    }
    // 1.0.8: 移除 ml_consolidation_v2 (mlc2) live start 記錄
    if (stratParams.strategy === 'trend') {
        log('TREND: LIVE ' + trendTfUsageText(stratParams)
            + ' RR1:' + stratParams.rr_ratio
            + ' C=' + stratParams.breakout_confirm_bars
            + ' sessionLimit=' + (stratParams.tr_one_trade_per_session ? 'ON' : 'OFF')
            + ' market=' + allowedSessionsLabel(stratParams.tr_allowed_sessions), 'info');
    }
    if (stratParams.strategy === 'sigma') {
        log('DISTRIBUTION: LIVE Roll' + stratParams.sigma_window_minutes
            + ' ' + String(stratParams.sigma_method || 'std').toUpperCase()
            + ' accept=' + stratParams.sigma_accept_mode
            + ' TP=' + stratParams.sigma_target_mode
            + ' SL=' + stratParams.sigma_stop_span + 'sigma'
            + ' market=' + allowedSessionsLabel(stratParams.tr_allowed_sessions), 'info');
    }

    // Show live top bar (chart data stays as-is from connect)
    document.getElementById('live-top-bar').style.display = 'block';
    updateLiveTopBar();

    // ── Start candle polling + status polling (always, even if engine fails) ──
    _lastLiveCandleTime = '';  // reset
    if (_liveInterval) clearInterval(_liveInterval);
    _liveInterval = setInterval(pollLiveCandle, 1000); // every 1s (backend caches API calls)
    pollLiveCandle(); // immediate first poll

    if (_liveStatusInterval) clearInterval(_liveStatusInterval);
    _liveStatusInterval = setInterval(pollLiveStatus, 1000); // every 1s
    pollLiveStatus({ restart: true });

    // Update top bar session info
    updateLiveTopBar();

    // Keep controls locked until /live/start confirms that the engine is running.

    // ── Call /live/start to start the live trading engine ──
    // stratParams now carries contract_id + contract_size (v1.0.6), so the
    // /live/start request reflects whatever the user picked in LIVE PARAMS.
    const liveParams = {
        account_id: liveAccount.id,
        value_area_pct: stratParams.value_area_pct,
        ...stratParams,
    };

    let engineStarted = false;
    try {
        const resp = await fetch(API + '/live/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(liveParams),
        });
        const data = await resp.json();
        if (!resp.ok) {
            log('Trading engine start failed: ' + (data.detail || JSON.stringify(data)), 'warn');
            statusEl.style.color = 'var(--amber)';
            statusEl.textContent = 'MONITOR ONLY';
        } else {
            engineStarted = true;
            log('Trading engine started successfully ✓', 'success');
        }
    } catch(e) {
        log('Trading engine connection failed: ' + e.message + ' (monitor-only mode)', 'warn');
        statusEl.style.color = 'var(--amber)';
        statusEl.textContent = 'MONITOR ONLY';
    }

    if (!engineStarted) _liveStartInProgress = false;

    if (engineStarted) {
        statusEl.style.color = 'var(--amber)';
        statusEl.textContent = 'STARTING...';
        const dot = document.getElementById('live-status-dot');
        if (dot) { dot.style.background = 'var(--amber)'; dot.style.boxShadow = '0 0 6px var(--amber)'; }
        const stopBtn = document.getElementById('btn-stop-live');
        if (stopBtn) stopBtn.disabled = true;
        const flattenBtn = document.getElementById('btn-flatten');
        if (flattenBtn) flattenBtn.disabled = true;
        log('Trading engine started successfully ✓', 'success');
    } else {
        const dot = document.getElementById('live-status-dot');
        if (dot) { dot.style.background = 'var(--amber)'; dot.style.boxShadow = '0 0 6px var(--amber)'; }
        goBtn.disabled = false;
        const stopBtn = document.getElementById('btn-stop-live');
        if (stopBtn) stopBtn.disabled = true;
        const flattenBtn = document.getElementById('btn-flatten');
        if (flattenBtn) flattenBtn.disabled = true;
        log('Monitor-only mode — candles update every second (trading engine not running)', 'info');
    }

    // Auto-fetch real account state
    fetchRealState();
}

async function stopLive() {
    const statusEl = document.getElementById('live-status-text')
        || document.getElementById('lv-status-label');
    _liveStartInProgress = false;
    // Stop the scheduler before POST and invalidate any pre-stop response.
    // A failed stop restarts bounded polling below without claiming STOPPED.
    if (_liveStatusInterval) {
        clearInterval(_liveStatusInterval);
        _liveStatusInterval = null;
    }
    _cancelLivePoll(_liveStatusPollState);
    try {
        const resp = await fetch(API + '/live/stop', { method: 'POST' });
        let data = {};
        try { data = await resp.json(); } catch (e) {}
        if (!resp.ok) {
            throw new Error(data.detail || ('HTTP ' + resp.status));
        }
        // Invalidate again: a poll could have been started by a visibility/tab
        // event while the stop POST was in flight.
        _cancelLivePoll(_liveStatusPollState);
        _liveStatusPollState.lastGood = null;
        _liveStatusPollState.lastGoodAccountId = null;
        log('Trading engine stopped', 'info');
    } catch(e) {
        log('Stop error: ' + e.message, 'error');
        _cancelLivePoll(_liveStatusPollState);
        const statusAccount = _focusMainLiveAccount() || liveAccount;
        const accountId = statusAccount && statusAccount.id ? String(statusAccount.id) : '';
        _markLiveStatusStale(accountId);
        _liveStatusInterval = setInterval(pollLiveStatus, 1000);
        pollLiveStatus({ restart: true });
        return;
    }

    if (_liveInterval) { clearInterval(_liveInterval); _liveInterval = null; }

    const goBtn = document.getElementById('btn-go-live');
    const stopBtn = document.getElementById('btn-stop-live');
    const flattenBtn = document.getElementById('btn-flatten');
    if (goBtn) goBtn.disabled = false;
    if (stopBtn) stopBtn.disabled = true;
    if (flattenBtn) flattenBtn.disabled = true;

    if (statusEl) {
        statusEl.style.color = 'var(--text3)';
        statusEl.textContent = 'STOPPED';
        statusEl.title = '';
    }
    const dot = document.getElementById('live-status-dot');
    if (dot) { dot.style.background = 'var(--text3)'; dot.style.boxShadow = 'none'; }

    // Hide signal row & clean up price lines
    const sigRow = document.getElementById('lv-signal-row');
    if (sigRow) sigRow.style.display = 'none';
    if (window._liveTpLine) { try { candleSeries.removePriceLine(window._liveTpLine); } catch(e){} window._liveTpLine = null; }
    if (window._liveSlLine) { try { candleSeries.removePriceLine(window._liveSlLine); } catch(e){} window._liveSlLine = null; }
    if (window._liveEntryLine) { try { candleSeries.removePriceLine(window._liveEntryLine); } catch(e){} window._liveEntryLine = null; }
    _setLiveRealtimeMarkers([]);
}

async function flattenLive() {
    if (!confirm('Confirm emergency flatten?')) return;
    try {
        const resp = await fetch(API + '/live/flatten', { method: 'POST' });
        const data = await resp.json();
        log('Emergency flatten: ' + (data.message || 'OK'), 'warn');
    } catch(e) {
        log('Flatten error: ' + e.message, 'error');
    }
}

async function fetchRealState() {
    const el = document.getElementById('tpx-real-state');
    el.innerHTML = '<span style="color:var(--amber);">Loading...</span>';
    try {
        const resp = await fetch(API + '/live/account-state');
        if (!resp.ok) {
            const err = await resp.json();
            el.innerHTML = '<span style="color:var(--red);">ERROR: ' + (err.detail || resp.statusText) + '</span>';
            return;
        }
        const data = await resp.json();
        let html = '<div style="color:var(--text2);margin-bottom:4px;">Updated: ' + new Date().toLocaleTimeString() + '</div>';

        // Accounts
        (data.accounts || []).forEach(acc => {
            const isPrac = acc.is_practice;
            html += '<div style="border:1px solid var(--border);padding:6px;margin-bottom:6px;">';
            html += '<div style="color:' + (isPrac ? 'var(--cyan)' : 'var(--red)') + ';font-weight:600;">' + acc.name + '</div>';
            html += '<div>Balance: $' + (acc.balance || 0).toLocaleString() + '</div>';

            // Positions
            const pos = acc.positions || [];
            if (pos.length > 0 && !pos[0].error) {
                html += '<div style="color:var(--amber);margin-top:4px;">POSITIONS (' + pos.length + '):</div>';
                pos.forEach(p => {
                    const sideMeta = positionSideMeta(p);
                    const sideColor = sideMeta.isLong ? 'var(--green)' : 'var(--red)';
                    const symbol = positionContractLabel(p, document.getElementById('contract-live')?.value);
                    html += '<div style="padding-left:8px;">  <span style="color:' + sideColor + ';">' + sideMeta.label + '</span> ' +
                        positionQty(p) + ' ' + symbol + ' @ ' + positionAvgText(p) +
                        ' | PnL: ' + (p.pnl || p.unrealizedPnl || '--') + '</div>';
                });
            } else {
                html += '<div style="color:var(--text3);margin-top:4px;">NO POSITIONS</div>';
            }

            // Orders
            const orders = acc.orders || [];
            if (orders.length > 0 && !orders[0].error) {
                html += '<div style="color:var(--amber);margin-top:4px;">ORDERS (' + orders.length + '):</div>';
                orders.forEach(o => {
                    const side = o.side === 1 ? 'BUY' : (o.side === 2 ? 'SELL' : o.side);
                    const sideColor = o.side === 1 ? 'var(--green)' : 'var(--red)';
                    const typeMap = {1:'Limit', 2:'Market', 4:'Stop', 5:'TrailingStop'};
                    const oType = typeMap[o.type] || o.type || '?';
                    const status = o.status || '?';
                    const statusColor = status === 'Filled' ? 'var(--green)' : (status === 'Open' || status === 'Working' ? 'var(--amber)' : 'var(--text3)');
                    html += '<div style="padding-left:8px;">' +
                        '#' + (o.id || o.orderId || '?') + ' ' +
                        '<span style="color:' + sideColor + ';">' + side + '</span> ' +
                        oType + ' ' + (o.size || o.qty || 1) + ' ' +
                        (o.limitPrice ? 'Lmt=' + o.limitPrice : '') +
                        (o.stopPrice ? ' Stp=' + o.stopPrice : '') +
                        (o.executePrice ? ' Fill=' + o.executePrice : '') +
                        ' <span style="color:' + statusColor + ';">[' + status + ']</span>' +
                        (o.timestamp ? ' ' + new Date(o.timestamp).toLocaleTimeString() : '') +
                        '</div>';
                });
            } else {
                html += '<div style="color:var(--text3);margin-top:4px;">NO ORDERS</div>';
            }

            html += '</div>';
        });

        // Engine state comparison
        if (data.engine) {
            const eng = data.engine;
            html += '<div style="border:1px solid var(--border);padding:6px;margin-bottom:6px;">';
            html += '<div style="color:var(--cyan);font-weight:600;">ENGINE STATE</div>';
            html += '<div>Running: ' + eng.running + '</div>';
            html += '<div>Pending Order ID: ' + (eng.pending_order_id || 'none') + '</div>';
            if (eng.pending_signal) {
                const s = eng.pending_signal;
                html += '<div style="color:var(--amber);">Signal: ' + s.direction + ' @ ' + s.entry.toFixed(2) +
                    ' SL=' + s.sl.toFixed(2) + ' TP=' + s.tp.toFixed(2) + ' [' + s.strategy + ']</div>';
            }
            html += '<div>Candles processed: ' + eng.candles_processed + '</div>';
            if (eng.open_position) {
                html += '<div style="color:var(--green);">Open position: ' + JSON.stringify(eng.open_position) + '</div>';
            }
            // Last 10 log entries
            if (eng.log && eng.log.length > 0) {
                html += '<div style="color:var(--amber);margin-top:4px;">ENGINE LOG (last 10):</div>';
                eng.log.slice(-10).forEach(l => {
                    html += '<div style="padding-left:8px;color:var(--text3);font-size:9px;white-space:pre-wrap;overflow-wrap:anywhere;">' + _acctEsc(l) + '</div>';
                });
            }
            html += '</div>';
        } else {
            html += '<div style="color:var(--text3);">No engine running</div>';
        }

        el.innerHTML = html;
        log('[TPX] Real state loaded: ' + (data.accounts || []).length + ' accounts', 'info');
    } catch(e) {
        el.innerHTML = '<span style="color:var(--red);">FETCH ERROR: ' + e.message + '</span>';
    }
}

// 1.0.9: Live 風控閘狀態列 — 顯示每個「封鎖型」風控限制目前是否生效,讓使用者
// 一眼知道現在是「盤段外 / 已達日限 / 日虧休息 / 波動閘封鎖 / TP 鎖」而沒有進單。
//   綠 = 通行(armed 可交易)、灰 = OFF(未啟用)、紅 = 正在封鎖/休息、琥珀 = 已計數但未達上限。
function renderLiveRiskGates(st) {
    st = st || {};
    const gates = st.risk_gates || {};
    const GREY = 'var(--text3)', GREEN = 'var(--green)', RED = 'var(--red)',
          AMBER = 'var(--amber)', CYAN = 'var(--cyan)';
    const setChip = (id, text, color) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = text;
        el.style.color = color;
        el.style.borderColor = color;
    };

    // ── 盤段 (market session window) — 前端用允許盤段 vs 現在盤段判斷 ──
    const sess = getMarketSession();                       // {label, color}
    const allowedLabel = st.active_allowed_sessions || st.trend_allowed_sessions || '';
    let marketOk, marketText;
    if (sess.label === 'CLOSED') {
        marketOk = false; marketText = 'MARKET CLOSED';
    } else if (!allowedLabel || allowedLabel === 'ALL') {
        marketOk = true; marketText = 'MARKET ' + sess.label + '·ALL';
    } else {
        const allow = String(allowedLabel).split(/[+,]/).map(s => s.trim().toUpperCase());
        marketOk = allow.includes(sess.label);
        marketText = 'MARKET ' + sess.label + (marketOk ? '·ALLOWED' : '·OUTSIDE');
    }
    setChip('lv-rg-market', marketText, marketOk ? GREEN : GREY);

    // ── 日限:每 zone/方向 一單 (session-direction lock 開關) ──
    const sessOn = gates.session_limit ? !!gates.session_limit.on : false;
    setChip('lv-rg-session', 'SESSION LIMIT ' + (sessOn ? 'ON' : 'OFF'), sessOn ? GREEN : GREY);

    // ── 程式虧損鎖:只計 bot-owned 交易，手動交易不消耗額度 ──
    const dl = gates.daily_loss || {};
    let dlText, dlColor;
    if (!dl.limit) { dlText = 'BOT LOSS OFF'; dlColor = GREY; }
    else if (dl.resting) { dlText = 'BOT LOSS LOCKED ' + (dl.count || 0) + '/' + dl.limit; dlColor = RED; }
    else { dlText = 'BOT LOSS ' + (dl.count || 0) + '/' + dl.limit; dlColor = (dl.count || 0) > 0 ? AMBER : GREEN; }
    setChip('lv-rg-dailyloss', dlText, dlColor);

    // ── PREV-RV 波動閘:前一日高波動 → 今日封鎖 ──
    const rv = gates.prev_rv || {};
    let rvText, rvColor;
    if (!rv.lookback) { rvText = 'VOLATILITY OFF'; rvColor = GREY; }
    else if (rv.blocking) { rvText = 'VOLATILITY BLOCKED'; rvColor = RED; }
    else { rvText = 'VOLATILITY ' + rv.lookback + 'D·PASS'; rvColor = CYAN; }
    setChip('lv-rg-prevrv', rvText, rvColor);

    // ── TP 鎖(只在有設定時顯示) ──
    const tp = gates.tp_lock || {};
    const tpChip = document.getElementById('lv-rg-tplock');
    if (tpChip) {
        if (!tp.on) {
            tpChip.style.display = 'none';
        } else {
            tpChip.style.display = '';
            setChip('lv-rg-tplock', tp.locked ? 'TP LOCK LOCKED' : 'TP LOCK ARMED', tp.locked ? RED : GREY);
        }
    }

    // ── 側欄 LIVE STATUS 鏡像(日虧 / 波動閘) ──
    const dlPanel = document.getElementById('live-rg-dailyloss-text');
    if (dlPanel) { dlPanel.textContent = dlText.replace(/^BOT LOSS /, ''); dlPanel.style.color = dlColor; }
    const rvPanel = document.getElementById('live-rg-prevrv-text');
    if (rvPanel) { rvPanel.textContent = rvText.replace(/^VOLATILITY /, ''); rvPanel.style.color = rvColor; }
}

function _markLiveStatusStale(accountId) {
    const last = _liveStatusPollState.lastGoodAccountId === accountId
        ? _liveStatusPollState.lastGood
        : null;
    const statusEl = document.getElementById('live-status-text')
        || document.getElementById('lv-status-label');
    const dot = document.getElementById('live-status-dot');
    if (statusEl) {
        statusEl.style.color = 'var(--amber)';
        statusEl.textContent = _liveStartInProgress
            ? 'STARTING · STATUS STALE'
            : (last && last.running ? 'RUNNING · STATUS STALE' : 'STATUS STALE');
        statusEl.title = 'The last live status request did not complete; showing the last known state.';
    }
    if (dot) {
        dot.style.background = 'var(--amber)';
        dot.style.boxShadow = '0 0 6px var(--amber)';
    }
}

function pollLiveStatus(options) {
    const statusAccount = _focusMainLiveAccount() || liveAccount;
    const accountId = statusAccount && statusAccount.id ? String(statusAccount.id) : '';
    const url = API + '/live/status' + (accountId ? ('?account_id=' + accountId) : '');

    if (_liveStatusPollState.lastGoodAccountId !== accountId) {
        // Never let account B inherit account A's last-known RUNNING when B's
        // first status request times out or fails.
        _liveStatusPollState.lastGood = null;
        _liveStatusPollState.lastGoodAccountId = accountId;
    }

    // Market-session badges are clock state, not server state; keep them live
    // even while a bounded status request is pending.
    const session = getMarketSession();
    const elSession = document.getElementById('lv-session');
    if (elSession) { elSession.textContent = session.label; elSession.style.color = session.color; }
    const elMarket = document.getElementById('lv-market-session');
    if (elMarket) { elMarket.textContent = session.label; elMarket.style.color = session.color; }

    return _runBoundedLivePoll(
        _liveStatusPollState,
        url,
        options,
        (st) => {
            const current = _focusMainLiveAccount() || liveAccount;
            const currentId = current && current.id ? String(current.id) : '';
            if (currentId !== accountId) return;
            _liveStatusPollState.lastGood = st;
            _liveStatusPollState.lastGoodAccountId = accountId;
            _renderLiveStatus(st);
        },
        () => _markLiveStatusStale(accountId),
    );
}

function _renderLiveStatus(st) {
    // Always update market session (even without engine)
    const session = getMarketSession();
    const elSession = document.getElementById('lv-session');
    if (elSession) { elSession.textContent = session.label; elSession.style.color = session.color; }
    const elMarket = document.getElementById('lv-market-session');
    if (elMarket) { elMarket.textContent = session.label; elMarket.style.color = session.color; }

    try {
        renderLiveRiskGates(st);   // 1.0.9: 風控閘狀態列(running / stopped 皆更新)
        if (!st.running) {
            if (_liveStartInProgress) {
                const statusEl = document.getElementById('live-status-text')
                    || document.getElementById('lv-status-label');
                if (statusEl) {
                    statusEl.style.color = 'var(--amber)';
                    statusEl.textContent = 'STARTING...';
                    statusEl.title = '';
                }
                const dot = document.getElementById('live-status-dot');
                if (dot) {
                    dot.style.background = 'var(--amber)';
                    dot.style.boxShadow = '0 0 6px var(--amber)';
                }
                const phaseText = st.phase || 'BUILDING ZONES...';
                const panelPhase = document.getElementById('live-position-text');
                if (panelPhase) panelPhase.textContent = phaseText;
                const phaseTopEl = document.getElementById('lv-phase-top');
                if (phaseTopEl) {
                    phaseTopEl.textContent = phaseText;
                    phaseTopEl.style.color = 'var(--amber)';
                }
                const goBtn = document.getElementById('btn-go-live');
                if (goBtn) goBtn.disabled = true;
                const stopBtn = document.getElementById('btn-stop-live');
                if (stopBtn) stopBtn.disabled = true;
                const flattenBtn = document.getElementById('btn-flatten');
                if (flattenBtn) flattenBtn.disabled = true;
                return;
            }
            const statusEl = document.getElementById('live-status-text')
                || document.getElementById('lv-status-label');
            if (statusEl) {
                statusEl.style.color = 'var(--text3)';
                statusEl.textContent = 'STOPPED';
                statusEl.title = '';
            }
            const dot = document.getElementById('live-status-dot');
            if (dot) {
                dot.style.background = 'var(--text3)';
                dot.style.boxShadow = 'none';
            }

            const phaseText = st.auto_oco_fail_safe_triggered
                ? 'AUTO OCO missing - engine stopped'
                : (st.phase || 'Engine stopped');
            const panelPhase = document.getElementById('live-position-text');
            if (panelPhase) panelPhase.textContent = phaseText;
            const phaseTopEl = document.getElementById('lv-phase-top');
            if (phaseTopEl) {
                phaseTopEl.textContent = phaseText;
                phaseTopEl.style.color = st.auto_oco_fail_safe_triggered ? 'var(--red)' : 'var(--text3)';
            }
            const posEl = document.getElementById('lv-position');
            if (posEl) {
                posEl.textContent = st.position ? 'CHECK BROKER' : 'FLAT';
                posEl.style.color = st.position ? 'var(--red)' : 'var(--text2)';
            }

            const pnlInfo = liveDisplayedDailyPnl(st);
            const pnl = pnlInfo.pnl || 0;
            const pnlEl = document.getElementById('live-pnl-text');
            if (pnlEl) {
                pnlEl.textContent = '$' + (pnl >= 0 ? '+' : '-') + Math.abs(pnl).toFixed(0);
                pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
            }
            const capEl = document.getElementById('lv-capital');
            if (capEl && st.capital !== undefined && st.capital !== null) {
                const pnlStr = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(0);
                const pnlColor = pnl >= 0 ? 'var(--green)' : 'var(--red)';
                capEl.innerHTML =
                    '$' + Number(st.capital).toLocaleString(undefined, {maximumFractionDigits: 0}) +
                    ' <span style="color:' + pnlColor + ';font-size:10px;">(' + pnlStr + ')</span>';
                capEl.title = 'Daily PnL source: ' + pnlInfo.source
                    + ' / Topstep day ' + (pnlInfo.day || '')
                    + (pnlInfo.count ? (' / ' + pnlInfo.count + ' closes') : '');
            }
            const sigRow = document.getElementById('lv-signal-row');
            if (sigRow) sigRow.style.display = 'none';
            const goBtn = document.getElementById('btn-go-live');
            if (goBtn && liveAccount) goBtn.disabled = false;
            const stopBtn = document.getElementById('btn-stop-live');
            if (stopBtn) stopBtn.disabled = true;
            const flattenBtn = document.getElementById('btn-flatten');
            if (flattenBtn) flattenBtn.disabled = true;
            return;
        }

        const isStarting = st.health === 'starting' || st.starting === true;
        if (!isStarting) _liveStartInProgress = false;
        const isDegraded = !isStarting && (st.health === 'degraded'
            || st.disconnected
            || st.task_alive === false
            || (st.strategy_mode === 'pi' && st.pi_listener_alive === false));
        const statusEl = document.getElementById('live-status-text')
            || document.getElementById('lv-status-label');
        if (statusEl) {
            statusEl.style.color = (isStarting || isDegraded) ? 'var(--amber)' : 'var(--green)';
            statusEl.textContent = isStarting
                ? 'STARTING...'
                : (isDegraded ? 'RUNNING · DEGRADED' : 'RUNNING');
            statusEl.title = isDegraded
                ? ((st.health_reasons || []).join(', ') || 'Live engine health is degraded')
                : '';
        }
        const dot = document.getElementById('live-status-dot');
        if (dot) {
            dot.style.background = (isStarting || isDegraded) ? 'var(--amber)' : 'var(--green)';
            dot.style.boxShadow = (isStarting || isDegraded)
                ? '0 0 6px var(--amber)'
                : '0 0 6px var(--green)';
        }
        const stopBtn = document.getElementById('btn-stop-live');
        if (stopBtn) stopBtn.disabled = isStarting;
        const flattenBtn = document.getElementById('btn-flatten');
        if (flattenBtn) flattenBtn.disabled = isStarting;

        // Show engine version in console for debugging
        if (st.engine_version && !window._loggedVersion) {
            log('[ENGINE] ' + st.engine_version, 'info');
            window._loggedVersion = true;
        }

        // ── Top bar: Strategy ──
        const stratTopEl = document.getElementById('lv-strategy');
        if (stratTopEl) {
            const rawStrategy = st.strategy_mode || collectStrategyParams('live').strategy || '--';
            const sn = rawStrategy === '--' ? '--' : strategyDisplayName(rawStrategy);
            stratTopEl.textContent = sn;
            stratTopEl.style.color = 'var(--cyan)';
            window._liveStrategyName = sn;
        }

        // ── Top bar: ML (confluence) decision-basis banner ──
        const confRow = document.getElementById('lv-confluence-row');
        if (confRow) {
            // 1.0.8: 移除 mlc2 分支,只保留 confluence
            const sigs = st.confluence_signals || [];
            if (st.confluence_mode && sigs.length) {
                const last = sigs[sigs.length - 1];
                const basis = last.basis || (
                    (last.mode ? '[' + last.mode + '] ' : '') + (last.direction || '') + ' ' + (last.side || '')
                    + ' entry=' + last.entry + ' sl=' + last.sl + ' tp=' + last.tp
                    + ' prob=' + (last.prob != null ? last.prob.toFixed(2) : '?')
                );
                const bannerTag = st.confluence_shadow ? 'SHADOW · ' : '';
                const bannerScorer = st.confluence_scorer ? (' · scorer=' + st.confluence_scorer) : '';
                document.getElementById('lv-conf-basis').textContent = bannerTag + basis + bannerScorer;
                confRow.style.display = 'flex';
            } else {
                confRow.style.display = 'none';
            }
        }

        // ── Top bar: Active zone levels (text) ──
        const zoneRow = document.getElementById('lv-zone-row');
        if (zoneRow && st.zones && st.zones.length > 0) {
            // Find the active zone (or the most recent one)
            const activeZone = st.zones.find(z => z.status === 'active') || st.zones[st.zones.length - 1];
            if (activeZone) {
                zoneRow.style.display = 'flex';
                document.getElementById('lv-z-vah').textContent = activeZone.vah_80.toFixed(2);
                document.getElementById('lv-z-poc').textContent = activeZone.poc.toFixed(2);
                document.getElementById('lv-z-val').textContent = activeZone.val_80.toFixed(2);
                document.getElementById('lv-z-h100').textContent = activeZone.high_100.toFixed(2);
                document.getElementById('lv-z-l100').textContent = activeZone.low_100.toFixed(2);
                document.getElementById('lv-z-id').textContent = activeZone.zone_id + ' (' + activeZone.num_candles + ' bars)';
            }
        } else if (zoneRow) {
            zoneRow.style.display = 'none';
        }

        // ── Top bar: Position ──
        const posEl = document.getElementById('lv-position');
        if (st.position) {
            const sideMeta = positionSideMeta(st.position);
            const symbol = positionContractLabel(st.position, st.contract_id || document.getElementById('contract-live')?.value);
            const avgFromPosition = positionAvgText(st.position);
            const fillAvg = Number(st.fill_price);
            const avgText = avgFromPosition !== '?' ? avgFromPosition : (Number.isFinite(fillAvg) ? fillAvg.toFixed(2) : '?');
            posEl.textContent = sideMeta.label + ' ' + positionQty(st.position) + ' ' + symbol + ' @ ' + avgText;
            posEl.style.color = sideMeta.isLong ? 'var(--green)' : 'var(--red)';
        } else if (st.pending_order_id) {
            const age = st.pending_age || 0;
            const timeout = st.pending_timeout || 30;
            posEl.textContent = 'PENDING (' + age + '/' + timeout + ' min)';
            posEl.style.color = 'var(--amber)';
        } else {
            posEl.textContent = 'FLAT';
            posEl.style.color = 'var(--text2)';
        }

        // ── Capital & PnL ──
        // st.capital = real balance from API (already includes today's PnL)
        // st.daily_pnl = today's realized PnL from trade history.
        if (st.capital) {
            const pnlInfo = liveDisplayedDailyPnl(st);
            const pnl = pnlInfo.pnl || 0;
            const pnlStr = (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(0);
            const pnlColor = pnl >= 0 ? 'var(--green)' : 'var(--red)';
            const counts = st.full_tp_counts || {};
            const locks = st.full_tp_locks || {};
            const lockedParts = [];
            if ((locks.trend || 0) > 0 && (counts.tr || 0) >= locks.trend) {
                lockedParts.push('TR ' + (counts.tr || 0) + '/' + locks.trend);
            }
            const lockBadge = st.tp_locked
                ? ' <span style="color:var(--amber);font-size:9px;letter-spacing:1px;">TP LOCK ' + (lockedParts.length ? lockedParts.join(' ') : ((st.full_tp_count || 0) + '/' + (st.full_tp_lock || 0))) + '</span>'
                : '';
            const dcBadge = st.disconnected
                ? ' <span style="color:var(--red);font-size:9px;letter-spacing:1px;animation:blink 1s infinite;">⚠ OFFLINE</span>'
                : '';
            document.getElementById('lv-capital').innerHTML =
                '$' + st.capital.toLocaleString(undefined, {maximumFractionDigits: 0}) +
                ' <span style="color:' + pnlColor + ';font-size:10px;">(' + pnlStr + ')</span>' + lockBadge + dcBadge;
            document.getElementById('lv-capital').title =
                'Daily PnL source: ' + pnlInfo.source
                + ' / Topstep day ' + (pnlInfo.day || '')
                + (pnlInfo.count ? (' / ' + pnlInfo.count + ' closes') : '');

            // Update account info badge with live balance
            const infoEl = document.getElementById('live-account-info');
            if (infoEl && liveAccount) {
                const type = liveAccount.is_practice ? 'PRACTICE' : 'FUNDED';
                let infoHtml = '<span style="color:var(--text2);">' + type + '</span> | Balance: <span style="color:var(--green);">$' + st.capital.toLocaleString(undefined, {maximumFractionDigits: 0}) + '</span>';
                if (!liveAccount.is_practice) {
                    infoHtml += '<br><span style="color:var(--red);">WARNING: FUNDED ACCOUNT</span>';
                }
                infoEl.innerHTML = infoHtml;
            }
        }

        // MODE = execution/sub-mode, kept distinct from STRAT (no duplication).
        //   ML (confluence): 影子(不下單) vs 實盤  ← shadow gate
        //   Trend: the active sub-mode, only when it differs from the strategy name
        const isMLmode = (st.strategy_mode === 'confluence') || st.confluence_mode;
        let modeText, modeColor;
        if (isMLmode) {
            modeText = st.confluence_shadow ? 'SHADOW (NO ORDERS)' : 'LIVE';
            modeColor = st.confluence_shadow ? 'var(--amber)' : 'var(--green)';
        } else {
            const amRaw = st.active_mode || '';
            const snRaw = st.strategy_mode || '';
            const am = amRaw ? strategyDisplayName(amRaw) : '';
            const sn = snRaw ? strategyDisplayName(snRaw) : '';
            modeText = (am && am !== sn) ? am : 'LIVE';
            modeColor = 'var(--green)';
        }
        const modeTopEl = document.getElementById('lv-mode');
        const modePanelEl = document.getElementById('live-mode-text');
        if (modeTopEl) { modeTopEl.textContent = modeText; modeTopEl.style.color = modeColor; }
        if (modePanelEl) {
            modePanelEl.textContent = modeText;
            modePanelEl.style.color = modeColor;
        }

        // ── Phase — both top bar and left panel ──
        let phaseText = st.phase || '--';
        const phaseDisplayText = phaseText + (st.tp_locked ? ' TPLOCK' : '');
        const isMLStatus = (st.strategy_mode === 'confluence') || st.confluence_mode;
        const panelPhaseEl = document.getElementById('live-position-text');
        if (panelPhaseEl) {
            panelPhaseEl.textContent = phaseDisplayText;
            panelPhaseEl.style.color = st.tp_locked
                ? 'var(--amber)'
                : (isMLStatus ? 'var(--text1)' : 'var(--text3)');
        }
        // Status-line label adapts to the active strategy (ML 狀態 / TREND 狀態)
        const statusLabelEl = document.getElementById('lv-status-label');
        if (statusLabelEl) {
            const strategyStatusLabel = isMLStatus
                ? 'ML STATUS'
                : (strategyDisplayName(st.strategy_mode || collectStrategyParams('live').strategy) + ' STATUS');
            statusLabelEl.textContent = isStarting
                ? strategyStatusLabel + ' · STARTING'
                : (isDegraded ? strategyStatusLabel + ' · DEGRADED' : strategyStatusLabel);
            statusLabelEl.style.color = (isStarting || isDegraded) ? 'var(--amber)' : '';
            statusLabelEl.title = isDegraded
                ? ((st.health_reasons || []).join(', ') || 'Live engine health is degraded')
                : '';
        }
        const phaseTopEl = document.getElementById('lv-phase-top');
        if (phaseTopEl) {
            phaseTopEl.textContent = phaseDisplayText;
            // Color based on phase
            if (st.tp_locked) phaseTopEl.style.color = 'var(--amber)';
            else if (/交易中|持倉|TRADE/i.test(phaseText)) phaseTopEl.style.color = 'var(--green)';
            else if (/突破中|出界|break/i.test(phaseText)) phaseTopEl.style.color = 'var(--amber)';
            else if (/盤整|區間內/i.test(phaseText)) phaseTopEl.style.color = 'var(--cyan)';
            else phaseTopEl.style.color = isMLStatus ? 'var(--cyan)' : 'var(--text3)';
        }

        // ── ML level-universe overlay (chart bottom-right) ──
        // Vertical list of every recent zone per TF (4h, 4h-1, … 2h, 2h-1 …) with
        // its confluence weight + signed distance to price. TFs in the chosen
        // cluster are highlighted green so you see exactly what fed the decision.
        const levelsPanel = document.getElementById('lv-levels-panel');
        const levelsBody = document.getElementById('lv-levels-body');
        if (levelsPanel && levelsBody) {
            const isMLuniv = (st.strategy_mode === 'confluence') || st.confluence_mode;
            const universe = st.confluence_universe || [];
            if (isMLuniv && universe.length) {
                const sigs = st.confluence_signals || [];
                const lastSig = sigs.length ? sigs[sigs.length - 1] : null;
                const clusterTFs = new Set((lastSig && lastSig.tfs) || []);
                const maxW = Math.max.apply(null, universe.map(r => r.weight || 0)) || 1;
                let html = '';
                universe.forEach(r => {
                    const inC = clusterTFs.has(r.tf);
                    const wPct = Math.min(1, (r.weight || 0) / maxW);
                    const wColor = inC ? 'var(--green)'
                        : (wPct > 0.66 ? 'var(--cyan)' : (wPct > 0.33 ? 'var(--text2)' : 'var(--text3)'));
                    const d = r.dist_ticks;
                    const hasD = (d !== null && d !== undefined);
                    const dStr = hasD ? ((d >= 0 ? '+' : '') + d + 't') : '--';
                    const dColor = !hasD ? 'var(--text3)' : (Math.abs(d) <= 12 ? 'var(--amber)' : 'var(--text3)');
                    html += '<div style="display:flex;justify-content:space-between;gap:8px;padding:1px 8px;'
                        + (inC ? 'background:rgba(0,229,160,0.10);' : '') + '">'
                        + '<span style="color:' + wColor + ';min-width:44px;font-weight:' + (inC ? '600' : '400') + ';">' + r.label + '</span>'
                        + '<span style="color:var(--text2);min-width:50px;text-align:right;">W ' + (r.weight != null ? r.weight.toFixed(1) : '--') + '</span>'
                        + '<span style="color:' + dColor + ';min-width:50px;text-align:right;">' + dStr + '</span>'
                        + '</div>';
                });
                levelsBody.innerHTML = html;
                levelsPanel.style.display = 'block';
            } else {
                levelsPanel.style.display = 'none';
            }
        }

        // ── Left panel: PnL ──
        const pnlEl = document.getElementById('live-pnl-text');
        const pnlInfo = liveDisplayedDailyPnl(st);
        const pnl = pnlInfo.pnl || 0;
        pnlEl.textContent = '$' + (pnl >= 0 ? '+' : '-') + Math.abs(pnl).toFixed(0);
        pnlEl.style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
        pnlEl.title = 'Daily PnL source: ' + pnlInfo.source
            + ' / Topstep day ' + (pnlInfo.day || '')
            + (pnlInfo.count ? (' / ' + pnlInfo.count + ' closes') : '');

        // ── Top bar: Signal row (show on pending signal or position) ──
        const sigRow = document.getElementById('lv-signal-row');
        const sig = st.pending_signal;
        if (sig) {
            sigRow.style.display = 'flex';
            const sigDir = String(sig.direction || '').toLowerCase();
            const isLong = sigDir === 'long' || sigDir === 'buy';
            const orderType = String(sig.order_type || 'limit').toUpperCase();
            document.getElementById('lv-sig-direction').textContent = isLong ? '▲ ' + orderType + ' BUY' : '▼ ' + orderType + ' SELL';
            document.getElementById('lv-sig-direction').style.color = isLong ? 'var(--green)' : 'var(--red)';
            const sigSL = sig.original_sl_price != null ? sig.original_sl_price : sig.sl_price;
            const sigTP = sig.original_tp_price != null ? sig.original_tp_price : sig.tp_price;
            document.getElementById('lv-sig-entry').textContent = sig.entry_price.toFixed(2);
            document.getElementById('lv-sig-sl').textContent = Number(sigSL).toFixed(2);
            document.getElementById('lv-sig-tp').textContent = Number(sigTP).toFixed(2);
            const sigStatus = document.getElementById('lv-sig-status');
            if (st.position) {
                const hasSLTP = st.sl_order_id && st.tp_order_id;
                sigStatus.textContent = hasSLTP ? 'FILLED ✓ SL/TP SET' : 'FILLED ✓ SL/TP...';
                sigStatus.style.color = hasSLTP ? 'var(--green)' : 'var(--amber)';
            } else if (st.pending_order_id) {
                const age = st.pending_age || 0;
                const timeout = st.pending_timeout || 30;
                sigStatus.textContent = 'PENDING(' + age + '/' + timeout + 'min)';
                sigStatus.style.color = 'var(--amber)';
            } else {
                sigStatus.textContent = 'WILL SET';
                sigStatus.style.color = 'var(--text3)';
            }

            // An admitted signal is showing — clear any faded candidate preview.
            removeCandidateLines();
            // Do not draw chart price-lines for Entry/SL/TP. The chart overlay
            // only shows the decision zone; exact prices stay in the control UI.
            if (window._liveTpLine) { try { candleSeries.removePriceLine(window._liveTpLine); } catch(e){} window._liveTpLine = null; }
            if (window._liveSlLine) { try { candleSeries.removePriceLine(window._liveSlLine); } catch(e){} window._liveSlLine = null; }
            if (window._liveEntryLine) { try { candleSeries.removePriceLine(window._liveEntryLine); } catch(e){} window._liveEntryLine = null; }
            updateLiveWorkingDecision(sig, isLong, sigSL, sigTP);
        } else {
            sigRow.style.display = 'none';
            // Remove admitted-signal lines when no signal
            if (window._liveTpLine) { try { candleSeries.removePriceLine(window._liveTpLine); } catch(e){} window._liveTpLine = null; }
            if (window._liveSlLine) { try { candleSeries.removePriceLine(window._liveSlLine); } catch(e){} window._liveSlLine = null; }
            if (window._liveEntryLine) { try { candleSeries.removePriceLine(window._liveEntryLine); } catch(e){} window._liveEntryLine = null; }
            // FLAT / no working order: do not draw candidate Entry/SL/TP lines.
            // They are model previews, not live orders, and looked too much like
            // stale SL/TP after manual flatten/restart.
            removeCandidateLines();
            clearLiveWorkingDecision();
        }

        // ── Live realtime markers on chart (pending/open only) ──
        if (candleSeries) {
            if (st.trades) window._lastLiveTradeCount = st.trades.length;
            const liveMarkers = [];

            // Show pending signal as marker
            if (sig) {
                const sigDir = String(sig.direction || '').toLowerCase();
                const isLong = sigDir === 'buy' || sigDir === 'long';
                const pseudoTrade = {
                    direction: isLong ? 'buy' : 'sell',
                    entry_price: sig.entry_price,
                    mode: sig.mode,
                    side: sig.side,
                };
                const decision = _tradeDecisionPhrase(pseudoTrade);
                liveMarkers.push({
                    time: utcMsToChartTime(Date.now()),
                    position: isLong ? 'belowBar' : 'aboveBar',
                    color: '#ffa726',
                    shape: isLong ? 'arrowUp' : 'arrowDown',
                    text: 'PENDING' + (decision ? '\n' + decision : ''),
                });
            }
            // Show filled position as marker
            if (st.position && st.fill_price) {
                const posIsLong = positionSideMeta(st.position).isLong;
                const pseudoTrade = {
                    direction: posIsLong ? 'buy' : 'sell',
                    entry_price: st.fill_price,
                    mode: sig && sig.mode,
                    side: sig && sig.side,
                };
                const decision = _tradeDecisionPhrase(pseudoTrade);
                liveMarkers.push({
                    time: utcMsToChartTime(Date.now()),
                    position: posIsLong ? 'belowBar' : 'aboveBar',
                    color: '#ffa726',
                    shape: posIsLong ? 'arrowUp' : 'arrowDown',
                    text: 'OPEN' + (decision ? '\n' + decision : ''),
                });
            }

            _setLiveRealtimeMarkers(liveMarkers);
        }

        const hasWorkingTrade = !!st.position || !!st.pending_order_id || !!st.pending_signal;
        const closedTrades = (st.trades || []).filter(t => t && t.type === 'closed');
        const newestClosed = closedTrades.length ? closedTrades[closedTrades.length - 1] : null;
        const newestClosedKey = newestClosed
            ? [newestClosed.time || '', newestClosed.exit_reason || '', newestClosed.entry_price || ''].join('|')
            : '';
        const newClosedTrade = !!newestClosedKey && newestClosedKey !== _lastLiveClosedKey;
        const becameFlat = _lastLiveHadWorkingTrade && !hasWorkingTrade;
        if (newClosedTrade || becameFlat) {
            refreshTradeHistoryForCurrentAccount(newClosedTrade || becameFlat);
        } else {
            // A hidden/suspended tab can miss the one poll where position turns
            // flat. While Execute Trades is visible, make a bounded passive
            // request; the backend cache TTL decides when Trade/search is due.
            refreshVisibleExecuteTrades(false);
        }
        if (newestClosedKey) _lastLiveClosedKey = newestClosedKey;
        _lastLiveHadWorkingTrade = hasWorkingTrade;

        // Log recent events
        if (st.log && st.log.length > 0) {
            const lastLog = st.log[st.log.length - 1];
            if (lastLog !== window._lastLiveLog) {
                window._lastLiveLog = lastLog;
                log('[LIVE] ' + lastLog, 'info');
            }
        }

    } catch(e) {
        // silent
    }
}

let _lastLiveCandleTime = '';

/* 1.0.10p — chart catch-up after an idle stretch.
 *
 * `/data/latest-candles` always returns the newest 60 one-minute bars; `since`
 * only filters inside that window. So any pause longer than an hour leaves a
 * hole the live poll can never fill — and this poll pauses on every CLOSED
 * session, so an overnight or weekend gap was permanent until the page was
 * reloaded by hand. Observed 2026-08-14: the backend had ingested an unbroken
 * 60-bars-per-hour record all night, while the open tab still showed the
 * previous afternoon. The engine was fine; the screen said otherwise, which is
 * the expensive half — a stale chart reads exactly like a dead engine.
 *
 * When the gap is wider than the 60-bar window, reload the whole series from
 * the store instead of appending a bar across the hole.
 */
const _CHART_GAP_SEC = 120;                  // > 2 bars of daylight = a hole
const _CHART_CLOSED_CHECK_MS = 60 * 1000;    // how often to look while CLOSED
let _chartClosedCheckAt = 0;

function _chartNewestBarTime() {
    const buf = _rawCandleBuffer;
    return (buf && buf.length) ? buf[buf.length - 1].time : null;
}

/* Reload the whole series from the store.
 *
 * Deliberately NOT triggered by a wall-clock staleness threshold. A weekend
 * leaves the chart legitimately hours old with nothing newer to fetch, and a
 * clock rule fires anyway — reloading on a timer and yanking the view back to
 * the default range while the user is panning. The only honest trigger is
 * "the server has bars we do not", which is what the caller checks.
 */
async function _chartCatchUp(reason) {
    const before = _chartNewestBarTime();
    try {
        // A background catch-up must never send the user's viewport back to
        // the default recent window while they are inspecting another area.
        await fetchAndShowChart('1m', true);
    } catch (e) {
        log('Chart catch-up failed: ' + e.message, 'warn');
        return false;
    }
    if (_chartNewestBarTime() !== before) log('Chart caught up after ' + reason, 'info');
    _lastLiveCandleTime = '';   // incremental cursor is meaningless after a reload
    return true;
}

async function pollLiveCandle() {
    // Fetch fresh candles from API and append new bars
    const session = getMarketSession();

    // Still checked while CLOSED, at a slow rate: coming back to a tab left
    // open overnight has to show current data without a manual refresh, and
    // the poll below is the thing that stops during a closed session.
    if (session && session.label === 'CLOSED') {
        const now = Date.now();
        if (now - _chartClosedCheckAt < _CHART_CLOSED_CHECK_MS) return;
        _chartClosedCheckAt = now;
        try {
            const resp = await fetch(API + '/data/latest-candles');
            if (!resp.ok) return;
            const data = await resp.json();
            const rows = (data && data.candles) || [];
            if (!rows.length) return;
            const serverNewest = Math.max(...rows.map(
                c => isoToChartTime(c.time || c.timestamp)));
            const ours = _chartNewestBarTime();
            // Only when the server genuinely holds bars we are missing.
            if (ours == null || serverNewest - ours > _CHART_GAP_SEC) {
                await _chartCatchUp('a closed-session gap');
            }
        } catch (e) { /* offline while closed — nothing to repair */ }
        return;
    }

    try {
        const url = API + '/data/latest-candles' + (_lastLiveCandleTime ? '?since=' + encodeURIComponent(_lastLiveCandleTime) : '');
        const resp = await fetch(url);
        if (!resp.ok) return;
        const data = await resp.json();
        if (!data.candles || data.candles.length === 0) return;

        // Sort ascending — API returns newest-first; update() requires oldest-first
        const sorted = [...data.candles].sort((a, b) => {
            const ta = new Date(a.time || a.timestamp).getTime();
            const tb = new Date(b.time || b.timestamp).getTime();
            return ta - tb;
        });

        // The response is the evidence, not the clock: if its oldest bar is
        // already past ours, the missing stretch is wider than the 60-bar
        // window can hand back and appending would draw straight across the
        // hole. Reload from the store, which does have those bars.
        const lastBar = _chartNewestBarTime();
        if (lastBar != null && sorted.length) {
            const oldest = isoToChartTime(sorted[0].time || sorted[0].timestamp);
            if (oldest - lastBar > _CHART_GAP_SEC) {
                if (await _chartCatchUp('a feed gap')) return;
            }
        }

        let updated = 0;
        for (const c of sorted) {
            const t = isoToChartTime(c.time || c.timestamp);
            const bar = { time: t, open: c.open, high: c.high, low: c.low, close: c.close };
            const raw = { time: t, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume || 0 };

            // Update raw buffer (upsert by time)
            const bidx = _rawCandleBuffer.findIndex(x => x.time === t);
            const prevRaw = bidx >= 0 ? _rawCandleBuffer[bidx] : null;
            const unchanged = prevRaw &&
                prevRaw.open === raw.open &&
                prevRaw.high === raw.high &&
                prevRaw.low === raw.low &&
                prevRaw.close === raw.close &&
                (prevRaw.volume || 0) === raw.volume;
            if (unchanged) continue;

            try { candleSeries.update(bar); } catch(e) { continue; }
            if (bidx >= 0) _rawCandleBuffer[bidx] = raw; else _rawCandleBuffer.push(raw);
            if (!window._lastChartData) window._lastChartData = [];
            const cidx = window._lastChartData.findIndex(x => x.time === t);
            if (cidx >= 0) window._lastChartData[cidx] = bar; else window._lastChartData.push(bar);

            updated++;
        }

        // Track NEWEST candle time — next poll uses ?since=newest to get only new bars
        const newestC = sorted[sorted.length - 1];
        _lastLiveCandleTime = newestC.time || newestC.timestamp || '';

        if (updated > 0) {
            window._lastChartData.sort((a, b) => a.time - b.time);
            if (layerOn('prevday70')) refreshPreviousDayValueAreas(false);
            if (layerOn('footprint') || layerOn('cvd')) scheduleFootprintRefresh();
            refreshIndicatorSignalMarkers(false);
            refreshPiSignalMarkers();
            _refreshAllMarkers();
            log('Candle update: ' + newestC.close.toFixed(2) + ' (' + updated + ' bars)', 'info');
        }
    } catch(e) {
        // silent
    }
}

function updateAccountBadge() {
    const badge = document.getElementById('account-badge');
    if (!currentAccount) { badge.textContent = '--'; badge.className = 'account-badge'; return; }
    if (currentAccount.is_practice) {
        badge.textContent = 'PRACTICE';
        badge.className = 'account-badge practice';
    } else {
        badge.textContent = 'FUNDED';
        badge.className = 'account-badge funded';
    }
}

// -- Chart -----------------------------------------

function _chartHistoryCandleData(row) {
    if (!row) return null;
    const rawTime = row.time || row.timestamp;
    if (!rawTime) return null;
    let time = null;
    try { time = isoToChartTime(String(rawTime)); } catch (_) { return null; }
    if (!Number.isFinite(time)) return null;
    const open = Number(row.open);
    const high = Number(row.high);
    const low = Number(row.low);
    const close = Number(row.close);
    if (![open, high, low, close].every(Number.isFinite)) return null;
    return {
        time,
        open,
        high,
        low,
        close,
        volume: Number(row.volume) || 0,
    };
}

/**
 * Prepend one page of older candles when the visible range reaches the left
 * edge.  The backend reads the append-only local store, so this works even
 * when CONNECT intentionally kept only its recent warm-up window in memory.
 * The logical range is shifted by the number of newly prepended bars, which
 * keeps the candle under the user's cursor in the same screen position.
 */
async function loadOlderChartHistory() {
    if (_chartHistoryLoading || _chartHistoryExhausted || !chart || !candleSeries) return false;
    const current = window._lastChartData || [];
    if (!current.length) return false;
    const first = current[0];
    const beforeMs = chartTimeToUtcMs(first.time);
    if (!Number.isFinite(beforeMs)) return false;

    _chartHistoryLoading = true;
    try {
        const before = new Date(beforeMs).toISOString();
        const response = await fetch(
            API + '/data/candles?before=' + encodeURIComponent(before)
                + '&limit=' + CHART_HISTORY_PAGE_SIZE,
        );
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        const incoming = (data.candles || []).map(_chartHistoryCandleData).filter(Boolean);
        const oldRange = chart.timeScale().getVisibleLogicalRange();
        // `before` is exclusive, so only the incoming page needs deduping and
        // sorting. Rebuilding a Map and sorting the entire accumulated chart on
        // every left-edge page made each successive fetch slower than the last.
        const olderByTime = new Map();
        incoming.forEach(row => {
            if (row.time < first.time) olderByTime.set(row.time, row);
        });
        const older = [...olderByTime.values()].sort((a, b) => a.time - b.time);
        const added = older.length;
        if (!added) {
            // Older servers do not understand `before` and return the same
            // recent page. Stop retrying on every mousemove and surface the
            // reason in the chart log; a server restart activates the route.
            _chartHistoryExhausted = data.has_more_before === false || incoming.length === 0;
            if (!_chartHistoryExhausted) _chartHistoryExhausted = true;
            log('No older chart candles returned; history pagination is exhausted', 'info');
            return false;
        }

        const currentRaw = (_rawCandleBuffer && _rawCandleBuffer.length === current.length)
            ? _rawCandleBuffer
            : current.map(row => ({
                time: row.time, open: row.open, high: row.high, low: row.low,
                close: row.close, volume: row.volume || 0,
            }));
        const merged = older.concat(currentRaw);
        window._lastChartData = merged.map(row => ({
            time: row.time, open: row.open, high: row.high, low: row.low, close: row.close,
        }));
        _rawCandleBuffer = merged;
        // The page may finish while corrected drag/inertia is still running.
        // Stop that old writer before restoring the shifted range, otherwise
        // it can overwrite the restored position on the next animation frame.
        _cancelChartPanInteraction();
        // setData emits range-change callbacks while Lightweight Charts is
        // rebuilding its time scale. Suppress canvas work until both the data
        // and the restored viewport are stable, then paint exactly once.
        _chartHistoryApplying = true;
        try {
            candleSeries.setData(window._lastChartData);
            if (oldRange) {
                chart.timeScale().setVisibleLogicalRange({
                    from: oldRange.from + added,
                    to: oldRange.to + added,
                });
            }
        } finally {
            _chartHistoryApplying = false;
        }
        if (data.has_more_before === false || incoming.length < CHART_HISTORY_PAGE_SIZE) {
            _chartHistoryExhausted = data.has_more_before === false;
        }
        scheduleChartOverlayRedraw();
        if (layerOn('prevday70')) refreshPreviousDayValueAreas(true);
        if (layerOn('footprint') || layerOn('cvd')) refreshOrderflowLayers(true);
        log('Loaded ' + added + ' older chart candles (' + (data.source || 'history') + ')', 'info');
        return true;
    } catch (error) {
        log('Older chart history failed: ' + error.message, 'warn');
        return false;
    } finally {
        _chartHistoryApplying = false;
        _chartHistoryLoading = false;
    }
}

function maybeLoadOlderChartHistory(range) {
    // setData()/setVisibleLogicalRange during a programmatic refresh can emit
    // the same left-edge callback as a user drag.  Give that refresh a short
    // quiet window; the next real pan still loads immediately.
    if (Date.now() < _chartHistorySuppressUntil) return;
    if (!range || _chartHistoryLoading || _chartHistoryExhausted) return;
    if (Number(range.from) > CHART_HISTORY_TRIGGER_BARS) return;
    loadOlderChartHistory();
}

function _chartPriceRangeFromCanvas() {
    if (!candleSeries || !chart) return null;
    const container = document.getElementById('chart-container');
    if (!container) return null;
    const paneHeight = Math.max(1, container.clientHeight - _timeAxisHeight());
    try {
        const top = Number(candleSeries.coordinateToPrice(0));
        const bottom = Number(candleSeries.coordinateToPrice(paneHeight));
        if (!Number.isFinite(top) || !Number.isFinite(bottom) || top === bottom) return null;
        return {
            minValue: Math.min(top, bottom),
            maxValue: Math.max(top, bottom),
        };
    } catch (_) {
        return null;
    }
}

function _chartNow() {
    return (typeof performance !== 'undefined' && performance.now)
        ? performance.now()
        : Date.now();
}

function _cancelChartAutoscaleFrame() {
    if (_chartAutoscaleRafId !== null) {
        cancelAnimationFrame(_chartAutoscaleRafId);
        _chartAutoscaleRafId = null;
    }
}

function _copyChartPriceRange(range) {
    if (!range) return null;
    const minValue = Number(range.minValue);
    const maxValue = Number(range.maxValue);
    if (!Number.isFinite(minValue) || !Number.isFinite(maxValue) || minValue === maxValue) {
        return null;
    }
    return { minValue, maxValue };
}

function _invalidateChartAutoscale() {
    if (!chart) return;
    // Re-validate the provider without toggling autoScale.  Repeatedly
    // applying {autoScale:true} resets the internal scale first, which is the
    // one-frame default shrink visible at the start of every new scroll.
    try {
        if (candleSeries) candleSeries.applyOptions({});
        else chart.applyOptions({});
    } catch (_) {}
}

function _releaseChartAutoscaleHold() {
    if (_chartAutoscaleReleaseTimer !== null) {
        clearTimeout(_chartAutoscaleReleaseTimer);
        _chartAutoscaleReleaseTimer = null;
    }
    if (_chartAutoscaleHoldRange) {
        _chartAutoscalePresentedRange = _copyChartPriceRange(_chartAutoscaleHoldRange);
    }
    _chartAutoscaleHoldUntil = 0;
    _chartAutoscaleHoldRange = null;
    if (!chart) return;
    let autoScale = false;
    try { autoScale = chart.priceScale('right').options().autoScale === true; } catch (_) {}
    if (autoScale) _invalidateChartAutoscale();
    scheduleChartOverlayRedraw();
}

function _holdChartAutoscaleDuringScroll() {
    if (!chart) return;
    _cancelChartPanInertia();
    const now = _chartNow();
    if (!_chartAutoscaleHoldRange) {
        _chartAutoscaleHoldRange = _copyChartPriceRange(_chartAutoscalePresentedRange)
            || _chartPriceRangeFromCanvas();
    }
    _chartAutoscaleHoldUntil = now + CHART_AUTOSCALE_HOLD_MS;
    _chartAutoscaleEase = null;
    _chartAutoscaleAnimateNext = false;
    _cancelChartAutoscaleFrame();
    if (_chartAutoscaleReleaseTimer !== null) clearTimeout(_chartAutoscaleReleaseTimer);
    _chartAutoscaleReleaseTimer = setTimeout(() => {
        _chartAutoscaleReleaseTimer = null;
        _releaseChartAutoscaleHold();
    }, CHART_AUTOSCALE_HOLD_MS);
}

function _chartAutoscaleTargetChanged(next) {
    const previous = _chartAutoscaleTarget;
    if (!previous) return true;
    const span = Math.max(1, Math.abs(next.maxValue - next.minValue));
    const epsilon = span * 1e-5;
    return Math.abs(previous.minValue - next.minValue) > epsilon
        || Math.abs(previous.maxValue - next.maxValue) > epsilon;
}

function _scheduleChartAutoscaleFrame() {
    if (_chartAutoscaleRafId !== null || !chart || !_chartAutoscaleEase) return;
    _chartAutoscaleRafId = requestAnimationFrame(() => {
        _chartAutoscaleRafId = null;
        if (!_chartAutoscaleEase || !chart) return;
        // The provider below supplies the interpolated range. Re-invalidating
        // only during the short transition keeps the normal auto-scale path
        // untouched without resetting the scale to Lightweight Charts'
        // default range on every animation frame.
        let autoScale = false;
        try { autoScale = chart.priceScale('right').options().autoScale === true; } catch (_) {}
        if (autoScale) _invalidateChartAutoscale();
        scheduleChartOverlayRedraw();
    });
}

function _smoothChartAutoscale(baseImplementation) {
    const info = baseImplementation();
    if (!info || !info.priceRange) {
        _chartAutoscaleEase = null;
        return info;
    }

    const target = {
        minValue: Number(info.priceRange.minValue),
        maxValue: Number(info.priceRange.maxValue),
    };
    if (!Number.isFinite(target.minValue) || !Number.isFinite(target.maxValue)) return info;

    const now = _chartNow();
    if (now < _chartAutoscaleHoldUntil) {
        if (_chartAutoscaleHoldRange) {
            const held = _copyChartPriceRange(_chartAutoscaleHoldRange);
            if (held) _chartAutoscalePresentedRange = held;
            return { ...info, priceRange: held || _chartAutoscaleHoldRange };
        }
        return info;
    }
    if (_chartAutoscaleHoldUntil) {
        _chartAutoscaleHoldUntil = 0;
        _chartAutoscaleHoldRange = null;
    }
    if (!_chartAutoscaleInitialised) {
        _chartAutoscaleInitialised = true;
        _chartAutoscaleTarget = target;
        _chartAutoscaleAnimateNext = false;
        _chartAutoscalePresentedRange = _copyChartPriceRange(target);
        return info;
    }
    if (!_chartAutoscaleTarget || _chartAutoscaleAnimateNext || _chartAutoscaleTargetChanged(target)) {
        const current = _copyChartPriceRange(_chartAutoscalePresentedRange)
            || _chartPriceRangeFromCanvas();
        _chartAutoscaleEase = {
            from: current || target,
            to: target,
            startedAt: now,
        };
        _chartAutoscaleTarget = target;
        _chartAutoscaleAnimateNext = false;
    }
    if (!_chartAutoscaleEase) {
        _chartAutoscalePresentedRange = _copyChartPriceRange(target);
        return info;
    }

    const transition = _chartAutoscaleEase;
    const rawProgress = Math.max(0, Math.min(1,
        (now - transition.startedAt) / CHART_AUTOSCALE_TRANSITION_MS));
    // Ease-out keeps the first part responsive and removes the hard stop at
    // the end of a resize/zoom while remaining finite and deterministic.
    const progress = 1 - Math.pow(1 - rawProgress, 3);
    if (rawProgress >= 1) {
        _chartAutoscaleEase = null;
        _chartAutoscalePresentedRange = _copyChartPriceRange(target);
        return info;
    }

    const presented = {
        minValue: transition.from.minValue
            + (transition.to.minValue - transition.from.minValue) * progress,
        maxValue: transition.from.maxValue
            + (transition.to.maxValue - transition.from.maxValue) * progress,
    };
    _chartAutoscalePresentedRange = presented;
    _scheduleChartAutoscaleFrame();
    return {
        ...info,
        priceRange: presented,
    };
}

function _scheduleChartResize(container) {
    if (!container || !chart || _chartResizeRafId !== null) return;
    _chartResizeRafId = requestAnimationFrame(() => {
        _chartResizeRafId = null;
        if (!chart) return;
        const width = Math.max(0, container.clientWidth);
        const height = Math.max(0, container.clientHeight);
        const key = width + 'x' + height;
        if (key === _chartLastSize) return;
        _chartLastSize = key;
        try { chart.applyOptions({ width, height }); } catch (_) {}
        scheduleChartOverlayRedraw();
    });
}

function _setChartPriceAutoScale(enabled) {
    if (!chart) return;
    if (!enabled) {
        const current = _copyChartPriceRange(_chartPriceRangeFromCanvas());
        if (current) _chartAutoscalePresentedRange = current;
    }
    _chartAutoscaleEase = null;
    _chartAutoscaleHoldUntil = 0;
    _chartAutoscaleHoldRange = null;
    _cancelChartAutoscaleFrame();
    if (_chartAutoscaleReleaseTimer !== null) {
        clearTimeout(_chartAutoscaleReleaseTimer);
        _chartAutoscaleReleaseTimer = null;
    }
    if (enabled) {
        _chartAutoscaleTarget = null;
        _chartAutoscaleAnimateNext = _chartAutoscaleInitialised;
    }
    try {
        chart.priceScale('right').applyOptions({ autoScale: Boolean(enabled) });
    } catch (_) {}
}

function _isChartControlTarget(target) {
    return !!(target && target.closest && target.closest(
        'button, input, select, textarea, a, #chart-quick-btns, '
        + '#chart-layer-pop, #chart-status-cluster, #live-top-bar, #lv-levels-panel'
    ));
}

function _cancelChartPanInertia() {
    if (_chartPanInertiaRafId !== null) {
        cancelAnimationFrame(_chartPanInertiaRafId);
        _chartPanInertiaRafId = null;
    }
    _chartPanInertiaPosition = null;
    _chartPanInertiaVelocity = 0;
}

// A data replacement must not leave an old correction frame or inertia loop
// writing to the time scale after the new viewport has been restored.  This is
// a motion-cancellation helper only; it never chooses a new horizontal range.
function _cancelChartPanInteraction() {
    _cancelChartPanInertia();
    if (_chartPanCorrectionRafId !== null) {
        cancelAnimationFrame(_chartPanCorrectionRafId);
        _chartPanCorrectionRafId = null;
    }
    _chartPanLastX = null;
    _chartPanGesture = null;
}

function _startChartPanInertia(position, velocity) {
    _cancelChartPanInertia();
    if (!chart || !Number.isFinite(position) || !Number.isFinite(velocity)
        || Math.abs(velocity) < CHART_PAN_INERTIA_MIN_SPEED) return;

    let current = position;
    let speed = velocity;
    let previousAt = _chartNow();
    _chartPanInertiaPosition = current;
    _chartPanInertiaVelocity = speed;

    const tick = () => {
        _chartPanInertiaRafId = null;
        if (!chart) {
            _cancelChartPanInertia();
            return;
        }
        const now = _chartNow();
        const elapsed = Math.max(1, Math.min(48, now - previousAt));
        previousAt = now;
        current += speed * elapsed;
        try {
            chart.timeScale().scrollToPosition(current, false);
        } catch (_) {
            _cancelChartPanInertia();
            return;
        }
        _chartPanInertiaPosition = current;
        speed *= Math.exp(-elapsed / CHART_PAN_INERTIA_DECAY_MS);
        _chartPanInertiaVelocity = speed;
        scheduleChartOverlayRedraw();
        if (Math.abs(speed) < CHART_PAN_INERTIA_MIN_SPEED) {
            _cancelChartPanInertia();
            return;
        }
        _chartPanInertiaRafId = requestAnimationFrame(tick);
    };

    _chartPanInertiaRafId = requestAnimationFrame(tick);
}

function _beginChartPan(event, container) {
    _cancelChartPanInteraction();
    if (!chart || !container || event.button !== 0 || _isChartControlTarget(event.target)) return;

    // Keep price-axis gestures dedicated to vertical scale control.  The
    // plot area is the only place where the horizontal sensitivity correction
    // should run.
    const rect = container.getBoundingClientRect();
    let rightScaleWidth = 0;
    try { rightScaleWidth = chart.priceScale('right').width(); } catch (_) {}
    if (rightScaleWidth > 0 && event.clientX >= rect.right - rightScaleWidth) return;

    let barSpacing = 6;
    try {
        const value = Number(chart.timeScale().options().barSpacing);
        if (Number.isFinite(value) && value > 0) barSpacing = value;
    } catch (_) {}

    _chartPanGesture = {
        startX: event.clientX,
        startY: event.clientY,
        startScroll: chart.timeScale().scrollPosition(),
        barSpacing,
        lastAppliedX: event.clientX,
        lastAppliedPosition: chart.timeScale().scrollPosition(),
        lastSampleAt: _chartNow(),
        velocity: 0,
        direction: 'pending',
        started: false,
    };
}

function _applyChartPanSensitivity(event) {
    const gesture = _chartPanGesture;
    if (!gesture || gesture.direction !== 'horizontal'
        || !(event.buttons & 1) || !chart) return;
    _chartPanLastX = event.clientX;
    if (_chartPanCorrectionRafId !== null) return;
    _chartPanCorrectionRafId = requestAnimationFrame(() => {
        _chartPanCorrectionRafId = null;
        const active = _chartPanGesture;
        const clientX = _chartPanLastX;
        if (!active || clientX === null || !chart) return;
        _applyChartPanSensitivityAt(clientX, active);
    });
}

function _routeChartPanMove(event) {
    const gesture = _chartPanGesture;
    if (!gesture || !(event.buttons & 1) || !chart) return;

    const deltaX = event.clientX - gesture.startX;
    const deltaY = event.clientY - gesture.startY;
    const distance = Math.hypot(deltaX, deltaY);
    if (gesture.direction === 'pending' && distance < CHART_PAN_DIRECTION_THRESHOLD_PX) {
        // Do not let the native handler start on a tiny diagonal jitter.  Once
        // a direction is clear, the event is either released to the native
        // vertical path or kept by the single horizontal controller below.
        event.preventDefault();
        event.stopPropagation();
        return;
    }

    if (gesture.direction === 'pending') {
        gesture.direction = Math.abs(deltaX) > Math.abs(deltaY)
            ? 'horizontal'
            : 'vertical';
        // Lightweight Charts cannot price-drag while autoScale is enabled.
        // Only turn it off after a real drag has been identified, so a simple
        // click never changes the chart's vertical range.
        if (!gesture.started) {
            gesture.started = true;
            _setChartPriceAutoScale(false);
        }
    }

    if (gesture.direction !== 'horizontal') return;

    // The native pressedMouseMove listener is still available for vertical
    // dragging, but it must not see horizontal events or it will compete with
    // the corrected 1.10x position below.  Capture at window before the chart
    // document listener and let this controller be the only horizontal writer.
    event.preventDefault();
    event.stopPropagation();
    _applyChartPanSensitivity(event);
}

function _applyChartPanSensitivityAt(clientX, gesture) {
    const deltaX = clientX - gesture.startX;
    if (!Number.isFinite(deltaX)) return;
    const now = _chartNow();
    const targetPosition = gesture.startScroll
        - (deltaX / gesture.barSpacing) * CHART_PAN_SENSITIVITY;
    const previousPosition = Number.isFinite(gesture.lastAppliedPosition)
        ? gesture.lastAppliedPosition
        : targetPosition;
    const elapsed = Math.max(1, now - (gesture.lastSampleAt || now));
    const sampledVelocity = (targetPosition - previousPosition) / elapsed;
    // A short EMA removes event-rate noise without adding a second animation
    // source.  Every frame still resolves to the one deterministic target
    // derived from the original pointer position.
    gesture.velocity = gesture.velocity * 0.72 + sampledVelocity * 0.28;
    gesture.lastSampleAt = now;
    gesture.lastAppliedX = clientX;
    gesture.lastAppliedPosition = targetPosition;
    try {
        const currentPosition = chart.timeScale().scrollPosition();
        if (!Number.isFinite(currentPosition)
            || Math.abs(currentPosition - targetPosition) > 0.001) {
            chart.timeScale().scrollToPosition(targetPosition, false);
        }
    } catch (_) {}
}

function _endChartPan(cancelMotion = false) {
    const gesture = _chartPanGesture;
    if (_chartPanCorrectionRafId !== null) {
        cancelAnimationFrame(_chartPanCorrectionRafId);
        _chartPanCorrectionRafId = null;
    }
    if (gesture && Number.isFinite(_chartPanLastX)
        && gesture.lastAppliedX !== _chartPanLastX) {
        _applyChartPanSensitivityAt(_chartPanLastX, gesture);
    }
    _chartPanLastX = null;
    _chartPanGesture = null;
    if (cancelMotion) {
        _cancelChartPanInertia();
        return;
    }
    if (gesture && gesture.direction === 'horizontal'
        && Number.isFinite(gesture.lastAppliedPosition)) {
        _startChartPanInertia(gesture.lastAppliedPosition, gesture.velocity);
    }
}

function initChart() {
    const container = document.getElementById('chart-container');
    chart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: container.clientHeight,
        layout: {
            background: { type: 'solid', color: _appThemeColor('--surface-chart', '#08090d') },
            textColor: _appThemeColor('--ink-muted', '#556178'),
            fontSize: 11,
            fontFamily: 'IBM Plex Mono, monospace',
        },
        grid: {
            vertLines: { color: _appThemeColor('--chart-grid', 'rgba(100, 220, 255, 0.03)') },
            horzLines: { color: _appThemeColor('--chart-grid', 'rgba(100, 220, 255, 0.03)') },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: _appThemeColor('--chart-crosshair', 'rgba(100, 220, 255, 0.2)'), style: 0, width: 1 },
            horzLine: { color: _appThemeColor('--chart-crosshair', 'rgba(100, 220, 255, 0.2)'), style: 0, width: 1 },
        },
        rightPriceScale: {
            autoScale: true,
            borderColor: _appThemeColor('--line-subtle', 'rgba(100, 220, 255, 0.08)'),
        },
        handleScroll: {
            mouseWheel: true,
            pressedMouseMove: true,
            horzTouchDrag: true,
            vertTouchDrag: true,
        },
        handleScale: {
            axisPressedMouseMove: { time: true, price: true },
            axisDoubleClickReset: { time: true, price: true },
            mouseWheel: true,
            pinch: true,
        },
        timeScale: {
            borderColor: _appThemeColor('--line-subtle', 'rgba(100, 220, 255, 0.08)'),
            timeVisible: true,
            secondsVisible: false,
            // Horizontal framing is free after the one-shot initial view.
            // Keep every edge/resize follow rule explicitly disabled so a
            // chart refresh cannot silently re-lock the user's viewport.
            fixLeftEdge: false,
            fixRightEdge: false,
            lockVisibleTimeRangeOnResize: false,
            rightBarStaysOnScroll: false,
            tickMarkFormatter: _chartTickMark,
        },
        kineticScroll: {
            // Horizontal mouse inertia is applied after the sensitivity
            // correction, so Lightweight Charts cannot snap back to the
            // uncorrected offset when the pointer is released.
            mouse: false,
            touch: true,
        },
        localization: {
            // Crosshair tooltip shows the full "2026.04.15 15:30" stamp.
            timeFormatter: _chartStampFull,
        },
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: _appThemeColor('--candle-up', '#888888'),
        downColor: _appThemeColor('--candle-down', '#555555'),
        borderDownColor: _appThemeColor('--candle-down', '#555555'),
        borderUpColor: _appThemeColor('--candle-up', '#888888'),
        wickDownColor: _appThemeColor('--candle-down', '#555555'),
        wickUpColor: _appThemeColor('--candle-up', '#888888'),
        autoscaleInfoProvider: (baseImplementation) => _smoothChartAutoscale(baseImplementation),
    });
    _syncFootprintCandleVisibility();

    _chartLastSize = container.clientWidth + 'x' + container.clientHeight;
    new ResizeObserver(() => _scheduleChartResize(container)).observe(container);

    // Redraw VP overlay on scroll / zoom — continuous following via rAF
    const _redrawOverlays = () => {
        markOrderflowInteraction();
        try { maybeLoadOlderChartHistory(chart.timeScale().getVisibleLogicalRange()); } catch (_) {}
        scheduleChartOverlayRedraw();
        if (layerOn('footprint') || layerOn('cvd')) scheduleFootprintRefresh();
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(_redrawOverlays);
    // Vertical zoom (wheel on price scale or chart body)
    container.addEventListener('wheel', _redrawOverlays, { passive: true });
    // A wheel event can change the visible bar range several times before the
    // browser paints the next frame. Hold the current price window during that
    // burst; the final stable target is eased only after the gesture settles.
    container.addEventListener('wheel', _holdChartAutoscaleDuringScroll, {
        passive: true,
        capture: true,
    });
    // Free the vertical price range after a real plot drag direction is known.
    // Lightweight Charts keeps ownership of vertical movement; horizontal
    // movement is routed to the single sensitivity controller below.
    container.addEventListener('mousedown', (event) => _beginChartPan(event, container), true);
    // Continuous drag redraw
    container.addEventListener('mousemove', (e) => {
        if (!e.buttons) return;
        _redrawOverlays();
    }, { passive: true });
    // Route before Lightweight Charts' document listener.  Horizontal drags
    // are fully owned by the sensitivity controller; vertical drags continue
    // through to the native price-scroll implementation.
    window.addEventListener('mousemove', _routeChartPanMove, {
        passive: false,
        capture: true,
    });
    container.addEventListener('mouseup', _redrawOverlays);
    window.addEventListener('mouseup', _endChartPan);
    window.addEventListener('blur', () => _endChartPan(true));

    log('Chart initialized', 'info');
}

// -- Chart overlay canvases ------------------------------------
let fadeLevelsCanvas = null;
let previousDayValueAreaCanvas = null;
let footprintCanvas = null;
let cvdCanvas = null;

function createFadeLevelsCanvas() {
    if (fadeLevelsCanvas) return fadeLevelsCanvas;
    const container = document.getElementById('chart-container');
    const canvas = document.createElement('canvas');
    canvas.id = 'fade-level-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:3;';
    container.appendChild(canvas);
    fadeLevelsCanvas = canvas;
    return canvas;
}

function createPreviousDayValueAreaCanvas() {
    if (previousDayValueAreaCanvas) return previousDayValueAreaCanvas;
    const container = document.getElementById('chart-container');
    const canvas = document.createElement('canvas');
    canvas.id = 'previous-day-va-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:3;';
    container.appendChild(canvas);
    previousDayValueAreaCanvas = canvas;
    return canvas;
}

function createFootprintCanvas() {
    if (footprintCanvas) return footprintCanvas;
    const container = document.getElementById('chart-container');
    const canvas = document.createElement('canvas');
    canvas.id = 'footprint-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:3;';
    container.appendChild(canvas);
    footprintCanvas = canvas;
    return canvas;
}

function createCvdCanvas() {
    if (cvdCanvas) return cvdCanvas;
    const container = document.getElementById('chart-container');
    const canvas = document.createElement('canvas');
    canvas.id = 'cvd-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:4;';
    container.appendChild(canvas);
    cvdCanvas = canvas;
    return canvas;
}

let sessionDividerCanvas = null;
function createSessionDividerCanvas() {
    if (sessionDividerCanvas) return sessionDividerCanvas;
    const container = document.getElementById('chart-container');
    const canvas = document.createElement('canvas');
    canvas.id = 'session-divider-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:2;';
    container.appendChild(canvas);
    sessionDividerCanvas = canvas;
    return canvas;
}

// Session boundaries in the configured market timezone. nyLocalToUtcMs()
// resolves EST/EDT separately for each date.
const SESSION_BOUNDARIES = [
    { h: 18, m: 0,  label: 'ASIA' },
    { h: 3,  m: 0,  label: 'EURO' },
    { h: 7,  m: 0,  label: 'PRE'  },
    { h: 9,  m: 30, label: 'RTH'  },
    { h: 16, m: 0,  label: 'AH'   },
];
const NO_TRADE_WINDOWS_ET = [
    // Chart-only close warning: 12:45 PM–3:00 PM Pacific, represented here
    // as 15:45–18:00 in the New York market clock used by this renderer.
    { startH: 15, startM: 45, endH: 18, endM: 0, label: 'NO TRADE' },
];
const NY_OPEN_ZONE_WINDOWS = [
    {
        startH: 8, startM: 0, endH: 8, endM: 15,
        label: 'NY OPEN ZONE 08:00-08:15',
        fill: 'rgba(168, 85, 247, 0.105)',
        stroke: 'rgba(196, 145, 255, 0.72)',
        text: 'rgba(222, 190, 255, 0.88)',
        labelY: 42,
    },
    {
        startH: 9, startM: 30, endH: 9, endM: 45,
        label: 'NY 09:30-09:45',
        fill: 'rgba(0, 229, 160, 0.080)',
        stroke: 'rgba(0, 229, 160, 0.62)',
        text: 'rgba(0, 245, 180, 0.85)',
        labelY: 56,
    },
];

function utcMsToChartTime(ms) {
    const instantMs = Number(ms);
    if (!Number.isFinite(instantMs)) return NaN;
    // lightweight-charts has no IANA timezone setting.  Store the instant as
    // a New York wall-clock epoch so the chart is identical on a laptop,
    // monitor, Windows, and macOS.  The offset is resolved for the instant,
    // therefore EDT/EST transitions remain DST-aware.
    const marketOffsetSec = _timeZoneOffsetMs(
        SYSTEM_TIME_ZONES.market, instantMs,
    ) / 1000;
    return Math.floor(instantMs / 1000) + marketOffsetSec;
}

/* Inverse of utcMsToChartTime. Chart time is UTC seconds shifted by the
   configured New York market offset, so recovering the instant needs that
   offset back out. Resolve it from the approximate instant, then re-check at
   the candidate UTC instant for a DST boundary. */
function chartTimeToUtcMs(chartTime) {
    const approxMs = Number(chartTime) * 1000;
    if (!Number.isFinite(approxMs)) return NaN;
    const offsetMs = _timeZoneOffsetMs(SYSTEM_TIME_ZONES.market, approxMs);
    let utcMs = approxMs - offsetMs;
    const correctedOffsetMs = _timeZoneOffsetMs(SYSTEM_TIME_ZONES.market, utcMs);
    if (correctedOffsetMs !== offsetMs) utcMs = approxMs - correctedOffsetMs;
    return utcMs;
}

function _timeZoneOffsetMs(timeZone, utcMs) {
    try {
        const parts = new Intl.DateTimeFormat('en-US', {
            timeZone,
            timeZoneName: 'shortOffset',
            hour: '2-digit',
            minute: '2-digit',
        }).formatToParts(new Date(utcMs));
        const name = (parts.find(p => p.type === 'timeZoneName') || {}).value || 'GMT';
        if (name === 'GMT' || name === 'UTC') return 0;
        const m = name.match(/GMT([+-])(\d{1,2})(?::?(\d{2}))?/);
        if (!m) return 0;
        const sign = m[1] === '-' ? -1 : 1;
        const hours = parseInt(m[2], 10) || 0;
        const mins = parseInt(m[3] || '0', 10) || 0;
        return sign * (hours * 60 + mins) * 60000;
    } catch (e) {
        // Futures dates in this app are modern US dates; if Intl shortOffset is
        // unavailable, EDT/EST precision only affects the background annotation.
        return -4 * 3600000;
    }
}

function nyLocalToUtcMs(year, month, day, hour, minute) {
    const guess = Date.UTC(year, month, day, hour, minute, 0);
    let offset = _timeZoneOffsetMs(SYSTEM_TIME_ZONES.market, guess);
    let utc = guess - offset;
    const offset2 = _timeZoneOffsetMs(SYSTEM_TIME_ZONES.market, utc);
    if (offset2 !== offset) utc = guess - offset2;
    return utc;
}

function _newYorkParts(utcMs) {
    const parts = new Intl.DateTimeFormat('en-US-u-ca-gregory', {
        timeZone: SYSTEM_TIME_ZONES.market,
        year: 'numeric', month: '2-digit', day: '2-digit',
        weekday: 'short', hour: '2-digit', minute: '2-digit',
        hourCycle: 'h23',
    }).formatToParts(new Date(utcMs));
    const value = type => (parts.find(p => p.type === type) || {}).value;
    return {
        year: parseInt(value('year'), 10),
        month: parseInt(value('month'), 10),
        day: parseInt(value('day'), 10),
        hour: parseInt(value('hour'), 10) % 24,
        minute: parseInt(value('minute'), 10),
        weekday: value('weekday'),
    };
}

// Chart time values are local-wall-clock encoded as UTC seconds (see
// utcMsToChartTime), so format them with getUTC* to read back the wall clock.
function _chartStampFull(time) {
    if (time && typeof time === 'object' && time.year != null) {
        const M = String(time.month).padStart(2, '0');
        const D = String(time.day).padStart(2, '0');
        return time.year + '.' + M + '.' + D;
    }
    const d = new Date(time * 1000);
    const M = String(d.getUTCMonth() + 1).padStart(2, '0');
    const D = String(d.getUTCDate()).padStart(2, '0');
    const h = String(d.getUTCHours()).padStart(2, '0');
    const mi = String(d.getUTCMinutes()).padStart(2, '0');
    return d.getUTCFullYear() + '.' + M + '.' + D + ' ' + h + ':' + mi;
}

// Axis ticks: compact — date (MM.DD) on day/month/year marks, HH:mm intraday.
function _chartTickMark(time, tickMarkType) {
    if (time && typeof time === 'object' && time.year != null) {
        return String(time.month).padStart(2, '0') + '.' + String(time.day).padStart(2, '0');
    }
    const d = new Date(time * 1000);
    if (tickMarkType <= 2) {   // Year / Month / DayOfMonth
        return String(d.getUTCMonth() + 1).padStart(2, '0') + '.' + String(d.getUTCDate()).padStart(2, '0');
    }
    return String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0');
}

function getNextSessionBoundaryMs(isoStr) {
    if (!isoStr) return null;
    let s = String(isoStr);
    if (!s.endsWith('Z') && !s.includes('+') && !s.includes('-', 10)) s += 'Z';
    const formed = new Date(s);
    if (isNaN(formed.getTime())) return null;

    const dayMs = 86400000;
    const startDay = new Date(formed.getTime() - dayMs);
    startDay.setUTCHours(0, 0, 0, 0);
    const formedMs = formed.getTime();
    let best = null;

    for (let d = startDay.getTime(); d <= formedMs + 2 * dayMs; d += dayMs) {
        const day = new Date(d);
        SESSION_BOUNDARIES.forEach(b => {
            const boundaryMs = nyLocalToUtcMs(
                day.getUTCFullYear(),
                day.getUTCMonth(),
                day.getUTCDate(),
                b.h, b.m
            );
            if (boundaryMs > formedMs + 1000 && (best === null || boundaryMs < best)) {
                best = boundaryMs;
            }
        });
    }

    return best;
}


// ════════════════════════════════════════════════════════════════════
// 1.0.10: 圖層開關。原本所有 overlay 都無條件畫,圖上東西多到看不出重點。
// 預設只留 EMAPMO 訊號與 PI 訊號,其餘由右下角的圖層選單自行開啟。
//
// 每個 draw 函式在開頭檢查自己的旗標並提早返回 —— 不是隱藏 canvas,
// 而是根本不畫,順便省掉重繪成本(VP 與 zone 線在 233 萬根上很吃 CPU)。
// ════════════════════════════════════════════════════════════════════
const CHART_LAYERS = [
    { key: 'emapmo',   label: 'EMAPMO',                  on: true  },
    { key: 'pi',       label: 'PI',                      on: true  },
    { key: 'trades',   label: 'Trade brackets (SL/TP)',  on: true  },
    { key: 'mrev',     label: 'MREV',                    on: false },
    { key: 'kdjma',    label: 'KDJMA',                   on: false },
    { key: 'intramom', label: 'INTRAMOM',                on: false },
    { key: 'dayzone',  label: 'DAY ZONE',                on: false },
    { key: 'prevday70',label: 'PRIOR DAY 70% VAH/VAL/POC', on: false },
    { key: 'optionwall', label: 'QQQ OPTION WALL',       on: false },
    { key: 'footprint', label: 'FOOTPRINT',              on: false },
    { key: 'cvd',      label: 'CVD',                      on: false },
];
const CHART_LAYER_STORAGE_KEY = 'ancserTPX.chartLayers';

function _loadChartLayerPreferences() {
    const state = Object.fromEntries(CHART_LAYERS.map(layer => [layer.key, layer.on]));
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(CHART_LAYER_STORAGE_KEY) || 'null'); } catch (e) {}
    if (!saved || typeof saved !== 'object' || Array.isArray(saved)) return state;
    CHART_LAYERS.forEach(layer => {
        if (typeof saved[layer.key] === 'boolean') state[layer.key] = saved[layer.key];
    });
    return state;
}

const CHART_OVERLAYS = _loadChartLayerPreferences();

function _persistChartLayerPreferences() {
    const state = Object.fromEntries(
        CHART_LAYERS.map(layer => [layer.key, CHART_OVERLAYS[layer.key] !== false]),
    );
    try { localStorage.setItem(CHART_LAYER_STORAGE_KEY, JSON.stringify(state)); } catch (e) {}
}

function _seedChartLayerMarkup() {
    document.querySelectorAll('.glass-switch').forEach(track => {
        if (track.closest('.optical-stage-copy')) return;
        if (!track.tpxSetState && !track.dataset.nativeSwitchBound) {
            track.dataset.nativeSwitchBound = '1';
            track.addEventListener('click', () => {
                const key = String(track.dataset.switchProxy || '').replace(/^lp-/, '');
                const next = track.getAttribute('aria-checked') !== 'true';
                if (key in CHART_OVERLAYS) toggleChartLayer(key, next);
                else {
                    track.classList.toggle('on', next);
                    track.setAttribute('aria-checked', String(next));
                    const proxy = document.getElementById(track.dataset.switchProxy || '');
                    proxy?.click();
                }
            });
        }
        const key = String(track.dataset.switchProxy || '').replace(/^lp-/, '');
        if (!(key in CHART_OVERLAYS)) return;
        const on = CHART_OVERLAYS[key] !== false;
        track.classList.toggle('on', on);
        track.setAttribute('aria-checked', String(on));
    });
}

// The static markup is already present because this bundle loads at the end
// of <body>. Seed it before tpx-glass.js boots so its internal committed value
// starts from the restored choice, including the first click after a reload.
_seedChartLayerMarkup();

function _clearCanvas(id) {
    const c = document.getElementById(id);
    if (!c) return;
    const ctx = c.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
}

function layerOn(key) { return CHART_OVERLAYS[key] !== false; }

function _syncFootprintCandleVisibility() {
    if (!candleSeries) return;
    // Keep the series alive for priceToCoordinate() and all other overlays,
    // but remove its visual body once the footprint has actual cells to show.
    // If the cache is unavailable, keep the normal candles visible instead of
    // leaving the chart blank.
    const hideCandles = layerOn('footprint') && _footprintBars.length > 0;
    // applyOptions invalidates the chart even for an unchanged value. Read
    // the current series rather than memoizing a previous series instance.
    try {
        if (candleSeries.options().visible !== !hideCandles) {
            candleSeries.applyOptions({visible: !hideCandles});
        }
    } catch (_) {}
}

// 四種指標訊號共用 indicator-signal-overlay 這張畫布,所以不能整層關掉,
// 得逐筆依 row.type 過濾。
const _SIGNAL_TYPE_LAYER = {
    emapmo: 'emapmo',
    momentum_reversion: 'mrev',
    icefishball: 'kdjma',
    intramom: 'intramom',
};

function toggleChartLayer(key, on) {
    if (!(key in CHART_OVERLAYS)) return;
    CHART_OVERLAYS[key] = !!on;
    const next = CHART_OVERLAYS[key];
    // Native mode has no Glass controller to commit the visual state.  Keep
    // the real switch in sync here so a close/reopen cycle never leaves the
    // track and aria state stuck on the previous value.  A Glass controller,
    // when present, still owns its spring; this only mirrors the committed
    // semantic state.
    document.querySelectorAll(
        '#chart-layer-pop [data-switch-proxy="lp-' + key + '"]'
    ).forEach(track => {
        if (track.closest('.optical-stage-copy')) return;
        track.classList.toggle('on', next);
        track.setAttribute('aria-checked', String(next));
    });
    _persistChartLayerPreferences();
    if (key === 'footprint') _syncFootprintCandleVisibility();
    if (key === 'pi' && on && !_piSignalRows.length) { refreshPiSignalMarkers(); return; }
    if (key === 'optionwall' && on && !_optionWallSnapshots.length) { refreshOptionWallLayer(); return; }
    if (key === 'prevday70' && on) { refreshPreviousDayValueAreas(true); return; }
    if (key === 'footprint' || key === 'cvd') {
        if (on) refreshOrderflowLayers(true);
        else { drawFootprintLayer(); drawCvdLayer(); }
        return;
    }
    try { redrawAllOverlays(); } catch (e) {}
}

function redrawAllOverlays() {
    try { drawSessionDividers(); } catch (e) {}
    try { drawIndicatorSignalOverlay(); } catch (e) {}
    try { drawOptionWallOverlay(); } catch (e) {}
    try { if (_overlaySyncData && _overlaySyncData.zones) drawFadeDailyLevels(_overlaySyncData.zones); } catch (e) {}
    try { drawPreviousDayValueAreas(); } catch (e) {}
    try { drawFootprintLayer(); } catch (e) {}
    try { drawCvdLayer(); } catch (e) {}
    try { drawPositionTools(backtestData && backtestData.trades ? backtestData.trades : []); } catch (e) {}
}

function scheduleChartOverlayRedraw() {
    if (_chartHistoryApplying || _chartOverlayRafId) return;
    _chartOverlayRafId = requestAnimationFrame(() => {
        _chartOverlayRafId = null;
        if (_chartHistoryApplying) return;
        try { redrawTradeDecisionOverlays(); } catch (e) {}
        try { drawSessionDividers(); } catch (e) {}
        try { drawIndicatorSignalOverlay(); } catch (e) {}
        try { drawOptionWallOverlay(); } catch (e) {}
        try {
            if (_overlaySyncData && _overlaySyncData.zones) {
                drawFadeDailyLevels(_overlaySyncData.zones);
            }
        } catch (e) {}
        try { drawPreviousDayValueAreas(); } catch (e) {}
        try { drawFootprintLayer(); } catch (e) {}
        try { drawCvdLayer(); } catch (e) {}
        window.TpxGlass?.sync?.();
    });
}

// 開關是靜態 HTML(玻璃引擎的 liveAll() 只掃一次,動態產生的抓不到),
// 所以這裡只做「把 DOM 狀態同步回 CHART_OVERLAYS」,不再生成 markup。
function buildChartLayerMenu() {
    document.querySelectorAll('#chart-layer-pop .glass-switch').forEach(tr => {
        if (tr.closest('.optical-stage-copy')) return;
        const key = String(tr.dataset.switchProxy || '').replace(/^lp-/, '');
        if (!(key in CHART_OVERLAYS)) return;
        const on = CHART_OVERLAYS[key];
        // 只翻 `on` class 沒有用 —— 拇指位置活在彈簧裡,class 和位置會各說各話
        // (軌道變綠但拇指還停在左邊)。tpxSetState 才會把彈簧一起推過去。
        //
        // 但 tpxSetState 的行程是從 track.clientWidth 算的,面板還 display:none
        // 時那是 0 → 行程被夾成 1px,拇指就卡在最左邊不動了。所以有版面才推。
        // Opening the hidden panel already has the correct state in aria and
        // in the switch controller's committed value.  Re-committing that
        // same value would restart the long glass spring and leave the thumb
        // in the dark interacting material for ~2 seconds.  Only drive the
        // spring when the source state actually changed while the panel was
        // closed.
        const current = tr.getAttribute('aria-checked') === 'true';
        if (current === on) return;
        if (tr.tpxSetState && tr.clientWidth > 0) tr.tpxSetState(on);
        else {
            tr.classList.toggle('on', on);
            tr.setAttribute('aria-checked', String(on));
        }
    });
}

// tpx-glass.js 的 switch onChange 走 `byId(proxy).click()`,不帶新狀態進來。
// commit() 在呼叫 onChange **之前**就寫好 aria-checked,但 `on` class 要等
// 下一幀彈簧 apply() 才更新 —— 所以讀 aria-checked,不要讀 class。
function onLayerProxy(key) {
    const tr = [...document.querySelectorAll(
        '#chart-layer-pop [data-switch-proxy="lp-' + key + '"]'
    )].find((node) => !node.closest('.optical-stage-copy'));
    if (!tr) return;
    toggleChartLayer(key, tr.getAttribute('aria-checked') === 'true');
}

function toggleChartLayerMenu(force) {
    const pop = document.getElementById('chart-layer-pop');
    if (!pop) return;
    const show = (force === undefined) ? pop.classList.contains('hidden') : !!force;
    pop.classList.toggle('hidden', !show);
    if (show) {
        buildChartLayerMenu();
        // Popup switch surfaces are mounted at boot while this panel is
        // hidden; resync once it has a measurable box so their local optical
        // layers are not left display:none.
        window.TpxGlass?.sync?.(false);
    }
}

document.addEventListener('DOMContentLoaded', () => setTimeout(() => {
    buildChartLayerMenu();   // 面板還隱藏著,只對齊 class
}, 0));
// 點圖層選單以外的地方就收起來
document.addEventListener('click', (e) => {
    const pop = document.getElementById('chart-layer-pop');
    const btn = document.getElementById('chart-layer-btn');
    if (!pop || pop.classList.contains('hidden')) return;
    if (pop.contains(e.target) || (btn && btn.contains(e.target))) return;
    pop.classList.add('hidden');
});

function drawSessionDividers() {
    if (!candleSeries || !chart) return;
    const canvas = createSessionDividerCanvas();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth;
    const H = container.clientHeight;
    _sizeOrderflowCanvas(canvas, container, dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // Get visible time range
    const range = chart.timeScale().getVisibleRange();
    if (!range || !range.from || !range.to) return;

    // Iterate over each calendar day in the visible range, find each session boundary
    const fromMs = range.from * 1000;
    const toMs = range.to * 1000;
    const dayMs = 86400000;

    // Start one day earlier to catch boundaries that may be visible
    const startDay = new Date(fromMs - dayMs);
    startDay.setUTCHours(0, 0, 0, 0);
    const endMs = toMs + dayMs;

    // Keep session dividers visible without shading NY-open windows by default.

    ctx.strokeStyle = 'rgba(247, 239, 224, 0.25)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.font = '9px "IBM Plex Mono", monospace';
    ctx.fillStyle = 'rgba(247, 239, 224, 0.5)';
    ctx.textAlign = 'left';

    // 1.0.10: 盤段標籤從頂端移到底部時間軸正上方 —— 頂端是價格行為最密集的
    // 區域,標籤壓在那裡會擋住 K 棒;貼著時間軸則跟它標示的時間位置在同一側。
    let _axisH = 28;
    try {
        const ts = chart.timeScale();
        if (ts && typeof ts.height === 'function') _axisH = ts.height() || _axisH;
    } catch (e) { /* 舊版 lightweight-charts 沒有 height(),用預設值 */ }
    const labelY = Math.max(11, H - _axisH - 4);

    for (let d = startDay.getTime(); d <= endMs; d += dayMs) {
        const day = new Date(d);
        SESSION_BOUNDARIES.forEach(b => {
            const boundary = new Date(nyLocalToUtcMs(
                day.getUTCFullYear(),
                day.getUTCMonth(),
                day.getUTCDate(),
                b.h, b.m
            ));
            const bMs = boundary.getTime();
            if (bMs < fromMs - dayMs || bMs > toMs + dayMs) return;

            // Convert to lightweight-charts time in the configured market zone.
            const chartTime = utcMsToChartTime(bMs);
            const x = chart.timeScale().timeToCoordinate(chartTime);
            if (x === null || x < 0 || x > W) return;

            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, H);
            ctx.stroke();

            // 1.0.10: 標籤貼在時間軸上方,不再壓在頂端 K 棒上
            ctx.fillText(b.label, x + 3, labelY);
        });
    }

    drawNoTradeHatching(ctx, W, H, startDay.getTime(), endMs, fromMs, toMs);
}

function drawNYOpenZoneBackgrounds(ctx, W, H, startDayMs, endMs, fromMs, toMs) {
    const dayMs = 86400000;
    ctx.save();
    ctx.setLineDash([]);
    ctx.lineWidth = 1;
    ctx.font = '9px "IBM Plex Mono", monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';

    // Iterate a wider day span because chart timestamps are encoded as New
    // York wall-clock epochs while these windows are also New York local time.
    for (let d = startDayMs - dayMs; d <= endMs + dayMs; d += dayMs) {
        const day = new Date(d);
        NY_OPEN_ZONE_WINDOWS.forEach(w => {
            const startUtcMs = nyLocalToUtcMs(
                day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(),
                w.startH, w.startM
            );
            const endUtcMs = nyLocalToUtcMs(
                day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(),
                w.endH, w.endM
            );
            const startChartMs = utcMsToChartTime(startUtcMs) * 1000;
            const endChartMs = utcMsToChartTime(endUtcMs) * 1000;
            if (endChartMs < fromMs || startChartMs > toMs) return;

            const x1 = chart.timeScale().timeToCoordinate(utcMsToChartTime(startUtcMs));
            const x2 = chart.timeScale().timeToCoordinate(utcMsToChartTime(endUtcMs));
            if (x1 === null || x2 === null) return;
            const left = Math.max(0, Math.min(x1, x2));
            const right = Math.min(W, Math.max(x1, x2));
            if (right <= 0 || left >= W || right - left < 2) return;

            ctx.fillStyle = w.fill;
            ctx.strokeStyle = w.stroke;
            ctx.fillRect(left, 0, right - left, H);
            ctx.strokeRect(left + 0.5, 0.5, Math.max(0, right - left - 1), Math.max(0, H - 1));

            if (right - left >= 22) {
                ctx.fillStyle = w.text;
                ctx.fillText(w.label, left + 4, w.labelY);
            }
        });
    }
    ctx.restore();
}

// No-trade windows use their own hatch layer.  The risk/reward fill stays
// independent so zooming into or out of a window cannot change its intensity.
function drawNoTradeHatching(ctx, W, H, startDayMs, endMs, fromMs, toMs) {
    const dayMs = 86400000;
    const slope = Math.tan(80 * Math.PI / 180);
    const dx = H / slope;
    ctx.save();
    ctx.lineWidth = 0.8;
    ctx.strokeStyle = 'rgba(255, 48, 72, 0.55)';
    ctx.fillStyle = 'rgba(255, 48, 72, 0.035)';
    ctx.setLineDash([]);
    ctx.font = '9px "IBM Plex Mono", monospace';
    ctx.textAlign = 'left';

    for (let d = startDayMs; d <= endMs; d += dayMs) {
        const day = new Date(d);
        NO_TRADE_WINDOWS_ET.forEach(w => {
            const startMs = nyLocalToUtcMs(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), w.startH, w.startM);
            const endWindowMs = nyLocalToUtcMs(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), w.endH, w.endM);
            if (endWindowMs < fromMs || startMs > toMs) return;
            const x1 = chart.timeScale().timeToCoordinate(utcMsToChartTime(startMs));
            const x2 = chart.timeScale().timeToCoordinate(utcMsToChartTime(endWindowMs));
            if (x1 === null || x2 === null) return;
            const left = Math.max(0, Math.min(x1, x2));
            const right = Math.min(W, Math.max(x1, x2));
            if (right <= 0 || left >= W || right - left < 2) return;

            // 1.0.9: 移除整列淡紅底填色(fillRect)— 疊在其他半透明層上會
            // 變成灰白色豎柱;斜線 + 標籤已足夠標示禁交易時段。
            ctx.save();
            ctx.beginPath();
            ctx.rect(left, 0, right - left, H);
            ctx.clip();
            for (let base = left - dx - 14; base <= right + 14; base += 10) {
                ctx.beginPath();
                ctx.moveTo(base, H);
                ctx.lineTo(base + dx, 0);
                ctx.stroke();
            }
            ctx.restore();
            ctx.fillStyle = 'rgba(255, 48, 72, 0.65)';
            ctx.fillText(w.label, left + 4, 25);
            ctx.fillStyle = 'rgba(255, 48, 72, 0.035)';
        });
    }
    ctx.restore();
}

function clearFadeDailyLevels() {
    if (fadeLevelsCanvas) {
        const ctx = fadeLevelsCanvas.getContext('2d');
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, fadeLevelsCanvas.width, fadeLevelsCanvas.height);
    }
}

function drawFadeDailyLevels(zones) {
    if (!layerOn('dayzone')) { try { _clearCanvas('fade-level-overlay'); } catch (e) {} return; }
    const fadeZones = (zones || []).filter(z => String(z.timeframe || '').toLowerCase() === 'fade');
    if (!fadeZones.length) {
        clearFadeDailyLevels();
        return;
    }

    const canvas = createFadeLevelsCanvas();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth;
    const H = container.clientHeight;
    _sizeOrderflowCanvas(canvas, container, dpr);

    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const timeToX = (sec) => {
        if (sec == null || !Number.isFinite(sec)) return null;
        let x = null;
        try { x = chart.timeScale().timeToCoordinate(sec); } catch (_) {}
        if (x !== null && x !== undefined) return x;
        try {
            const vr = chart.timeScale().getVisibleRange();
            if (!vr || vr.to <= vr.from) return null;
            return (sec - vr.from) * (W / (vr.to - vr.from));
        } catch (_) { return null; }
    };

    let vFrom = null;
    let vTo = null;
    try {
        const vr = chart.timeScale().getVisibleRange();
        if (vr) { vFrom = vr.from; vTo = vr.to; }
    } catch (_) {}

    const rightEdge = W - 60;
    const drawLine = (x0, x1, y) => {
        if (y === null || y < -80 || y > H + 80) return;
        if (x1 < -20 || x0 > W + 20) return;
        x0 = Math.max(0, x0);
        x1 = Math.min(rightEdge, x1);
        if (x1 <= x0 + 2) return;
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(x1, y);
        ctx.stroke();
    };

    ctx.save();
    ctx.strokeStyle = 'rgba(247, 239, 224, 0.86)';
    ctx.lineWidth = 1;
    ctx.setLineDash([]);

    fadeZones.forEach(z => {
        const start = isoToChartTime(z.formed_at);
        const end = z.left_at ? isoToChartTime(z.left_at) : null;
        if (vFrom !== null && vTo !== null && end !== null && (end < vFrom || start > vTo)) return;

        let x0 = timeToX(start);
        let x1 = end !== null ? timeToX(end) : rightEdge;
        if (x0 === null && vFrom !== null && vTo !== null) x0 = (start - vFrom) * (W / (vTo - vFrom));
        if (x1 === null && end !== null && vFrom !== null && vTo !== null) x1 = (end - vFrom) * (W / (vTo - vFrom));
        if (x0 === null) return;
        if (x1 === null || x1 <= x0) x1 = rightEdge;

        const vah = Number(z.vah_80);
        const val = Number(z.val_80);
        if (!Number.isFinite(vah) || !Number.isFinite(val)) return;
        const yVAH = candleSeries.priceToCoordinate(vah);
        const yVAL = candleSeries.priceToCoordinate(val);
        drawLine(x0, x1, yVAH);
        drawLine(x0, x1, yVAL);
    });

    ctx.restore();
}

// -- Prior trade-day 70% value area overlay -----------------
// This is deliberately separate from the strategy's configurable 80% zones.
// Each row describes the completed source day and the following trade-day
// window on which its VAH/VAL/POC are displayed.
let _previousDayValueAreas = [];
let _previousDayValueAreasLoading = false;
let _previousDayValueAreasQueued = false;
let _previousDayValueAreasLastKey = '';
let _previousDayValueAreasLastRequestAt = 0;
const PREVIOUS_DAY_VALUE_AREA_REFRESH_MS = 60 * 1000;

function _previousDayValueAreaQuery() {
    const buf = _rawCandleBuffer || [];
    if (!buf.length) return null;
    const firstMs = chartTimeToUtcMs(Number(buf[0].time));
    const lastMs = chartTimeToUtcMs(Number(buf[buf.length - 1].time));
    if (!Number.isFinite(firstMs) || !Number.isFinite(lastMs)) return null;
    const contractEl = document.getElementById('contract-id');
    const symbol = (contractEl && contractEl.value && contractEl.value.trim()) || 'MNQ';
    return {
        start: new Date(Math.min(firstMs, lastMs)).toISOString(),
        end: new Date(Math.max(firstMs, lastMs)).toISOString(),
        symbol,
        key: [firstMs, lastMs, symbol].join('|'),
    };
}

async function refreshPreviousDayValueAreas(force) {
    if (!layerOn('prevday70')) {
        _previousDayValueAreas = [];
        try { _clearCanvas('previous-day-va-overlay'); } catch (e) {}
        return;
    }
    const query = _previousDayValueAreaQuery();
    if (!query) {
        _previousDayValueAreas = [];
        try { _clearCanvas('previous-day-va-overlay'); } catch (e) {}
        return;
    }
    if (_previousDayValueAreasLoading) {
        _previousDayValueAreasQueued = true;
        return;
    }
    const now = Date.now();
    if (!force && query.key === _previousDayValueAreasLastKey) {
        drawPreviousDayValueAreas();
        return;
    }
    // Live candles update every minute, but prior-day values only change at a
    // trade-day rollover. Keep the layer responsive without issuing a profile
    // calculation for every incoming candle.
    if (!force && now - _previousDayValueAreasLastRequestAt < PREVIOUS_DAY_VALUE_AREA_REFRESH_MS) {
        drawPreviousDayValueAreas();
        return;
    }

    _previousDayValueAreasLoading = true;
    _previousDayValueAreasLastRequestAt = now;
    try {
        const url = API + '/data/previous-day-value-areas'
            + '?start=' + encodeURIComponent(query.start)
            + '&end=' + encodeURIComponent(query.end)
            + '&symbol=' + encodeURIComponent(query.symbol);
        const resp = await fetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        _previousDayValueAreas = Array.isArray(data.areas) ? data.areas : [];
        _previousDayValueAreasLastKey = query.key;
    } catch (e) {
        // Optional chart annotation: keep the last successful values on a
        // transient backend/store error rather than blanking the chart.
    } finally {
        _previousDayValueAreasLoading = false;
        drawPreviousDayValueAreas();
        if (_previousDayValueAreasQueued) {
            _previousDayValueAreasQueued = false;
            refreshPreviousDayValueAreas(false);
        }
    }
}

function drawPreviousDayValueAreas() {
    if (!layerOn('prevday70') || !chart || !candleSeries) {
        try { _clearCanvas('previous-day-va-overlay'); } catch (e) {}
        return;
    }
    if (!_previousDayValueAreas.length) {
        try { _clearCanvas('previous-day-va-overlay'); } catch (e) {}
        return;
    }

    const canvas = createPreviousDayValueAreaCanvas();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth;
    const H = container.clientHeight;
    // Reuse the guarded resize path used by footprint/CVD.  Assigning
    // canvas.width/height on every scroll frame clears the backing store and
    // can make the three overlay layers compete for a fresh GPU surface.
    _sizeOrderflowCanvas(canvas, container, dpr);

    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    let vFrom = null;
    let vTo = null;
    try {
        const range = chart.timeScale().getVisibleRange();
        if (range) { vFrom = range.from; vTo = range.to; }
    } catch (_) {}

    const rightEdge = Math.max(0, W - 60);
    const timeToX = (sec) => {
        if (sec == null || !Number.isFinite(sec)) return null;
        try {
            const x = chart.timeScale().timeToCoordinate(sec);
            if (x !== null && x !== undefined) return x;
        } catch (_) {}
        if (vFrom === null || vTo === null || vTo <= vFrom) return null;
        return (sec - vFrom) * (W / (vTo - vFrom));
    };
    const drawLine = (x0, x1, price, color, dash) => {
        const y = candleSeries.priceToCoordinate(price);
        if (y === null || y < -80 || y > H + 80) return;
        if (x1 < -20 || x0 > W + 20 || x1 <= x0 + 2) return;
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.25;
        ctx.setLineDash(dash);
        ctx.beginPath();
        ctx.moveTo(Math.max(0, x0), y);
        ctx.lineTo(Math.min(rightEdge, x1), y);
        ctx.stroke();
        ctx.restore();
    };

    _previousDayValueAreas.forEach(area => {
        const start = area.start_at ? isoToChartTime(String(area.start_at)) : null;
        const end = area.end_at ? isoToChartTime(String(area.end_at)) : null;
        if (start === null || end === null || !Number.isFinite(start) || !Number.isFinite(end)) return;
        if (vFrom !== null && vTo !== null && (end < vFrom || start > vTo)) return;

        let x0 = timeToX(start);
        let x1 = timeToX(end);
        if (x0 === null && vFrom !== null && start <= vFrom) x0 = 0;
        if (x1 === null && vTo !== null && end >= vTo) x1 = rightEdge;
        if (x0 === null || x1 === null || x1 <= x0) return;

        const vah = Number(area.vah_70);
        const val = Number(area.val_70);
        const poc = Number(area.poc);
        // Exactly three lines per displayed trade day: VAH70, VAL70, and POC.
        if (Number.isFinite(vah)) drawLine(x0, x1, vah, 'rgba(80, 210, 255, 0.88)', []);
        if (Number.isFinite(val)) drawLine(x0, x1, val, 'rgba(80, 210, 255, 0.88)', []);
        if (Number.isFinite(poc)) drawLine(x0, x1, poc, 'rgba(255, 165, 0, 0.96)', [5, 4]);
    });
}

// -- Decision-zone overlay --
// Draw only the primary VAH/VAL range used by each trade decision.

function scrollToLatest() {
    try {
        _cancelChartPanInteraction();
        _setChartPriceAutoScale(true);
        chart.timeScale().scrollToRealTime();
    } catch (_) {}
}

let _footprintBars = [];
let _footprintMeta = null;
let _footprintRequestKey = '';
let _footprintRefreshTimer = null;
let _footprintAbortController = null;
let _orderflowDataPromise = null;
let _orderflowPendingKey = '';
let _orderflowRequestSerial = 0;
let _orderflowInteractionUntil = 0;
let _orderflowSettleTimer = null;

function markOrderflowInteraction() {
    _orderflowInteractionUntil = Math.max(_orderflowInteractionUntil,
        performance.now() + 260);
    clearTimeout(_orderflowSettleTimer);
    _orderflowSettleTimer = setTimeout(() => {
        _orderflowInteractionUntil = 0;
        _orderflowSettleTimer = null;
        scheduleChartOverlayRedraw();
    }, 280);
}

function _orderflowChartSpacing() {
    if (!chart) return null;
    try {
        const range = chart.timeScale().getVisibleLogicalRange();
        if (!range) return null;
        const center = Math.max(0, Math.floor((range.from + range.to) / 2));
        const x0 = chart.timeScale().logicalToCoordinate(center);
        const x1 = chart.timeScale().logicalToCoordinate(center + 1);
        const spacing = Math.abs(Number(x1) - Number(x0));
        return Number.isFinite(spacing) && spacing > 0 ? spacing : null;
    } catch (_) {
        return null;
    }
}

function _sizeOrderflowCanvas(canvas, container, dpr) {
    const width = Math.max(1, Math.round(container.clientWidth * dpr));
    const height = Math.max(1, Math.round(container.clientHeight * dpr));
    // Assigning canvas.width/height resets the entire backing store.  The old
    // renderer did that on every scroll frame, which caused visible flicker
    // and unnecessary GPU allocation even when the panel size was unchanged.
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    canvas.style.width = container.clientWidth + 'px';
    canvas.style.height = container.clientHeight + 'px';
}

// Order-flow data can be much denser than the chart can display.  These are
// display-only limits: _footprintBars remains the complete response/cache,
// while the canvas receives a pixel-sized summary when the chart is zoomed
// out.  Without this guard, one visible minute can create hundreds of
// overlapping fills, tier beads, and depth strokes.
const ORDERFLOW_DETAIL_CELL_LIMIT = 12000;
const ORDERFLOW_COMPACT_CELL_LIMIT = 900;
const ORDERFLOW_COMPACT_COLUMN_LIMIT = 1;
// Overview-only noise gate.  The cache sample is contract quantity, not
// dollars: MNQ 1m price cells top out around a few hundred contracts, so
// 1000 would blank the overview rather than isolate useful institutional flow.
// Delta mode uses the imbalance magnitude, not the gross traded volume, so
// balanced buy/sell cells do not dominate the one-cell-per-screen-column view.
const ORDERFLOW_COMPACT_MIN_DELTA = 50;
const ORDERFLOW_MAX_DEPTH_LEVELS = 8;

function _compactFootprintBars(prepared, spacing, tickSize, plotBottom,
    priceToY = (price) => candleSeries.priceToCoordinate(price)) {
    const bucketPx = Math.max(4, Math.min(10, spacing > 0 ? spacing : 4));
    const priceBucketPx = 4;
    const columns = new Map();
    for (const bar of prepared) {
        const xBucket = Math.round(bar.x / bucketPx);
        let column = columns.get(xBucket);
        if (!column) {
            column = {x: xBucket * bucketPx, cells: new Map()};
            columns.set(xBucket, column);
        }
        for (const cell of bar.cells) {
            if (!Array.isArray(cell) || cell.length < 5) continue;
            const y = priceToY(Number(cell[0]) * tickSize);
            if (y == null || y < -12 || y > plotBottom + 12) continue;
            const yBucket = Math.round(y / priceBucketPx);
            let aggregate = column.cells.get(yBucket);
            if (!aggregate) {
                aggregate = {
                    cell: [Number(cell[0]) || 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
                    importance: 0,
                };
                column.cells.set(yBucket, aggregate);
            }
            const target = aggregate.cell;
            target[1] += Math.max(0, Number(cell[1] || 0));
            target[2] += Math.max(0, Number(cell[2] || 0));
            target[3] += Math.max(0, Number(cell[3] || 0));
            target[4] += Math.max(0, Number(cell[4] || 0));
            // Depth is a level property rather than executed volume.  Keep
            // the strongest observed quote in the pixel bucket so overview
            // mode can still display the important walls without drawing
            // every one-minute sample.
            target[5] = Math.max(target[5], Number(cell[5] || 0));
            target[6] = Math.max(target[6], Number(cell[6] || 0));
            const deltaMagnitude = Math.abs(target[1] - target[2]);
            aggregate.importance = deltaMagnitude +
                0.25 * (target[3] + target[4]) +
                Math.sqrt(Math.max(target[5], target[6]));
        }
    }

    const bars = [];
    let kept = 0;
    for (const column of [...columns.values()].sort((a, b) => a.x - b.x)) {
        const allItems = [...column.cells.values()];
        const eligible = allItems.filter((item) => Math.abs(
            Number(item.cell[1] || 0) - Number(item.cell[2] || 0),
        ) >= ORDERFLOW_COMPACT_MIN_DELTA);
        // Keep the strongest visible price cell even when a quiet column has
        // no 50-lot aggregate; otherwise the price path would disappear.
        const source = (eligible.length ? eligible : allItems)
            .sort((a, b) => b.importance - a.importance);
        const cells = source
            .slice(0, ORDERFLOW_COMPACT_COLUMN_LIMIT)
            .map((item) => item.cell);
        if (!cells.length) continue;
        const remaining = ORDERFLOW_COMPACT_CELL_LIMIT - kept;
        if (remaining <= 0) break;
        const limited = cells.slice(0, remaining);
        bars.push({x: column.x, cells: limited});
        kept += limited.length;
    }
    return {bars, spacing: bucketPx};
}

function _footprintVisibleWindow() {
    const rows = window._lastChartData || [];
    if (!rows.length || !chart) return null;
    let range = null;
    try { range = chart.timeScale().getVisibleLogicalRange(); } catch (_) {}
    const first = Math.max(0, Math.floor(range ? range.from : 0));
    const last = Math.min(rows.length - 1, Math.ceil(range ? range.to : rows.length - 1));
    if (last < first) return null;
    const pad = 5 * 60;
    const fromTime = rows[first].time - pad;
    const toTime = rows[last].time + pad;
    const span = Math.max(60, toTime - fromTime);
    if (span > 14 * 86400) return {tooWide: true};
    return {
        start: new Date(chartTimeToUtcMs(fromTime)).toISOString(),
        end: new Date(chartTimeToUtcMs(toTime)).toISOString(),
        interval: span > 3 * 86400 ? '5m' : '1m',
    };
}

function scheduleFootprintRefresh() {
    if (!layerOn('footprint') && !layerOn('cvd')) return;
    clearTimeout(_footprintRefreshTimer);
    _footprintRefreshTimer = setTimeout(() => refreshOrderflowLayers(false), 180);
}

async function _loadOrderflowData(force) {
    const windowRange = _footprintVisibleWindow();
    if (!windowRange) return false;
    if (windowRange.tooWide) {
        _orderflowRequestSerial += 1;
        _footprintAbortController?.abort();
        _footprintBars = [];
        _footprintMeta = null;
        _footprintRequestKey = '';
        _orderflowPendingKey = '';
        return false;
    }
    const contract = String(document.getElementById('contract-id')?.value || 'MNQ');
    const query = new URLSearchParams({
        start: windowRange.start,
        end: windowRange.end,
        symbol: contract,
        interval: windowRange.interval,
        limit: '10000',
    });
    const key = query.toString();
    if (!force && key === _footprintRequestKey) return true;
    if (_orderflowDataPromise && _orderflowPendingKey === key) {
        await _orderflowDataPromise;
        return true;
    }
    _footprintAbortController?.abort();
    const requestId = ++_orderflowRequestSerial;
    const controller = new AbortController();
    _footprintAbortController = controller;
    _orderflowPendingKey = key;
    const request = (async () => {
        try {
            const response = await fetch(API + '/data/orderflow/footprint?' + key, {
                signal: controller.signal,
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const payload = await response.json();
            // AbortController is advisory: a mocked/browser-cached response
            // can still resolve after abort.  Never let an old viewport paint
            // over the newest one.
            if (requestId !== _orderflowRequestSerial || controller.signal.aborted) {
                return false;
            }
            _footprintRequestKey = key;
            _footprintBars = Array.isArray(payload.bars) ? payload.bars : [];
            _footprintMeta = payload.meta || null;
            return true;
        } catch (error) {
            if (error.name !== 'AbortError' && requestId === _orderflowRequestSerial) {
                log('Order-flow cache unavailable: ' + error.message, 'warn');
            }
            return false;
        }
    })();
    _orderflowDataPromise = request;
    try { return await request; }
    finally {
        if (_orderflowDataPromise === request) {
            _orderflowDataPromise = null;
            _orderflowPendingKey = '';
        }
    }
}

async function refreshOrderflowLayers(force) {
    if (!layerOn('footprint') && !layerOn('cvd')) {
        drawFootprintLayer();
        drawCvdLayer();
        return;
    }
    await _loadOrderflowData(force);
    drawFootprintLayer();
    drawCvdLayer();
}

async function refreshFootprintLayer(force) {
    await refreshOrderflowLayers(force);
}

async function refreshCvdLayer(force) {
    await refreshOrderflowLayers(force);
}

function drawFootprintLayer() {
    const canvas = footprintCanvas || document.getElementById('footprint-overlay');
    _syncFootprintCandleVisibility();
    if (!layerOn('footprint') || !_footprintBars.length) {
        if (canvas) {
            const context = canvas.getContext('2d');
            context.setTransform(1, 0, 0, 1, 0, 0);
            context.clearRect(0, 0, canvas.width, canvas.height);
        }
        return;
    }
    const target = createFootprintCanvas();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    _sizeOrderflowCanvas(target, container, dpr);
    const ctx = target.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, container.clientWidth, container.clientHeight);
    const plotBottom = container.clientHeight - _timeAxisHeight();
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, container.clientWidth, plotBottom);
    ctx.clip();

    const tickSize = Number(_footprintMeta?.tick_size || 0.25);
    // A full session repeats the same prices across thousands of cells.
    // Cache only within this paint: pan/zoom/autoscale/series replacement
    // must always resolve fresh coordinates on the next frame.
    const priceCoordinates = new Map();
    const priceToY = (price) => {
        if (!priceCoordinates.has(price)) {
            priceCoordinates.set(price, candleSeries.priceToCoordinate(price));
        }
        return priceCoordinates.get(price);
    };
    const depthReady = Number(_footprintMeta?.schema_version || 0) >= 4;
    let maxDelta = 1;
    let maxPassive = 1;
    let maxDepth = 1;
    let rawCellCount = 0;
    const prepared = [];
    for (const bar of _footprintBars) {
        const chartTime = isoToChartTime(String(bar.time));
        const x = _timeToXViaBars(chartTime);
        if (x == null || x < -30 || x > container.clientWidth + 30) continue;
        const cells = Array.isArray(bar.cells) ? bar.cells : [];
        rawCellCount += cells.length;
        for (const cell of cells) {
            const buy = Number(cell[1] || 0);
            const sell = Number(cell[2] || 0);
            maxDelta = Math.max(maxDelta, Math.abs(buy - sell));
            maxPassive = Math.max(maxPassive,
                Number(cell[3] || 0), Number(cell[4] || 0));
            maxDepth = Math.max(maxDepth, Number(cell[5] || 0), Number(cell[6] || 0));
        }
        prepared.push({x, cells});
    }
    const spacing = prepared.length > 1
        ? Math.abs(prepared[prepared.length - 1].x - prepared[0].x) / (prepared.length - 1)
        : (_orderflowChartSpacing() || 8);
    const compactMode = performance.now() < _orderflowInteractionUntil ||
        spacing < 12 || rawCellCount > ORDERFLOW_DETAIL_CELL_LIMIT;
    const compacted = compactMode
        ? _compactFootprintBars(prepared, spacing, tickSize, plotBottom, priceToY)
        : {bars: prepared, spacing};
    const renderBars = compacted.bars;
    const renderSpacing = compacted.spacing;
    const detailedNumbers = !compactMode && spacing >= 60;

    // Resting depth is drawn as a small set of continuous horizontal levels.
    // The old renderer painted one short stroke for every bar/cell, which
    // created the vertical comb visible at overview scale.  Scan the complete
    // visible response for the maximum depth per price level, then extend only
    // the strongest levels across their observed x-range.  This deliberately
    // does not use renderBars: compact delta selection must not hide a wall.
    const depthLevels = {bid: new Map(), ask: new Map()};
    for (const bar of (depthReady ? prepared : [])) {
        for (const cell of bar.cells) {
            if (!Array.isArray(cell) || cell.length < 7) continue;
            const tick = Number(cell[0]);
            if (!Number.isFinite(tick)) continue;
            for (const side of [
                {key: 'bid', index: 5},
                {key: 'ask', index: 6},
            ]) {
                const depth = Number(cell[side.index] || 0);
                if (!Number.isFinite(depth) || depth <= 0) continue;
                let level = depthLevels[side.key].get(tick);
                if (!level) {
                    level = {tick, max: depth, firstX: bar.x, lastX: bar.x};
                    depthLevels[side.key].set(tick, level);
                } else {
                    level.max = Math.max(level.max, depth);
                    level.firstX = Math.min(level.firstX, bar.x);
                    level.lastX = Math.max(level.lastX, bar.x);
                }
            }
        }
    }
    for (const side of [
        {key: 'bid', rgb: '255,255,255'},
        {key: 'ask', rgb: '255,45,70'},
    ]) {
        const levels = [...depthLevels[side.key].values()]
            .sort((a, b) => b.max - a.max)
            .slice(0, compactMode
                ? Math.floor(ORDERFLOW_MAX_DEPTH_LEVELS / 2)
                : ORDERFLOW_MAX_DEPTH_LEVELS);
        for (const level of levels) {
            const y = priceToY(level.tick * tickSize);
            if (y == null || y < -2 || y > plotBottom + 2) continue;
            const strength = Math.sqrt(level.max / maxDepth);
            const halfSpan = Math.max(8, renderSpacing * 0.6);
            const x0 = Math.max(0, level.firstX - halfSpan);
            const x1 = Math.min(container.clientWidth, level.lastX + halfSpan);
            ctx.strokeStyle = 'rgba(' + side.rgb + ',' + (0.28 + 0.62 * strength) + ')';
            ctx.lineWidth = 1 + 1.2 * strength;
            ctx.beginPath();
            ctx.moveTo(x0, y);
            ctx.lineTo(Math.max(x0 + 1, x1), y);
            ctx.stroke();
        }
    }

    for (const bar of renderBars) {
        for (const cell of bar.cells) {
            if (!Array.isArray(cell) || cell.length < 7) continue;
            maxDelta = Math.max(maxDelta, Math.abs(
                Number(cell[1] || 0) - Number(cell[2] || 0),
            ));
        }
    }

    function drawFootprintDeltaBar(x, y, delta, passive, maxWidth, cellHeight) {
        const signedDelta = Number(delta) || 0;
        const magnitude = Math.abs(signedDelta);
        if (!magnitude) return;
        const strength = Math.sqrt(magnitude / Math.max(1, maxDelta));
        const passiveStrength = Math.min(1,
            Math.log1p(Math.max(0, Number(passive) || 0)) /
            Math.log1p(Math.max(1, maxPassive)));
        const width = Math.max(compactMode ? 1 : 1.5, maxWidth * strength);
        const gap = compactMode ? 0 : 1;
        const fillWidth = Math.max(1, width - (compactMode ? 0 : 1));
        const alpha = compactMode
            ? 0.48 + 0.26 * strength + 0.10 * passiveStrength
            : 0.30 + 0.54 * strength + 0.16 * passiveStrength;
        const x0 = signedDelta < 0 ? x - width : x + gap;
        ctx.fillStyle = signedDelta >= 0
            ? 'rgba(245,248,252,' + alpha + ')'
            : 'rgba(255,45,70,' + alpha + ')';
        ctx.fillRect(x0, y - cellHeight / 2, fillWidth, cellHeight);
    }

    for (const bar of renderBars) {
        for (const cell of bar.cells) {
            if (!Array.isArray(cell) || cell.length < 7) continue;
            const price = Number(cell[0]) * tickSize;
            const y = priceToY(price);
            if (y == null || y < -12 || y > plotBottom + 12) continue;
            const buy = Number(cell[1] || 0);
            const sell = Number(cell[2] || 0);
            const passiveBid = Number(cell[3] || 0);
            const passiveAsk = Number(cell[4] || 0);
            // A 1m candle can contain hundreds of price cells.  Render one
            // signed delta bar per price: negative delta extends left in red,
            // positive delta extends right in white.  Compact mode keeps only
            // the strongest delta cell per screen column; detailed mode keeps
            // every visible price cell without overlapping bubbles.
            const delta = buy - sell;
            const barWidth = compactMode
                ? Math.max(1.5, Math.min(5, renderSpacing * 0.42))
                : Math.max(8, Math.min(96, renderSpacing * 0.42));
            const nextY = priceToY((Number(cell[0]) + 1) * tickSize);
            const cellHeight = Math.max(1.5, Math.min(14,
                nextY == null ? 4 : Math.abs(nextY - y) - 0.5));
            drawFootprintDeltaBar(
                bar.x, y, delta, delta < 0 ? passiveBid : passiveAsk,
                barWidth, cellHeight,
            );

            const total = buy + sell;
            const ratio = Math.max(buy, sell) / Math.max(1, Math.min(buy, sell));
            if ((!compactMode && total >= 10 || compactMode && total >= 50) && ratio >= 3) {
                ctx.fillStyle = buy > sell ? '#ffffff' : '#ff4562';
                ctx.font = '600 8px IBM Plex Mono, monospace';
                ctx.textAlign = 'center';
                ctx.fillText('I', bar.x, y - 7);
            }
            if (detailedNumbers && delta) {
                ctx.font = '8px IBM Plex Mono, monospace';
                ctx.textBaseline = 'middle';
                const labelWidth = Math.min(96, renderSpacing * 0.42);
                const label = delta > 0 ? '+' + String(delta) : String(delta);
                if (delta < 0) {
                    ctx.textAlign = 'right'; ctx.fillStyle = '#ff6a7f';
                    ctx.fillText(label, bar.x - labelWidth - 3, y);
                } else {
                    ctx.textAlign = 'left'; ctx.fillStyle = '#ffffff';
                    ctx.fillText(label, bar.x + labelWidth + 3, y);
                }
            }
        }
    }
    if (compactMode && renderBars.length) {
        ctx.font = '600 8px IBM Plex Mono, monospace';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'top';
        ctx.fillStyle = 'rgba(184, 198, 220, 0.72)';
        ctx.fillText('FOOTPRINT COMPACT · ZOOM IN FOR LEVELS',
            container.clientWidth - 10, 8);
    }
    ctx.restore();
}

function _cvdSessionDate(bar) {
    const stamp = Date.parse(String(bar?.time || ''));
    return Number.isFinite(stamp) ? new Date(stamp).toISOString().slice(0, 10) : '';
}

function drawCvdLayer() {
    const canvas = cvdCanvas || document.getElementById('cvd-overlay');
    if (!layerOn('cvd') || !_footprintBars.length) {
        if (canvas) {
            const context = canvas.getContext('2d');
            context.setTransform(1, 0, 0, 1, 0, 0);
            context.clearRect(0, 0, canvas.width, canvas.height);
        }
        return;
    }
    const target = createCvdCanvas();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    _sizeOrderflowCanvas(target, container, dpr);
    const ctx = target.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, container.clientWidth, container.clientHeight);

    const plotBottom = container.clientHeight - _timeAxisHeight();
    const panelHeight = Math.max(72, Math.min(142, plotBottom * 0.24));
    const panelTop = Math.max(8, plotBottom - panelHeight);
    const panelBottom = plotBottom - 2;
    const points = [];
    let fallbackCvd = 0;
    let previousDate = '';
    for (const bar of _footprintBars) {
        const date = _cvdSessionDate(bar);
        if (date && date !== previousDate) fallbackCvd = 0;
        previousDate = date;
        fallbackCvd += Number(bar.buy || 0) - Number(bar.sell || 0);
        const value = Number.isFinite(Number(bar.cvd)) ? Number(bar.cvd) : fallbackCvd;
        if (!Number.isFinite(value)) continue;
        const chartTime = isoToChartTime(String(bar.time));
        const x = _timeToXViaBars(chartTime);
        if (x == null || x < -30 || x > container.clientWidth + 30) continue;
        points.push({x, value, date, delta: Number(bar.delta || 0)});
    }
    if (!points.length) return;

    let min = 0;
    let max = 0;
    for (const point of points) {
        min = Math.min(min, point.value);
        max = Math.max(max, point.value);
    }
    if (max === min) max = min + 1;
    const yFor = value => panelBottom - ((value - min) / (max - min)) * (panelBottom - panelTop - 8);
    const zeroY = yFor(0);

    ctx.save();
    ctx.fillStyle = 'rgba(5, 12, 20, 0.72)';
    ctx.fillRect(0, panelTop, container.clientWidth, plotBottom - panelTop);
    ctx.beginPath();
    ctx.rect(0, panelTop, container.clientWidth, plotBottom - panelTop);
    ctx.clip();
    ctx.strokeStyle = 'rgba(100, 220, 255, 0.20)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, zeroY);
    ctx.lineTo(container.clientWidth, zeroY);
    ctx.stroke();

    for (let index = 1; index < points.length; index += 1) {
        const previous = points[index - 1];
        const point = points[index];
        if (point.date !== previous.date) continue;
        ctx.strokeStyle = point.value >= previous.value ? '#38d9ff' : '#ff526e';
        ctx.lineWidth = 1.35;
        ctx.beginPath();
        ctx.moveTo(previous.x, yFor(previous.value));
        ctx.lineTo(point.x, yFor(point.value));
        ctx.stroke();
    }
    ctx.restore();

    const latest = points[points.length - 1];
    ctx.font = '600 9px IBM Plex Mono, monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#38d9ff';
    ctx.fillText('CVD RTH', 8, panelTop + 5);
    ctx.fillStyle = latest.value >= 0 ? '#38d9ff' : '#ff526e';
    ctx.fillText(String(Math.round(latest.value)), 66, panelTop + 5);
    ctx.fillStyle = 'rgba(160, 180, 200, 0.62)';
    ctx.fillText('Δ ' + String(Math.round(latest.delta)), 110, panelTop + 5);
}

let posToolCanvas = null;

// 1.0.9: 時間→X 統一走「最近 bar 索引 + logicalToCoordinate」。
// timeToCoordinate 對非整分時間(live 成交帶秒數)或可視範圍外回 null;
// 舊 fallback 按可視「秒數」線性外推,遇到週末/維護縫隙會把幾分鐘的單
// 畫成超寬的半透明長方形(zoom out 特別明顯)。bar 索引空間不受縫隙影響。
function _nearestBarIndex(sec) {
    const cd = window._lastChartData;
    if (!cd || cd.length === 0) return null;
    let lo = 0, hi = cd.length - 1;
    if (sec <= cd[0].time) return 0;
    if (sec >= cd[hi].time) return hi;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (cd[mid].time < sec) lo = mid + 1; else hi = mid;
    }
    return (lo > 0 && (sec - cd[lo - 1].time) <= (cd[lo].time - sec)) ? lo - 1 : lo;
}

function _timeToXViaBars(sec) {
    if (sec == null || !Number.isFinite(sec)) return null;
    let x = null;
    try { x = chart.timeScale().timeToCoordinate(sec); } catch (_) {}
    if (x !== null && x !== undefined) return x;
    const idx = _nearestBarIndex(sec);
    if (idx === null) return null;
    // 1.0.9: 最近 bar 和目標時間差 > 30 分鐘 = 該時間不在已載入的資料範圍內。
    // live trade history 回溯 60 天,比圖表資料更早的交易會被 clamp 到第 1 根
    // bar,紅綠框畫在錯誤的位置(「飛出去」)。回 null → 該筆不畫框。
    // backtest 交易一定落在資料範圍內,不受影響。
    const cd = window._lastChartData;
    if (Math.abs(cd[idx].time - sec) > 1800) return null;
    try {
        const xi = chart.timeScale().logicalToCoordinate(idx);
        if (xi !== null && xi !== undefined) return xi;
    } catch (_) {}
    return null;
}

// 1.0.9: 底部時間軸高度 — 覆蓋層繪製夾在價格窗格內用
function _timeAxisHeight() {
    try {
        const h = chart.timeScale().height();
        if (h > 0) return h;
    } catch (_) {}
    return 28;
}

function createPosToolCanvas() {
    if (posToolCanvas) return posToolCanvas;
    const container = document.getElementById('chart-container');
    const canvas = document.createElement('canvas');
    canvas.id = 'pos-tool-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:4;';
    container.appendChild(canvas);
    posToolCanvas = canvas;
    return canvas;
}

function drawPositionTools(trades) {
    if (!layerOn('trades') || !trades || trades.length === 0) {
        clearPositionOverlay();
        return;
    }

    const canvas = createPosToolCanvas();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    _sizeOrderflowCanvas(canvas, container, dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, container.clientWidth, container.clientHeight);

    const chartW = container.clientWidth;
    const chartH = container.clientHeight;
    // Collect fills and paint each color as one compound path after all
    // decision lines are drawn. A compound path is a union, so overlapping
    // trades do not stack semi-transparent alpha into a brighter rectangle.
    const riskRewardFills = { green: [], red: [] };

    // 1.0.9: 繪製夾在價格窗格內 — 交易框/區間線不再蓋住底部時間軸
    ctx.beginPath();
    ctx.rect(0, 0, chartW, chartH - _timeAxisHeight());
    ctx.clip();

    // Only draw trades whose entry is visible in the current viewport
    let drawn = 0;
    const maxDraw = 25; // limit to avoid clutter

    // 1.0.9: 時間→X 改走 bar 索引映射(見 _timeToXViaBars)— 修復 zoom out 時
    // 短單的紅綠底框被線性外推畫成超寬灰白色長方形的問題。
    const timeToX = (sec) => _timeToXViaBars(sec);

    const drawHLine = (x0, x1, y, color) => {
        if (y === null || y < -50 || y > chartH + 50) return;
        ctx.save();
        ctx.lineWidth = 1;
        ctx.strokeStyle = color;
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(x1, y);
        ctx.stroke();
        ctx.restore();
    };

    const drawPrimaryZone = (t, fallbackEntryX) => {
        const z = t.primary_zone || {};
        const vah = Number(z.vah_80);
        const val = Number(z.val_80);
        if (!Number.isFinite(vah) || !Number.isFinite(val)) return false;
        const yVAH = candleSeries.priceToCoordinate(vah);
        const yVAL = candleSeries.priceToCoordinate(val);
        if (yVAH === null || yVAL === null) return false;
        if ((yVAH < -80 && yVAL < -80) || (yVAH > chartH + 80 && yVAL > chartH + 80)) return false;

        // 1.0.8: 一律按 zone 真實時間 (formed_at→left_at) 算座標,zoom 穩定;
        // 沒有 left_at(仍活躍)才用 entry 時間補右界。
        let x0 = z.formed_at ? timeToX(isoToChartTime(z.formed_at)) : null;
        let x1 = z.left_at ? timeToX(isoToChartTime(z.left_at)) : null;
        if (x0 === null) x0 = fallbackEntryX - 80;
        if (x1 === null || x1 <= x0) x1 = Math.max(fallbackEntryX, x0 + 8);
        if (x1 < -20 || x0 > chartW + 20) return false;
        x0 = Math.max(0, x0);
        x1 = Math.min(chartW - 60, x1);
        if (x1 <= x0 + 4) return false;

        drawHLine(x0, x1, yVAH, 'rgba(247, 239, 224, 0.78)');
        drawHLine(x0, x1, yVAL, 'rgba(247, 239, 224, 0.78)');
        return true;
    };

    const drawOrRange = (t, fallbackEntryX) => {
        const r = (t && t.or_range) || {};
        const orHigh = Number(r.or_high != null ? r.or_high : (r.high_100 != null ? r.high_100 : r.vah_80));
        const orLow = Number(r.or_low != null ? r.or_low : (r.low_100 != null ? r.low_100 : r.val_80));
        if (!Number.isFinite(orHigh) || !Number.isFinite(orLow)) return false;
        const yHigh = candleSeries.priceToCoordinate(orHigh);
        const yLow = candleSeries.priceToCoordinate(orLow);
        if (yHigh === null || yLow === null) return false;
        if ((yHigh < -80 && yLow < -80) || (yHigh > chartH + 80 && yLow > chartH + 80)) return false;

        let x0 = r.formed_at ? timeToX(isoToChartTime(r.formed_at)) : null;
        let xWindowEnd = r.left_at ? timeToX(isoToChartTime(r.left_at)) : null;
        if (x0 === null) x0 = fallbackEntryX - 90;
        if (xWindowEnd === null || xWindowEnd <= x0) xWindowEnd = x0 + 18;
        let xEnd = Math.max(fallbackEntryX, xWindowEnd);
        if (xEnd < -20 || x0 > chartW + 20) return false;
        x0 = Math.max(0, x0);
        xWindowEnd = Math.min(chartW - 60, xWindowEnd);
        xEnd = Math.min(chartW - 60, xEnd);
        if (xEnd <= x0 + 4) return false;

        const top = Math.min(yHigh, yLow);
        const h = Math.abs(yLow - yHigh);
        ctx.save();
        ctx.fillStyle = 'rgba(255, 167, 38, 0.07)';
        if (xWindowEnd > x0 + 2 && h > 1) ctx.fillRect(x0, top, xWindowEnd - x0, h);
        ctx.strokeStyle = 'rgba(255, 167, 38, 0.88)';
        ctx.lineWidth = 1;
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(x0, yHigh);
        ctx.lineTo(xEnd, yHigh);
        ctx.moveTo(x0, yLow);
        ctx.lineTo(xEnd, yLow);
        ctx.stroke();
        ctx.setLineDash([]);
        const label = _tradeIsBuy(t) ? 'OR15 low fake -> LONG' : 'OR15 high fake -> SHORT';
        const ly = _tradeIsBuy(t) ? yLow + 14 : yHigh - 6;
        if (ly > 10 && ly < chartH - 10) {
            ctx.font = '10px IBM Plex Mono, monospace';
            ctx.fillStyle = 'rgba(255, 167, 38, 0.92)';
            ctx.fillText(label, Math.max(4, Math.min(fallbackEntryX + 5, chartW - 190)), ly);
        }
        ctx.restore();
        return true;
    };

    const tradePrice = (t, keys) => {
        for (const k of keys) {
            const n = Number(t && t[k]);
            // Manual Topstep rows use 0 for an absent SL/TP. Zero is not a
            // tradable futures price, so it must not become a giant fallback
            // risk box when the off-screen fill is clipped.
            if (Number.isFinite(n) && n > 0) return n;
        }
        return NaN;
    };

    const drawRiskRewardBox = (t, entryX) => {
        const entry = tradePrice(t, ['entry_price', 'entry']);
        const sl = tradePrice(t, ['original_sl_price', 'sl_price', 'sl']);
        const tp = tradePrice(t, ['original_tp_price', 'tp_price', 'tp']);
        if (!Number.isFinite(entry) || !Number.isFinite(sl) || !Number.isFinite(tp)) return false;

        // 1.0.9: 進出場相隔 > 8 小時的「交易」是跨日配對殘影(本系統 SL/TP
        // 內日出場,不會持倉 8h+)— 不畫底框,箭頭標記照常顯示。
        if (t.entry_time && t.exit_time) {
            const durSec = isoToChartTime(t.exit_time) - isoToChartTime(t.entry_time);
            if (Number.isFinite(durSec) && durSec > 8 * 3600) return false;
        }

        const yEntry = candleSeries.priceToCoordinate(entry);
        const ySL = candleSeries.priceToCoordinate(sl);
        const yTP = candleSeries.priceToCoordinate(tp);
        if (yEntry === null || ySL === null || yTP === null) return false;

        // 1.0.8: 右界一律按 exit_time 的「時間」算(timeToX 帶外推),
        // 修復 zoom 時 2 分鐘的單被畫成固定 120px 長條的問題。
        let x1 = t.exit_time ? timeToX(isoToChartTime(t.exit_time)) : null;
        if (x1 === null || x1 <= entryX + 3) x1 = entryX + 8;
        if (x1 < -20 || entryX > chartW + 20) return false;
        const x0 = Math.max(0, entryX);
        const xEnd = Math.min(chartW - 60, x1);
        if (xEnd <= x0 + 3) return false;

        const greenTop = Math.min(yEntry, yTP);
        const greenH = Math.abs(yTP - yEntry);
        const redTop = Math.min(yEntry, ySL);
        const redH = Math.abs(ySL - yEntry);

        // Keep the visible part when SL/TP is outside the price pane.  The
        // canvas is clipped to the pane above, so an off-screen endpoint can
        // be safely trimmed instead of making the whole box disappear.
        const paneH = Math.max(0, chartH - _timeAxisHeight());
        const MIN_FILL_H = 2;
        const clippedFill = (top, height) => {
            if (!Number.isFinite(top) || !Number.isFinite(height) || height <= 0) return null;
            const visibleTop = Math.max(0, top);
            const visibleBottom = Math.min(paneH, top + height);
            if (visibleBottom - visibleTop < MIN_FILL_H) return null;
            return { top: visibleTop, height: visibleBottom - visibleTop };
        };
        const greenFill = clippedFill(greenTop, greenH);
        const redFill = clippedFill(redTop, redH);

        // Keep fills muted even when the trade crosses a no-trade window.
        // That window is already communicated by its own hatch layer; it must
        // not change the color of the entire risk/reward box.
        if (greenFill) {
            riskRewardFills.green.push({
                x: x0,
                y: greenFill.top,
                width: xEnd - x0,
                height: greenFill.height,
            });
        }
        if (redFill) {
            riskRewardFills.red.push({
                x: x0,
                y: redFill.top,
                width: xEnd - x0,
                height: redFill.height,
            });
        }
        return true;
    };

    const paintRiskRewardFills = (rects, fillStyle) => {
        if (!rects.length) return;
        ctx.save();
        // The position canvas is above the chart but also contains decision
        // lines. Painting behind existing pixels keeps those lines crisp while
        // the single fill call prevents alpha accumulation.
        ctx.globalCompositeOperation = 'destination-over';
        ctx.fillStyle = fillStyle;
        if (typeof Path2D === 'function') {
            const path = new Path2D();
            rects.forEach(r => path.rect(r.x, r.y, r.width, r.height));
            ctx.fill(path);
        } else {
            // Chromium supports Path2D. This fallback keeps older embedded
            // runtimes functional if they lack compound paths.
            rects.forEach(r => ctx.fillRect(r.x, r.y, r.width, r.height));
        }
        ctx.restore();
    };

    trades.forEach((t) => {
        if (drawn >= maxDraw) return;

        const entryTime = isoToChartTime(t.entry_time);
        // 1.0.9: live 成交時間帶秒數,timeToCoordinate 直查會回 null → bar 索引映射
        const entryX = timeToX(entryTime);
        if (entryX === null) return;
        if (entryX < -200 || entryX > chartW + 50) return;

        const drewZone = drawPrimaryZone(t, entryX);
        const drewRisk = drawRiskRewardBox(t, entryX);
        const drewOr = drawOrRange(t, entryX);
        if (!drewRisk && !drewZone && !drewOr) return;

        drawn++;
    });

    paintRiskRewardFills(
        riskRewardFills.green,
        'rgba(0, 229, 160, ' + CHART_RISK_REWARD_GREEN_ALPHA + ')',
    );
    paintRiskRewardFills(
        riskRewardFills.red,
        'rgba(255, 64, 96, ' + CHART_RISK_REWARD_RED_ALPHA + ')',
    );
}

// Legacy shim kept for older callers: live trades now use the same primary-zone
// decision overlay as backtests.
function drawLiveTrades(trades) {
    drawPositionTools(trades || []);
}

async function fetchAndDrawTradeHistory(refresh, accountId) {
    try {
        const params = new URLSearchParams();
        if (refresh) params.set('refresh', 'true');
        if (accountId) params.set('account_id', accountId);
        const qs = params.toString();
        const url = API + '/live/trade-history' + (qs ? '?' + qs : '');
        const resp = await fetch(url);
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.trades && data.trades.length > 0) {
            const trades = dedupeLiveCompletedTrades(data.trades);
            window._liveCompletedTrades = trades;
            _calLiveTrades = trades;
            const dedupeMsg = trades.length !== data.trades.length
                ? ' / ' + trades.length + ' unique'
                : '';
            log('[HISTORY] ' + data.count + ' live trades loaded' + dedupeMsg + ' (' + data.source + ') acct=' +
                (data.account_id || 'all'), 'info');

            renderExecuteTrades(trades);

            // Re-render metrics comparison if backtest already ran.  A refresh
            // initiated by the EXECUTE TRADES tab must not move the parameter
            // sidebar to PERFORMANCE as a side effect.
            if (backtestData && backtestData.metrics) {
                renderMetrics(backtestData.metrics, backtestData.trades, {
                    preserveSidebarScroll: executeTradesTabIsActive(),
                });
            }

            // setMarkers handles all viewports; the canvas overlay is redrawn with
            // both backtest and completed live decisions so SL/TP/zone style stays unified.
            drawLiveTradeMarkers(trades);
            if (_overlaySyncData) {
                drawFadeDailyLevels(_overlaySyncData.zones);
                redrawTradeDecisionOverlays();
                drawSessionDividers();
            }
        } else {
            window._liveCompletedTrades = [];
            _calLiveTrades = [];
            renderExecuteTrades([]);
            drawLiveTradeMarkers([]);
            if (_overlaySyncData) {
                drawFadeDailyLevels(_overlaySyncData.zones);
                redrawTradeDecisionOverlays();
                drawSessionDividers();
            }
            if (backtestData && backtestData.metrics) {
                renderMetrics(backtestData.metrics, backtestData.trades, {
                    preserveSidebarScroll: executeTradesTabIsActive(),
                });
            }
            log('[HISTORY] No live trades found (' + data.source + ')', 'info');
        }
    } catch(e) {
        // silent fail — cache may not exist yet
    }
}

function dedupeLiveCompletedTrades(trades) {
    const seen = new Set();
    return (trades || []).filter(t => {
        const entry = String(t.entry_time || '').slice(0, 19);
        const exit = String(t.exit_time || '').slice(0, 19);
        const key = [
            entry,
            exit,
            t.direction || '',
            Number(t.entry_price || 0).toFixed(2),
            Number(t.exit_price || 0).toFixed(2),
            Math.round(Number(t.pnl || 0)),
        ].join('|');
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

let _tradeHistoryRefreshInFlight = false;
let _lastLiveHadWorkingTrade = false;
let _lastLiveClosedKey = '';
let _executeLatestTradeKey = '';
let _lastExecuteHistoryPassiveRequestMs = 0;
const EXECUTE_HISTORY_PASSIVE_REFRESH_MS = 30 * 1000;

async function refreshTradeHistoryForCurrentAccount(refresh) {
    if (_tradeHistoryRefreshInFlight) return;
    _tradeHistoryRefreshInFlight = true;
    try {
        const mainAcc = _focusMainLiveAccount() || getMainLiveAccount();
        const accId = mainAcc ? mainAcc.id : (currentAccount ? currentAccount.id : 0);
        await fetchAndDrawTradeHistory(refresh, accId);
    } finally {
        _tradeHistoryRefreshInFlight = false;
    }
}

function executeTradesTabIsActive() {
    const tab = document.querySelector('.bottom-tab.active');
    return !!tab && tab.dataset.btab === 'execute';
}

function revealNewestExecuteTrade() {
    const tbody = document.getElementById('execute-tbody');
    const content = tbody ? tbody.closest('.bottom-content') : null;
    if (content) content.scrollTop = 0;
}

function refreshVisibleExecuteTrades(force) {
    if (!executeTradesTabIsActive()) return;
    const now = Date.now();
    if (!force && now - _lastExecuteHistoryPassiveRequestMs < EXECUTE_HISTORY_PASSIVE_REFRESH_MS) {
        return;
    }
    _lastExecuteHistoryPassiveRequestMs = now;
    refreshTradeHistoryForCurrentAccount(!!force);
}

function drawPendingOrderOverlay(po, idx) {
    // Deprecated: pending/live decision visuals are handled by
    // drawPositionTools() via the primary-zone overlay only.
}

function clearPositionOverlay() {
    if (posToolCanvas) {
        const ctx = posToolCanvas.getContext('2d');
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, posToolCanvas.width, posToolCanvas.height);
    }
    // Also remove any legacy DOM overlays
    document.querySelectorAll('.position-overlay').forEach(el => el.remove());
}

// -- Backtest Zone Drawing -------------------------

function drawBacktestZones(zones) {
    // DAY ZONE is the only legacy zone overlay retained here.
    clearFadeDailyLevels();
    drawFadeDailyLevels(zones);
}

// -- API Calls -------------------------------------

let _healthProbeInFlight = false;
let _healthBackendOffline = false;
let _healthStatusBeforeOffline = null;
// The health endpoint is cheap, but chart projection/rendering can briefly
// occupy the browser and the API worker. Keep real failures visible without
// turning one transient scheduling delay into an OFFLINE flash.
const HEALTH_CHECK_TIMEOUT_MS = 8000;
const HEALTH_CHECK_INTERVAL_MS = 5000;

async function checkHealth() {
    if (_healthProbeInFlight) return;
    _healthProbeInFlight = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), HEALTH_CHECK_TIMEOUT_MS);
    try {
        const resp = await fetch(API + '/health', {
            cache: 'no-store',
            signal: controller.signal,
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (data.status !== 'ok' || data.service !== 'ancserTPX') {
            throw new Error('Unexpected health response');
        }
        if (_healthBackendOffline) {
            _healthBackendOffline = false;
            const previous = _healthStatusBeforeOffline || { type: 'ok', text: 'ONLINE' };
            _healthStatusBeforeOffline = null;
            setStatus(previous.type, previous.text);
            log('Backend health restored.', 'success');
        } else if (_connectionStatusKind === 'idle') {
            setStatus('ok', 'ONLINE');
        }
    } catch(e) {
        if (!_healthBackendOffline) {
            _healthStatusBeforeOffline = {
                type: _connectionStatusKind,
                text: _connectionStatusText,
            };
            _healthBackendOffline = true;
            log('Backend health lost: ' + (e.name === 'AbortError' ? 'timeout' : e.message), 'error');
        }
        setStatus('err', 'BACKEND OFFLINE');
    } finally {
        clearTimeout(timeout);
        _healthProbeInFlight = false;
    }
}

function updateConnectionInitial(username) {
    const initial = document.getElementById('connection-initial');
    if (!initial) return;
    const value = String(username || '').trim();
    const first = value ? Array.from(value)[0].toLowerCase() : '?';
    initial.textContent = first;
    initial.setAttribute('aria-label', value ? 'Topstep user ' + first : 'Topstep user not configured');
}

function _topstepCredentialsPresent() {
    const username = document.getElementById('username');
    const apikey = document.getElementById('apikey');
    const userValue = String(username?.value || '').trim();
    const keyValue = String(apikey?.value || '').trim();
    const storedKey = apikey?.dataset?.configured === '1';
    return Boolean(userValue && (keyValue || storedKey));
}

function _providerRowStateClass(state) {
    return {
        empty: 'is-empty',
        error: 'is-error',
        starting: 'is-starting',
        connected: 'is-connected',
    }[String(state || '').toLowerCase()] || 'is-error';
}

function _renderProviderStatus(provider, payload) {
    const row = document.querySelector(
        '.chart-latency-row[data-provider="' + provider + '"]'
    );
    if (!row) return;
    const data = payload || {};
    const state = String(data.state || 'error').toLowerCase();
    const stateClass = _providerRowStateClass(state);
    const dot = document.getElementById('latency-dot-' + provider);
    const value = document.getElementById('latency-' + provider);
    const label = String(provider || '').toUpperCase();
    const latency = Number(data.latency_ms);
    const hasLatency = Number.isFinite(latency);
    if (dot) {
        dot.className = 'chart-latency-dot ' + stateClass;
        dot.setAttribute('aria-label', label + ' ' + state);
    }
    if (value) value.textContent = hasLatency ? String(Math.max(0, Math.round(latency))) : '--';
    row.dataset.connectionState = state;
    row.title = label + ' · ' + state + (data.detail ? ' · ' + String(data.detail) : '');
    row.setAttribute('aria-label', label + ' ' +
        (hasLatency ? Math.max(0, Math.round(latency)) + ' ms' : '-- ms') + ' · ' + state);
}

function _renderTopstepProviderFromUi() {
    const configured = _topstepCredentialsPresent();
    let state = 'empty';
    let detail = 'Topstep credentials not configured';
    if (configured && _topstepConnectInProgress) {
        state = 'starting';
        detail = 'Topstep connection starting';
    } else if (configured &&
            typeof _connectionStatusKind !== 'undefined' &&
            _connectionStatusKind === 'ok' &&
            /^CONNECTED$/i.test(_connectionStatusText || '')) {
        state = 'connected';
        detail = 'Topstep REST client connected';
    } else if (configured) {
        state = 'error';
        detail = 'Credentials loaded; connect required';
    }
    _renderProviderStatus('topstep', { state, configured, detail });
}

function _renderConnectionStatus(data) {
    const providers = (data && data.providers) || {};
    const localTopstep = _topstepCredentialsPresent();
    const topstep = Object.assign({}, providers.topstep || {});
    // The form can contain credentials before they are saved to .env. Keep
    // that local input authoritative until the backend sees the next connect.
    if (_topstepConnectInProgress) {
        topstep.state = 'starting';
        topstep.detail = 'Topstep connection starting';
    } else if (!localTopstep) {
        topstep.state = 'empty';
        topstep.configured = false;
        topstep.detail = 'Topstep credentials not configured';
    } else if (topstep.state === 'empty') {
        topstep.state = 'error';
        topstep.configured = true;
        topstep.detail = 'Credentials loaded; connect required';
    }
    _renderProviderStatus('topstep', topstep);
    _renderProviderStatus('discord', providers.discord || { state: 'empty' });
    _renderProviderStatus('databento', providers.databento || { state: 'empty' });
}

async function refreshConnectionStatus() {
    if (_connectionStatusProbeInFlight) return;
    _connectionStatusProbeInFlight = true;
    try {
        const resp = await fetch(API + '/connection/status', {
            cache: 'no-store',
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        _renderConnectionStatus(await resp.json());
    } catch (_) {
        // The existing backend health indicator owns the hard offline message.
        // Keep the last provider snapshot visible during a transient poll gap.
        _renderTopstepProviderFromUi();
    } finally {
        _connectionStatusProbeInFlight = false;
    }
}

function startConnectionStatusMonitor() {
    refreshConnectionStatus();
    if (!_connectionStatusPollTimer) {
        _connectionStatusPollTimer = setInterval(
            refreshConnectionStatus, CONNECTION_STATUS_POLL_INTERVAL_MS);
    }
}

async function connectAPI() {
    const btn = document.getElementById('btn-connect');
    const username = document.getElementById('username').value.trim();
    const apikey = document.getElementById('apikey').value.trim();
    const contractId = document.getElementById('contract-id')?.value.trim() || defaultContractId();
    if (btn.dataset.busy === '1') {
        log('Already connecting; ignored duplicate click.', 'warn');
        return;
    }
    _topstepConnectInProgress = true;
    btn.dataset.busy = '1';
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"><span></span><span></span><span></span><span></span></span> CONNECTING...';
    setStatus('loading', 'CONNECTING...');
    log('Connecting to TopstepX API...', 'info');

    // 1.0.10: 硬性逾時。實測後端全部 200 回來了,前端卻仍卡在 CONNECTING ——
    // 中間某個 await 沒有 resolve。與其逐一追,不如保證 UI 一定會還原:
    // 逾時後把按鈕與狀態燈復位,並明確告訴使用者連線沒完成。
    const _connWatchdog = setTimeout(() => {
        if (btn.dataset.busy !== '1') return;
        btn.dataset.busy = '';
        btn.disabled = false;
        btn.textContent = 'CONNECT';
        setStatus('err', 'CONNECT TIMED OUT');
        log('Connection timed out after 60 seconds; UI was released.', 'error');
    }, 60000);

    // CONNECT loads only the recent warm-up window (CONNECT_WARMUP_DAYS) so the
    // app is interactive immediately. The full multi-year range is pulled lazily
    // on the first backtest / ML / LEARN & LIVE click (_ensureBacktestData).
    const _now = new Date();
    const startDate = new Date(_now.getTime() - CONNECT_WARMUP_DAYS * 86400000)
        .toISOString().slice(0, 10);
    const endDate   = _now.toISOString().slice(0, 10);

    // The connection form keeps the instrument internal; use the configured
    // front-month root when the hidden compatibility value is unavailable.
    const resolvedContract = contractId || defaultContractId();

    const body = {
        unit: 2,
        unit_number: 1,
        load_scope: 'connect',
        start_time: startDate + 'T00:00:00Z',
        end_time: endDate + 'T23:59:59Z',
        continuous_contract: true,
    };
    if (username) body.username = username;
    if (apikey) body.api_key = apikey;
    if (resolvedContract) body.contract_id = resolvedContract;

    try {
        const resp = await fetch(API + '/data/fetch-historical', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || resp.statusText);
        }

        const data = await resp.json();

        if (!data.success) {
            throw new Error('API returned success=false');
        }

        setStatus('ok', 'CONNECTED');
        document.getElementById('conn-trigger').classList.add('connected');
        document.getElementById('btn-backtest').disabled = false;
        const btnRunAll = document.getElementById('btn-run-all');
        if (btnRunAll) btnRunAll.disabled = false;
        const btnFullFilter = document.getElementById('btn-full-filter');
        if (btnFullFilter) btnFullFilter.disabled = false;
        // CONNECT only loaded the recent warm-up window → record it so the first
        // backtest / ML / LEARN sees the range mismatch and pulls the full history.
        _btDataRange = {
            start: startDate,
            end: endDate,
            contract: resolvedContract || '',
            resolvedContract: data.contract_id || '',
            worksetToken: data.workset_token || '',
        };

        // Auto-save credentials to .env if user typed them
        if (username || apikey) {
            const saveBody = {};
            if (username) saveBody.username = username;
            if (apikey) saveBody.api_key = apikey;
            try {
                await fetch(API + '/save-config', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(saveBody)
                });
                log('Credentials saved to .env', 'success');
            } catch(e) { /* silent */ }
        }

        log('Connected // Contract: ' + data.contract_id, 'success');
        if (data.contracts && data.contracts.length > 1) {
            log('Continuous contract: ' + data.contracts.map(contractLabelFromId).join(' + '), 'info');
        }
        if (data.continuous && data.continuous.roll_at) {
            const adj = Number(data.continuous.price_adjustment || 0);
            log('Roll adjusted @ ' + data.continuous.roll_at + ' | old contract offset ' + adj.toFixed(2), 'info');
        }
        log('Loaded ' + data.candles_count + ' bars (' + data.interval + ') from ' + data.first + ' to ' + data.last, 'success');

        // Close connection dropdown
        toggleConnDropdown(false);

        // Load accounts after successful connect (await so account is ready)
        await loadAccounts();

        // Fetch and display chart data (1m bars from connect — fresh, no settle delay)
        await fetchAndShowChart('1m');

        // Fetch actual trades from TopstepX (refresh cache) for the active account
        const mainAcc = _focusMainLiveAccount() || getMainLiveAccount() || currentAccount;
        const accId = mainAcc ? mainAcc.id : 0;
        await fetchAndDrawTradeHistory(true, accId);

    } catch(e) {
        setStatus('err', 'FAILED');
        log('Connection failed: ' + e.message, 'error');
    } finally {
        _topstepConnectInProgress = false;
        clearTimeout(_connWatchdog);
        btn.dataset.busy = '';
        btn.disabled = false;
        btn.textContent = 'CONNECT';
        _renderTopstepProviderFromUi();
        refreshConnectionStatus();
    }
}

async function fetchAndShowChart(interval, preserveViewport = false) {
    try {
        // Only fetch the most recent slice for charting — the full range can be
        // hundreds of thousands of 1m bars and rendering them all freezes the tab.
        // Backtest / machine learning still use the full backend dataset.
        const resp = await fetch(API + '/data/candles?limit=' + CHART_MAX_CANDLES);
        const data = await resp.json();
        if (data.candles && data.candles.length > 0) {
            showCandleData(data.candles, preserveViewport);
            const shown = data.shown != null ? data.shown : data.candles.length;
            log('Chart showing ' + shown + ' / ' + data.count + ' candles (recent slice)', 'info');
            return;
        }
    } catch(e) {}

    log('No candle data available -- click CONNECT to fetch historical data', 'info');
}

function _captureChartViewport() {
    if (!chart) return null;
    let visible = null;
    let logical = null;
    try { visible = chart.timeScale().getVisibleRange(); } catch (_) {}
    try { logical = chart.timeScale().getVisibleLogicalRange(); } catch (_) {}
    if (!visible && !logical) return null;
    return { visible, logical };
}

function _restoreChartViewport(viewport) {
    if (!chart || !viewport) return false;
    if (viewport.visible) {
        try {
            chart.timeScale().setVisibleRange(viewport.visible);
            return true;
        } catch (_) {}
    }
    if (viewport.logical) {
        try {
            chart.timeScale().setVisibleLogicalRange(viewport.logical);
            return true;
        } catch (_) {}
    }
    return false;
}

function showCandleData(candles, preserveViewport = false) {
    // Capture at the moment the new data is actually applied, not when its
    // request started.  A user can begin panning while a catch-up fetch is in
    // flight; restoring an older snapshot is the exact jump this guard avoids.
    const preservedViewport = preserveViewport ? _captureChartViewport() : null;
    _cancelChartPanInteraction();
    // Convert and deduplicate by time, sort ascending
    const seen = new Set();
    const chartData = [];
    const rawBuf = [];

    candles.forEach(c => {
        const t = isoToChartTime(c.time);
        if (seen.has(t)) return;
        seen.add(t);
        chartData.push({ time: t, open: c.open, high: c.high, low: c.low, close: c.close });
        rawBuf.push({ time: t, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume || 0 });
    });

    chartData.sort((a, b) => a.time - b.time);
    rawBuf.sort((a, b) => a.time - b.time);
    _rawCandleBuffer = rawBuf;
    _chartHistoryLoading = false;
    _chartHistoryExhausted = false;
    _chartHistorySuppressUntil = Date.now() + 500;

    candleSeries.setData(chartData);
    _syncFootprintCandleVisibility();
    window._lastChartData = chartData;

    if (!_restoreChartViewport(preservedViewport)) applyDefaultChartView(chartData);
    drawSessionDividers();
    if (layerOn('prevday70')) refreshPreviousDayValueAreas(true);
    refreshIndicatorSignalMarkers(true);
    refreshPiSignalMarkers();
    if (layerOn('optionwall')) refreshOptionWallLayer();
    if (layerOn('footprint') || layerOn('cvd')) refreshOrderflowLayers(true);
}

function applyDefaultChartView(chartData, zones) {
    if (!chartData || chartData.length === 0) {
        _setChartPriceAutoScale(true);
        chart.timeScale().fitContent();
        return;
    }

    // Determine candle interval in seconds
    let intervalSec = 60; // default 1min
    if (chartData.length >= 2) {
        intervalSec = chartData[1].time - chartData[0].time;
        if (intervalSec <= 0) intervalSec = 60;
    }

    // Keep the historical 18-hour scale, but place the latest smooth-200
    // window at the viewport midpoint.  The remaining right-side whitespace
    // is intentional and gives the live chart room to breathe.
    const totalVisibleBars = Math.max(
        1,
        Math.round((CHART_DEFAULT_VISIBLE_HOURS * 3600) / intervalSec)
    );
    const totalBars = chartData.length;
    const smoothBars = Math.min(CHART_DEFAULT_SMOOTH_BARS, totalBars);
    const smoothCenter = totalBars - ((smoothBars + 1) / 2);
    const visibleBars = Math.max(1, Math.min(totalVisibleBars, Math.max(1, smoothCenter * 2)));
    const halfVisibleBars = visibleBars / 2;

    let fromIdx = smoothCenter - halfVisibleBars;
    let toIdx = smoothCenter + halfVisibleBars;
    if (fromIdx < 0) {
        toIdx -= fromIdx;
        fromIdx = 0;
    }

    _setChartPriceAutoScale(true);
    chart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: toIdx });
}

function buildBacktestBody() {
    const params = reconcilePresetStrategyForDispatch('bt', collectStrategyParams('bt'), 'BACKTEST');
    // v1.0.6: ML (confluence, explainable) backtest is selected via the STRATEGY dropdown.
    const confParams = collectConfluenceParams('bt');
    if (false && confParams) {
        params.strategy = 'confluence';
        Object.assign(params, confParams);
    }
    return {
        initial_capital: 50000,
        ...params,
        workset_token: (_btDataRange && _btDataRange.worksetToken) || '',
    };
}

// ── Backtest data lazy-loader ──────────────────────
// Tracks which date range is currently loaded in the backend.
// CONNECT only loads 14 days (fast); full range is fetched on first backtest or Machine Learning click.
let _btDataRange = null;  // { start, end, contract, resolvedContract, worksetToken }

function _profitLockBoundaryISO(dateStr) {
    const parts = String(dateStr || '').split('-').map(Number);
    if (parts.length !== 3 || parts.some(n => !Number.isFinite(n))) return dateStr + 'T00:00:00Z';
    // Follow Topstep/CME trading-day boundary: California 3:15pm (a ~1h
    // maintenance gap follows, so the prior trading day is complete by then).
    return new Date(parts[0], parts[1] - 1, parts[2], 15, 15, 0, 0).toISOString();
}

async function _ensureBacktestData(btn, overrideStart, overrideEnd, force) {
    // overrideStart / overrideEnd let callers use a fixed window instead of
    // the hidden full-range defaults.
    const startDate = overrideStart || document.getElementById('start-date').value;
    const endDate   = overrideEnd   || document.getElementById('end-date').value;
    const username   = document.getElementById('username').value.trim();
    const apikey     = document.getElementById('apikey').value.trim();
    const contractId = document.getElementById('contract-id')?.value.trim()
        || defaultContractId();
    const sameContract = _btDataRange && (_btDataRange.contract || '') === (contractId || '');

    let fetchStartTime = startDate + 'T00:00:00Z';
    let fetchLabel = startDate;
    let appendFetch = false;
    if (sameContract && _btDataRange.start === startDate && _btDataRange.end < endDate) {
        fetchStartTime = _profitLockBoundaryISO(_btDataRange.end);
        fetchLabel = _btDataRange.end + ' 15:15 PT';
        appendFetch = true;
    }

    btn.innerHTML = '<span class="think-dots"><span></span><span></span><span></span><span></span></span> loading data...';
    log((appendFetch ? 'Syncing new backtest data' : 'Fetching backtest data') + ' (' + fetchLabel + ' -> ' + endDate + ')...', 'info');

    const body = { unit: 2, unit_number: 1, load_scope: 'backtest',   // always 1m bars for backtest / machine learning
        start_time: fetchStartTime,
        end_time:   endDate   + 'T23:59:59Z',
        append: appendFetch,
        continuous_contract: true,
    };
    if (sameContract && _btDataRange.worksetToken) {
        body.workset_token = _btDataRange.worksetToken;
    }
    if (username)    body.username    = username;
    if (apikey)      body.api_key     = apikey;
    if (contractId)  body.contract_id = contractId;

    try {
        let resp = await fetch(API + '/data/fetch-historical', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        // Only the current backend workset is retained. If another selection
        // superseded this token, reselect the full requested range once.
        if (resp.status === 409 && body.workset_token) {
            delete body.workset_token;
            body.append = false;
            body.start_time = startDate + 'T00:00:00Z';
            resp = await fetch(API + '/data/fetch-historical', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        }
        if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || resp.statusText); }
        const data = await resp.json();
        _btDataRange = {
            start: startDate,
            end: endDate,
            contract: contractId || '',
            resolvedContract: data.contract_id || '',
            worksetToken: data.workset_token || '',
        };
        if (data.contracts && data.contracts.length > 1) {
            log('Continuous contract: ' + data.contracts.map(contractLabelFromId).join(' + '), 'info');
        }
        if (data.continuous && data.continuous.roll_at) {
            const adj = Number(data.continuous.price_adjustment || 0);
            log('Roll adjusted @ ' + data.continuous.roll_at + ' | old contract offset ' + adj.toFixed(2), 'info');
        }
        const storeTag = data.from_store ? ' [local store + incremental]' : '';
        log('Backtest data ready: ' + data.candles_count + ' bars' + (data.fetched_count != null ? ' (' + data.fetched_count + ' fetched)' : '') + storeTag, 'success');
        // Load the expanded dataset without taking ownership of the user's
        // current horizontal viewport.  The first ever chart load has no
        // viewport to preserve and still receives the default smooth-200 view.
        await fetchAndShowChart('1m', true);
        return true;
    } catch(e) {
        log('Data fetch failed: ' + e.message, 'error');
        return false;
    }
}

async function _postBacktestWithWorksetRetry(url, body, btn) {
    const send = () => fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    let resp = await send();
    if (resp.status !== 409 || !body.workset_token) return resp;

    log('Backtest data selection changed; reselecting once before retry...', 'warn');
    const ready = await _ensureBacktestData(btn);
    if (!ready) return resp;
    Object.assign(body, buildBacktestBody());
    return await send();
}

let _backtestProgressInterval = null;
let _lastBacktestProgressStage = '';

function _startBacktestProgress() {
    const wrap = document.getElementById('backtest-progress-wrap');
    const bar = document.getElementById('backtest-progress-bar');
    const text = document.getElementById('backtest-progress-text');
    if (wrap) wrap.style.display = 'block';
    if (bar) {
        bar.style.width = '0%';
        bar.style.background = 'var(--green)';
    }
    if (text) text.textContent = 'preparing';
    _lastBacktestProgressStage = '';

    const poll = async () => {
        try {
            const resp = await fetch(API + '/backtest/progress');
            if (!resp.ok) return;
            const d = await resp.json();
            const stage = d.stage || 'preparing';
            const current = Number(d.current) || 0;
            const total = Number(d.total) || 0;
            const detail = d.detail ? ' — ' + d.detail : '';
            if (text) {
                text.textContent = stage + (total > 0 ? ' ' + current + ' / ' + total : '') + detail;
            }
            if (bar) {
                bar.style.width = (total > 0 ? Math.min(100, current / total * 100) : 0).toFixed(1) + '%';
            }
            if (stage !== _lastBacktestProgressStage && stage !== 'complete') {
                _lastBacktestProgressStage = stage;
                log('Backtest: ' + stage + detail, d.status === 'error' ? 'error' : 'info');
            }
        } catch(e) { /* non-blocking progress poll */ }
    };
    poll();
    _backtestProgressInterval = setInterval(poll, 500);
}

function _stopBacktestProgress(success) {
    if (_backtestProgressInterval) {
        clearInterval(_backtestProgressInterval);
        _backtestProgressInterval = null;
    }
    const wrap = document.getElementById('backtest-progress-wrap');
    const bar = document.getElementById('backtest-progress-bar');
    const text = document.getElementById('backtest-progress-text');
    if (bar) {
        bar.style.width = success ? '100%' : bar.style.width;
        if (!success) bar.style.background = 'var(--red)';
    }
    if (text) text.textContent = success ? 'complete' : 'failed';
    setTimeout(() => { if (wrap) wrap.style.display = 'none'; }, success ? 1200 : 3500);
}

// Programmatic switching for the lower navigation tabs.
function _showBottomTab(name) {
    document.querySelectorAll('.bottom-tab').forEach(x => x.classList.toggle('active', x.dataset.btab === name));
    ['trades', 'execute', 'log'].forEach(id => {
        const p = document.getElementById('btab-' + id);
        if (p) p.classList.toggle('hidden', id !== name);
    });
    _animateBottomPane(document.getElementById('btab-' + name));
    if (name === 'log') scrollSystemLogToBottom();
    if (name === 'execute') {
        revealNewestExecuteTrade();
        refreshVisibleExecuteTrades(true);
    }
}

async function runBacktest() {
    const btn = document.getElementById('btn-backtest');
    if (!btn) return;
    btn.disabled = true;
    let succeeded = false;
    let progressStarted = false;

    try {
        // Keep every preflight step inside the same guard. A malformed
        // parameter must never leave the button disabled at "thinking...".
        const dataOk = await _ensureBacktestData(btn);
        if (!dataOk) return;

        btn.innerHTML = '<span class="think-dots"><span></span><span></span><span></span><span></span></span> thinking...';
        const btBody = buildBacktestBody();
        const _sess = btBody.tr_allowed_sessions || btBody.conf_allowed_sessions || 'ALL';
        const _sessLabel = Array.isArray(_sess) ? _sess.join('+') : String(_sess);
        log('BT PARAMS → strategy=' + (btBody.strategy || '?')
            + ' session=' + _sessLabel
            + ' ' + (btBody.strategy === 'trend' ? trendTfUsageText(btBody) : ('TF=' + (btBody.area_timeframe || '?')))
            + ' RR=1:' + (btBody.rr_ratio || '?')
            + ' SL=' + (btBody.sl_ticks || '?') + 't'
            + (btBody.conf_sl_reference_tf ? (' SLref=' + btBody.conf_sl_reference_tf) : '')
            + ' trail=' + (btBody.trail_trigger_pct > 0 ? (btBody.trail_trigger_pct * 100).toFixed(0) + '%' : 'OFF')
            + ' confirm=' + (btBody.breakout_confirm_bars || '?')
            , 'info');
        log('Running backtest...', 'info');
        _startBacktestProgress();
        progressStarted = true;

        const resp = await _postBacktestWithWorksetRetry(
            API + '/backtest/run', btBody, btn,
        );

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || resp.statusText);
        }

        backtestData = await resp.json();

        const piReplayCount = Number(backtestData.pi_replay_count || 0);
        if (piReplayCount > 0 && String(btBody.strategy || '').toLowerCase() === 'pi') {
            log('PI replay overlay → ' + piReplayCount + ' in-range audit mark(s)', 'info');
        }

        log('Backtest complete // ' + backtestData.metrics.total_trades + ' trades // ' +
            'Win rate: ' + (backtestData.metrics.win_rate * 100).toFixed(1) + '% // ' +
            'PnL: $' + backtestData.metrics.total_pnl.toFixed(0), 'success');

        renderChart(backtestData);
        renderMetrics(backtestData.metrics, backtestData.trades);
        renderTrades(backtestData.trades);
        _saveBacktestCache(backtestData);   // persist for next app restart
        try {
            setPerfSource({
                preset: backtestData.preset_name,
                strategy: normalizeStrategyName(_mlSelectValue('strategy-bt', 'factor')),
                saved_at: new Date().toISOString(), stale: false,
            });
        } catch (e) {}
        await refreshTradeHistoryForCurrentAccount(true);
        succeeded = true;

    } catch(e) {
        log('Backtest failed: ' + e.message, 'error');
    } finally {
        if (progressStarted) _stopBacktestProgress(succeeded);
        btn.disabled = false;
        btn.textContent = 'EXECUTE BACKTEST';
    }
}

// -- Machine Learning -----------------------------------


// -- Bottom-panel drag-to-resize -------------------
(function () {
    const handle = document.getElementById('bottom-drag-handle');
    const panel  = document.getElementById('bottom-panel');
    if (!handle || !panel) return;

    let dragging = false;
    let startY   = 0;
    let startH   = 0;
    const MIN_H  = 80;
    const MAX_H  = () => Math.floor(window.innerHeight * 0.75);

    handle.addEventListener('mousedown', e => {
        dragging = true;
        startY   = e.clientY;
        startH   = panel.offsetHeight;
        document.body.style.cursor     = 'row-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
    });

    document.addEventListener('mousemove', e => {
        if (!dragging) return;
        const dy   = startY - e.clientY;           // drag up = +dy = taller
        const newH = Math.min(Math.max(startH + dy, MIN_H), MAX_H());
        panel.style.height    = newH + 'px';
        panel.style.maxHeight = 'none';
    });

    document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        document.body.style.cursor     = '';
        document.body.style.userSelect = '';
    });
})();

// -- Render ----------------------------------------

let _overlaySyncRAF = null;
let _overlaySyncData = null;
let _indicatorSignalRows = [];
let _indicatorSignalCanvas = null;
let _indicatorSignalsLoading = false;
let _indicatorSignalsQueued = false;
let _lastIndicatorSignalLogKey = '';
let _backtestMarkers = [];
let _liveMarkers = [];
let _liveRealtimeMarkers = [];

const INDICATOR_SIGNAL_TYPES = {
    // 1.0.9: EMAPMO shows a large up/down triangle above/below the candle
    // (candle-body tint was invisible on 1m bars). long → up triangle under
    // the low; short → down triangle over the high.
    emapmo: { kind: 'triangle', radius: 6 },
    // 1.0.9: MREV 泡泡縮小到與 KDJMA 圓點同尺寸 (16 → 6)
    momentum_reversion: { kind: 'bubble', radius: 6 },
    icefishball: { kind: 'dot', radius: 6 },
    // 1.0.9: INTRAMOM —— 白色上下箭頭,與三個因子的顏色明確區隔。
    // 進場時刻絕大多數是 22:30 UTC(交易日開始後 30 分鐘)。
    intramom: { kind: 'arrow', radius: 7, rgb: '255, 255, 255' },
};
const INDICATOR_LONG_RGB = '56, 189, 248';
const INDICATOR_SHORT_RGB = '168, 85, 247';
const INDICATOR_KDJMA_LONG_RGB = '250, 204, 21';
const INDICATOR_KDJMA_SHORT_RGB = '244, 63, 94';

function _refreshAllMarkers() {
    if (!candleSeries) return;
    let persistent = [..._backtestMarkers, ..._liveMarkers];
    let realtime = [..._liveRealtimeMarkers];
    // Drop markers whose time is before the first chart bar.
    // Lightweight Charts snaps older markers to the first bar → visual stacking.
    const cd = window._lastChartData;
    if (cd && cd.length > 0) {
        const firstT = cd[0].time;
        const lastT  = cd[cd.length - 1].time;
        persistent = persistent.filter(m => m.time >= firstT && m.time <= lastT);
        realtime = realtime.filter(m => m.time >= firstT);
    }
    const all = [...persistent, ...realtime].sort((a, b) => a.time - b.time);
    try { candleSeries.setMarkers(all); } catch(e) {}
    drawIndicatorSignalOverlay();
}

function _setLiveRealtimeMarkers(markers) {
    _liveRealtimeMarkers = (markers || []).filter(m => m && m.time && !isNaN(m.time));
    _refreshAllMarkers();
}

function _signalRowFromApi(row) {
    if (!row || !row.time) return null;
    const t = isoToChartTime(row.time);
    if (!t || isNaN(t)) return null;
    const dir = String(row.direction || '').toLowerCase();
    const isLong = dir === 'long' || dir === 'buy' || dir === 'l';
    const isShort = dir === 'short' || dir === 'sell' || dir === 's';
    return {
        ...row,
        chartTime: t,
        direction: isShort ? 'short' : (isLong ? 'long' : ''),
        type: String(row.type || '').toLowerCase(),
    };
}

function createIndicatorSignalCanvas() {
    if (_indicatorSignalCanvas) return _indicatorSignalCanvas;
    const container = document.getElementById('chart-container');
    if (!container) return null;
    const canvas = document.createElement('canvas');
    canvas.id = 'indicator-signal-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:5;';
    container.appendChild(canvas);
    _indicatorSignalCanvas = canvas;
    return canvas;
}

function clearIndicatorSignalOverlay() {
    if (!_indicatorSignalCanvas) return;
    const ctx = _indicatorSignalCanvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, _indicatorSignalCanvas.width, _indicatorSignalCanvas.height);
}

function _findCandleAtChartTime(chartTime) {
    const cd = window._lastChartData || [];
    let lo = 0;
    let hi = cd.length - 1;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const mt = cd[mid].time;
        if (mt === chartTime) return cd[mid];
        if (mt < chartTime) lo = mid + 1;
        else hi = mid - 1;
    }
    return null;
}

function _indicatorSignalPrice(row) {
    const candidates = [row.price, row.entry_price, row.close, row.open];
    for (const value of candidates) {
        const n = Number(value);
        if (Number.isFinite(n)) return n;
    }
    const candle = _findCandleAtChartTime(row.chartTime);
    if (candle) {
        const close = Number(candle.close);
        if (Number.isFinite(close)) return close;
    }
    return null;
}

function _indicatorTimeToX(sec, W, visibleRange) {
    // 1.0.9: 改走 bar 索引映射 — 時間線性外推在資料縫隙處會把信號畫錯位/疊在一起
    return _timeToXViaBars(sec);
}

function _drawIndicatorBubble(ctx, x, y, radius, rgb) {
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(' + rgb + ', 0.60)';
    ctx.fill();
}

function _drawKdjmaDot(ctx, x, y, radius, rgb) {
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(' + rgb + ', 0.60)';
    ctx.fill();
}

// 1.0.9: EMAPMO signal triangle. dir='long' → up triangle sitting just below
// the bar low; dir='short' → down triangle just above the bar high. yRef is the
// screen-Y of the bar low (long) or high (short).
function _drawIndicatorArrow(ctx, cx, yRef, dir, rgb) {
    // 1.0.9: INTRAMOM 的白色箭頭 —— 箭頭 + 箭桿,與 EMAPMO 的純三角形區隔。
    const half = 6;      // 箭頭底邊半寬
    const head = 9;      // 箭頭高
    const shaft = 7;     // 箭桿長
    const gap = 4;       // 與 K 棒的間距
    const up = dir !== 'short';
    const tipY = up ? (yRef + gap) : (yRef - gap);
    const s = up ? 1 : -1;               // 往下為正
    ctx.save();
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = 'rgba(' + rgb + ', 1)';
    ctx.fillStyle = 'rgba(' + rgb + ', 0.92)';
    // 箭桿
    ctx.beginPath();
    ctx.moveTo(cx, tipY + s * head);
    ctx.lineTo(cx, tipY + s * (head + shaft));
    ctx.stroke();
    // 箭頭(頂點朝向 K 棒)
    ctx.beginPath();
    ctx.moveTo(cx, tipY);
    ctx.lineTo(cx - half, tipY + s * head);
    ctx.lineTo(cx + half, tipY + s * head);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.restore();
}


function _drawIndicatorTriangle(ctx, cx, yRef, dir, rgb) {
    // 1.0.9: 縮小到與 KDJMA 圓點 (r=6, 直徑12px) 相同視覺大小
    const half = 6;      // half base width (base 12px)
    const height = 10;   // triangle height
    const gap = 4;       // gap between bar and triangle
    ctx.beginPath();
    if (dir === 'short') {
        const topY = yRef - gap - height;   // above the high, apex points down
        ctx.moveTo(cx - half, topY);
        ctx.lineTo(cx + half, topY);
        ctx.lineTo(cx, topY + height);
    } else {
        const botY = yRef + gap + height;   // below the low, apex points up
        ctx.moveTo(cx - half, botY);
        ctx.lineTo(cx + half, botY);
        ctx.lineTo(cx, botY - height);
    }
    ctx.closePath();
    ctx.fillStyle = 'rgba(' + rgb + ', 0.95)';
    ctx.fill();
    ctx.strokeStyle = 'rgba(' + rgb + ', 1)';
    ctx.lineWidth = 1;
    ctx.stroke();
}

function _baseCandleBar(bar) {
    return {
        time: bar.time,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
    };
}

// 1.0.9: EMAPMO no longer tints candle bodies (too small to see on 1m). It is
// drawn as a triangle in drawIndicatorSignalOverlay instead. Kept as a no-op so
// existing call sites need no change; strips any stale per-bar signal colors.
function applyIndicatorSignalCandleColors() {
    if (!candleSeries || !window._lastChartData || window._lastChartData.length === 0) return;
    let needsReset = false;
    const nextData = window._lastChartData.map(bar => {
        if (bar.color || bar.borderColor) { needsReset = true; return _baseCandleBar(bar); }
        return bar;
    });
    if (!needsReset) return;
    window._lastChartData = nextData;
    // The timestamps are unchanged, so Lightweight Charts keeps the current
    // logical range.  Re-applying a range captured before this async signal
    // request completed can overwrite a pan that happened in the meantime.
    try { candleSeries.setData(nextData); } catch (_) {}
}

function drawIndicatorSignalOverlay() {
    if (!chart || !candleSeries) return;
    const canvas = createIndicatorSignalCanvas();
    if (!canvas) return;
    const container = document.getElementById('chart-container');
    if (!container) return;

    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth;
    const H = container.clientHeight;
    // Reuse the guarded resize path used by footprint/CVD.  Assigning
    // canvas.width/height on every scroll frame clears the backing store and
    // can make the three overlay layers compete for a fresh GPU surface.
    _sizeOrderflowCanvas(canvas, container, dpr);

    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    if (!_indicatorSignalRows || _indicatorSignalRows.length === 0) { drawPiSignalOverlay(); return; }

    // 1.0.9: 夾在價格窗格內 — 信號不再蓋住底部時間軸
    ctx.beginPath();
    ctx.rect(0, 0, W, H - _timeAxisHeight());
    ctx.clip();

    let visibleRange = null;
    try { visibleRange = chart.timeScale().getVisibleRange(); } catch (_) {}

    for (const row of _indicatorSignalRows) {
        const spec = INDICATOR_SIGNAL_TYPES[row.type];
        if (!spec) continue;
        if (!layerOn(_SIGNAL_TYPE_LAYER[row.type] || row.type)) continue;
        const t = Number(row.chartTime);
        if (!Number.isFinite(t)) continue;
        if (visibleRange && (t < visibleRange.from - 300 || t > visibleRange.to + 300)) continue;

        const x = _indicatorTimeToX(t, W, visibleRange);
        if (x === null || x < -80 || x > W + 80) continue;

        const dir = row.direction === 'short' ? 'short' : 'long';
        // spec.rgb 讓某些訊號用固定色(INTRAMOM 白)而非多空雙色
        const rgb = spec.rgb || (dir === 'short' ? INDICATOR_SHORT_RGB : INDICATOR_LONG_RGB);

        if (spec.kind === 'triangle') {
            // 1.0.9: EMAPMO — anchor to the bar's high (short) / low (long)
            const candle = _findCandleAtChartTime(t);
            const refPrice = candle
                ? (dir === 'short' ? candle.high : candle.low)
                : _indicatorSignalPrice(row);
            if (refPrice === null || refPrice === undefined) continue;
            let yRef = null;
            try { yRef = candleSeries.priceToCoordinate(Number(refPrice)); } catch (_) {}
            if (yRef === null || yRef === undefined || yRef < -80 || yRef > H + 80) continue;
            _drawIndicatorTriangle(ctx, x, yRef, dir, rgb);
            continue;
        }

        if (spec.kind === 'arrow') {
            // 1.0.9: INTRAMOM —— 帶箭桿的空心箭頭,和 EMAPMO 的實心三角形
            // 在形狀上就分得開(顏色也不同)。long 在低點下方朝上,short 在
            // 高點上方朝下。
            const candle = _findCandleAtChartTime(t);
            const refPrice = candle
                ? (dir === 'short' ? candle.high : candle.low)
                : _indicatorSignalPrice(row);
            if (refPrice === null || refPrice === undefined) continue;
            let yRef = null;
            try { yRef = candleSeries.priceToCoordinate(Number(refPrice)); } catch (_) {}
            if (yRef === null || yRef === undefined || yRef < -80 || yRef > H + 80) continue;
            _drawIndicatorArrow(ctx, x, yRef, dir, rgb);
            continue;
        }

        const price = _indicatorSignalPrice(row);
        if (price === null) continue;
        let y = null;
        try { y = candleSeries.priceToCoordinate(price); } catch (_) {}
        if (y === null || y === undefined || y < -80 || y > H + 80) continue;

        if (spec.kind === 'bubble') {
            _drawIndicatorBubble(ctx, x, y, spec.radius, rgb);
        } else if (spec.kind === 'dot') {
            const dotRgb = dir === 'short' ? INDICATOR_KDJMA_SHORT_RGB : INDICATOR_KDJMA_LONG_RGB;
            _drawKdjmaDot(ctx, x, y, spec.radius, dotRgb);
        }
    }
    drawPiSignalOverlay();
}


// ════════════════════════════════════════════════════════════════════
// 1.0.10: PI 訊號標記。畫在 indicator-signal-overlay 這張畫布上,和
// EMAPMO 三角形共用一層。
//
// 顏色語意來自使用者:**藍色 = 上漲、紫/粉 = 下跌**。π 字符是「π 級別」
// (青π / 粉π —— PF 3.05 / 2.28),圈是「圈級別」(淡蓝 PF 1.35、
// 紫圈 PF 1.18,接近噪音)。所以 π 畫實心加粗、圈畫空心細框,
// 一眼就分得出哪個是策略真的在吃的訊號。
//
// PI 沒有自己的 MNQ/MES 價位(訊號源是 SPY/QQQ),所以錨在該根 K 棒的
// 高/低點:看多錨低點下方,看空錨高點上方。
// ════════════════════════════════════════════════════════════════════
// 標記種類 → 顏色 / 方向 / 呈現形式。
//
// 1.0.10:**不再使用訊息裡的 size 欄位**。實測它零資訊:
//   深蓝圈 13/13 都是「大」、淡蓝圈 97/97 都是「大」、紫圈 150/151 是「大」
//   —— 圈類的 size 是常數;而 青π/粉π 的 中/小 分佈(23/25、25/31)依使用者
//   說明是**視覺系統的多餘分類**,π 符號本身沒有大小之分。
// 真正的強弱軸就是「種類」本身:深蓝圈 = 大威力,淡蓝圈 = 小威力。
// 所以圈的半徑改由種類決定,π 一律同一個字級。
const PI_MARK_STYLE = {
    '青π':   { rgb: '34, 211, 238',  glyph: 'π', dir: 'long',  r: 0  },
    '粉π':   { rgb: '244, 114, 182', glyph: 'π', dir: 'short', r: 0  },
    '深蓝圈': { rgb: '37, 99, 235',   glyph: null, dir: 'long',  r: 24 },  // 大威力
    '淡蓝圈': { rgb: '125, 211, 252', glyph: null, dir: 'long',  r: 14 },  // 小威力
    '紫圈':   { rgb: '168, 85, 247',  glyph: null, dir: 'short', r: 14 },
};
const PI_GLYPH_SIZE = 10;      // π 一律同一個字級

let _piSignalRows = [];        // [{chartTime, marks:[{kind,size}]}]
let _piSignalsLoading = false;
let _piSignalRefreshTimer = null;

function _ensurePiSignalRefreshTimer() {
    if (_piSignalRefreshTimer !== null) return;
    // Backfill runs independently of candle polling. Refresh the audit feed
    // periodically so repaired signals appear without a new candle.
    _piSignalRefreshTimer = setInterval(() => {
        if (document.hidden) return;
        const activeTab = document.querySelector('.tab.active')?.dataset?.tab;
        // The record-only audit feed is overlaid on both destinations. Keep
        // Backtest live as well as Live so a Discord catch-up appears without
        // waiting for a new candle or a tab switch.
        if ((activeTab === 'live' || activeTab === 'backtest') && layerOn('pi')) {
            refreshPiSignalMarkers();
        }
    }, 5000);
}

function _piChartSourceAllowed(ts) {
    // PI-001 is a source-time rule, not a browser-local clock rule.  Format
    // in the configured PI source zone so PDT/PST both keep the
    // 07:00 boundary and replay rows can never become chart marks.
    try {
        const parts = new Intl.DateTimeFormat('en-US', {
            timeZone: SYSTEM_TIME_ZONES.pi_source, hour: '2-digit', minute: '2-digit',
            hour12: false, hourCycle: 'h23',
        }).formatToParts(new Date(ts));
        const hour = Number(parts.find(p => p.type === 'hour')?.value);
        const minute = Number(parts.find(p => p.type === 'minute')?.value);
        return Number.isFinite(hour) && Number.isFinite(minute)
            ? (hour > 7 || (hour === 7 && minute >= 0))
            : false;
    } catch (_) {
        // Keep the existing fail-open behavior for an unsupported runtime;
        // malformed timestamps still fail the Date.parse/snap checks below.
        return true;
    }
}

async function refreshPiSignalMarkers() {
    _ensurePiSignalRefreshTimer();
    if (_piSignalsLoading) return;   // 進行中就跳過;呼叫端每次圖表同步都會再叫一次
    if (!layerOn('pi')) { _piSignalRows = []; return; }
    const rows = window._lastChartData;
    if (!rows || !rows.length) return;
    _piSignalsLoading = true;
    try {
        // Full contract IDs use the fourth segment; bare symbols stay valid.
        // 1.0.10 BUG:這裡原本寫 fv('contract-id', ...)。fv 不是全域 helper,
        // 它是 collectConfluenceParams() 內部的區域箭頭函式(而且是 parseFloat,
        // 本來就讀不了字串)。ReferenceError 被下面的 catch 吞掉 → 靜默清空
        // _piSignalRows → 圖上永遠沒有 PI 標記,而且 console 一片乾淨。
        const _cidEl = document.getElementById('contract-id');
        const cid = (_cidEl && _cidEl.value) || defaultContractId();
        const sym = (String(cid).split('.')[3] || String(cid) || 'MNQ').toUpperCase();
        const qs = new URLSearchParams({ symbol: sym || '' });
        const resp = await fetch(API + '/pi/signals?' + qs.toString());
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        const seen = new Set();
        _piSignalRows = (data.signals || []).map(sig => ({
            chartTime: _snapToBarTime(utcMsToChartTime(Date.parse(sig.ts))),
            marks: sig.marks || [],
            sourceTs: sig.ts,
        })).filter(r => Number.isFinite(r.chartTime) && _piChartSourceAllowed(r.sourceTs));
        // History and the live audit can describe the same PI event.  A
        // repost has a different Discord message_id, so the visual identity
        // is the same 1m bar + chart symbol + PI kind used by the engine.
        for (const row of _piSignalRows) {
            for (const mark of (row.marks || [])) {
                if (mark && mark.kind) seen.add([row.chartTime, sym, mark.kind].join('|'));
            }
        }

        // The active Discord listener writes one durable audit row before any
        // strategy callback.  Reuse that same row on both chart tabs instead
        // of starting a second fetcher or mutating the immutable history file.
        // Chart rendering is read-only; when the user explicitly runs a PI
        // Backtest, the API separately replays the same in-range audit rows for
        // that run and reports the count in the response.
        const activeTab = document.querySelector('.tab.active')?.dataset?.tab;
        if (activeTab === 'live' || activeTab === 'backtest') {
            try {
                // events= filters server-side BEFORE the limit. Without it the
                // listener's per-poll heartbeat fills the 2000-row window and
                // the chart only ever sees the last few hours of signals.
                const auditResp = await fetch(
                    API + '/pi/signals/audit?limit=2000&events=received,recorded');
                if (auditResp.ok) {
                    const audit = await auditResp.json();
                    for (const event of (audit.events || [])) {
                        // ``received`` and ``recorded`` are both chart-visible
                        // audit events.  Preset acceptance never controls chart visibility.
                        // Neither event mutates history.
                        if (!event || event.event !== 'received' && event.event !== 'recorded' || !event.ts || !event.kind) continue;
                        if (sym.startsWith('MNQ') && event.future !== 'MNQ') continue;
                        if (sym.startsWith('MES') && event.future !== 'MES') continue;
                        if (!_piChartSourceAllowed(event.ts)) continue;
                        const chartTime = _snapToBarTime(utcMsToChartTime(Date.parse(event.ts)));
                        if (!Number.isFinite(chartTime)) continue;
                        const key = [chartTime, sym, event.kind].join('|');
                        if (seen.has(key)) continue;
                        seen.add(key);
                        let row = _piSignalRows.find(item => item.chartTime === chartTime);
                        if (!row) {
                            row = { chartTime, marks: [] };
                            _piSignalRows.push(row);
                        }
                        row.marks.push({ kind: event.kind, size: event.size || '?', count: 1 });
                    }
                    _piSignalRows.sort((a, b) => a.chartTime - b.chartTime);
                }
            } catch (auditError) {
                // The historical overlay remains useful if an older server has
                // not deployed the optional live-audit endpoint yet.
                console.warn('[PI] live audit load failed:', auditError);
            }
        }
    } catch (e) {
        _piSignalRows = [];
        // 吞掉例外正是上面那個 bug 難找的原因 —— 至少要留下痕跡
        console.error('[PI] 訊號載入失敗:', e);
        try { log('PI signal load failed: ' + e.message, 'error'); } catch (_) {}
    } finally {
        _piSignalsLoading = false;
    }
    scheduleChartOverlayRedraw();
}

// 把任意秒數的訊號時間吸附到「它所屬的那根 K 棒」。
//
// 1.0.10 BUG:PI 訊號的時戳是 Discord 的發文時刻(例如 13:33:01.240),
// 落在秒上;K 棒時間是整分鐘。_findCandleAtChartTime() 是**精確比對**的
// 二分搜尋,所以永遠找不到 → 繪製迴圈的 `if (!candle) continue` 把每一筆
// 都跳掉,圖上一個標記都沒有。EMAPMO 不受影響是因為它的訊號時間由後端
// 產生時就已經對齊 K 棒了。
function _snapToBarTime(chartTime) {
    const cd = window._lastChartData || [];
    if (!cd.length) return null;
    if (chartTime < cd[0].time || chartTime > cd[cd.length - 1].time + 3600) return null;
    let lo = 0, hi = cd.length - 1, best = -1;
    while (lo <= hi) {                       // 找最後一根 time <= chartTime
        const mid = (lo + hi) >> 1;
        if (cd[mid].time <= chartTime) { best = mid; lo = mid + 1; }
        else hi = mid - 1;
    }
    if (best < 0) return null;
    // 距離太遠代表訊號落在資料缺口裡(收盤、假日),不要硬掛到前一根上
    return (chartTime - cd[best].time <= 3600) ? cd[best].time : null;
}

// 由 live listener 推進來的即時訊號,直接補進來不用重打 API
function pushPiSignalMarker(tsMs, marks) {
    const t = _snapToBarTime(utcMsToChartTime(tsMs));
    if (!Number.isFinite(t)) return;
    let row = _piSignalRows.find(item => item.chartTime === t);
    if (!row) {
        row = { chartTime: t, marks: [] };
        _piSignalRows.push(row);
    }
    for (const mark of (Array.isArray(marks) ? marks : [])) {
        if (!mark || !mark.kind) continue;
        const duplicate = row.marks.some(existing => existing && existing.kind === mark.kind);
        if (!duplicate) row.marks.push(mark);
    }
    scheduleChartOverlayRedraw();
}

// PI 的「π 級」標記 —— 借 _drawIndicatorTriangle 的錨定方式(多錨低點下方、
// 空錨高點上方、固定 gap),形狀換成 π 字符。EMAPMO 維持三角形不變,所以兩層
// 疊在一起還是分得開:▲▼ = EMAPMO,π = PI。
function _drawPiGlyph(ctx, cx, yRef, dir, rgb, size) {
    const gap = 4;
    const h = size * 1.9;
    const y = (dir === 'short') ? (yRef - gap - h * 0.15) : (yRef + gap + h * 0.85);
    ctx.save();
    ctx.font = `700 ${h.toFixed(1)}px "IBM Plex Mono", monospace`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'alphabetic';
    // 深色描邊,免得 π 落在亮 K 棒上糊掉
    ctx.lineWidth = 3;
    ctx.strokeStyle = 'rgba(8, 12, 18, 0.85)';
    ctx.strokeText('π', cx, y);
    ctx.fillStyle = `rgba(${rgb}, 1)`;
    ctx.fillText('π', cx, y);
    ctx.restore();
}

// PI 的「圈級」標記 —— 就是 MREV 泡泡(_drawIndicatorBubble)放大版:
// 同樣是半透明實心圓,只是半徑從 6 拉到 14/18/24;不畫 edge,避免
// 泡泡在密集訊號上互相切割。
function _drawPiBubble(ctx, x, y, radius, rgb) {
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = `rgba(${rgb}, 0.34)`;
    ctx.fill();
}

function drawPiSignalOverlay() {
    // 和 EMAPMO 共用畫布。drawIndicatorSignalOverlay 是唯一的清空及重繪
    // owner，資料更新只排程該 owner，避免 PI bubble 在同一畫布重複疊色。
    if (!chart || !candleSeries) return;
    if (!layerOn('pi') || !_piSignalRows.length) return;
    const canvas = document.getElementById('indicator-signal-overlay');
    const container = document.getElementById('chart-container');
    if (!canvas || !container) return;

    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth;
    const H = container.clientHeight;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, W, H - _timeAxisHeight());
    ctx.clip();

    let visibleRange = null;
    try { visibleRange = chart.timeScale().getVisibleRange(); } catch (_) {}

    const drawnMarks = new Set();
    for (const row of _piSignalRows) {
        const t = Number(row.chartTime);
        if (visibleRange && (t < visibleRange.from - 300 || t > visibleRange.to + 300)) continue;
        const x = _indicatorTimeToX(t, W, visibleRange);
        if (x === null || x < -40 || x > W + 40) continue;
        const candle = _findCandleAtChartTime(t);
        if (!candle) continue;

        // 同一則訊息可能帶多個標記 —— 多空各自往外堆疊,不要疊在一起
        let upN = 0, dnN = 0;
        for (const m of row.marks) {
            const style = PI_MARK_STYLE[m.kind];
            if (!style) continue;
            // History/audit rows can describe the same 1m mark more than once.
            // Keep distinct source levels, but never alpha-stack an identical
            // mark during one repaint.
            const sourceLevel = m.level ?? m.source_level ?? m.size ?? '';
            const markKey = [t, m.kind, sourceLevel].join('|');
            if (drawnMarks.has(markKey)) continue;
            drawnMarks.add(markKey);
            const long_ = style.dir === 'long';
            const px = long_ ? candle.low : candle.high;
            let yRef = null;
            try { yRef = candleSeries.priceToCoordinate(Number(px)); } catch (_) {}
            if (yRef === null || yRef === undefined) continue;

            if (style.glyph) {
                // π 級:錨在 K 棒外側,和 EMAPMO 三角同一套定位
                const sz = PI_GLYPH_SIZE;
                const step = sz * 2.2;
                const off = long_ ? (upN++ * step) : -(dnN++ * step);
                _drawPiGlyph(ctx, x, yRef + off, style.dir, style.rgb, sz);
            } else {
                // 圈級:放大的 MREV 泡泡。半徑由**種類**決定(深蓝=大威力 24、
                // 淡蓝/紫=小威力 14),不再讀訊息裡那個恆為「大」的 size 欄位。
                const r = style.r || 14;
                const off = long_ ? (r + upN++ * 6) : -(r + dnN++ * 6);
                const y = yRef + off;
                if (y < -60 || y > H + 60) continue;
                _drawPiBubble(ctx, x, y, r, style.rgb);
            }
        }
    }
    ctx.restore();
}

// Read-only QQQ 0DTE research layer. The API serves five-minute point-in-time
// snapshots derived from actual OPRA one-minute data; price levels arrive
// already mapped into the active MNQ coordinate. It never enters order state.
let _optionWallCanvas = null;
let _optionWallSnapshots = [];
let _optionWallPiSignals = [];
let _optionWallLoading = false;
let _optionWallMeta = null;
const _optionWallSessionMetaCache = new Map();
const OPTION_WALL_SNAPSHOT_SEC = 5 * 60;
const OPTION_WALL_OVERLAP_TOLERANCE = 0.25;
const OPTION_WALL_OVERLAP_OFFSET_PX = 1.5;

function _createOptionWallCanvas() {
    if (_optionWallCanvas) return _optionWallCanvas;
    const container = document.getElementById('chart-container');
    if (!container) return null;
    const canvas = document.createElement('canvas');
    canvas.id = 'option-wall-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:3;';
    container.appendChild(canvas);
    _optionWallCanvas = canvas;
    return canvas;
}

function _clearOptionWallOverlay() {
    const canvas = _optionWallCanvas || document.getElementById('option-wall-overlay');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
}

async function refreshOptionWallLayer() {
    if (_optionWallLoading) return;
    if (!layerOn('optionwall')) { _clearOptionWallOverlay(); return; }
    const contract = String(document.getElementById('contract-id')?.value || defaultContractId());
    const symbol = (contract.split('.')[3] || contract || 'MNQ').toUpperCase();
    if (!symbol.startsWith('MNQ')) {
        _optionWallSnapshots = [];
        _optionWallPiSignals = [];
        _clearOptionWallOverlay();
        return;
    }
    _optionWallLoading = true;
    try {
        const response = await fetch(API + '/options-wall/demo?symbol=MNQ');
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        if (!data.available) {
            _optionWallSnapshots = [];
            _optionWallPiSignals = [];
            _optionWallMeta = null;
            _clearOptionWallOverlay();
            return;
        }
        _optionWallSnapshots = (data.snapshots || []).map(row => {
            const session = _optionWallSessionMeta(row);
            return {
                ...row,
                chartTime: isoToChartTime(row.as_of),
                _wallSessionKey: session.key,
                _wallSessionClose: session.close,
            };
        }).filter(row => Number.isFinite(row.chartTime))
            .sort((a, b) => a.chartTime - b.chartTime);
        _optionWallPiSignals = (data.pi_signals || []).map(row => ({
            ...row,
            chartTime: isoToChartTime(row.ts),
        })).filter(row => Number.isFinite(row.chartTime));
        _optionWallMeta = data;
        drawOptionWallOverlay();
        try {
            log('Option Wall demo loaded: ' + data.date + ' · ' + _optionWallSnapshots.length
                + ' snapshots · $' + Number(data.paid_cost_usd || 0).toFixed(2), 'info');
        } catch (_) {}
    } catch (error) {
        _optionWallSnapshots = [];
        _optionWallPiSignals = [];
        _optionWallMeta = null;
        _clearOptionWallOverlay();
        console.error('[OPTION WALL] layer load failed:', error);
    } finally {
        _optionWallLoading = false;
    }
}

function _optionWallLowerBound(chartTime) {
    let lo = 0;
    let hi = _optionWallSnapshots.length;
    while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (_optionWallSnapshots[mid].chartTime < chartTime) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

function _optionWallPriorSnapshot(chartTime) {
    const index = _optionWallLowerBound(chartTime);
    if (index < _optionWallSnapshots.length
        && _optionWallSnapshots[index].chartTime === chartTime) {
        return _optionWallSnapshots[index];
    }
    return index > 0 ? _optionWallSnapshots[index - 1] : null;
}

function _optionWallSessionMeta(row) {
    const raw = String((row && row.as_of) || '');
    const cacheKey = raw.slice(0, 10);
    if (cacheKey && _optionWallSessionMetaCache.has(cacheKey)) {
        return _optionWallSessionMetaCache.get(cacheKey);
    }
    const asOfMs = Date.parse(raw);
    let meta = { key: cacheKey, close: NaN };
    if (Number.isFinite(asOfMs)) {
        const ny = _newYorkParts(asOfMs);
        meta = {
            key: ny.year + '-' + String(ny.month).padStart(2, '0')
                + '-' + String(ny.day).padStart(2, '0'),
            close: utcMsToChartTime(
                nyLocalToUtcMs(ny.year, ny.month - 1, ny.day, 16, 0),
            ),
        };
    }
    if (cacheKey) _optionWallSessionMetaCache.set(cacheKey, meta);
    return meta;
}

function _optionWallSessionKey(row) {
    return (row && row._wallSessionKey) || _optionWallSessionMeta(row).key;
}

function _optionWallSameSession(row, next) {
    return !!next && _optionWallSessionKey(row) === _optionWallSessionKey(next);
}

function _optionWallSessionCloseTime(row) {
    const cached = Number(row && row._wallSessionClose);
    return Number.isFinite(cached) ? cached : _optionWallSessionMeta(row).close;
}

function _optionWallSegmentEndTime(row, next) {
    const sessionClose = _optionWallSessionCloseTime(row);
    if (_optionWallSameSession(row, next) && Number(next.chartTime) > Number(row.chartTime)) {
        // A snapshot can legitimately arrive after the RTH close (for example
        // while the data service is still publishing the final evening state).
        // Do not clamp that segment backwards to 16:00; doing so makes the
        // first after-hours rows disappear when the chart only has the recent
        // window loaded.  Clamp only rows that are still before the close.
        return Number.isFinite(sessionClose) && sessionClose > Number(row.chartTime)
            ? Math.min(next.chartTime, sessionClose)
            : Number(next.chartTime);
    }
    const suppliedCadence = Number(row && row.cadence_seconds);
    const cadence = Number.isFinite(suppliedCadence) && suppliedCadence > 0
        ? suppliedCadence
        : OPTION_WALL_SNAPSHOT_SEC;
    if (Number.isFinite(sessionClose) && sessionClose >= Number(row.chartTime)) {
        return Math.min(Number(row.chartTime) + cadence, sessionClose);
    }
    return Number(row.chartTime) + cadence;
}

function _optionWallsOverlap(row) {
    const call = Number(row && row.call_wall_mnq);
    const put = Number(row && row.put_wall_mnq);
    return Number.isFinite(call) && Number.isFinite(put)
        && Math.abs(call - put) <= OPTION_WALL_OVERLAP_TOLERANCE;
}

function _optionWallVisibleWindow(visibleRange) {
    if (!visibleRange || !Number.isFinite(Number(visibleRange.from))
        || !Number.isFinite(Number(visibleRange.to))) {
        return { start: 0, end: _optionWallSnapshots.length };
    }
    const start = Math.max(0, _optionWallLowerBound(Number(visibleRange.from)) - 1);
    const end = Math.min(
        _optionWallSnapshots.length,
        _optionWallLowerBound(Number(visibleRange.to)) + 1,
    );
    return { start, end };
}

function drawOptionWallOverlay() {
    const canvas = _createOptionWallCanvas();
    if (!canvas || !chart || !candleSeries) return;
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth;
    const H = container.clientHeight;
    const pixelW = Math.max(1, Math.round(W * dpr));
    const pixelH = Math.max(1, Math.round(H * dpr));
    if (canvas.width !== pixelW || canvas.height !== pixelH) {
        canvas.width = pixelW;
        canvas.height = pixelH;
        canvas.style.width = W + 'px';
        canvas.style.height = H + 'px';
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!layerOn('optionwall') || !_optionWallSnapshots.length) return;

    const plotH = H - _timeAxisHeight();
    let visibleRange = null;
    try { visibleRange = chart.timeScale().getVisibleRange(); } catch (_) {}
    const specs = [
        { key: 'call_wall_mnq', color: 'rgba(40,209,124,0.96)', dash: [], label: 'CALL WALL' },
        { key: 'put_wall_mnq', color: 'rgba(255,93,115,0.96)', dash: [], label: 'PUT WALL' },
        { key: 'gamma_flip_mnq', color: 'rgba(255,181,71,0.92)', dash: [6, 4], label: 'GAMMA FLIP' },
    ];
    const visibleWindow = _optionWallVisibleWindow(visibleRange);
    // Time/session work is shared by all three series. Previously each series
    // repeated the same chart-coordinate and New York session conversions for
    // every snapshot on every drag frame.
    const visiblePoints = [];
    for (let index = visibleWindow.start; index < visibleWindow.end; index++) {
        const row = _optionWallSnapshots[index];
        const next = _optionWallSnapshots[index + 1];
        const x1 = _indicatorTimeToX(row.chartTime, W, visibleRange);
        const x2 = _indicatorTimeToX(_optionWallSegmentEndTime(row, next), W, visibleRange);
        if (x1 === null || x2 === null) continue;
        const overlap = _optionWallsOverlap(row);
        const ys = {};
        for (const spec of specs) {
            const value = Number(row[spec.key]);
            let y = Number.isFinite(value) ? candleSeries.priceToCoordinate(value) : null;
            if (y !== null && y !== undefined && overlap) {
                if (spec.key === 'call_wall_mnq') y -= OPTION_WALL_OVERLAP_OFFSET_PX;
                if (spec.key === 'put_wall_mnq') y += OPTION_WALL_OVERLAP_OFFSET_PX;
            }
            ys[spec.key] = y;
        }
        visiblePoints.push({ x1, x2, session: _optionWallSessionKey(row), ys });
    }

    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, W, plotH);
    ctx.clip();
    for (const spec of specs) {
        ctx.strokeStyle = spec.color;
        ctx.lineWidth = spec.key === 'gamma_flip_mnq' ? 1.2 : 1.7;
        ctx.setLineDash(spec.dash);
        ctx.beginPath();
        let drawing = false;
        let currentSession = null;
        let lastX = null;
        let lastY = null;
        for (const point of visiblePoints) {
            const { x1, x2, session } = point;
            const y = point.ys[spec.key];
            if (x1 === null || x2 === null || y === null || y === undefined) {
                drawing = false;
                continue;
            }
            if (!drawing || session !== currentSession) {
                ctx.moveTo(x1, y);
            } else if (Math.abs(lastX - x1) > 0.01 || Math.abs(lastY - y) > 0.01) {
                // A changed level remains a continuous step inside the RTH
                // session. Stable levels skip this redundant path command.
                ctx.lineTo(x1, y);
            }
            ctx.lineTo(x2, y);
            drawing = true;
            currentSession = session;
            lastX = x2;
            lastY = y;
        }
        ctx.stroke();
    }
    ctx.setLineDash([]);

    // At the three actual PI times, label the latest point-in-time GEX state.
    ctx.font = '500 10px "IBM Plex Mono", monospace';
    ctx.textBaseline = 'top';
    _optionWallPiSignals.forEach((signal, index) => {
        const snapshot = _optionWallPriorSnapshot(signal.chartTime);
        if (!snapshot) return;
        const x = _indicatorTimeToX(signal.chartTime, W, visibleRange);
        if (x === null || x < -10 || x > W + 10) return;
        ctx.strokeStyle = signal.side === 'long' ? 'rgba(54,215,255,0.42)' : 'rgba(209,132,255,0.42)';
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, plotH); ctx.stroke();
        const oi = Number(snapshot.net_oi_gex_1pct) / 1e9;
        const vol = Number(snapshot.net_volume_gex_1pct) / 1e9;
        const text = 'PI · OI ' + oi.toFixed(2) + 'B · VOL ' + vol.toFixed(1) + 'B';
        const textW = ctx.measureText(text).width + 10;
        const boxX = Math.max(4, Math.min(x + 5, W - textW - 4));
        const boxY = 48 + (index % 3) * 18;
        ctx.fillStyle = 'rgba(8,12,18,0.82)';
        ctx.fillRect(boxX, boxY, textW, 15);
        ctx.fillStyle = signal.side === 'long' ? 'rgba(54,215,255,0.98)' : 'rgba(209,132,255,0.98)';
        ctx.fillText(text, boxX + 5, boxY + 2);
    });

    const visibleRows = _optionWallSnapshots
        .slice(visibleWindow.start, visibleWindow.end)
        .filter(row => !visibleRange
            || (row.chartTime >= visibleRange.from && row.chartTime <= visibleRange.to));
    const last = visibleRows[visibleRows.length - 1];
    if (last) {
        const overlap = _optionWallsOverlap(last);
        const labelSpecs = overlap
            ? [{
                value: (Number(last.call_wall_mnq) + Number(last.put_wall_mnq)) / 2,
                color: 'rgba(225,231,239,0.96)',
                label: 'CALL+PUT WALL',
                overlap: true,
            }, { ...specs[2], value: Number(last[specs[2].key]) }]
            : specs.map(spec => ({ ...spec, value: Number(last[spec.key]) }));
        for (const spec of labelSpecs) {
            const value = Number(spec.value);
            const y = Number.isFinite(value) ? candleSeries.priceToCoordinate(value) : null;
            if (y === null || y === undefined || y < 4 || y > plotH - 12) continue;
            const labelW = Math.ceil(ctx.measureText(spec.label).width) + 12;
            const boxX = W - labelW - 4;
            ctx.fillStyle = 'rgba(8,12,18,0.78)';
            ctx.fillRect(boxX, y - 7, labelW, 14);
            if (spec.overlap) {
                ctx.fillStyle = 'rgba(40,209,124,0.96)';
                ctx.fillRect(boxX, y - 7, 3, 7);
                ctx.fillStyle = 'rgba(255,93,115,0.96)';
                ctx.fillRect(boxX, y, 3, 7);
            }
            ctx.fillStyle = spec.color;
            ctx.fillText(spec.label, boxX + 7, y - 5);
        }
    }
    ctx.restore();
}

async function refreshIndicatorSignalMarkers(logSummary) {
    if (!candleSeries || !window._lastChartData || window._lastChartData.length === 0) return;
    if (_indicatorSignalsLoading) {
        _indicatorSignalsQueued = true;
        return;
    }
    _indicatorSignalsLoading = true;
    try {
        const resp = await fetch(API + '/data/mnq-signals?limit=' + CHART_MAX_CANDLES);
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.skipped) {
            _indicatorSignalRows = [];
            applyIndicatorSignalCandleColors();
            _refreshAllMarkers();
            clearIndicatorSignalOverlay();
            if (logSummary && data.skipped !== _lastIndicatorSignalLogKey) {
                _lastIndicatorSignalLogKey = data.skipped;
                log('MNQ signal overlay skipped: ' + data.skipped, 'info');
            }
            return;
        }

        const seen = new Set();
        _indicatorSignalRows = (data.signals || []).map(_signalRowFromApi).filter(row => {
            if (!row) return false;
            const key = row.chartTime + '|' + row.type + '|' + row.direction + '|' + (row.subtype || '');
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
        applyIndicatorSignalCandleColors();
        _refreshAllMarkers();

        if (logSummary) {
            const counts = data.counts || {};
            const summary = [
                'EMAPMO ' + (counts.emapmo || 0),
                'MREV ' + (counts.momentum_reversion || 0),
                'KDJMA ' + (counts.icefishball || 0),
            ].join(' / ');
            const key = data.shown + '|' + summary;
            if (key !== _lastIndicatorSignalLogKey) {
                _lastIndicatorSignalLogKey = key;
                log('MNQ signal overlay loaded: ' + summary + ' (' + (data.shown || 0) + ')', 'info');
            }
        }
    } catch(e) {
        // Optional overlay; keep chart rendering quiet if it fails.
    } finally {
        _indicatorSignalsLoading = false;
        if (_indicatorSignalsQueued) {
            _indicatorSignalsQueued = false;
            refreshIndicatorSignalMarkers(false);
            refreshPiSignalMarkers();
        }
    }
}

function renderChart(data) {
    if (!data) { log('No backtest data to render', 'warn'); return; }

    // Draw the strategy's day-zone overlay when the backtest provides one.
    drawBacktestZones(data.zones);
    if (layerOn('prevday70')) refreshPreviousDayValueAreas(true);

    // Draw decision overlays (entry marker + primary VAH/VAL zone)
    drawPositionTools([...(data.trades || []), ...(window._liveCompletedTrades || [])]);
    drawTradeMarkers(data.trades);

    // Do not reframe the time scale while rendering a result.  The default
    // smooth-200 framing belongs only to showCandleData() when no viewport
    // exists; result rendering must never become a horizontal lock or reset a
    // user pan.  The explicit "latest" chart button remains the only
    // user-triggered recenter action.

    // Start continuous overlay sync (handles both horizontal AND vertical scrolling)
    _overlaySyncData = data;
    startOverlaySync();
}

// Continuously sync canvas overlays with chart coordinates using rAF.
// Only redraws when coordinates actually change (cheap check per frame).
function startOverlaySync() {
    if (_overlaySyncRAF) return; // already running

    let lastCheckY = null;
    let lastCheckX = null;

    function tick() {
        _overlaySyncRAF = requestAnimationFrame(tick);
        if (!_overlaySyncData || !candleSeries) return;

        const data = _overlaySyncData;
        // Pick a reference price to check if Y coordinate changed
        let refPrice = 0;
        if (data.zones && data.zones.length > 0) {
            refPrice = data.zones[0].poc;
        } else if (data.trades && data.trades.length > 0) {
            refPrice = data.trades[0].entry_price;
        }
        if (refPrice === 0) return;

        const curY = candleSeries.priceToCoordinate(refPrice);
        const range = chart.timeScale().getVisibleLogicalRange();
        const curX = range ? range.from : null;

        if (curY !== lastCheckY || curX !== lastCheckX) {
            lastCheckY = curY;
            lastCheckX = curX;
            scheduleChartOverlayRedraw();
        }
    }

    _overlaySyncRAF = requestAnimationFrame(tick);
}

function stopOverlaySync() {
    if (_overlaySyncRAF) {
        cancelAnimationFrame(_overlaySyncRAF);
        _overlaySyncRAF = null;
    }
    _overlaySyncData = null;
}

// ── Utility: ISO time string → lightweight-charts UTC seconds ──
// Remove the faded "best candidate" preview lines (scorer's current pick that
// has NOT cleared the admission gate). Safe to call when none exist.
function removeCandidateLines() {
    if (typeof candleSeries === 'undefined' || !candleSeries) return;
    ['_candEntryLine', '_candTpLine', '_candSlLine'].forEach(k => {
        if (window[k]) { try { candleSeries.removePriceLine(window[k]); } catch(e){} window[k] = null; }
    });
}

function isoToChartTime(iso) {
    let s = iso;
    if (!s.endsWith('Z') && !s.includes('+') && !s.includes('-', 10)) s += 'Z';
    const d = new Date(s);
    return utcMsToChartTime(d.getTime());
}

function _tradeIsBuy(t) {
    const d = String((t && t.direction) || '').toLowerCase();
    return d === 'buy' || d === 'long';
}

function _tradePnlText(t) {
    const pnl = Number((t && t.pnl) || 0);
    return (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(0);
}

function _tradeDecisionPhrase(t) {
    const labels = Array.isArray(t && t.labels) ? t.labels.join('|').toLowerCase() : '';
    if ((t && t.or_range) || labels.includes('or15:') || String((t && t.mode) || '').toLowerCase() === 'or15_false_break') {
        return _tradeIsBuy(t) ? 'OR15 long' : 'OR15 short';
    }
    return _tradeIsBuy(t) ? 'long' : 'short';
}

function _entryDecisionMarker(t, fallbackColor) {
    if (!t || !t.entry_time) return null;
    const entryTime = isoToChartTime(t.entry_time);
    if (!entryTime || isNaN(entryTime)) return null;
    const isBuy = _tradeIsBuy(t);
    const pnl = Number(t.pnl || 0);
    const pnlColor = pnl >= 0 ? '#00e5a0' : '#ff4060';
    const decision = _tradeDecisionPhrase(t);
    return {
        time: entryTime,
        position: isBuy ? 'belowBar' : 'aboveBar',
        color: fallbackColor || pnlColor,
        shape: isBuy ? 'arrowUp' : 'arrowDown',
        text: _tradePnlText(t) + (decision ? ' ' + decision : ''),
    };
}

function updateLiveWorkingDecision(sig, isLong, sigSL, sigTP) {
    if (!sig || sig.entry_price == null) return;
    const entryNum = Number(sig.entry_price);
    const slNum = Number(sigSL);
    const tpNum = Number(sigTP);
    if (!Number.isFinite(entryNum) || !Number.isFinite(slNum) || !Number.isFinite(tpNum)) return;
    const key = [
        sig.direction || '',
        entryNum,
        slNum,
        tpNum,
        sig.mode || '',
        sig.side || '',
        (sig.primary_zone && sig.primary_zone.zone_id) || '',
    ].join('|');
    if (window._liveWorkingDecisionKey !== key) {
        window._liveWorkingDecisionKey = key;
        window._liveWorkingDecisionTs = new Date().toISOString();
    }
    window._liveWorkingDecisionTrade = {
        trade_id: 'LIVE_WORKING',
        direction: isLong ? 'buy' : 'sell',
        entry_price: entryNum,
        entry_time: window._liveWorkingDecisionTs || new Date().toISOString(),
        exit_time: null,
        sl_price: slNum,
        tp_price: tpNum,
        original_sl_price: slNum,
        original_tp_price: tpNum,
        pnl: 0,
        mode: sig.mode,
        side: sig.side,
        largest_tf: sig.largest_tf,
        risk_tf: sig.risk_tf,
        labels: sig.labels || [],
        primary_zone: sig.primary_zone,
    };
    redrawTradeDecisionOverlays();
}

function clearLiveWorkingDecision() {
    if (!window._liveWorkingDecisionTrade && !window._liveWorkingDecisionKey) return;
    window._liveWorkingDecisionTrade = null;
    window._liveWorkingDecisionKey = null;
    window._liveWorkingDecisionTs = null;
    redrawTradeDecisionOverlays();
}

function redrawTradeDecisionOverlays() {
    clearPositionOverlay();
    const _allDecision = [
        ...((backtestData && backtestData.trades) ? backtestData.trades : []),
        ...(window._liveCompletedTrades || []),
        ...(window._liveWorkingDecisionTrade ? [window._liveWorkingDecisionTrade] : []),
    ];
    drawPositionTools(_allDecision);
}


function drawTradeMarkers(trades) {
    if (!trades || trades.length === 0) { _backtestMarkers = []; _refreshAllMarkers(); return; }
    const markers = trades.map(t => _entryDecisionMarker(t)).filter(Boolean);
    markers.sort((a, b) => a.time - b.time);
    _backtestMarkers = markers;
    _refreshAllMarkers();
}

function drawLiveTradeMarkers(trades) {
    _liveMarkers = (!trades || trades.length === 0)
        ? []
        : trades.map(t => _entryDecisionMarker(t, '#ffa726')).filter(Boolean);
    _refreshAllMarkers();
}

function topstepTradeDateKey(value) {
    return window.TPXTopstepEval.tradeDateKey(value);
}

function tradeRealizedDayKey(trade) {
    const iso = trade && (trade.exit_time || trade.entry_time);
    return iso ? topstepTradeDateKey(iso) : null;
}

function liveDisplayedDailyPnl(st) {
    const day = (st && st.topstep_trade_date) || topstepTradeDateKey(new Date());
    const combined = [
        ...((window._liveCompletedTrades || [])),
        ...((_calLiveTrades || [])),
    ];
    const trades = dedupeLiveCompletedTrades(combined);
    if (day && trades.length) {
        let pnl = 0;
        let count = 0;
        trades.forEach(t => {
            if (tradeRealizedDayKey(t) !== day) return;
            pnl += Number(t.pnl || 0);
            count += 1;
        });
        if (count > 0) {
            return {
                pnl: pnl,
                source: 'frontend trade history net',
                day: day,
                count: count,
            };
        }
    }
    return {
        pnl: Number((st && st.daily_pnl) || 0),
        source: (st && st.daily_pnl_source) || 'live status',
        day: day || (st && st.topstep_trade_date) || '',
        count: 0,
    };
}

function getSessionCodeFromDate(d) {
    if (!d || isNaN(d.getTime())) return null;
    const local = _newYorkParts(d.getTime());
    const h = local.hour;
    const m = local.minute;
    if (h >= 18 || h < 3) return 'ASIA';
    if (h < 7) return 'EURO';
    if (h < 9 || (h === 9 && m < 30)) return 'PRE';
    if (h < 16) return 'RTH';
    return 'AH';
}

// Compute summary metrics from a list of trades (used for backtest-day-span and live comparison)
function _computeTradeStats(trades) {
    if (!trades || trades.length === 0) {
        return {
            total_pnl: 0, trades: 0, wins: 0, losses: 0,
            total_gain: 0, total_loss: 0,
            win_rate: 0, avg_win: 0, avg_loss: 0, profit_factor: 0,
            rr_ratio: 0, max_dd: 0, calmar: 0,
            days: 0, daily_pnl: {}, consec3Pass: false, maxStreak: 0,
            maxDayPnl: 0, maxDayPass: false, maxDayPct: 0,
        };
    }
    let total = 0;
    const wins = [], losses = [];
    const daily = {};
    // Sort chronologically for equity curve / max DD
    const sorted = [...trades].sort((a,b) => {
        const ta = (a.exit_time || a.entry_time) ? new Date(a.exit_time || a.entry_time).getTime() : 0;
        const tb = (b.exit_time || b.entry_time) ? new Date(b.exit_time || b.entry_time).getTime() : 0;
        return ta - tb;
    });

    let cum = 0, peak = 0, maxDD = 0;
    const pnlsAll = [];
    for (const t of sorted) {
        const p = t.pnl || 0;
        total += p;
        pnlsAll.push(p);
        cum += p;
        if (cum > peak) peak = cum;
        const dd = peak - cum;
        if (dd > maxDD) maxDD = dd;
        if (p > 0) wins.push(p);
        else if (p < 0) losses.push(p);
        const key = tradeRealizedDayKey(t);
        if (key) {
            daily[key] = (daily[key] || 0) + p;
        }
    }
    const count = sorted.length;
    const winRate = count ? wins.length / count : 0;
    const totalGain = wins.reduce((a,b)=>a+b, 0);
    const totalLoss = losses.reduce((a,b)=>a+b, 0);
    const avgWin = wins.length ? wins.reduce((a,b)=>a+b,0) / wins.length : 0;
    const avgLoss = losses.length ? losses.reduce((a,b)=>a+b,0) / losses.length : 0;
    const grossProfit = totalGain;
    const grossLoss = Math.abs(totalLoss);
    const pf = grossLoss > 0 ? grossProfit / grossLoss : (grossProfit > 0 ? Infinity : 0);
    const rr = Math.abs(avgLoss) > 0 ? Math.abs(avgWin / avgLoss) : 0;

    // Calmar ratio = total PnL / max drawdown
    const calmar = maxDD > 0 ? cum / maxDD : (cum > 0 ? 999 : 0);

    const dailyEntries = Object.entries(daily).sort((a,b)=>a[0].localeCompare(b[0]));
    const dailyPnls = dailyEntries.map(e=>e[1]);
    let streak = 0, maxStreak = 0;
    for (const p of dailyPnls) {
        if (p >= 150) { streak += 1; if (streak > maxStreak) maxStreak = streak; }
        else { streak = 0; }
    }
    const maxDayPnl = dailyPnls.length ? Math.max(...dailyPnls) : 0;
    const maxDayPct = total > 0 ? (maxDayPnl / total) * 100 : 0;
    const maxDayPass = total > 0 && maxDayPct < 40;

    return {
        total_pnl: total,
        total_gain: totalGain,
        total_loss: totalLoss,
        trades: count,
        wins: wins.length,
        losses: losses.length,
        win_rate: winRate,
        avg_win: avgWin,
        avg_loss: avgLoss,
        profit_factor: pf,
        rr_ratio: rr,
        max_dd: maxDD,
        calmar: calmar,
        days: Object.keys(daily).length,
        daily_pnl: daily,
        consec3Pass: maxStreak >= 3,
        maxStreak: maxStreak,
        maxDayPnl: maxDayPnl,
        maxDayPct: maxDayPct,
        maxDayPass: maxDayPass,
    };
}

// Filter backtest trades to only those days present in the live trade history
function _alignBacktestToLiveDays(backtestTrades, liveStats) {
    if (!liveStats || !backtestTrades) return backtestTrades || [];
    const liveDates = new Set(Object.keys(liveStats.daily_pnl));
    if (liveDates.size === 0) return backtestTrades;
    return backtestTrades.filter(t => {
        const key = tradeRealizedDayKey(t);
        return liveDates.has(key);
    });
}

// ── Performance window (selectable lookback in the panel title) ──────
let _perfWindowDays = 0;   // 0 = ALL; otherwise lookback in days

// Candidate windows; only those that fit inside the available data show up.
const _PERF_WINDOWS = [
    { label: '1W',  days: 7 },
    { label: '2W',  days: 14 },
    { label: '1M',  days: 30 },
    { label: '2M',  days: 60 },
    { label: '3M',  days: 90 },
    { label: '6M',  days: 180 },
    { label: '1Y',  days: 365 },
];

function _populatePerfWindowOptions(spanDays) {
    const sel = document.getElementById('perf-window');
    if (!sel) return;
    // spanDays is the CALENDAR span of the data (first→last trade), matching the
    // calendar-based window filter. Offer a window once the data covers ≥90% of
    // it, so e.g. a ~58-day dataset still surfaces the 2M (60d) option.
    const span = spanDays || 0;
    const avail = _PERF_WINDOWS.filter(w => span >= w.days * 0.9);
    const opts = avail.map(w => '<option value="' + w.days + '">' + w.label + '</option>')
        .concat(['<option value="0">ALL</option>']);   // ALL last
    const joined = opts.join('');
    if (sel.dataset.built === joined) return;   // avoid clobbering selection on re-render
    sel.innerHTML = joined;
    sel.dataset.built = joined;
    // Restore current selection if still valid, else fall back to ALL
    const valid = Array.from(sel.options).some(o => +o.value === _perfWindowDays);
    sel.value = valid ? String(_perfWindowDays) : '0';
    if (!valid) _perfWindowDays = 0;
}

function onPerfWindowChange() {
    const sel = document.getElementById('perf-window');
    _perfWindowDays = sel ? (parseInt(sel.value, 10) || 0) : 0;
    if (typeof backtestData !== 'undefined' && backtestData && backtestData.metrics) {
        renderMetrics(backtestData.metrics, backtestData.trades);
    }
}

// Group a {YYYY-MM-DD: pnl} map into ISO weeks and measure week-to-week
// variation — client-side mirror of the backend _weekly_stats().
function _weeklyFromDaily(daily) {
    const buckets = {};
    Object.entries(daily || {}).forEach(([day, pnl]) => {
        const d = new Date(String(day).slice(0, 10) + 'T00:00:00Z');
        if (isNaN(d.getTime())) return;
        const t = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
        const dayNum = (t.getUTCDay() + 6) % 7;          // Mon=0..Sun=6
        t.setUTCDate(t.getUTCDate() - dayNum + 3);        // nearest Thursday
        const firstThu = new Date(Date.UTC(t.getUTCFullYear(), 0, 4));
        const week = 1 + Math.round(((t - firstThu) / 86400000 - 3 + ((firstThu.getUTCDay() + 6) % 7)) / 7);
        const key = t.getUTCFullYear() + '-W' + String(week).padStart(2, '0');
        buckets[key] = (buckets[key] || 0) + (Number(pnl) || 0);
    });
    const weekly = Object.keys(buckets).sort().map(k => buckets[k]);
    const n = weekly.length;
    if (!n) return { weekly_count: 0, weekly_std: 0, weekly_cv: 0, weekly_consistency: 0 };
    const mean = weekly.reduce((a, b) => a + b, 0) / n;
    const variance = weekly.reduce((a, b) => a + (b - mean) * (b - mean), 0) / n;
    const std = Math.sqrt(variance);
    const cv = mean ? std / Math.abs(mean) : 0;
    const positive = weekly.filter(w => w > 0).length;
    return { weekly_count: n, weekly_std: std, weekly_cv: cv, weekly_consistency: positive / n };
}

// 1.0.9: 把訊息安全塞進 title="" —— 提示字裡有 $ 與括號,不轉義會破壞屬性。
function _attr(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// 1.0.9: 單筆獲利上限 —— 滑桿用**美元**,引擎吃的是價格距離 ticks。
// 換算需要合約的每 tick 值 × 口數(tickDollarValue 已含口數),所以換合約或
// 改口數都要重算 —— 否則 $2,000 的上限在 MES 上會變成完全不同的價格距離。
//
// 為什麼是「單筆」而不是「每日」:實測 BEST/EMAPMO 一個交易日只開一單,
// 所以擋新單的日上限(PDPT)完全沒作用,$2,572 是單筆賺出來的。
// 真正能壓住 Topstep 一致性天花板的只有單筆 TP 上限。
// 1.0.9: 換算需要合約的每 tick 值 × 口數(tickDollarValue 已含口數)。
function capTickValue(mode) {
    const cEl = document.getElementById('contract-' + mode);
    const sEl = document.getElementById('size-' + mode);
    const size = (sEl ? parseInt(sEl.value, 10) : 1) || 1;
    return { size: size, tv: tickDollarValue(String((cEl && cEl.value) || ''), size) };
}

// ── 單一真相:hidden input `max-profit-ticks-<mode>`(價距 ticks)────────
// 滑桿位置與文字**都**從它衍生,所以兩者不可能各說各話。
// 先前是滑桿當真相、文字跟著它:applyStrategyParams 改了值之後只有文字更新,
// 滑桿留在舊位置 —— 畫面顯示 $2,000 但實際參數是 OFF。
function capTicks(mode) {
    const hid = document.getElementById('max-profit-ticks-' + mode);
    return hid ? (parseInt(hid.value, 10) || 0) : 0;
}

function setCapTicks(mode, ticks) {
    const hid = document.getElementById('max-profit-ticks-' + mode);
    if (hid) hid.value = String(Math.max(0, ticks | 0));
    renderCapUi(mode);
}

// 把 hidden 的 ticks 畫成「滑桿位置 + 文字」。任何改動最後都要走這裡。
function renderCapUi(mode) {
    const ticks = capTicks(mode);
    const c = capTickValue(mode);
    const sl = document.getElementById('tp-cap-usd-' + mode);
    const out = document.getElementById('tp-cap-usd-' + mode + '-val');
    const hint = document.getElementById('tp-cap-hint-' + mode);
    let usd = (ticks > 0 && c.tv > 0) ? Math.round(ticks * c.tv / 500) * 500 : 0;
    usd = Math.max(0, Math.min(2000, usd));
    if (sl) {
        sl.value = String(usd);
        // glass skin 把 range 換成 proxy div,只有拖曳會單向寫回真的 input。
        // 程式改 .value 它不會重畫,所以要主動通知(見 tpx-glass.js 的 glass-sync)。
        try { sl.dispatchEvent(new Event('glass-sync', { bubbles: false })); } catch (e) {}
    }
    if (out) {
        out.textContent = usd > 0 ? ('$' + usd.toLocaleString()) : 'OFF';
        out.classList.toggle('off', usd === 0);
    }
    if (hint) {
        hint.textContent = usd > 0
            ? ('(price distance ' + ticks + 't · ' + c.size + ' contracts → $' + c.tv.toFixed(2) + '/tick)')
            : '(per-trade profit cap · 0=unlimited)';
    }
}

// 使用者拖滑桿 → 美元換成 ticks 寫回真相,再重畫
function syncTpCapUsd(mode) {
    const sl = document.getElementById('tp-cap-usd-' + mode);
    if (!sl) return;
    const usd = parseInt(sl.value, 10) || 0;
    const c = capTickValue(mode);
    setCapTicks(mode, (usd > 0 && c.tv > 0) ? Math.max(1, Math.round(usd / c.tv)) : 0);
}

// 換合約/改口數 → ticks 不變(價距是絕對的),美元顯示要重算
function refreshCapsForContract(mode) { renderCapUi(mode); }
function refreshTpCapForContract(mode) { renderCapUi(mode); }

function renderMetrics(m, backtestTrades, options) {
    const renderOptions = options || {};
    const panel = document.getElementById('metrics-panel');
    // Only show the metrics panel when the BACKTEST tab is active.
    // If user is on LIVE, keep it hidden — data still gets rendered into the
    // grid below and will appear next time they switch back to BACKTEST.
    const backtestActive = document.querySelector('.tab.active')?.dataset?.tab === 'backtest';
    panel.style.display = 'block';
    if (backtestActive) {
        panel.classList.remove('hidden');
        // Scroll the sidebar so a newly-completed backtest is actually in view.
        // Passive trade-history refreshes opt out so EXECUTE TRADES never
        // hijacks the user's current parameter-panel position.
        if (!renderOptions.preserveSidebarScroll) {
            setTimeout(() => {
                try { panel.scrollIntoView({behavior: 'smooth', block: 'nearest'}); } catch(_){}
            }, 0);
        }
    } else {
        panel.classList.add('hidden');
    }

    // Clean up old per-strategy breakdown
    const oldSub = document.getElementById('strategy-breakdown');
    if (oldSub) oldSub.remove();

    const grid = document.getElementById('metrics-grid');
    const allTrades = backtestTrades || [];
    const fullStats = _computeTradeStats(allTrades);

    // ── Selectable performance window ────────────────────────────────
    // Build the dropdown from how much data we actually have (1w/2w/1m/…/ALL),
    // then filter the trades to the chosen lookback and recompute everything.
    // Calendar span (first→last trade) drives which windows are offered, so it
    // matches the calendar-based filter below (not the trading-day count).
    let _spanDays = 0;
    if (allTrades.length) {
        let minTs = Infinity, maxTs = 0;
        allTrades.forEach(t => {
            const iso = t.entry_time || t.exit_time;
            const ms = iso ? new Date(iso).getTime() : 0;
            if (!ms) return;
            if (ms < minTs) minTs = ms;
            if (ms > maxTs) maxTs = ms;
        });
        if (maxTs > minTs) _spanDays = (maxTs - minTs) / 86400000;
    }
    _populatePerfWindowOptions(_spanDays);
    const winDays = _perfWindowDays || 0;          // 0 = ALL
    let windowedTrades = allTrades;
    if (winDays > 0 && allTrades.length) {
        let maxTs = 0;
        allTrades.forEach(t => {
            const iso = t.exit_time || t.entry_time;
            const ms = iso ? new Date(iso).getTime() : 0;
            if (ms > maxTs) maxTs = ms;
        });
        const cutoff = maxTs - winDays * 86400000;
        windowedTrades = allTrades.filter(t => {
            const iso = t.entry_time || t.exit_time;
            return iso && new Date(iso).getTime() >= cutoff;
        });
    }
    const windowed = winDays > 0;
    const backtestStats = windowed ? _computeTradeStats(windowedTrades) : fullStats;

    // Live trade stats — shown in parentheses as reference only.
    const live = window._liveCompletedTrades;
    const liveStats = (live && live.length > 0) ? _computeTradeStats(live) : null;

    // Primary source: full-range backend metrics when window=ALL; otherwise
    // recompute everything client-side from the windowed trade subset.
    // 1.0.10 BUG FIX:這些值全部會走 .toFixed(),而 undefined.toFixed 是
    // TypeError —— 它在 innerHTML 組好**之前**就拋出,所以後果不是「少一張
    // 卡」而是**整個 metrics panel 一張都不出來**,而且畫面上看不出原因。
    //
    // 實際踩到的是 rr_ratio:它讀的是 `m.avg_rr_ratio`(不是 m.rr_ratio),
    // 後端只要沒送這個欄位、或改了名字,整片面板就消失。max_dd 讀
    // `m.max_drawdown` 也是同一個形狀。與其在 28 個 .toFixed() 呼叫點各補
    // 一次防呆,不如在來源就強制成數字 —— 下游全部自動安全。
    const num = (v, d = 0) => Number.isFinite(Number(v)) ? Number(v) : d;
    const total_pnl    = num(windowed ? backtestStats.total_pnl  : m.total_pnl);
    const total_gain   = num(windowed ? backtestStats.total_gain : (m.total_gain != null ? m.total_gain : backtestStats.total_gain));
    const total_loss   = num(windowed ? backtestStats.total_loss : (m.total_loss != null ? m.total_loss : backtestStats.total_loss));
    const total_trades = num(windowed ? backtestStats.trades     : m.total_trades);
    const rr_ratio     = num(windowed ? backtestStats.rr_ratio   : m.avg_rr_ratio);
    const max_dd       = num(windowed ? backtestStats.max_dd     : m.max_drawdown);
    const calmarPrimary = num(windowed ? backtestStats.calmar : m.calmar_ratio);
    const activeDaily  = windowed ? (backtestStats.daily_pnl || {}) : (m.daily_pnl || {});

    const totalPnlLabel = 'FINAL PNL';

    const paren = (v) => liveStats ? ' <span class="metric-real">(' + v + ')</span>' : '';

    // Profit Factor = gross gain / gross loss. PF>1 profitable, >2 strong.
    const profitFactorOf = (gain, loss) => Math.abs(loss || 0) > 0 ? Math.abs(gain || 0) / Math.abs(loss) : (gain > 0 ? Infinity : 0);
    const pfPrimary = windowed ? profitFactorOf(total_gain, total_loss)
                               : ((m.profit_factor != null) ? m.profit_factor : profitFactorOf(total_gain, total_loss));
    const pfLive = liveStats ? (liveStats.profit_factor != null ? liveStats.profit_factor : profitFactorOf(liveStats.total_gain, liveStats.total_loss)) : null;
    const fmtPF = (v) => (Number.isFinite(v) && v < 999) ? v.toFixed(2) : '∞';

    // Week-to-week variation: expose the coefficient of variation only.
    // The card is a compact stability diagnostic; dollar σ and week-count
    // context belong in the detail/research view, not in the metric grid.
    const wk = windowed ? _weeklyFromDaily(activeDaily) : (m.weekly_stats || {});
    const wkCount = wk.weekly_count || 0;
    const wkCv = wk.weekly_cv != null ? wk.weekly_cv : 0;
    const wkConsist = wk.weekly_consistency != null ? wk.weekly_consistency : 0;
    const weeklyVarItem = {
        label: 'WEEKLY CV',
        value: wkCount > 0 ? wkCv.toFixed(2) : '--',
        // Steady = low CV. Flag green when weekly CV < 1 and most weeks positive.
        cls: wkCount > 0 ? ((wkCv < 1 && wkConsist >= 0.6) ? 'pos' : (wkCv > 2 ? 'neg' : '')) : '',
    };

    // 1.0.8: WORST DAY — 當前窗口最差單日 pnl(實盤對照括號)
    const _dailyVals = Object.values(activeDaily || {});
    const worstDay = _dailyVals.length ? Math.min(..._dailyVals) : null;
    const liveDailyVals = liveStats && liveStats.daily_pnl ? Object.values(liveStats.daily_pnl) : [];
    const liveWorst = liveDailyVals.length ? Math.min(...liveDailyVals) : null;

    // 1.0.9: BEST DAY + Topstep XFA 一致性檢查。
    // XFA 請款門檻 = 最大單日淨利 ÷ 總淨利 <= 40%。關鍵在於**最大單日一旦創新高
    // 就鎖定到請款為止** —— 所以這張卡要一直看得到,不能只在超標時出現。
    // activeDaily 已經用 topstepTradeDateKey(17:00 CT 換日)分組,與 Topstep 一致。
    const bestDay = _dailyVals.length ? Math.max(..._dailyVals) : null;
    const netTotal = _dailyVals.reduce((a, b) => a + b, 0);
    const consistPct = (bestDay != null && bestDay > 0 && netTotal > 0)
        ? (bestDay / netTotal) : null;
    // 1.0.10: 兩段警示。分母(總淨利)在**每次請款後歸零**,但最大單日會鎖到
    // 請款為止 —— 所以「現在佔比很低」不代表安全,下一個請款週期一開始
    // 同一個 best day 佔比會瞬間變得很高。40% 起就要看得到黃色警告。
    //   >= 40%  黃色 ⚠  已達 XFA 上限
    //   >= 50%  紅色 ▲  同時踩到 Combine 的 50% 門檻
    const consistWarn = consistPct != null && consistPct >= 0.40;
    const consistDanger = consistPct != null && consistPct >= 0.50;
    // 超標時要再賺多少才合格:bestDay / 0.40 - 目前總淨利
    const consistNeed = consistWarn ? (bestDay / 0.40 - netTotal) : 0;
    const consistTip = consistPct == null
        ? 'Topstep XFA consistency: largest single trade-day net / total net must be <= 40%.'
        : (consistWarn
            ? ('Topstep consistency ' + (consistPct * 100).toFixed(0) + '% — '
               + (consistDanger ? 'OVER the Combine 50% limit as well. ' : 'at/over the XFA 40% limit. ')
               + 'Payout blocked until total net reaches $'
               + (bestDay / 0.40).toFixed(0) + ' (earn $' + consistNeed.toFixed(0) + ' more). '
               + 'The best day is locked in until you take a payout, and the denominator resets after it — '
               + 'a NEW record day raises the bar again. Cap daily gains with the PDPT slider.')
            : ('Topstep consistency ' + (consistPct * 100).toFixed(0) + '% — within the 40% limit. '
               + 'Any single trade-day above $' + (netTotal * 0.40).toFixed(0)
               + ' would push you over. Note the denominator resets after each payout.'));

    // 1.0.8: MONTHLY GAIN AVG = 30.44 天歸一化月率(run-rate,非日曆月平均)。
    // 例:2 個月賺 $4k → $2k/月;1 週 $400 → ~$1.7k/月。部分月不失真。
    const _monthlyRate = (dailyMap, totalPnl) => {
        const keys = Object.keys(dailyMap || {}).sort();
        if (!keys.length) return null;
        const spanDays = Math.max(
            1,
            (new Date(keys[keys.length - 1]) - new Date(keys[0])) / 86400000 + 1
        );
        return (totalPnl || 0) * 30.44 / spanDays;
    };
    const monthlyAvg = _monthlyRate(activeDaily, total_pnl);
    const liveMonthlyAvg = liveStats ? _monthlyRate(liveStats.daily_pnl, liveStats.total_pnl) : null;

    // 1.0.10: 品質門檻警示。這幾張卡改成一律白字 —— 用綠/紅去暗示「好/壞」
    // 在這裡是誤導:PF 1.05 是綠的,但那其實是接近噪音的策略。改成數值中性,
    // 只有**跨過門檻**才用跟 BEST DAY 同一顆黃色 ⚠ 標出來。
    //
    // 門檻取值理由:
    //   PF   < 1.2   扣掉滑價與手續費後,1.0~1.2 這段實務上等於打平
    //   RR   < 1     賺賠比小於 1,要靠勝率硬撐
    //   CALMAR < 1   年化報酬撐不過最大回撤
    //   交易 < 20     樣本太小,任何統計量都不可信
    //   WORST DAY 虧超過 $1k —— 單日損失已接近多數 Topstep 帳戶的 DLL
    // One Unicode warning shape for every severity. The wrapper supplies only
    // the semantic color (amber warning vs red danger).
    const _alertMark = '<span class="tpx-alert-mark" aria-hidden="true">&#9888;</span>';
    const _warn = (tip) => ' <span class="tpx-warn" title="' + _attr(tip) + '">' + _alertMark + '</span>';
    const _nTrades = Number(total_trades || 0);
    // 沒有交易時 PF/RR/CALMAR 沒有意義,不要掛警示(交易數本身仍會警示)
    const _judgeable = _nTrades > 0;
    const _pfWarn = _judgeable && Number.isFinite(pfPrimary) && pfPrimary < 1.2;
    const _rrWarn = _judgeable && Number.isFinite(rr_ratio) && rr_ratio < 1;
    const _calWarn = _judgeable && Number.isFinite(calmarPrimary) && calmarPrimary < 1;
    const _cntWarn = _nTrades < 20;
    const _worstWarn = worstDay != null && worstDay < -1000;
    // MAX DD 兩段:$1k 是多數 Topstep $50K 帳戶單日虧損上限的量級,
    // $2k 已經吃掉 $50K Combine 最大虧損額度的一大塊。
    const _ddWarn = max_dd > 1000;
    const _ddDanger = max_dd > 2000;
    const _ddTip = _ddDanger
        ? ('Max drawdown $' + max_dd.toFixed(0) + ' — over $2,000. On a $50K Combine this eats a large '
           + 'share of the max loss limit; a repeat of this drawdown from a worse starting point ends the account.')
        : ('Max drawdown $' + max_dd.toFixed(0) + ' — over $1,000, which is the order of the daily loss '
           + 'limit on most Topstep accounts. Size down or tighten the stop.');

    // Layout is kept compact by pairing the primary account metrics in two columns.
    const items = [
        // 1.0.10: 版面兩兩配對(.metrics-grid 是兩欄,順序即配對):
        //   FINAL PNL | MONTHLY   ·  TOTAL GAIN | TOTAL LOSS  ·  BEST | WORST DAY
        //   MAX DD | WEEKLY VAR   ← 風險/穩定度提前,在看品質指標之前先看風險
        //   PF | CALMAR           ·  TRADES | RR             ·  LONG | SHORT WIN
        { label: totalPnlLabel,
          value: '$' + total_pnl.toFixed(0) + paren(liveStats ? '$' + num(liveStats.total_pnl).toFixed(0) : ''),
          cls: total_pnl >= 0 ? 'pos' : 'neg' },
        { label: 'MONTHLY PNL',
          value: (monthlyAvg != null ? '$' + monthlyAvg.toFixed(0) : '--')
                 + paren(liveMonthlyAvg != null ? '$' + liveMonthlyAvg.toFixed(0) : ''),
          cls: (monthlyAvg != null && monthlyAvg >= 0) ? 'pos' : 'neg' },
        { label: 'TOTAL WIN',
          value: '$' + total_gain.toFixed(0) + paren(liveStats ? '$' + num(liveStats.total_gain).toFixed(0) : ''),
          cls: total_gain > 0 ? 'pos' : '' },
        { label: 'TOTAL LOSS',
          value: '$' + total_loss.toFixed(0) + paren(liveStats ? '$' + num(liveStats.total_loss).toFixed(0) : ''),
          cls: total_loss < 0 ? 'neg' : '' },
        // 1.0.10: BEST DAY 的數值本身是獲利,一律綠色 —— 風險只用圖示表達。
        // One Unicode glyph; wrapper color conveys the severity.
        //   >=40% amber alert (XFA)   >=50% red alert (Combine)
        { label: 'BEST DAY'
                 + (consistDanger
                    ? ' <span class="tpx-danger" title="' + _attr(consistTip) + '">' + _alertMark + '</span>'
                    : (consistWarn
                       ? ' <span class="tpx-warn" title="' + _attr(consistTip) + '">' + _alertMark + '</span>'
                       : '')),
          value: (bestDay != null ? '$' + bestDay.toFixed(0) : '--')
                 + (consistPct != null
                    ? ' <span class="metric-real'
                      + (consistDanger ? ' tpx-danger' : (consistWarn ? ' tpx-warn' : ''))
                      + '" title="' + _attr(consistTip) + '">('
                      + (consistPct * 100).toFixed(0) + '% consist)</span>'
                    : ''),
          cls: bestDay > 0 ? 'pos' : '' },
        { label: 'WORST DAY'
                 + (_worstWarn ? _warn('Worst single trade-day loss exceeds $1,000 — that is close to the '
                                       + 'daily loss limit on most Topstep accounts. One such day can end the run.') : ''),
          value: (worstDay != null ? '$' + worstDay.toFixed(0) : '--')
                 + paren(liveWorst != null ? '$' + liveWorst.toFixed(0) : ''),
          cls: (worstDay != null && worstDay < 0) ? 'neg' : '' },
        // 1.0.10: MAX DD 顯示為負數 —— 它是回撤,語意上是損失,跟 WORST DAY 一致。
        // 後端的 max_dd 是正值幅度(這裡只改顯示不動資料),所以門檻直接比正值。
        // One Unicode glyph; wrapper color conveys the severity.
        //   > $1k amber alert   > $2k red alert
        { label: 'MAX DD'
                 + (_ddDanger
                    ? ' <span class="tpx-danger" title="' + _attr(_ddTip) + '">' + _alertMark + '</span>'
                    : (_ddWarn
                       ? ' <span class="tpx-warn" title="' + _attr(_ddTip) + '">' + _alertMark + '</span>'
                       : '')),
          value: '$' + (max_dd > 0 ? '-' + max_dd.toFixed(0) : max_dd.toFixed(0))
                 + paren(liveStats
                    ? '$' + (num(liveStats.max_dd) > 0 ? '-' + num(liveStats.max_dd).toFixed(0)
                                                       : num(liveStats.max_dd).toFixed(0))
                    : ''),
          cls: max_dd > 0 ? 'neg' : '' },
        weeklyVarItem,
        { label: 'PROFIT FACTOR'
                 + (_pfWarn ? _warn('PF below 1.2 — after slippage and commission this is effectively break-even. '
                                    + 'Treat it as no edge, not a small edge.') : ''),
          value: fmtPF(pfPrimary) + paren(pfLive != null ? fmtPF(pfLive) : ''),
          cls: '' },
        { label: 'CALMAR'
                 + (_calWarn ? _warn('Calmar below 1 — annualised return does not cover the max drawdown. '
                                     + 'The equity curve is paying more in pain than it returns.') : ''),
          value: calmarPrimary.toFixed(2) + paren(liveStats ? num(liveStats.calmar).toFixed(2) : ''),
          cls: '' },
        { label: 'TRADE COUNTS'
                 + (_cntWarn ? _warn('Fewer than 20 trades — the sample is too small for PF, RR, Calmar or win '
                                     + 'rate to mean anything. Widen the window before drawing conclusions.') : ''),
          value: String(total_trades || 0) + paren(liveStats ? String(liveStats.trades || 0) : ''),
          cls: '' },
        { label: 'RR RATIO'
                 + (_rrWarn ? _warn('RR below 1 — average win is smaller than average loss, so the strategy '
                                    + 'depends entirely on win rate holding up.') : ''),
          value: rr_ratio.toFixed(2) + paren(liveStats ? num(liveStats.rr_ratio).toFixed(2) : ''),
          cls: '' },
        // 1.0.9: WIN RATE 拆成 LONG / SHORT 兩卡(自動兩兩對齊)
        (function () {
            let ln = 0, lw = 0;
            (backtestTrades || []).forEach(t => {
                const d = String(t.direction || '').toUpperCase();
                if (d.includes('BUY') || d.includes('LONG')) { ln++; if ((t.pnl || 0) > 0) lw++; }
            });
            return { label: 'LONG WIN RATE',
                value: (ln ? (lw / ln * 100).toFixed(1) : '--') + '%' + paren(ln + ' tr'),
                cls: '' };
        })(),
        (function () {
            let sn = 0, sw = 0;
            (backtestTrades || []).forEach(t => {
                const d = String(t.direction || '').toUpperCase();
                if (!(d.includes('BUY') || d.includes('LONG'))) { sn++; if ((t.pnl || 0) > 0) sw++; }
            });
            return { label: 'SHORT WIN RATE',
                value: (sn ? (sw / sn * 100).toFixed(1) : '--') + '%' + paren(sn + ' tr'),
                cls: '' };
        })(),
    ];

    grid.innerHTML = items.map(i => `
        <div class="metric-card"${i.full ? ' style="grid-column:1 / -1;"' : ''}>
            <div class="label">${i.label}</div>
            <div class="value ${i.cls}">${i.value}</div>
        </div>
    `).join('');
}

const TRADE_DISPLAY_TIME_ZONE = SYSTEM_TIME_ZONES.market;

function _tradeDisplayTime(iso) {
    if (!iso) return null;
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return null;
    const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: TRADE_DISPLAY_TIME_ZONE,
        month: '2-digit', day: '2-digit', year: 'numeric',
        hour: '2-digit', minute: '2-digit', hour12: true,
    }).formatToParts(date);
    const value = (type) => {
        const part = parts.find(p => p.type === type);
        return part ? part.value : '';
    };
    const minute = value('minute');
    return {
        date: value('month') + '/' + value('day') + '/' + value('year'),
        time: value('hour') + ':' + minute + value('dayPeriod'),
    };
}

function formatTradeTimeRange(entryIso, exitIso) {
    const entry = _tradeDisplayTime(entryIso);
    const exit = _tradeDisplayTime(exitIso);
    if (!entry && !exit) return '--';
    if (!entry) return exit.date + ' ' + exit.time;
    if (!exit) return entry.date + ' ' + entry.time + ' → --';
    if (entry.date === exit.date) return entry.date + ' ' + entry.time + ' → ' + exit.time;
    return entry.date + ' ' + entry.time + ' → ' + exit.date + ' ' + exit.time;
}

// Keep the public/plain formatter above for aria labels and diagnostics, but
// render the visible range as fixed columns.  The date parts, clock, meridiem
// and separator then have the same x-coordinate on every row even when one
// row is 03:55AM and another is 11:49AM.
function _tradeDateMarkup(dateText) {
    const parts = String(dateText || '').split('/');
    if (parts.length !== 3) return '<span class="trade-date trade-date-raw">' + _attr(dateText) + '</span>';
    return '<span class="trade-date">'
        + '<span class="trade-date-part trade-month">' + _attr(parts[0]) + '</span>'
        + '<span class="trade-date-sep" aria-hidden="true">/</span>'
        + '<span class="trade-date-part trade-day">' + _attr(parts[1]) + '</span>'
        + '<span class="trade-date-sep" aria-hidden="true">/</span>'
        + '<span class="trade-date-part trade-year">' + _attr(parts[2]) + '</span>'
        + '</span>';
}

function _tradeTimeMarkup(timeText) {
    const match = /^(\d{1,2})(?::(\d{2}))?(AM|PM)$/i.exec(String(timeText || '').replace(/\s+/g, ''));
    if (!match) return '<span class="trade-time trade-time-raw">' + _attr(timeText) + '</span>';
    const clock = match[1] + (match[2] ? ':' + match[2] : '');
    return '<span class="trade-time">'
        + '<span class="trade-clock">' + _attr(clock) + '</span>'
        + '<span class="trade-meridiem">' + _attr(match[3].toUpperCase()) + '</span>'
        + '</span>';
}

function formatTradeTimeRangeMarkup(entryIso, exitIso, plainLabel) {
    const entry = _tradeDisplayTime(entryIso);
    const exit = _tradeDisplayTime(exitIso);
    const label = plainLabel == null ? formatTradeTimeRange(entryIso, exitIso) : plainLabel;
    const attr = ' aria-label="' + _attr(label) + '"';
    if (!entry && !exit) {
        return '<span class="trade-time-range trade-time-empty"' + attr + '>--</span>';
    }
    if (!entry) {
        return '<span class="trade-time-range single-event"' + attr + '>'
            + _tradeDateMarkup(exit.date) + _tradeTimeMarkup(exit.time) + '</span>';
    }
    if (!exit) {
        return '<span class="trade-time-range single-event"' + attr + '>'
            + _tradeDateMarkup(entry.date) + _tradeTimeMarkup(entry.time) + '</span>';
    }
    if (entry.date === exit.date) {
        return '<span class="trade-time-range same-day"' + attr + '>'
            + _tradeDateMarkup(entry.date)
            + _tradeTimeMarkup(entry.time)
            + '<span class="trade-arrow" aria-hidden="true">→</span>'
            + _tradeTimeMarkup(exit.time)
            + '</span>';
    }
    return '<span class="trade-time-range cross-day"' + attr + '>'
        + _tradeDateMarkup(entry.date)
        + _tradeTimeMarkup(entry.time)
        + '<span class="trade-arrow" aria-hidden="true">→</span>'
        + _tradeDateMarkup(exit.date)
        + _tradeTimeMarkup(exit.time)
        + '</span>';
}

function formatTradeDuration(entryIso, exitIso) {
    if (!entryIso || !exitIso) return '--';
    const diffMs = new Date(exitIso).getTime() - new Date(entryIso).getTime();
    if (!Number.isFinite(diffMs) || diffMs < 0) return '--';
    const totalMinutes = Math.floor(diffMs / 60000);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours) return hours + 'hr' + (minutes ? ' ' + minutes + 'm' : '');
    return minutes + 'm';
}

// Tables are populated by replacing tbody.innerHTML, so a normal CSS
// transition has no previous DOM state from which to interpolate.  Keep a
// stable signature per tbody and explicitly arm a short, one-shot row arrival
// only when the data really changed.  This avoids replaying the animation on
// every passive history refresh while still making a newly rendered table
// visible instead of appearing in a hard cut.
const _tradeTableRenderKeys = new WeakMap();

function _tradeTableRenderKey(rows) {
    return (rows || []).map((t) => [
        t.trade_id ?? t.id ?? '',
        t.entry_time ?? '',
        t.exit_time ?? '',
        t.entry_price ?? '',
        t.exit_price ?? '',
        t.pnl ?? '',
        t.gross_pnl ?? '',
        t.commission ?? '',
        t.fees ?? '',
        t.direction ?? '',
        t.size ?? t.contracts ?? '',
        t.symbol ?? '',
    ].map(value => String(value)).join('~')).join('|') || 'empty';
}

function _animateTradeTableRows(tbody, rows) {
    if (!tbody) return;
    const key = _tradeTableRenderKey(rows);
    if (_tradeTableRenderKeys.get(tbody) === key) return;
    _tradeTableRenderKeys.set(tbody, key);

    if (tbody._tradeTableAnimationTimer) {
        clearTimeout(tbody._tradeTableAnimationTimer);
        tbody._tradeTableAnimationTimer = null;
    }
    tbody.classList.remove('trade-table-animating');
    const renderedRows = Array.from(tbody.querySelectorAll(':scope > tr'));
    renderedRows.forEach((row, index) => {
        row.style.setProperty('--trade-row-index', String(Math.min(index, 8)));
    });

    // Force the class removal to commit before the next frame adds it again.
    // This is a single tbody reflow, not a per-cell layout loop.
    void tbody.offsetWidth;
    const schedule = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 16));
    schedule(() => {
        if (!tbody.isConnected) return;
        tbody.classList.add('trade-table-animating');
        tbody._tradeTableAnimationTimer = window.setTimeout(() => {
            tbody.classList.remove('trade-table-animating');
            renderedRows.forEach(row => row.style.removeProperty('--trade-row-index'));
            tbody._tradeTableAnimationTimer = null;
        }, 420);
    });
}

function _animateBottomPane(panel) {
    if (!panel || panel.classList.contains('hidden')) return;
    panel.classList.remove('bottom-pane-enter');
    void panel.offsetWidth;
    const schedule = window.requestAnimationFrame || ((callback) => window.setTimeout(callback, 16));
    schedule(() => {
        if (!panel.isConnected || panel.classList.contains('hidden')) return;
        panel.classList.add('bottom-pane-enter');
        window.setTimeout(() => panel.classList.remove('bottom-pane-enter'), 360);
    });
}

function renderTrades(trades) {
    const tbody = document.getElementById('trades-tbody');
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text2);padding:20px;">NO TRADE DATA</td></tr>';
        _animateTradeTableRows(tbody, []);
        return;
    }

    // Sort newest → oldest, like TopstepX journal
    const sorted = [...trades].sort((a, b) => {
        const ta = a.entry_time ? new Date(a.entry_time).getTime() : 0;
        const tb = b.entry_time ? new Date(b.entry_time).getTime() : 0;
        return tb - ta;
    });
    tbody.innerHTML = sorted.map((t, i) => {
        const netPnl = t.pnl || 0;
        const commission = (t.commission != null) ? t.commission : 1.0;
        const fees = (t.fees != null) ? t.fees : 2.80;
        // Gross P&L (matches TopstepX P&L column — before costs)
        const grossPnl = netPnl + commission + fees;
        const pnlClass = grossPnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        const dirLabel = t.direction === 'buy' ? 'Long' : 'Short';
        const dirColor = t.direction === 'buy' ? 'var(--green)' : 'var(--red)';
        const symbol = displaySymbolFromTrade(t);
        const size = t.size || t.contracts || 1;
        const contract = String(size) + ' ' + String(symbol).replace(/^\/+/, '');
        const grossStr = '$' + (grossPnl >= 0 ? '+' : '-') + Math.abs(grossPnl).toFixed(2);
        const timeLabel = formatTradeTimeRange(t.entry_time, t.exit_time);
        return '<tr>' +
            '<td class="trade-contract-cell" style="width:82px;">' + contract + '</td>' +
            '<td class="trade-time-cell" style="font-family:\'IBM Plex Mono\',monospace;">' + formatTradeTimeRangeMarkup(t.entry_time, t.exit_time, timeLabel) + '</td>' +
            '<td>' + formatTradeDuration(t.entry_time, t.exit_time) + '</td>' +
            '<td>' + t.entry_price.toFixed(2) + '</td>' +
            '<td>' + (t.exit_price ? t.exit_price.toFixed(2) : '--') + '</td>' +
            '<td class="' + pnlClass + '">' + grossStr + '</td>' +
            '<td class="pnl-neg">$-' + commission.toFixed(2) + '</td>' +
            '<td class="pnl-neg">$-' + fees.toFixed(2) + '</td>' +
            '<td style="color:' + dirColor + ';">' + dirLabel + '</td>' +
        '</tr>';
    }).join('');
    _animateTradeTableRows(tbody, sorted);
}

// Render real TopstepX trade history in the EXECUTE TRADES bottom tab
function renderExecuteTrades(trades) {
    const tbody = document.getElementById('execute-tbody');
    if (!tbody) return;
    if (!trades || trades.length === 0) {
        _executeLatestTradeKey = '';
        tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text2);padding:20px;">NO EXECUTE TRADE DATA</td></tr>';
        _animateTradeTableRows(tbody, []);
        return;
    }

    const sorted = [...trades].sort((a, b) => {
        const ta = a.entry_time ? new Date(a.entry_time).getTime() : 0;
        const tb = b.entry_time ? new Date(b.entry_time).getTime() : 0;
        return tb - ta;
    });
    const latest = sorted[0] || {};
    const latestKey = String(latest.trade_id || '') + '|' + String(latest.entry_time || '');
    const newestChanged = !!_executeLatestTradeKey && latestKey !== _executeLatestTradeKey;
    _executeLatestTradeKey = latestKey;

    tbody.innerHTML = sorted.map((t) => {
        const grossPnl = (t.gross_pnl != null)
            ? Number(t.gross_pnl)
            : Number(t.pnl || 0) + Number(t.commission || 0) + Number(t.fees || 0);
        const commission = (t.commission != null) ? t.commission : 1.0;
        const fees = (t.fees != null) ? t.fees : 2.80;
        const pnlClass = grossPnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        const dirLabel = t.direction === 'buy' ? 'Long' : 'Short';
        const dirColor = t.direction === 'buy' ? 'var(--green)' : 'var(--red)';
        const symbol = displaySymbolFromTrade(t);
        const size = t.size || 1;
        const contract = String(size) + ' ' + String(symbol).replace(/^\/+/, '');
        const grossStr = '$' + (grossPnl >= 0 ? '+' : '-') + Math.abs(grossPnl).toFixed(2);
        const timeLabel = formatTradeTimeRange(t.entry_time, t.exit_time);
        return '<tr>' +
            '<td class="trade-contract-cell" style="width:82px;">' + contract + '</td>' +
            '<td class="trade-time-cell" style="font-family:\'IBM Plex Mono\',monospace;">' + formatTradeTimeRangeMarkup(t.entry_time, t.exit_time, timeLabel) + '</td>' +
            '<td>' + formatTradeDuration(t.entry_time, t.exit_time) + '</td>' +
            '<td>' + (t.entry_price != null ? Number(t.entry_price).toFixed(2) : '--') + '</td>' +
            '<td>' + (t.exit_price != null ? Number(t.exit_price).toFixed(2) : '--') + '</td>' +
            '<td class="' + pnlClass + '">' + grossStr + '</td>' +
            '<td class="pnl-neg">$-' + commission.toFixed(2) + '</td>' +
            '<td class="pnl-neg">$-' + fees.toFixed(2) + '</td>' +
            '<td style="color:' + dirColor + ';">' + dirLabel + '</td>' +
        '</tr>';
    }).join('');
    _animateTradeTableRows(tbody, sorted);
    if (newestChanged && executeTradesTabIsActive()) revealNewestExecuteTrade();
}

function classifyZoneType(z) {
    if (!z.formed_at) return '-';
    const code = getSessionCodeFromDate(new Date(z.formed_at));
    return ['ASIA', 'EURO', 'PRE', 'RTH', 'AH'].indexOf(code) >= 0 ? code : '-';
}

function zpad(n) { return n < 10 ? '0'+n : n; }

function renderZones(zones) {
    const tbody = document.getElementById('zones-tbody');
    if (!zones || zones.length === 0) {
        tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text2);padding:20px;">NO ZONE DATA</td></tr>';
        return;
    }

    // Sort chronologically (oldest first) by formed_at
    const sorted = [...zones].sort((a, b) => new Date(a.formed_at) - new Date(b.formed_at));

    tbody.innerHTML = sorted.map((z, idx) => {
        const statusColor = z.status === 'active' ? 'var(--cyan)' : z.status === 'left' ? 'var(--text2)' : 'var(--text2)';
        
        const formatTime = (iso) => {
            if (!iso) return 'Active';
            const d = new Date(iso);
            return `${d.getMonth()+1}/${d.getDate()} ${zpad(d.getHours())}:${zpad(d.getMinutes())}`;
        };

        const formedStr = formatTime(z.formed_at);
        const endedStr = formatTime(z.left_at);
        const typeStr = classifyZoneType(z);

        // Duration: always positive
        let durationMin = z.duration_minutes || 0;
        if (z.formed_at && z.left_at) {
            durationMin = Math.round((new Date(z.left_at) - new Date(z.formed_at)) / 60000);
        }
        if (durationMin < 0) durationMin = Math.abs(durationMin);

        const matureStr = z.mature ? '<span style="background:#00e5a0; color:#0b0e14; padding:1px 4px; font-size:8px; border-radius:2px;">MATURE</span>' : '';

        return '<tr>' +
            '<td style="color:var(--cyan);font-weight:500;">' + (idx + 1) + ' ' + matureStr + '</td>' +
            '<td>' + typeStr + '</td>' +
            '<td style="color:' + statusColor + ';">' + z.status.toUpperCase() + '</td>' +
            '<td>' + formedStr + ' -> ' + endedStr + '</td>' +
            '<td>' + durationMin + ' min</td>' +
            '<td style="color:var(--amber);font-weight:500;">' + z.poc.toFixed(2) + '</td>' +
            '<td>' + z.vah_80.toFixed(2) + '</td>' +
            '<td>' + z.val_80.toFixed(2) + '</td>' +
            '<td>' + z.total_volume.toLocaleString() + '</td>' +
            '</tr>';
    }).join('');
}

// -- Utilities -------------------------------------

let _connectionStatusKind = 'idle';
let _connectionStatusText = 'DISCONNECTED';

function _refreshConnectionState(options) {
    const opts = options || {};
    const username = document.getElementById('username');
    const apikey = document.getElementById('apikey');
    const email = (username && username.value || '').trim();
    const emailValid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    const typedKey = (apikey && apikey.value || '').trim();
    const storedKey = !!(apikey && apikey.dataset.configured === '1');
    const keyValid = !!typedKey || storedKey;

    updateConnectionInitial(email);

    if (username) username.setAttribute('aria-invalid', emailValid ? 'false' : 'true');
    if (apikey) apikey.setAttribute('aria-invalid', keyValid ? 'false' : 'true');

    const connected = _connectionStatusKind === 'ok'
        && /^CONNECTED$/i.test(_connectionStatusText);
    const hardError = _connectionStatusKind === 'err'
        && /OFFLINE|FAILED|ERROR/i.test(_connectionStatusText);
    let state = 'ready';
    if (!emailValid || !keyValid) state = 'error';
    else if (connected && !opts.credentialsChanged) state = 'connected';
    else if (hardError && !opts.credentialsChanged) state = 'error';

    document.documentElement.dataset.connectionState = state;
    const trigger = document.getElementById('conn-trigger');
    if (trigger) trigger.classList.toggle('connected', state === 'connected');
    _renderTopstepProviderFromUi();
    return state;
}

function setStatus(type, text) {
    const dot = document.getElementById('api-status');
    const label = document.getElementById('api-status-text');
    dot.className = 'status-dot ' + type;
    label.textContent = text;
    _connectionStatusKind = type;
    _connectionStatusText = text;
    _refreshConnectionState();
    _renderTopstepProviderFromUi();
}

function scrollSystemLogToBottom() {
    const container = document.getElementById('log-container');
    if (!container) return;
    const bottomContent = container.closest('.bottom-content');
    requestAnimationFrame(() => {
        container.scrollTop = container.scrollHeight;
        const logTab = document.getElementById('btab-log');
        if (bottomContent && logTab && !logTab.classList.contains('hidden')) {
            bottomContent.scrollTop = bottomContent.scrollHeight;
        }
    });
}

let _lastLogEntry = null;
let _lastLogMsg = '';
let _lastLogType = '';
let _lastLogCount = 1;

function log(msg, type) {
    type = type || '';
    const container = document.getElementById('log-container');
    if (!container) return;
    const time = new Date().toLocaleTimeString('en-US', {hour12:false});
    if (_lastLogEntry && msg === _lastLogMsg && type === _lastLogType) {
        _lastLogCount += 1;
        _lastLogEntry.textContent = '[' + time + '] ' + msg + ' (' + _lastLogCount + ')';
        scrollSystemLogToBottom();
        return;
    }
    const entry = document.createElement('div');
    entry.className = 'log-entry ' + type;
    entry.textContent = '[' + time + '] ' + msg;
    container.appendChild(entry);
    _lastLogEntry = entry;
    _lastLogMsg = msg;
    _lastLogType = type;
    _lastLogCount = 1;
    scrollSystemLogToBottom();
}

// ── LEARN RESULT panel ─────────────────────────────
// Fetches the on-disk scorer(s) the last LEARN wrote and renders meta + full
// raw-space weights, so the user sees exactly what live/backtest will use.
function _fmtTs(s) {
    if (!s) return '--';
    try { return new Date(s).toLocaleString('en-US', { hour12: false }); }
    catch (e) { return s; }
}

// 1.0.8: 移除 LEARN RESULT 面板 (_renderScorerCard + loadLearnResult)

// ── Research PNL CURVE: cumulative equity + Topstep $2K trailing-DD line ──
// The DD line starts $2000 below break-even and trails UP only as each day's
// settled PnL sets a new equity high ("increase as income settles every day"),
// then LOCKS at break-even (0) once it has climbed from -2000 to 0 — i.e. once
// cumulative profit reaches +$2000. Mirrors Topstep's EOD trailing drawdown.
// 1.0.10 #1:內容畫好之後才叫 glass 重新取樣。
// glass 的折射是 DOM 快照,MutationObserver 那條路有 320ms 防抖 +
// requestIdleCallback + 每次只重建一個 stage,最壞超過一秒 —— 期間取樣停在
// 「內容還沒生成」的狀態,PNL 曲線連座標軸都還沒畫就被拷走,看起來就是全黑。
// 在真正畫完的那一刻主動通知,黑畫面與過時取樣都會消失。
// rAF 包一層是因為 canvas/表格常常在同一個 tick 才剛寫進 DOM。
function glassResample(target) {
    if (!window.TpxGlass || typeof TpxGlass.resample !== 'function') return;
    requestAnimationFrame(() => {
        try { TpxGlass.resample(target); } catch (e) {}
    });
}

function renderPnlCurve() {
    const host = document.getElementById('pnl-curve-body');
    if (!host) return;

    const css = getComputedStyle(document.documentElement);
    const pick = (name, fb) => (css.getPropertyValue(name).trim() || fb);
    const C = {
        green: pick('--green', '#00e5a0'), red: pick('--red', '#ff4060'),
        cyan:  pick('--cyan', '#64dcff'),  amber: pick('--amber', '#ffa726'),
        text2: pick('--text2', '#556178'), text3: pick('--text3', '#3a4560'),
        border: 'rgba(100,220,255,0.10)',
    };

    const trades = (backtestData && backtestData.trades) ? backtestData.trades.slice() : [];
    const done = trades.filter(t => t.pnl != null && (t.exit_time || t.entry_time));

    // 1.0.9: live 已平倉交易(紫色曲線)。無回測時 live 直接當主曲線。
    const liveDone = (window._liveCompletedTrades || [])
        .filter(t => t.pnl != null && (t.exit_time || t.entry_time))
        .slice()
        .sort((a, b) => new Date(a.exit_time || a.entry_time) - new Date(b.exit_time || b.entry_time));
    const baseIsLive = done.length === 0 && liveDone.length > 0;
    if (baseIsLive) done.push(...liveDone);

    const frame = host.closest('.bottom-content, .cal-analysis-panel') || host.parentElement;
    const W = Math.max(320, host.clientWidth || (frame ? frame.clientWidth : 600));
    const H = Math.max(220, host.clientHeight || 280);
    host.style.height = H + 'px';

    let canvas = document.getElementById('pnl-curve-canvas');
    if (!canvas) {
        canvas = document.createElement('canvas');
        canvas.id = 'pnl-curve-canvas';
        canvas.style.cssText = 'display:block;width:100%;height:100%;';
        host.appendChild(canvas);
    }
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    if (!done.length) {
        ctx.fillStyle = C.text2;
        ctx.font = '12px "IBM Plex Mono", monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText('No trades yet — run BACKTEST or load LIVE trades', W / 2, H / 2);
        return;
    }

    done.sort((a, b) => new Date(a.exit_time || a.entry_time) - new Date(b.exit_time || b.entry_time));

    const DD = 2000;
    let cum = 0;
    const pts = done.map(t => {
        cum += (t.pnl || 0);
        return { cum, day: tradeRealizedDayKey(t) || String(t.exit_time || t.entry_time).slice(0, 10) };
    });

    // End-of-day settled equity (last cum of each day)
    const eod = {};
    pts.forEach(p => { eod[p.day] = p.cum; });
    const days = Object.keys(eod).sort();

    // Threshold IN FORCE during each day = min(0, peak(EOD of EARLIER days) - DD).
    // Today's settle only lifts the line for tomorrow → the step is end-of-day.
    const thrDay = {};
    let prevPeak = 0;
    days.forEach(d => { thrDay[d] = Math.min(0, prevPeak - DD); prevPeak = Math.max(prevPeak, eod[d]); });
    const finalThr = Math.min(0, prevPeak - DD);
    const locked = finalThr >= 0;                 // climbed -2000 → 0, line frozen
    const thrAt = pts.map(p => thrDay[p.day]);

    // First time settled equity touches the line in force = account blown
    let breachIdx = -1;
    for (let i = 0; i < pts.length; i++) { if (pts[i].cum <= thrAt[i]) { breachIdx = i; break; } }

    // 1.0.9: 回測存在時,live 曲線作為疊加層(各自按成交序號鋪滿整個寬度,非時間對齊)
    let lpts = [];
    if (!baseIsLive && liveDone.length) {
        let lcum = 0;
        lpts = liveDone.map(t => { lcum += (t.pnl || 0); return { cum: lcum }; });
    }

    let lo = -DD, hi = DD * 0.25;
    for (const p of pts) { if (p.cum < lo) lo = p.cum; if (p.cum > hi) hi = p.cum; }
    for (const p of lpts) { if (p.cum < lo) lo = p.cum; if (p.cum > hi) hi = p.cum; }
    for (const v of thrAt) { if (v < lo) lo = v; }
    const vpad = (hi - lo) * 0.08 || 100; lo -= vpad; hi += vpad;

    const padL = 58, padR = 12, padT = 16, padB = 26;
    const plotW = W - padL - padR, plotH = H - padT - padB, n = pts.length;
    const x = i => padL + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
    const y = v => padT + (1 - (v - lo) / (hi - lo)) * plotH;

    ctx.font = '10px "IBM Plex Mono", monospace';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let g = 0; g <= 5; g++) {
        const v = lo + (hi - lo) * g / 5, yy = y(v);
        ctx.strokeStyle = C.border; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
        ctx.fillStyle = C.text3; ctx.fillText('$' + Math.round(v), padL - 6, yy);
    }

    ctx.strokeStyle = C.text2; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(padL, y(0)); ctx.lineTo(W - padR, y(0)); ctx.stroke();
    ctx.setLineDash([]);

    // x-axis date labels at first trade of selected days
    ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.fillStyle = C.text3;
    const firstIdx = {};
    pts.forEach((p, i) => { if (firstIdx[p.day] === undefined) firstIdx[p.day] = i; });
    const every = Math.max(1, Math.ceil(days.length / 8));
    days.forEach((d, di) => { if (di % every === 0) ctx.fillText(d.slice(5), x(firstIdx[d]), H - padB + 6); });

    // danger band under the stepped threshold
    ctx.globalAlpha = 0.06; ctx.fillStyle = C.red;
    ctx.beginPath(); ctx.moveTo(x(0), y(thrAt[0]));
    for (let i = 1; i < n; i++) { ctx.lineTo(x(i), y(thrAt[i - 1])); ctx.lineTo(x(i), y(thrAt[i])); }
    ctx.lineTo(x(n - 1), y(lo)); ctx.lineTo(x(0), y(lo)); ctx.closePath(); ctx.fill();
    ctx.globalAlpha = 1;

    // stepped trailing-DD line (amber while trailing, green once locked)
    ctx.lineWidth = 1.5; ctx.strokeStyle = locked ? C.green : C.amber;
    ctx.beginPath(); ctx.moveTo(x(0), y(thrAt[0]));
    for (let i = 1; i < n; i++) { ctx.lineTo(x(i), y(thrAt[i - 1])); ctx.lineTo(x(i), y(thrAt[i])); }
    ctx.stroke();

    // equity curve
    ctx.lineWidth = 1.8; ctx.strokeStyle = C.cyan; ctx.beginPath();
    pts.forEach((p, i) => { const xi = x(i), yi = y(p.cum); i ? ctx.lineTo(xi, yi) : ctx.moveTo(xi, yi); });
    ctx.stroke();

    // 1.0.9: live 累計 PnL 疊加曲線(紫色)
    if (lpts.length) {
        const m = lpts.length;
        const xl = i => padL + (m <= 1 ? plotW / 2 : (i / (m - 1)) * plotW);
        ctx.lineWidth = 1.6; ctx.strokeStyle = '#a855f7'; ctx.beginPath();
        lpts.forEach((p, i) => { const xi = xl(i), yi = y(p.cum); i ? ctx.lineTo(xi, yi) : ctx.moveTo(xi, yi); });
        ctx.stroke();
        const ll = lpts[m - 1];
        ctx.fillStyle = '#a855f7';
        ctx.beginPath(); ctx.arc(xl(m - 1), y(ll.cum), 3, 0, Math.PI * 2); ctx.fill();
    }

    const last = pts[n - 1];
    ctx.fillStyle = last.cum >= 0 ? C.green : C.red;
    ctx.beginPath(); ctx.arc(x(n - 1), y(last.cum), 3, 0, Math.PI * 2); ctx.fill();

    if (breachIdx >= 0) {
        ctx.strokeStyle = C.red; ctx.lineWidth = 1; ctx.setLineDash([2, 2]);
        ctx.beginPath(); ctx.moveTo(x(breachIdx), padT); ctx.lineTo(x(breachIdx), H - padB); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = C.red; ctx.beginPath(); ctx.arc(x(breachIdx), y(pts[breachIdx].cum), 4, 0, Math.PI * 2); ctx.fill();
        ctx.textAlign = 'left'; ctx.textBaseline = 'top';
        ctx.fillText('BLOWN', x(breachIdx) + 4, padT + 2);
    }

    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.fillStyle = C.cyan;
    ctx.fillText(baseIsLive ? '— live equity' : '— equity', padL + 4, padT + 2);
    ctx.fillStyle = locked ? C.green : C.amber;
    ctx.fillText(locked ? '— $2K DD (locked)' : '— $2K trailing DD', padL + 66, padT + 2);
    if (lpts.length) {
        ctx.fillStyle = '#a855f7';
        ctx.fillText('— live', padL + 200, padT + 2);
    }
    const status = document.getElementById('pnl-curve-status');
    if (status) {
        const liveText = lpts.length ? ' · live overlay' : '';
        status.textContent = (baseIsLive ? 'Live realized equity' : 'Backtest realized equity') + liveText;
    }
    // 1.0.9: 標題列統計 hint 已移除(final/peak/maxDD 文字)
    glassResample();   // 1.0.10 #1:曲線與座標軸畫完才取樣
}

function updateClock() {
    const now = new Date();
    document.getElementById('clock').textContent = now.toLocaleTimeString('en-US', {hour12:false});
}

// ════════════════════════════════════════════════════════════════════════
// Calendar comparison view (Backtest vs Live, monthly)
// ────────────────────────────────────────────────────────────────────────
// Backtest daily data comes from the LAST backtest result already in memory
// (`backtestData.trades`). Live daily data comes from /api/live/trade-history
// (deduped to one row per signal). Both bucket by the LOCAL entry date — the
// same rule the existing metrics use (computeMetrics groups by new Date(entry).
// getDate()), so the calendar agrees with the trade tables.
// ════════════════════════════════════════════════════════════════════════
let _calMonth = (function () {
    const key = topstepTradeDateKey(new Date());
    const parts = key ? key.split('-').map(Number) : [];
    return parts.length === 3
        ? new Date(parts[0], parts[1] - 1, 1)
        : (function () { const d = new Date(); return new Date(d.getFullYear(), d.getMonth(), 1); })();
})();
let _calLiveTrades = null;

function _calDateKey(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
function _calKeyFromTrade(t) {
    return tradeRealizedDayKey(t);
}
function _calFmtMoney(v) {
    return (v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function _calAgg(trades) {
    const map = {};
    for (const t of (trades || [])) {
        const k = _calKeyFromTrade(t);
        if (!k) continue;
        if (!map[k]) map[k] = { pnl: 0, n: 0 };
        map[k].pnl += (t.pnl || 0);
        map[k].n += 1;
    }
    return map;
}
function _calDedupeLive(trades) {
    // Collapse multi-account copies → one row per (entry-second, price, dir).
    const seen = new Set(), out = [];
    for (const t of (trades || [])) {
        const key = (t.entry_time || '').slice(0, 19) + '|' + t.entry_price + '|' + t.direction;
        if (seen.has(key)) continue;
        seen.add(key);
        out.push(t);
    }
    return out;
}

function _calTradesInVisibleMonth(trades) {
    const y = _calMonth.getFullYear();
    const m = _calMonth.getMonth();
    return (trades || []).filter(t => {
        const key = _calKeyFromTrade(t);
        if (!key) return false;
        const d = new Date(key + 'T00:00:00');
        return d.getFullYear() === y && d.getMonth() === m;
    });
}

function _tradeTs(t, key) {
    const raw = t && (t[key] || t.entry_time || t.time);
    if (!raw) return null;
    const d = new Date(raw);
    return Number.isFinite(d.getTime()) ? d : null;
}

function _tradeDir(t) {
    const d = String((t && t.direction) || '').toLowerCase();
    if (d === 'buy' || d === 'long') return 'buy';
    if (d === 'sell' || d === 'short') return 'sell';
    return d;
}

function _tradeEntry(t) {
    const v = Number(t && (t.entry_price != null ? t.entry_price : t.price));
    return Number.isFinite(v) ? v : null;
}

function _fmtPct(v) {
    return Number.isFinite(v) ? (v * 100).toFixed(1) + '%' : '—';
}

// 1.0.10: 移除 ORDER COMPARISON(Selected Preset vs Live Execution)。
// 配對只靠「同方向 + 進場時間 5 分鐘內」,歷史 live 列又常缺 preset 名,
// 歸因本來就是近似值,面板已從 HTML 移除。

async function _calFetchLive(force) {
    if (_calLiveTrades && !force) return;
    try {
        const resp = await fetch(API + '/live/trade-history' + (force ? '?refresh=true' : ''));
        const data = await resp.json();
        _calLiveTrades = _calDedupeLive(data.trades || []);
        window._liveCompletedTrades = _calLiveTrades;
    } catch (e) {
        _calLiveTrades = _calLiveTrades || [];
    }
}

// ════════════════════════════════════════════════════════════════════════
// RESEARCH robustness (1.0.9) — Monte Carlo · Walk-Forward · Slippage.
// Replaces the old Hunter/Liquidity summary. Runs entirely client-side
// on the latest backtest trades (cache-restored results work too); live fills
// already loaded for the Research view feed the slippage measurement.
// ════════════════════════════════════════════════════════════════════════

function _researchNum(value, digits) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return n.toFixed(digits == null ? 2 : digits);
}

function _researchClass(value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n === 0) return '';
    return n > 0 ? 'institution-pos' : 'institution-neg';
}

// 1.0.10: 月均損益。總 PnL 不可比 —— 回測 7 個月的 $5,000 跟 2 個月的 $5,000
// 是完全不同的東西,而正是這種比較讓 BEST 的 PF 4.25 看起來很好(那是 6-7 月
// 兩個月的數字)。一律用 30.44 天歸一化的月率來讀。
// 1.1.1: _ROB_DAYS_PER_MONTH (30.44) lived here and was already dead — the
// monthly figure arrives as rob.monthly_pnl / rob.span_months. A second copy
// of "how long a month is" is exactly the kind of constant that drifts
// unnoticed, so it is gone rather than kept "just in case".
// _robMonthlyOf below stays: it only divides an already-computed segment P&L
// by a month count for display, it does not redefine the month.

// 把一段期間的損益直接換算成月率(給走查各段用,各段長度不同才需要歸一化)
function _robMonthlyOf(pnl, months) {
    return (Number.isFinite(months) && months > 0) ? Number(pnl) / months : null;
}

// 1.1.1: the per-contract point-value table went with it. The backend already
// returns rob.slip.tick_value from backend.backtest.robustness.POINT_VALUE;
// keeping a browser copy as a "fallback" meant two tables that price P&L
// differently for whichever symbol was updated in only one of them.
const _ROB_TICK = 0.25;
// Documented EMAPMO market fill 2026-07-23 14:31Z: +3.5 pts vs strategy price.
const _ROB_SLIP_ANCHOR_TICKS = 14;
// Must match backend.backtest.robustness.DEFAULT_SLIP_LEVELS; the measured
// level is appended per render when it is not already one of these.
const _ROB_SLIP_LEVELS = [1, 2, 4, 8];


// 1.0.10p: _robSeriesStats moved to backend.backtest.robustness.series_stats.
// It was the shared kernel under Monte Carlo, walk-forward AND the slip table,
// so leaving a copy here would have kept all three able to drift from the
// backend independently.

// 1.0.10p: Monte Carlo and walk-forward used to be computed right here.
// They now live in backend/backtest/robustness.py, because a browser-only
// implementation meant the research path could not gate on these numbers, so
// scripts each rewrote their own bootstrap, and pytest could not reach any of
// it. The old one also used Math.random(), so the same trades produced
// different percentiles on every render and no reported figure could be
// checked afterwards; the backend seeds its RNG.
//
// This file now only formats what the endpoint returns. Do not reintroduce a
// local bootstrap "just for a preview" — that is the drift this move undid.
let _robTopstepCache = null;
let _robBackendCache = null;

function _robTradesCacheKey(trades) {
    return (trades || []).map(tr => [
        tr.trade_id || '', tr.exit_time || tr.entry_time || '',
        Number(tr.pnl || 0).toFixed(4), Number(tr.size || 1).toFixed(2), tr.symbol || '',
    ].join(':')).join('|');
}

async function _robFetchBackend(trades, slipLevels, force) {
    // Slip levels are part of the key: the measured level can change between
    // renders, and a cache hit would then serve a table missing that row.
    const cacheKey = _robTradesCacheKey(trades) + '#' + slipLevels.join(',');
    if (!force && _robBackendCache && _robBackendCache.cacheKey === cacheKey) {
        return _robBackendCache.value;
    }
    let value = null;
    try {
        const resp = await fetch(API + '/research/robustness', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                trades: trades.map(tr => ({
                    entry_time: tr.entry_time, pnl: tr.pnl,
                    size: tr.size, symbol: tr.symbol,
                })),
                iters: 1000,
                slip_levels: slipLevels,
            }),
        });
        if (resp.ok) value = await resp.json();
        else log('Robustness request failed: ' + resp.status, 'warn');
    } catch (e) {
        log('Robustness request failed: ' + e.message, 'warn');
    }
    _robBackendCache = { cacheKey: cacheKey, value: value };
    return value;
}

function _robPct(value, digits) {
    if (value == null) return '—';
    const n = Number(value);
    return Number.isFinite(n) ? (n * 100).toFixed(digits == null ? 1 : digits) + '%' : '—';
}

function _robUsd(value) {
    if (value == null) return '—';
    const n = Number(value);
    return Number.isFinite(n)
        ? '$' + Math.round(n).toLocaleString('en-US')
        : '—';
}

// 1.0.10: _robTopstepOutcomeText 隨「Observed sequence」文字區塊一併移除。

function _robHelpDot(text) {
    const value = String(text || '');
    return '<button type="button" class="help-dot rob-help-dot"'
        + ' aria-expanded="false" aria-describedby="global-help-tooltip"'
        + ' data-tip-en="' + _attr(value) + '">?</button>';
}

function _robAlert(severity, tip) {
    const cls = severity === 'danger' ? 'tpx-danger' : 'tpx-warn';
    return '<span class="' + cls + '" title="' + _attr(tip)
        + '"><span class="tpx-alert-mark" aria-hidden="true">&#9888;</span></span>';
}

function _robMaxDdAlert(value, source) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '';
    const label = source || 'Max DD';
    if (n > 2000) {
        return _robAlert('danger', label + ' is $' + Math.round(n)
            + ', above the $2,000 risk threshold.');
    }
    if (n > 1000) {
        return _robAlert('warn', label + ' is $' + Math.round(n)
            + ', above the $1,000 caution threshold.');
    }
    return '';
}

function _robPnlAlert(value, source) {
    const n = Number(value);
    if (!Number.isFinite(n) || n >= 0) return '';
    return _robAlert('warn', (source || 'PnL/mo') + ' P5 is below $0.');
}

function _robChartPct(value, min, max) {
    if (!Number.isFinite(value) || max <= min) return 50;
    return Math.max(0, Math.min(100, ((value - min) / (max - min)) * 100));
}

function _robPercentileChart(title, values, options) {
    const opts = options || {};
    const keys = ['p5', 'p25', 'p50', 'p75', 'p95'];
    const nums = keys.map(key => Number(values && values[key]));
    if (nums.some(value => !Number.isFinite(value))) {
        return '<div class="institution-status">No percentile data.</div>';
    }
    let min = opts.min == null ? Math.min.apply(Math, nums) : Number(opts.min);
    let max = opts.max == null ? Math.max.apply(Math, nums) : Number(opts.max);
    if (opts.includeZero !== false) {
        min = Math.min(min, 0);
        max = Math.max(max, 0);
    }
    if (!Number.isFinite(min) || !Number.isFinite(max)) {
        return '<div class="institution-status">No percentile data.</div>';
    }
    if (max <= min) { min -= 1; max += 1; }
    const pct = key => _robChartPct(Number(values[key]), min, max);
    const p5 = pct('p5'), p25 = pct('p25'), p50 = pct('p50');
    const p75 = pct('p75'), p95 = pct('p95');
    const format = typeof opts.format === 'function' ? opts.format : _researchNum;
    const tone = opts.tone || (nums[2] >= 0 ? 'chart-positive' : 'chart-negative');
    const zero = min <= 0 && max >= 0
        ? '<span class="rob-chart-zero" style="left:' + _robChartPct(0, min, max).toFixed(2) + '%"></span>'
        : '';
    const label = _attr(title + ' percentile chart');
    const itemHtml = keys.map((key, i) => '<span><b>P' + [5, 25, 50, 75, 95][i]
        + '</b><em>' + format(nums[i]) + '</em></span>').join('');
    return '<div class="rob-chart ' + tone + '" data-rob-chart="percentile">'
        + '<div class="rob-chart-title"><span>' + _attr(title) + '</span>'
        + (opts.alertHtml || '') + '</div>'
        + '<div class="rob-chart-track" role="img" aria-label="' + label + '">'
        + '<span class="rob-band rob-band-outer" style="left:' + p5.toFixed(2)
        + '%;width:' + Math.max(0, p25 - p5).toFixed(2) + '%"></span>'
        + '<span class="rob-band rob-band-inner" style="left:' + p25.toFixed(2)
        + '%;width:' + Math.max(0, p75 - p25).toFixed(2) + '%"></span>'
        + '<span class="rob-band rob-band-outer" style="left:' + p75.toFixed(2)
        + '%;width:' + Math.max(0, p95 - p75).toFixed(2) + '%"></span>'
        + zero
        + '<span class="rob-chart-median" style="left:' + p50.toFixed(2) + '%"></span>'
        + '</div>'
        + '<div class="rob-chart-axis"><span>' + format(min) + '</span><span>'
        + format(max) + '</span></div>'
        + '<div class="rob-percentile-values">' + itemHtml + '</div>'
        + '</div>';
}

function _robSeriesChart(title, points, options) {
    const opts = options || {};
    const rows = Array.isArray(points) ? points : [];
    const numeric = rows
        .filter(point => point && point.value != null && Number.isFinite(Number(point.value)))
        .map(point => Number(point.value));
    if (!numeric.length) return '<div class="institution-status">No chart data.</div>';
    const positiveOnly = !!opts.positiveOnly;
    const scale = Math.max(1, ...numeric.map(value => Math.abs(value)));
    const format = typeof opts.format === 'function' ? opts.format : _researchNum;
    const rowsHtml = rows.map(point => {
        const value = Number(point.value);
        const valid = point && point.value != null && Number.isFinite(value);
        const fraction = valid ? Math.min(1, Math.abs(value) / scale) : 0;
        const width = positiveOnly ? fraction * 100 : fraction * 50;
        const left = positiveOnly ? 0 : (valid && value < 0 ? 50 - width : 50);
        const barClass = positiveOnly ? 'risk' : (value < 0 ? 'neg' : 'pos');
        const used = point.used ? ' rob-series-used' : '';
        return '<div class="rob-series-row' + used + '">'
            + '<span class="rob-series-label">' + _attr(point.label) + '</span>'
            + '<span class="rob-series-track ' + (positiveOnly ? 'rob-series-positive' : 'rob-series-diverging') + '">'
            + (valid ? '<i class="rob-series-bar ' + barClass + '" style="left:' + left.toFixed(2)
                + '%;width:' + width.toFixed(2) + '%"></i>' : '')
            + '</span>'
            + '<span class="rob-series-value">' + (valid ? format(value) : '--') + '</span>'
            + '</div>';
    }).join('');
    return '<div class="rob-series-chart ' + (opts.tone || '') + '" data-rob-chart="series">'
        + '<div class="rob-chart-title"><span>' + _attr(title) + '</span>'
        + (opts.alertHtml || '') + '</div>'
        + '<div class="rob-series-rows">' + rowsHtml + '</div>'
        + '</div>';
}

function _robPercentileCurveSeries(curve, transform) {
    const keys = ['p5', 'p25', 'p50', 'p75', 'p95'];
    const rows = Array.isArray(curve) ? curve : [];
    return keys.map(key => ({
        key: key,
        label: key.toUpperCase(),
        points: rows.map(point => {
            const step = Number(point && point.step);
            const raw = Number(point && point[key]);
            const value = typeof transform === 'function' ? transform(raw) : raw;
            return { step: step, value: value };
        }).filter(point => Number.isFinite(point.step) && Number.isFinite(point.value)),
    })).filter(row => row.points.length);
}

function _robObservedCurveSeries(curve, valueKey, label, transform) {
    const rows = Array.isArray(curve) ? curve : [];
    const points = rows.map(point => {
        const step = Number(point && point.step);
        const raw = Number(point && point[valueKey]);
        const value = typeof transform === 'function' ? transform(raw) : raw;
        return { step: step, value: value };
    }).filter(point => Number.isFinite(point.step) && Number.isFinite(point.value));
    return points.length ? [{ key: 'observed', label: label || 'observed', points: points }] : [];
}

function _robSegmentCurveSeries(curve, valueKey, transform) {
    const rows = Array.isArray(curve) ? curve : [];
    const finalStep = rows.reduce((max, point) => Math.max(
        max, Number(point && point.step) || 0), 0);
    const grouped = new Map();
    rows.forEach(point => {
        const segment = Number(point && point.segment);
        const step = Number(point && point.step);
        const raw = Number(point && point[valueKey]);
        const value = typeof transform === 'function' ? transform(raw) : raw;
        if (!Number.isInteger(segment) || segment < 1 || !Number.isFinite(step)
            || !Number.isFinite(value)) return;
        if (!grouped.has(segment)) grouped.set(segment, []);
        grouped.get(segment).push({ step: step, value: value });
    });
    return Array.from(grouped.keys()).sort((a, b) => a - b).map(segment => {
        const points = grouped.get(segment).sort((a, b) => a.step - b.step);
        if (points.length) {
            // Every WF path owns the same global x-domain.  Without these
            // anchors, sparse segment points make SVG scale each path as if
            // it were a separate chart and the three paths appear detached.
            const first = points[0];
            const last = points[points.length - 1];
            points.unshift({ step: 0, value: 0 });
            if (first.step === 0) points[1] = { step: 0, value: first.value };
            if (finalStep > last.step) {
                points.push({ step: finalStep, value: last.value });
            }
        }
        return { key: 'segment-' + segment, label: segment + '/3', points: points };
    });
}

function _robLineChart(title, series, options) {
    const opts = options || {};
    const rows = (Array.isArray(series) ? series : []).map((line, index) => {
        const points = (Array.isArray(line.points) ? line.points : [])
            .filter(point => point && Number.isFinite(Number(point.step))
                && Number.isFinite(Number(point.value)))
            .map(point => ({ step: Number(point.step), value: Number(point.value) }))
            .sort((a, b) => a.step - b.step);
        return Object.assign({}, line, { points: points, index: index });
    }).filter(line => line.points.length);
    if (!rows.length) return '<div class="institution-status">No chart data.</div>';

    const values = rows.reduce((all, line) => all.concat(line.points.map(point => point.value)), []);
    let lo = opts.min == null ? Math.min.apply(Math, values) : Number(opts.min);
    let hi = opts.max == null ? Math.max.apply(Math, values) : Number(opts.max);
    if (opts.includeZero !== false) {
        lo = Math.min(lo, 0);
        hi = Math.max(hi, 0);
    }
    const thresholdValues = (Array.isArray(opts.thresholds) ? opts.thresholds : [])
        .map(Number).filter(Number.isFinite);
    if (thresholdValues.length) {
        lo = Math.min.apply(Math, [lo].concat(thresholdValues));
        hi = Math.max.apply(Math, [hi].concat(thresholdValues));
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) {
        return '<div class="institution-status">No chart data.</div>';
    }
    const rawRange = hi - lo;
    const pad = rawRange > 0 ? rawRange * 0.08 : Math.max(1, Math.abs(hi) * 0.08);
    lo -= pad;
    hi += pad;
    const minStep = Math.min.apply(Math, rows.reduce(
        (all, line) => all.concat(line.points.map(point => point.step)), []));
    const maxStep = Math.max.apply(Math, rows.reduce(
        (all, line) => all.concat(line.points.map(point => point.step)), []));
    const w = 720, h = 150, l = 8, r = 8, top = 8, bottom = 8;
    const x = step => l + (step - minStep) * ((w - l - r) / Math.max(1, maxStep - minStep));
    const y = value => top + (hi - value) * ((h - top - bottom) / Math.max(1, hi - lo));
    const pathFor = points => points.map((point, index) => (index ? 'L' : 'M')
        + x(point.step).toFixed(1) + ' ' + y(point.value).toFixed(1)).join(' ');
    const areaFor = (lower, upper) => {
        if (!lower || !upper || !lower.points.length || !upper.points.length) return '';
        const lowerPath = lower.points.map((point, index) => (index ? 'L' : 'M')
            + x(point.step).toFixed(1) + ' ' + y(point.value).toFixed(1)).join(' ');
        const upperPath = upper.points.slice().reverse().map(point => 'L'
            + x(point.step).toFixed(1) + ' ' + y(point.value).toFixed(1)).join(' ');
        return lowerPath + ' ' + upperPath + ' Z';
    };
    const palette = opts.palette || ['var(--red)', 'var(--cyan)', 'var(--white)', 'var(--green)', 'var(--amber)'];
    const colorFor = (line, index) => line.color || palette[index % palette.length];
    const lineClass = line => String(line.key || 'line').replace(/[^a-z0-9_-]/gi, '-');
    const byKey = new Map(rows.map(line => [String(line.key), line]));
    const bands = [];
    const outer = areaFor(byKey.get('p5'), byKey.get('p95'));
    const inner = areaFor(byKey.get('p25'), byKey.get('p75'));
    if (outer) bands.push('<path class="rob-line-band outer" d="' + outer + '"></path>');
    if (inner) bands.push('<path class="rob-line-band inner" d="' + inner + '"></path>');
    const zero = lo <= 0 && hi >= 0
        ? '<line class="rob-line-zero" x1="' + l + '" y1="' + y(0).toFixed(1)
            + '" x2="' + (w - r) + '" y2="' + y(0).toFixed(1) + '"></line>' : '';
    const thresholdLines = thresholdValues.map(value => '<line class="rob-line-threshold"'
        + ' data-threshold="' + value + '" x1="' + l + '" y1="' + y(value).toFixed(1)
        + '" x2="' + (w - r) + '" y2="' + y(value).toFixed(1) + '"></line>').join('');
    const paths = rows.map((line, index) => '<path class="rob-line-path ' + lineClass(line)
        + '" d="' + pathFor(line.points) + '" stroke="' + colorFor(line, index) + '"></path>').join('');
    const legend = rows.map((line, index) => '<span class="rob-line-key"><i style="background:'
        + colorFor(line, index) + '"></i>' + _attr(line.label || line.key || 'series') + '</span>').join('');
    const thresholdLegend = thresholdValues.map(value => '<span class="rob-line-key rob-threshold-key"><i></i>'
        + _attr(_robUsd(value)) + '</span>').join('');
    const format = typeof opts.format === 'function' ? opts.format : _researchNum;
    const aria = _attr(title + ' curve');
    return '<div class="rob-line-chart ' + (opts.tone || '') + '" data-rob-chart="curve">'
        + '<div class="rob-chart-title"><span>' + _attr(title) + '</span>'
        + (opts.alertHtml || '') + '</div>'
        + '<div class="rob-line-legend">' + legend + thresholdLegend + '</div>'
        + '<div class="rob-line-canvas" role="img" aria-label="' + aria + '">'
        + '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">'
        + '<rect x="0" y="0" width="' + w + '" height="' + h + '" fill="transparent"></rect>'
        + bands.join('') + thresholdLines + zero + paths + '</svg></div>'
        + '<div class="rob-line-axis"><span>' + format(lo) + '</span><span>'
        + format(hi) + '</span><span>trade 0–' + Math.round(maxStep) + '</span></div>'
        + '</div>';
}

function _robStatLines(items) {
    return '<div class="rob-stat-lines">' + (items || []).map(item =>
        '<span>' + _attr(item.label) + ' <b class="' + (item.cls || '') + '">'
        + String(item.value == null ? '--' : item.value) + '</b></span>').join('')
        + '</div>';
}

function _robTopstepRow(result, contracts, limit) {
    return result && Array.isArray(result.rows)
        ? result.rows.find(r => r.contracts === contracts && r.consistencyLimit === limit)
        : null;
}

function _robTopstepAnalysis(trades, slipPerContract, force) {
    const api = window.TPXTopstepEval;
    if (!api || typeof api.runPairedMonteCarlo !== 'function') {
        return { ok: false, error: 'Topstep simulator did not load.' };
    }
    const slipKey = Number(slipPerContract || 0).toFixed(6);
    const tradesKey = _robTradesCacheKey(trades);
    if (!force && _robTopstepCache
        && _robTopstepCache.tradesKey === tradesKey
        && _robTopstepCache.slipKey === slipKey) {
        return _robTopstepCache.value;
    }

    const common = {
        iterations: 10000,
        horizonDays: 60,
        maxLossLimit: 2000,
        sizes: [1, 2, 3, 5, 10],
        slippagePerContract: Number(slipPerContract) || 0,
    };
    const source = api.buildActiveDays(trades);
    common.source = source;
    // These are deliberately separate programs. Trading Combine uses the
    // official 50% consistency target and $3k objective. After passing, the
    // optional XFA Consistency payout path uses 40%, at least three active
    // days, and enough balance for the $125 minimum payout (50% of $250).
    const combine = api.runPairedMonteCarlo(trades, Object.assign({}, common, {
        baseTarget: 3000,
        minimumDays: 2,
        consistencyLimits: [0.5],
    }));
    const xfa = api.runPairedMonteCarlo(trades, Object.assign({}, common, {
        baseTarget: 250,
        minimumDays: 3,
        consistencyLimits: [0.4],
    }));
    const value = {
        ok: !!(combine.ok && xfa.ok),
        error: combine.error || xfa.error || null,
        combine: combine,
        xfa: xfa,
        slipPerContract: Number(slipPerContract) || 0,
    };
    _robTopstepCache = { tradesKey: tradesKey, slipKey: slipKey, value: value };
    return value;
}

function _robTopstepHtml(analysis, slipTicks) {
    if (!analysis || !analysis.ok) {
        return '<div class="institution-card institution-wide"><h3>TOPSTEP 50K</h3>'
            + '<div class="institution-status">' + ((analysis && analysis.error) || 'No result.') + '</div></div>';
    }

    const combine = analysis.combine;
    const xfa = analysis.xfa;
    const sizes = [1, 2, 3, 5, 10];
    const combineRows = sizes.map(size => _robTopstepRow(combine, size, 0.5));
    const xfaRows = sizes.map(size => _robTopstepRow(xfa, size, 0.4));
    const winner = combine.recommendation ? combine.recommendation.contracts : 1;
    const daysText = row => row && row.medianPassDays != null && row.p90PassDays != null
        ? row.medianPassDays + ' / ' + row.p90PassDays : '—';
    const topstepHead = (label, tip) => '<th>' + _attr(label) + _robHelpDot(tip) + '</th>';
    const combineHeaders = [
        topstepHead('SIZE', 'Contracts traded per signal in this replay.'),
        topstepHead('PASS', 'Percentage of replays reaching the $3,000 objective within 60 active days without an MLL or consistency failure.'),
        topstepHead('MLL FAIL', 'Percentage of replays breaching the $2,000 max-loss limit.'),
        topstepHead('OPEN@60', 'Percentage of replays still open after the 60-day horizon: neither pass nor fail.'),
        topstepHead('DAYS P50/P90', 'Median / 90th-percentile active days among passing replays.'),
        topstepHead('TARGET UP', 'Percentage of passing replays where the consistency rule raised the required target above $3,000.'),
        topstepHead('REQ P50', 'Median required target among passing replays.'),
    ].join('');
    const xfaHeaders = [
        topstepHead('SIZE', 'Contracts traded per signal in this replay.'),
        topstepHead('ELIGIBLE', 'Percentage of replays reaching the first-payout eligibility path within 60 active days.'),
        topstepHead('MLL FAIL', 'Percentage of replays breaching the $2,000 max-loss limit.'),
        topstepHead('OPEN@60', 'Percentage of replays still open after the 60-day horizon: neither eligible nor failed.'),
        topstepHead('DAYS P50/P90', 'Median / 90th-percentile active days among eligible replays.'),
        topstepHead('MIN PAYOUT BAL P50', 'Median required balance/target among eligible replays.'),
    ].join('');

    const combineRow = row => {
        if (!row) return '';
        return '<tr>'
            + '<td>' + row.contracts + ' MNQ</td>'
            + '<td class="institution-pos">' + _robPct(row.passRate, 2) + '</td>'
            + '<td class="' + (row.failRate > 0.02 ? 'institution-neg' : '') + '">' + _robPct(row.failRate, 2) + '</td>'
            + '<td>' + _robPct(row.openRate, 2) + '</td>'
            + '<td>' + daysText(row) + '</td>'
            + '<td>' + _robPct(row.targetRaisedAmongPassRate, 1) + '</td>'
            + '<td>' + _robUsd(row.medianPassTarget) + '</td>'
            + '</tr>';
    };
    const xfaRow = row => {
        if (!row) return '';
        return '<tr>'
            + '<td>' + row.contracts + ' MNQ</td>'
            + '<td class="institution-pos">' + _robPct(row.passRate, 2) + '</td>'
            + '<td class="' + (row.failRate > 0.02 ? 'institution-neg' : '') + '">' + _robPct(row.failRate, 2) + '</td>'
            + '<td>' + _robPct(row.openRate, 2) + '</td>'
            + '<td>' + daysText(row) + '</td>'
            + '<td>' + _robUsd(row.medianPassTarget) + '</td>'
            + '</tr>';
    };

    const source = combine.source || {};
    const sourceDays = Array.isArray(source.days) ? source.days.length : 0;
    const slipText = Number(slipTicks || 0) > 0
        ? Math.round(slipTicks) + ' ticks (' + _robUsd(analysis.slipPerContract) + '/contract)'
        : 'no added slippage';
    const topstepHelp = 'Topstep 50K simulation. Rows compare contract sizes; the table-column question marks '
        + 'define each metric. '
        + 'Trading Combine uses the 50% consistency rule and the XFA table is the '
        + 'post-Combine first-payout eligibility path. Results use 10,000 replays, '
        + slipText + ', the $2,000 max-loss limit, ' + source.tradeCount + ' source trades, and '
        + sourceDays + ' active days.';
    return '<div class="institution-card institution-wide rob-topstep-card">'
        + '<h3>TOPSTEP 50K' + _robHelpDot(topstepHelp) + '</h3>'
        + '<div class="rob-topstep-verdict"><strong>COMBINE · ' + winner + ' MNQ</strong></div>'
        + '<div class="rob-topstep-rules">'
        + '<section class="rob-topstep-rule"><h4>TRADING COMBINE · 50%'
        + _robHelpDot('Trading Combine: simulated pass rate within 60 active days. '
            + 'Use the column definitions for the exact meaning of each result; no row color implies a guarantee.') + '</h4>'
        + '<div class="rob-table-scroll"><table class="institution-table"><thead><tr>' + combineHeaders + '</tr></thead><tbody>'
         + combineRows.map(combineRow).join('') + '</tbody></table>'
        + '</div>'
        + '</section>'
        + '<section class="rob-topstep-rule"><h4>XFA CONSISTENCY · 40%'
        + _robHelpDot('XFA is evaluated after the Combine. Eligible means the simulated '
            + 'account reaches the first-payout path within 60 active days while staying '
            + 'inside the 40% consistency rule.') + '</h4>'
        + '<div class="rob-table-scroll"><table class="institution-table"><thead><tr>' + xfaHeaders + '</tr></thead><tbody>'
         + xfaRows.map(xfaRow).join('') + '</tbody></table>'
        + '</div>'
        + '</section></div>'
        + '</div>';
}

// 1.0.10p: _robWalkForward lived here and the standalone research worker had a second, independent
// three-way split. Two implementations of one concept, with nothing keeping
// them in step. backend.backtest.robustness.walk_forward is now the only one
// this panel uses; test_robustness.py pins it against the worker's.

// Measured slip: live fill vs the open of its 5m bar (the FACTOR backtest fill
// assumption). Market-order fills land <120s after the 5m boundary and are not
// deep price improvements; anything else is a limit fill and excluded. Falls
// back to the documented EMAPMO anchor (14t = 3.5 pts) while the sample is
// still too small to be trusted.
function _robMeasureSlip() {
    const out = { n: 0, medianTicks: null, usedTicks: _ROB_SLIP_ANCHOR_TICKS, anchor: true };
    const live = _calLiveTrades || [];
    const buf = _rawCandleBuffer || [];
    if (!live.length || !buf.length) return out;
    const openByTime = new Map();
    for (const c of buf) openByTime.set(c.time, c.open);
    const slips = [];
    for (const tr of live) {
        // _rawCandleBuffer uses chart time(UTC+本地位移)— fills 也得同基準
        let ts;
        try { ts = isoToChartTime(String(tr.entry_time)); } catch (e) { continue; }
        if (!Number.isFinite(ts) || tr.entry_price == null) continue;
        const dir = _tradeDir(tr);
        if (dir !== 'buy' && dir !== 'sell') continue;
        const m5 = Math.floor(ts / 300) * 300;
        const open = openByTime.get(m5);
        if (open == null) continue;
        const sign = dir === 'buy' ? 1 : -1;
        const slip = sign * (Number(tr.entry_price) - Number(open)) / _ROB_TICK;
        if ((ts - m5) < 120 && slip >= -4 && slip <= 60) slips.push(slip);
    }
    slips.sort((a, b) => a - b);
    out.n = slips.length;
    if (slips.length) out.medianTicks = slips[Math.floor(slips.length / 2)];
    if (slips.length >= 30) {
        out.usedTicks = Math.max(0, out.medianTicks);
        out.anchor = false;
    }
    return out;
}

// The old histogram/table pair was replaced by deterministic path charts fed
// by the backend's P5/P25/P50/P75/P95 ladder.

function _robBadge(pass, passText, failText) {
    return '<span class="rob-badge ' + (pass ? 'institution-pos' : 'institution-neg') + '">'
        + (pass ? passText : failText) + '</span>';
}

async function renderResearchRobustness(force) {
    const status = document.getElementById('robustness-status');
    const content = document.getElementById('robustness-content');
    if (!status || !content) return;
    const trades = (backtestData && backtestData.trades)
        ? backtestData.trades.filter(tr => tr.pnl != null) : [];
    if (!trades.length) {
        status.textContent = 'Run a backtest first — analysis uses the latest backtest trades.';
        content.innerHTML = '';
        return;
    }
    // Measured slip decides which extra level the slip table highlights, and
    // the backend scores the levels, so it has to be known before the request.
    const slip = _robMeasureSlip();
    const used = Math.max(1, Math.round(slip.usedTicks));
    const slipLevels = _ROB_SLIP_LEVELS.slice();
    if (slipLevels.indexOf(used) < 0) slipLevels.push(used);
    slipLevels.sort((a, b) => a - b);

    // 1.0.10p: every number below now comes from POST /api/research/robustness.
    // Bail out visibly rather than silently rendering a half-empty panel — a
    // blank card used to be indistinguishable from "no edge".
    status.textContent = 'Evaluating…';
    const rob = await _robFetchBackend(trades, slipLevels, !!force);
    if (!rob || !rob.stats) {
        status.textContent = 'Robustness service unavailable — see SYSTEM LOG.';
        content.innerHTML = '';
        return;
    }
    const base = {
        pnl: rob.stats.pnl, pf: rob.stats.pf,
        maxDd: rob.stats.max_dd, win: rob.stats.win, n: rob.stats.n,
    };
    // Sole source: backend.backtest.robustness.POINT_VALUE. evaluate() always
    // returns slip for a non-empty trade list, and the panel has already
    // returned above when rob is missing — so a local fallback table would
    // only ever be a second answer waiting to disagree.
    const tickVal = rob.slip.tick_value;
    const times = trades.map(tr => new Date(tr.entry_time).getTime()).filter(Number.isFinite);
    const d0 = times.length ? new Date(Math.min(...times)) : null;
    const d1 = times.length ? new Date(Math.max(...times)) : null;
    // 1.0.10: 月均是主要數字,總額退居括號 —— 不同長度的回測用總額比較沒有意義。
    const spanMonths = rob.span_months;
    const monthly = rob.monthly_pnl;
    status.textContent = 'Latest backtest · metrics shown below.';

    const topstepSlipPerContract = Math.max(0, Number(slip.usedTicks) || 0) * tickVal;
    const topstep = _robTopstepAnalysis(trades, topstepSlipPerContract, !!force);
    const topstepHtml = _robTopstepHtml(topstep, slip.usedTicks);

    // ── Monte Carlo ──────────────────────────────────────────
    // Backend field names are snake_case; map once here so the markup below
    // stays as it was rather than being rewritten in two places.
    const _mcRaw = rob.monte_carlo;
    const mc = _mcRaw && {
        iters: _mcRaw.iters,
        pnlP5: _mcRaw.pnl_p5, pnlP25: _mcRaw.pnl_p25,
        pnlP50: _mcRaw.pnl_p50, pnlP75: _mcRaw.pnl_p75, pnlP95: _mcRaw.pnl_p95,
        pLoss: _mcRaw.p_loss,
        ddP5: _mcRaw.dd_p5, ddP25: _mcRaw.dd_p25,
        ddP50: _mcRaw.dd_p50, ddP75: _mcRaw.dd_p75, ddP95: _mcRaw.dd_p95,
        pnlCurve: Array.isArray(_mcRaw.pnl_curve) ? _mcRaw.pnl_curve : [],
        ddCurve: Array.isArray(_mcRaw.dd_curve) ? _mcRaw.dd_curve : [],
        pDd2k: _mcRaw.p_dd_breach,
        pfP5: _mcRaw.pf_p5,
        pass: rob.monte_carlo_pass,
    };
    let mcHtml;
    if (!mc) {
        mcHtml = '<div class="institution-status">Not enough trades (need ≥10).</div>';
    } else {
        const perMo = (v) => (spanMonths && spanMonths > 0)
            ? _robUsd(v / spanMonths) : '—';
        const perMoValue = (v) => (spanMonths && spanMonths > 0)
            ? v / spanMonths : NaN;
        const mcPnlSeries = _robPercentileCurveSeries(mc.pnlCurve, perMoValue);
        const mcDdSeries = _robPercentileCurveSeries(mc.ddCurve);
        mcHtml = '<div class="rob-charts">'
            + _robLineChart('PNL / MO', mcPnlSeries, {
                format: perMo,
                alertHtml: _robPnlAlert(
                    spanMonths && spanMonths > 0 ? mc.pnlP5 / spanMonths : null,
                    'Monte Carlo'),
            })
            + _robLineChart('MAX DD', mcDdSeries, {
                format: _robUsd,
                tone: 'chart-risk',
                thresholds: [1000, 2000],
                alertHtml: _robMaxDdAlert(mc.ddP95, 'Monte Carlo P95 maxDD'),
            })
            + '</div>'
            + _robStatLines([
                { label: 'TOTAL PNL P5 / P50 / P95', value: _robUsd(mc.pnlP5) + ' / ' + _robUsd(mc.pnlP50) + ' / ' + _robUsd(mc.pnlP95), cls: _researchClass(mc.pnlP5) },
                { label: 'P(total loss)', value: _robPct(mc.pLoss), cls: mc.pLoss > 0.05 ? 'institution-neg' : 'institution-pos' },
                { label: 'P(maxDD > $2k)', value: _robPct(mc.pDd2k), cls: mc.pDd2k > 0.05 ? 'institution-neg' : 'institution-pos' },
                { label: 'PF P5', value: _researchNum(mc.pfP5, 2), cls: mc.pfP5 > 1 ? 'institution-pos' : 'institution-neg' },
            ]);
    }

    // ── Walk-forward ─────────────────────────────────────────
    const _wfRaw = rob.walk_forward;
    const wf = _wfRaw && {
        pass: _wfRaw.pass,
        curve: Array.isArray(_wfRaw.curve) ? _wfRaw.curve : [],
        stats: _wfRaw.segments.map(s => ({
            n: s.n, pnl: s.pnl, pf: s.pf, win: s.win, maxDd: s.max_dd,
        })),
    };
    let wfHtml;
    if (!wf) {
        wfHtml = '<div class="institution-status">Not enough trades (need ≥6).</div>';
    } else {
        // 每段長度相同(依時間三等分),所以段月數 = 總月數 / 3
        const segMonths = spanMonths ? spanMonths / 3 : null;
        const wfPoints = wf.stats.map((s, i) => ({
            label: (i + 1) + '/3', value: _robMonthlyOf(s.pnl, segMonths),
        }));
        const wfDdPoints = wf.stats.map((s, i) => ({
            label: (i + 1) + '/3', value: s.maxDd,
        }));
        const wfPnlSeries = _robSegmentCurveSeries(
            wf.curve, 'segment_pnl', value => segMonths ? value / segMonths : NaN);
        const wfDdSeries = _robSegmentCurveSeries(wf.curve, 'segment_max_dd');
        const wfPnlValues = wfPoints
            .filter(point => point.value != null && Number.isFinite(Number(point.value)))
            .map(point => Number(point.value));
        const wfWorstPnl = wfPnlValues.length ? Math.min.apply(Math, wfPnlValues) : null;
        const wfWorstDd = Math.max.apply(Math, wfDdPoints.map(point => Number(point.value)));
        wfHtml = '<div class="rob-charts">'
            + _robLineChart('PNL / MO', wfPnlSeries, {
                format: _robUsd,
                palette: ['var(--cyan)', 'var(--green)', 'var(--amber)'],
                alertHtml: _robPnlAlert(wfWorstPnl, 'Walk-forward'),
            })
            + _robLineChart('MAX DD', wfDdSeries, {
                format: _robUsd,
                tone: 'chart-risk',
                palette: ['var(--cyan)', 'var(--green)', 'var(--amber)'],
                thresholds: [1000, 2000],
                alertHtml: _robMaxDdAlert(wfWorstDd, 'Walk-forward'),
            })
            + '</div>'
            + _robStatLines(wf.stats.map((s, i) => {
                const mo = _robMonthlyOf(s.pnl, segMonths);
                return {
                    label: (i + 1) + '/3',
                    value: 'n=' + s.n + ' · PnL ' + _robUsd(s.pnl)
                        + ' · PF ' + _researchNum(s.pf, 2)
                        + ' · Win ' + _robPct(s.win)
                        + ' · maxDD ' + _robUsd(s.maxDd)
                        + (mo == null ? '' : ' · ' + _robUsd(mo) + '/mo'),
                    cls: s.pnl >= 0 && s.pf > 1 ? 'institution-pos' : 'institution-neg',
                };
            }));
    }

    // ── Slippage injection ───────────────────────────────────
    // Rows come from the backend (rob.slip.levels), which charges level × tick
    // × size per trade — same arithmetic, one implementation.
    const slipByLevel = new Map(
        (rob.slip && rob.slip.levels ? rob.slip.levels : []).map(
            row => [row.level, {
                pnl: row.stats.pnl, pf: row.stats.pf,
                maxDd: row.stats.max_dd, win: row.stats.win, n: row.stats.n,
            }]));
    const slipPoints = [{ label: 'original', level: 0, value: spanMonths ? base.pnl / spanMonths : base.pnl, pnl: base.pnl, stats: base }]
        .concat(slipLevels.filter(lvl => slipByLevel.has(lvl)).map(lvl => {
            const stats = slipByLevel.get(lvl);
            return {
                label: lvl + 't' + (lvl === used ? ' ★' : ''), level: lvl,
                value: spanMonths ? stats.pnl / spanMonths : stats.pnl,
                pnl: stats.pnl, stats: stats, used: lvl === used,
            };
        }));
    const slipTableRows = slipPoints.map(point => {
        const stats = point.stats;
        const delta = point.level === 0 ? '—'
            : _researchNum((stats.pf / Math.max(base.pf, 1e-9) - 1) * 100, 1) + '%';
        const rowClass = point.used ? ' class="rob-slip-used"' : '';
        return '<tr' + rowClass + '>'
            + '<td>' + _attr(point.label) + '</td>'
            + '<td class="' + (stats.pnl >= 0 ? 'institution-pos' : 'institution-neg') + '">'
            + _robUsd(stats.pnl) + '</td>'
            + '<td>' + _robUsd(point.value) + '</td>'
            + '<td class="' + (stats.pf >= 1.5 ? 'institution-pos' : 'institution-neg') + '">'
            + _researchNum(stats.pf, 2) + '</td>'
            + '<td>' + delta + '</td>'
            + '<td>' + _robUsd(stats.maxDd) + '</td>'
            + '<td>' + _robPct(stats.win) + '</td>'
            + '<td>' + stats.n + '</td>'
            + '</tr>';
    }).join('');
    const slipHtml = '<div class="rob-table-scroll"><table class="institution-table rob-slip-table">'
        + '<thead><tr><th>SLIP</th><th>PNL</th><th>PNL/MO</th><th>PF</th><th>ΔPF</th>'
        + '<th>MAXDD</th><th>WIN%</th><th>N</th></tr></thead><tbody>'
        + slipTableRows + '</tbody></table></div>';

    const usedStats = slipByLevel.get(used) || base;
    const rawSymbol = String((trades[0] || {}).symbol || '').toUpperCase();
    const contract = rawSymbol ? (rawSymbol.charAt(0) === '/' ? rawSymbol : '/' + rawSymbol) : '—';
    const dateText = d0
        ? d0.toISOString().slice(0, 10) + ' → ' + d1.toISOString().slice(0, 10)
        : '—';
    const dateSub = spanMonths ? _researchNum(spanMonths, 1) + ' months' : '';
    const observedDdClass = base.maxDd > 2000 ? 'tpx-danger'
        : (base.maxDd > 1000 ? 'tpx-warn' : '');
    const summaryCard = (label, value, cls, sub) =>
        '<div class="rob-summary-item"><span class="rob-summary-label">' + label
        + '</span><strong class="rob-summary-value ' + (cls || '') + '">' + value
        + '</strong>' + (sub ? '<small class="rob-summary-sub">' + sub + '</small>' : '')
        + '</div>';
    const summaryHtml = '<div class="rob-summary-grid">'
        + summaryCard('TRADES', String(base.n), '', '')
        + summaryCard('CONTRACT', contract, '', '')
        + summaryCard('DATE', dateText, 'rob-summary-date', dateSub)
        + summaryCard('PF', _researchNum(base.pf, 2), base.pf > 1 ? 'institution-pos' : 'institution-neg', '')
        + summaryCard('PNL/MO', monthly == null ? '—' : _robUsd(monthly), _researchClass(monthly), 'total ' + _robUsd(base.pnl))
        + summaryCard('MAXDD' + _robMaxDdAlert(base.maxDd, 'Observed maxDD'), _robUsd(base.maxDd), observedDdClass, '')
        + '</div>';
    content.innerHTML = summaryHtml
        + '<div class="institution-grid">'
        + '<div class="institution-card rob-mc-card"><h3>MONTE CARLO'
        + (mc ? ' ' + _robBadge(mc.pass, 'PASS', 'FAIL') : '') + '</h3>' + mcHtml + '</div>'
        + '<div class="institution-card rob-wf-card"><h3>WALK-FORWARD'
        + (wf ? ' ' + _robBadge(wf.pass, 'PASS', 'FAIL') : '') + '</h3>' + wfHtml + '</div>'
        + '<div class="institution-card institution-wide rob-slip-card"><h3>SLIPPAGE'
        + ' ' + _robBadge(usedStats.pf >= 1.5, 'PF OK AFTER SLIP', 'PF DEGRADES BELOW 1.5') + '</h3>'
        + slipHtml + '</div>'
        + topstepHtml
        + '</div>';
    content.querySelectorAll('.help-dot').forEach(_configureHelpDot);
    if (typeof glassResample === 'function') glassResample('#institution-panel');
}

function calShiftMonth(delta) {
    _calMonth = new Date(_calMonth.getFullYear(), _calMonth.getMonth() + delta, 1);
    renderCalendar();
    glassResample('#calendar-view');   // 1.0.10 #1:日曆重畫後才取樣
}
function calGoToday() {
    const key = topstepTradeDateKey(new Date());
    const parts = key ? key.split('-').map(Number) : [];
    if (parts.length === 3) {
        _calMonth = new Date(parts[0], parts[1] - 1, 1);
    } else {
        const d = new Date();
        _calMonth = new Date(d.getFullYear(), d.getMonth(), 1);
    }
    renderCalendar();
    glassResample('#calendar-view');   // 1.0.10 #1:日曆重畫後才取樣
}
async function renderCalendar(force) {
    await _calFetchLive(force);
    const bt = _calAgg(backtestData ? backtestData.trades : []);
    const live = _calAgg(_calLiveTrades);
    const month = _calMonth, y = month.getFullYear(), m = month.getMonth();

    const lbl = document.getElementById('cal-month-label');
    if (lbl) lbl.textContent = month.toLocaleString('en-US', { month: 'long', year: 'numeric' });

    const first = new Date(y, m, 1);
    const startIdx = first.getDay();                    // Sunday-first column (0=Sun..6=Sat)
    const daysInMonth = new Date(y, m + 1, 0).getDate();
    const todayKey = topstepTradeDateKey(new Date()) || _calDateKey(new Date());

    const grid = document.getElementById('cal-grid');
    if (!grid) return;
    grid.innerHTML = '';

    // Monthly totals over ALL days (independent of cell layout).
    let btTotal = 0, liveTotal = 0;
    for (let day = 1; day <= daysInMonth; day++) {
        const key = _calDateKey(new Date(y, m, day));
        if (bt[key]) btTotal += bt[key].pnl;
        if (live[key]) liveTotal += live[key].pnl;
    }

    const totalCells = startIdx + daysInMonth;
    const rows = Math.ceil(totalCells / 7);
    let weekNo = 0;

    for (let row = 0; row < rows; row++) {
        weekNo++;
        // Pre-sum this week's in-month days (all 7 cols, incl. Saturday).
        let wkBt = 0, wkBtN = 0, wkLv = 0, wkLvN = 0, wkHasBt = false, wkHasLv = false;
        for (let col = 0; col < 7; col++) {
            const dn = row * 7 + col - startIdx + 1;
            if (dn < 1 || dn > daysInMonth) continue;
            const key = _calDateKey(new Date(y, m, dn));
            const b = bt[key], l = live[key];
            if (b) { wkBt += b.pnl; wkBtN += b.n; wkHasBt = true; }
            if (l) { wkLv += l.pnl; wkLvN += l.n; wkHasLv = true; }
        }
        for (let col = 0; col < 7; col++) {
            const dn = row * 7 + col - startIdx + 1;
            const inMonth = dn >= 1 && dn <= daysInMonth;

            // Saturday column (index 6) = weekly summary cell.
            if (col === 6) {
                const cell = document.createElement('div');
                cell.className = 'cal-cell cal-week';
                if (wkHasBt) cell.classList.add(wkBt >= 0 ? 'cal-bt-pos' : 'cal-bt-neg');
                const btCls = wkHasBt ? (wkBt > 0 ? 'cal-pos' : (wkBt < 0 ? 'cal-neg' : 'cal-zero')) : 'cal-zero';
                const lvCls = wkHasLv ? (wkLv > 0 ? 'cal-pos' : (wkLv < 0 ? 'cal-neg' : 'cal-zero')) : 'cal-zero';
                cell.innerHTML =
                    '<div class="cal-week-label">Week ' + weekNo + '</div>' +
                    '<div class="cal-bt ' + btCls + '">' + (wkHasBt ? _calFmtMoney(wkBt) : '·') + '</div>' +
                    '<div class="cal-bt-sub">' + (wkBtN ? wkBtN + ' trades' : '') + '</div>' +
                    '<div class="cal-live">' +
                      '<span class="' + lvCls + '">' + (wkLvN ? _calFmtMoney(wkLv) : '·') + '</span>' +
                    '</div>';
                grid.appendChild(cell);
                continue;
            }

            if (!inMonth) {
                const c = document.createElement('div');
                c.className = 'cal-cell cal-empty';
                grid.appendChild(c);
                continue;
            }

            const key = _calDateKey(new Date(y, m, dn));
            const b = bt[key], l = live[key];
            const cell = document.createElement('div');
            cell.className = 'cal-cell';
            if (key === todayKey) cell.classList.add('cal-today');
            if (b) cell.classList.add(b.pnl >= 0 ? 'cal-bt-pos' : 'cal-bt-neg');
            const btCls = b ? (b.pnl > 0 ? 'cal-pos' : (b.pnl < 0 ? 'cal-neg' : 'cal-zero')) : 'cal-zero';
            const lvCls = l ? (l.pnl > 0 ? 'cal-pos' : (l.pnl < 0 ? 'cal-neg' : 'cal-zero')) : 'cal-zero';
            cell.innerHTML =
                '<div class="cal-daynum">' + dn + '</div>' +
                '<div class="cal-bt ' + btCls + '">' + (b ? _calFmtMoney(b.pnl) : '·') + '</div>' +
                '<div class="cal-bt-sub">' + (b ? b.n + ' trades' : '') + '</div>' +
                '<div class="cal-live">' +
                  '<span class="' + lvCls + '">' + (l ? _calFmtMoney(l.pnl) : '·') + '</span>' +
                '</div>';
            grid.appendChild(cell);
        }
    }

    const btEl = document.getElementById('cal-bt-total');
    const lvEl = document.getElementById('cal-live-total');
    const dfEl = document.getElementById('cal-diff');
    if (btEl) { btEl.textContent = _calFmtMoney(btTotal); btEl.className = 'cal-sum-val ' + (btTotal >= 0 ? 'cal-pos' : 'cal-neg'); }
    if (lvEl) { lvEl.textContent = _calFmtMoney(liveTotal); lvEl.className = 'cal-sum-val ' + (liveTotal >= 0 ? 'cal-pos' : 'cal-neg'); }
    if (dfEl) {
        if (Math.abs(btTotal) > 0.0001) {
            const pct = ((liveTotal - btTotal) / Math.abs(btTotal)) * 100;
            dfEl.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%';
            dfEl.className = 'cal-sum-val ' + (pct >= 0 ? 'cal-pos' : 'cal-neg');
        } else {
            dfEl.textContent = '—';
            dfEl.className = 'cal-sum-val cal-zero';
        }
    }
    const status = document.getElementById('cal-status');
    if (status) {
        const btN = backtestData && backtestData.trades ? backtestData.trades.length : 0;
        const lvN = (_calLiveTrades || []).length;
        status.textContent = btN
            ? (btN + ' backtest trades · ' + lvN + ' live trades loaded')
            : 'run a backtest first to populate the BT side · ' + lvN + ' live trades loaded';
    }
    // The Research view owns the full realized PNL curve.  It replaces the
    // old month-only BT/LIVE comparison so the graph uses the same trade set
    // and exit/DD logic as the former bottom-panel curve.
    renderPnlCurve();
    // 1.0.10p: now async (it fetches /research/robustness). Fire-and-forget is
    // fine for a panel render, but an unhandled rejection would only surface in
    // the devtools console, so route failures into the app log instead.
    renderResearchRobustness().catch(
        e => log('Robustness panel failed: ' + e.message, 'error'));
    glassResample('#calendar-view');   // 1.0.10 #1
}

// ════════════════════════════════════════════════════════════════════════
// Persist the last backtest result across app restarts (localStorage).
// We keep a TRIMMED copy (metrics + trades + daily_pnl) — enough to redraw the
// performance panel and the Data calendar — without storing the heavy candle/
// chart payload.  Restored on page load so reopening the app shows the last run.
// ════════════════════════════════════════════════════════════════════════
const _BT_CACHE_KEY = 'ancserTPX.lastBacktest.v1';
function _saveBacktestCache(d) {
    if (!d || !d.metrics) return;
    try {
        localStorage.setItem(_BT_CACHE_KEY, JSON.stringify({
            market_clock_version: d.market_clock_version || MARKET_CLOCK_VERSION,
            metrics: d.metrics,
            trades: d.trades || [],
            daily_pnl: d.daily_pnl || null,
            preset_name: d.preset_name || null,
            bt_span: d.bt_span || null,
            saved_at: new Date().toISOString()
        }));
    } catch (e) { /* quota / serialization — ignore, cache is best-effort */ }
}
function _restoreBacktestCache() {
    let raw;
    try { raw = localStorage.getItem(_BT_CACHE_KEY); } catch (e) { return; }
    if (!raw) return;
    let d;
    try { d = JSON.parse(raw); } catch (e) { return; }
    if (!d || !d.metrics) return;
    if (d.market_clock_version !== MARKET_CLOCK_VERSION) {
        try { log('Cached backtest used the old fixed-UTC clock; rerun required.', 'warning'); } catch (e) {}
        return;
    }
    backtestData = d;                       // feeds the Data calendar (uses .trades)
    try { renderMetrics(d.metrics, d.trades || []); } catch (e) {}
    try { renderTrades(d.trades || []); } catch (e) {}
    try {
        setPerfSource({ preset: d.preset_name, saved_at: d.saved_at, stale: true });
        log('Restored last backtest (' + (d.metrics.total_trades || (d.trades || []).length) +
            ' trades) from cache · ' + (d.preset_name || '?') + ' · ' +
            (d.saved_at ? d.saved_at.slice(0, 16).replace('T', ' ') : ''), 'info');
    } catch (e) {}
}
// Script tag is at end of <body>, so the DOM is already parsed here.
_restoreBacktestCache();
// ════════════════════════════════════════════════════════════════════════
// Live account slot - one selected account followed by the chart/workspace.
//   GO LIVE 對真實帳號下單,由使用者手動觸發;app 絕不自動下單。
// ════════════════════════════════════════════════════════════════════════
function _acctEsc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

let _liveSlotInterval = null;
const _LIVE_SLOTS_KEY = 'ancser_live_slots.v1';

function _loadLiveSlots() {
    try { return JSON.parse(localStorage.getItem(_LIVE_SLOTS_KEY) || '{}') || {}; } catch (e) { return {}; }
}
function _saveLiveSlots() {
    const o = {};
    [LIVE_MAIN_SLOT].forEach(s => {
        o['acct' + s] = (document.getElementById('live-acct-select-' + s) || {}).value || '';
        o['preset' + s] = (document.getElementById('live-acct-preset-' + s) || {}).value || '';
    });
    try { localStorage.setItem(_LIVE_SLOTS_KEY, JSON.stringify(o)); } catch (e) {}
}
function _defaultSlotAccount(slot, accts) {
    const express = accts.find(a => a.account_type === 'express');
    const main = accts.find(a => a.is_main);
    const s1 = (main || express || accts[0] || {}).id || '';
    return s1;
}

async function initLiveSlots() {
    // presets 需在快取(供 preset 下拉);若空則抓一次
    if (!_presetsCache || !Object.keys(_presetsCache.presets || {}).length) {
        try { const pr = await fetch(API + '/presets'); if (pr.ok) { const pd = await pr.json(); if (pd && pd.presets) _presetsCache = pd; } } catch (e) {}
    }
    const accts = allAccounts || [];
    const presetNames = Object.keys((_presetsCache && _presetsCache.presets) || {}).sort(_comparePresetNames);
    const saved = _loadLiveSlots();
    [LIVE_MAIN_SLOT].forEach(slot => {
        const accSel = document.getElementById('live-acct-select-' + slot);
        const preSel = document.getElementById('live-acct-preset-' + slot);
        if (accSel) {
            const cur = accSel.value;
            accSel.innerHTML = '<option value="">-- SELECT ACCOUNT --</option>' + accts.map(a =>
                '<option value="' + a.id + '">' + _acctEsc(a.name) + ' [' + String(a.account_type || '').toUpperCase() + '] $'
                + Number(a.balance || 0).toLocaleString(undefined, { maximumFractionDigits: 0 }) + '</option>').join('');
            let def = cur || saved['acct' + slot] || _defaultSlotAccount(slot, accts);
            if (def && accts.find(a => String(a.id) === String(def))) accSel.value = String(def);
        }
        if (preSel) {
            const cur = preSel.value;
            preSel.innerHTML = '<option value="">-- SELECT PRESET --</option>' + presetNames.map(n =>
                '<option value="' + _acctEsc(n) + '">' + _acctEsc(_presetDisplayName(n)) + '</option>').join('');
            const dp = cur || saved['preset' + slot] || '';
            if (dp && presetNames.includes(dp)) preSel.value = dp;
        }
    });
    _focusMainLiveAccount();
    syncMainAccountPresetToPanels(true);
    refreshTradeHistoryForCurrentAccount(true);
    pollLiveSlots();
}

function onLiveSlotChange(slot) {
    _saveLiveSlots();
    if (Number(slot) === LIVE_MAIN_SLOT) {
        _focusMainLiveAccount();
        syncMainAccountPresetToPanels(false);
    }
    pollLiveSlots({ restart: true });
    pollLiveStatus({ restart: true });
}

// 1.0.9: GO LIVE 成功後把槽位指派寫進 data/account_roles.json —
// MAIN 槽帳號 = main_account_id,每帳號記 preset + live 旗標。
// terminal 模式(backend.terminal_live)靠這個檔案自動跟隨 main 帳號與其 preset。
async function _persistLiveRolesFromSlots() {
    try {
        const r = await fetch(API + '/accounts/roles');
        const cur = r.ok ? (((await r.json()) || {}).roles || {}) : {};
        const accounts = Object.assign({}, cur.accounts || {});
        [LIVE_MAIN_SLOT].forEach(s => {
            const aid = (document.getElementById('live-acct-select-' + s) || {}).value || '';
            const pre = (document.getElementById('live-acct-preset-' + s) || {}).value || '';
            if (aid) accounts[String(aid)] = { preset: pre || null, live: true };
        });
        const mainId = (document.getElementById('live-acct-select-' + LIVE_MAIN_SLOT) || {}).value
            || cur.main_account_id || '';
        await fetch(API + '/accounts/roles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                email: cur.email || '',
                main_account_id: String(mainId),
                accounts: accounts,
            }),
        });
        log('Account assignments saved (account_roles.json); terminal mode will follow them', 'info');
    } catch (e) { /* 設定持久化失敗不影響交易 */ }
}

async function liveSlotGoLive(slot) {
    const accId = parseInt((document.getElementById('live-acct-select-' + slot) || {}).value);
    const presetName = (document.getElementById('live-acct-preset-' + slot) || {}).value;
    const slotName = liveSlotLabel(slot);
    if (!accId) { log(slotName + ': select account first', 'warn'); return; }
    if (!presetName || !(_presetsCache.presets || {})[presetName]) { log(slotName + ': select preset first', 'warn'); return; }
    slot = 'MAIN';
    const acc = (allAccounts || []).find(a => a.id === accId);
    const warn = (acc && acc.account_type === 'express') ? '\n⚠ EXPRESS FUNDED ACCOUNT: REAL ORDERS WILL BE PLACED!' : '';
    if (!confirm('GO LIVE (ACCOUNT ' + slot + ')\nAccount: ' + (acc ? acc.name : accId) + '\nPreset: ' + presetName + warn)) return;
    const preset = _presetsCache.presets[presetName];
    const body = Object.assign({}, preset, { account_id: accId });
    body.strategy = normalizeStrategyName(body.strategy);
    if (!body.contract_id) {
        body.contract_id = document.getElementById('contract-id')?.value || defaultContractId();
    }
    try {
        const resp = await fetch(API + '/live/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const data = await resp.json();
        if (!resp.ok) { log('ACCOUNT ' + slot + ' start failed: ' + _acctEsc(data.detail || JSON.stringify(data)), 'warn'); }
        else {
            log('ACCOUNT ' + slot + ' GO LIVE acct ' + accId + ' preset=' + presetName + ' ✓', 'success');
            _startLiveChartForAccount(acc, preset);   // 帶動圖表 + 頂欄(跟隨此帳號)
            _persistLiveRolesFromSlots();             // 1.0.9: 指派寫進 account_roles.json(terminal 跟隨)
        }
    } catch (e) { log('ACCOUNT ' + slot + ' start connection failed: ' + e.message, 'warn'); }
    _saveLiveSlots();
    setTimeout(() => pollLiveSlots({ restart: true }), 400);
}

async function liveSlotStop(slot) {
    const accId = parseInt((document.getElementById('live-acct-select-' + slot) || {}).value);
    slot = 'MAIN';
    if (!accId) return;
    try { const r = await fetch(API + '/live/stop?account_id=' + accId, { method: 'POST' }); const d = await r.json(); log('ACCOUNT ' + slot + ' STOP:' + _acctEsc(d.message || ''), 'info'); }
    catch (e) { log('ACCOUNT ' + slot + ' STOP failed: ' + e.message, 'warn'); }
    setTimeout(() => pollLiveSlots({ restart: true }), 300);
}

async function liveSlotFlatten(slot) {
    const accId = parseInt((document.getElementById('live-acct-select-' + slot) || {}).value);
    slot = 'MAIN';
    if (!accId) return;
    if (!confirm('Emergency flatten ACCOUNT ' + slot + ' (' + accId + ')?')) return;
    try { const r = await fetch(API + '/live/flatten?account_id=' + accId, { method: 'POST' }); const d = await r.json(); log('ACCOUNT ' + slot + ' FLATTEN:' + _acctEsc(d.message || ''), 'warn'); }
    catch (e) { log('ACCOUNT ' + slot + ' FLATTEN failed: ' + e.message, 'warn'); }
    setTimeout(() => pollLiveSlots({ restart: true }), 300);
}

// 帶動圖表/頂欄跟隨指定帳號(沿用既有 live chart machinery)
function _startLiveChartForAccount(acc, stratParams) {
    const mainAcc = _focusMainLiveAccount() || acc;
    if (mainAcc) liveAccount = mainAcc;
    stratParams = getMainLivePresetParams(stratParams);
    syncMainAccountPresetToPanels(true);
    const topBar = document.getElementById('live-top-bar');
    if (topBar) topBar.style.display = 'block';
    try { updateLiveTopBar(); } catch (e) {}
    _lastLiveCandleTime = '';
    if (_liveInterval) clearInterval(_liveInterval);
    _liveInterval = setInterval(pollLiveCandle, 1000); pollLiveCandle();
    if (_liveStatusInterval) clearInterval(_liveStatusInterval);
    _liveStatusInterval = setInterval(pollLiveStatus, 1000); pollLiveStatus({ restart: true });
}

function _liveSlotRenderStatus(slot, statusMap, sess, pollStale) {
    const accId = String((document.getElementById('live-acct-select-' + slot) || {}).value || '');
    const st = statusMap[accId];
    const dot = document.getElementById('live-slot-dot-' + slot);
    const set = (base, txt, color) => { const el = document.getElementById(base + '-' + slot); if (el) { el.textContent = txt; if (color) el.style.color = color; } };
    set('live-slot-mkt', sess.label, sess.color);
    if (!accId) {
        set('live-slot-status', '—', 'var(--text3)');
        if (dot) { dot.style.background = 'var(--text3)'; dot.style.boxShadow = 'none'; }
        ['live-slot-phase', 'live-slot-mode', 'live-slot-mbo', 'live-slot-dl', 'live-slot-rv', 'live-slot-pnl'].forEach(b => set(b, '--', 'var(--text3)'));
        return;
    }
    if (st && st.running) {
        const starting = st.health === 'starting' || st.starting === true;
        const deltaMode = st.strategy_mode === 'delta_absorption';
        const mbo = st.databento_mbo || null;
        const mboDisconnected = deltaMode && (!mbo || !mbo.connected);
        const degraded = !starting && (st.health === 'degraded'
            || st.disconnected
            || st.task_alive === false
            || (st.strategy_mode === 'pi' && st.pi_listener_alive === false)
            || mboDisconnected);
        const uncertain = !!pollStale || starting || degraded;
        set('live-slot-status', pollStale
            ? 'RUNNING · STATUS STALE'
            : (starting ? 'STARTING' : (degraded ? 'RUNNING · DEGRADED' : 'RUNNING')),
            uncertain ? 'var(--amber)' : 'var(--green)');
        if (dot) {
            dot.style.background = uncertain ? 'var(--amber)' : 'var(--green)';
            dot.style.boxShadow = uncertain
                ? '0 0 6px var(--amber)'
                : '0 0 6px var(--green)';
        }
        set('live-slot-phase', st.phase || '--', 'var(--text2)');
        const activeModeName = st.active_mode ? strategyDisplayName(st.active_mode) : '';
        const strategyModeName = st.strategy_mode ? strategyDisplayName(st.strategy_mode) : 'FACTOR';
        const mode = st.confluence_shadow ? 'SHADOW (NO ORDERS)'
            : ((activeModeName && activeModeName !== strategyModeName) ? activeModeName : 'LIVE');
        set('live-slot-mode', mode, 'var(--green)');
        if (deltaMode) {
            const mboState = String((mbo && mbo.state) || '').toLowerCase();
            const mboText = mbo && mbo.connected
                ? (mbo.ready ? 'CONNECTED · READY' : 'CONNECTED · WARMING')
                : (mboState === 'missing_key' ? 'MISSING KEY'
                    : (mboState === 'error' ? 'ERROR'
                        : (mboState === 'starting' ? 'STARTING' : 'DISCONNECTED')));
            const mboColor = mbo && mbo.connected
                ? (mbo.ready ? 'var(--green)' : 'var(--amber)')
                : 'var(--red)';
            set('live-slot-mbo', mboText, mboColor);
        } else {
            set('live-slot-mbo', 'N/A', 'var(--text3)');
        }
        const pnl = st.daily_pnl || 0;
        set('live-slot-pnl', (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(0), pnl >= 0 ? 'var(--green)' : 'var(--red)');
        const g = st.risk_gates || {}, dl = g.daily_loss || {}, rv = g.prev_rv || {};
        set('live-slot-dl', dl.limit ? (dl.resting ? ('LOCKED ' + (dl.count || 0) + '/' + dl.limit) : ((dl.count || 0) + '/' + dl.limit)) : 'OFF', dl.resting ? 'var(--red)' : 'var(--text2)');
        set('live-slot-rv', rv.lookback ? (rv.blocking ? 'BLOCKED' : rv.lookback + 'D PASS') : 'OFF', rv.blocking ? 'var(--red)' : 'var(--text2)');
    } else if (pollStale) {
        set('live-slot-status', 'STATUS STALE', 'var(--amber)');
        if (dot) { dot.style.background = 'var(--amber)'; dot.style.boxShadow = '0 0 6px var(--amber)'; }
    } else {
        set('live-slot-status', st ? 'STOPPED' : 'NOT STARTED', 'var(--text3)');
        if (dot) { dot.style.background = 'var(--text3)'; dot.style.boxShadow = 'none'; }
        ['live-slot-phase', 'live-slot-mode', 'live-slot-mbo', 'live-slot-dl', 'live-slot-rv', 'live-slot-pnl'].forEach(b => set(b, '--', 'var(--text3)'));
    }
}

function pollLiveSlots(options) {
    const lp = document.getElementById('live-panel');
    if (!lp || lp.classList.contains('hidden')) return;   // 只在 Live 分頁輪詢
    const sess = getMarketSession();
    return _runBoundedLivePoll(
        _liveSlotsPollState,
        API + '/live/status-all',
        options,
        (data) => {
            const statusMap = {};
            (data.engines || []).forEach(e => { statusMap[String(e.account_id)] = e.status || {}; });
            _liveSlotsPollState.lastGood = statusMap;
            [LIVE_MAIN_SLOT].forEach(slot => _liveSlotRenderStatus(slot, statusMap, sess, false));
        },
        () => {
            // Never turn a temporary request failure into NOT STARTED.  Keep the
            // last truthful engine state and make its uncertainty explicit.
            const statusMap = _liveSlotsPollState.lastGood || {};
            [LIVE_MAIN_SLOT].forEach(slot => _liveSlotRenderStatus(slot, statusMap, sess, true));
        },
    );
}


// ════════════════════════════════════════════════════════════════════════
// ════════════════════════════════════════════════════════════════════════
// 1.0.9: EMAPMO 進場門檻滑桿
// PMO 由「百分比」ROC 疊三層 EMA 得到,門檻卻是寫死的絕對值(-0.100),
// 所以它的鬆緊度跟商品的%波動綁死 —— 換商品或換波動環境就得重調。
// 滑桿送出的是縮放係數:0.90 → 門檻 -0.090(較鬆,訊號較多)。
// 只作用於 early(比 SIG 線);normal(比 PMO)另有參數,UI 暫不開放。
// ════════════════════════════════════════════════════════════════════════

// _factorFamily 是 buildParams 內的區域 helper,這裡直接讀 select 的值。
function _emapmoFamily(mode) {
    const el = document.getElementById('factor-family-' + mode);
    return String((el && el.value) || 'emapmo').toLowerCase();
}

function _emapmoThresholdScale(mode) {
    const el = document.getElementById('emapmo-th-' + mode);
    if (!el || _emapmoFamily(mode) !== 'emapmo') return 1.0;
    const v = parseFloat(el.value);
    return Number.isFinite(v) && v > 0 ? Number(v.toFixed(2)) : 1.0;
}

function onEmapmoThresholdChange(mode) {
    const el = document.getElementById('emapmo-th-' + mode);
    const out = document.getElementById('emapmo-th-' + mode + '-val');
    if (!el || !out) return;
    const scale = parseFloat(el.value) || 1.0;
    out.textContent = (-0.10 * scale).toFixed(3);
    out.style.color = Math.abs(scale - 1.0) < 0.005 ? 'var(--amber)' : 'var(--cyan)';
}

function _setEmapmoThreshold(mode, scale) {
    const el = document.getElementById('emapmo-th-' + mode);
    if (!el) return;
    const v = Number(scale);
    // preset 沒帶這個欄位(或帶 0)時視為原始門檻
    el.value = (Number.isFinite(v) && v > 0 ? v : 1.0).toFixed(2);
    onEmapmoThresholdChange(mode);
}

function syncEmapmoThresholdRow(mode) {
    // 注意:show() 是 buildParams 內的區域 helper,全域函式取不到,直接設 display。
    const row = document.getElementById('emapmo-th-row-' + mode);
    if (!row) return;
    const isFactor = String(_mlSelectValue('strategy-' + mode, 'factor')) === 'factor';
    row.style.display = (isFactor && _emapmoFamily(mode) === 'emapmo') ? '' : 'none';
    onEmapmoThresholdChange(mode);
}


// 1.0.9: 風險/獲利上限的即時 $ 換算。上限是「每口 ticks」,但使用者關心的是
// 金額,而 MNQ(1t=$0.50)與 MES(1t=$1.25)差 2.5 倍 —— 不換算很容易設錯。
// 注意:_paramVal / iv / _set 都是別的函式內的區域 helper,全域函式取不到,
// 所以這裡一律直接讀 DOM。
function updateRiskCapHint(mode) {
    const hint = document.getElementById('risk-cap-hint-' + mode);
    if (!hint) return;
    const num = (id) => {
        const el = document.getElementById(id + '-' + mode);
        return el ? (parseInt(el.value, 10) || 0) : 0;
    };
    const risk = num('max-risk-ticks');
    const prof = num('max-profit-ticks');
    if (!risk && !prof) { hint.textContent = '(both OFF — no cap)'; return; }
    const cEl = document.getElementById('contract-' + mode);
    const cid = String((cEl && cEl.value) || '');
    // CON.F.US.<SYM>.<expiry> — ENQ = 迷你 NQ($20/pt),MNQ 微型($2),MES 微型 ES($5)
    const sym = (cid.split('.')[3] || cid || 'MNQ').toUpperCase();
    const spec = SYSTEM_CONTRACT_SPECS[sym] || SYSTEM_CONTRACT_SPECS.MNQ || {};
    const pv = Number(spec.point_value || 2);
    const tickSize = Number(spec.tick_size || 0.25);
    const sEl = document.getElementById('size-' + mode);
    const size = (sEl ? parseInt(sEl.value, 10) : 1) || 1;
    const tv = tickSize * pv * size;
    const parts = [];
    // 1.0.9: 兩個上限各自獨立夾,不再等比縮放 —— 壓 TP 不會動到 SL
    if (risk) parts.push('Risk ≤ $' + Math.round(risk * tv));
    if (prof) parts.push('Profit ≤ $' + Math.round(prof * tv));
    hint.textContent = '(' + parts.join(' · ') + ' @ ' + size + ' contracts · SL/TP independent)';
}


// The PERFORMANCE source badge was retired from the UI. Keep this small
// compatibility hook because older restore/run paths still call it; the
// backtest cache itself remains available and is not changed by this UI-only
// removal.
function setPerfSource(_info) {}
