// ============================================
// ancserTPX Frontend
// ============================================

// Auto-detect port from current page URL (supports dynamic launcher ports)
const API = window.location.origin + '/api';
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
let chart = null;
let candleSeries = null;
let volumeSeries = null;   // kept for live-update guard; no longer rendered
let _rawCandleBuffer = []; // [{time(unix), open, high, low, close, volume}]
let zoneRectangles = [];
let tradeMarkers = [];
let backtestData = null;
let currentAccount = null;
let allAccounts = [];
let pocLine = null;
let vahLine = null;
let valLine = null;

// -- Strategy Params & Presets ----------------------

const DEFAULT_STRATEGY_PARAMS = {
    strategy: 'confluence',
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
    contract_id: 'CON.F.US.MNQ.M26',  // 3× Micro NQ default
    contract_size: 3,
    value_area_pct: 0.80,
    area_timeframe: '5m',
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
    mlc2_lookback: 30,
    mlc2_band_ticks: 2.0,
    mlc2_sl_buffer_ticks: 4.0,
    mlc2_tp_mode: 'rr',
    mlc2_rr: 4.0,
    mlc2_trail_trigger_pct: 0.0,
    mlc2_trail_lock_pct: 0.0,
    mlc2_session_limit: false,
    mlc2_min_score: 0.0,
    mlc2_allowed_sessions: ['ASIA', 'EURO'],
    mlc2_shadow: false,
};

const _appliedStrategyParamsByMode = {
    bt: Object.assign({}, DEFAULT_STRATEGY_PARAMS),
    live: Object.assign({}, DEFAULT_STRATEGY_PARAMS),
};

const MNQ_SIZE_CHOICES = [1, 3, 5, 10];
const TRAIL_TICK_STEP = 5;
const TRAIL_SL_PCT_CHOICES = [0.05, 0.10, 0.20, 0.30, 0.40, 0.50];
const TRAIL_TRIGGER_PCT_CHOICES = [0, 0.30, 0.50, 0.70];

function contractSymbolFromId(contractId) {
    const cid = (contractId || '').toUpperCase();
    return cid.indexOf('.MNQ.') >= 0 || cid === 'MNQ' ? 'MNQ' : 'NQ';
}

function contractLabelFromId(contractId) {
    return contractSymbolFromId(contractId) === 'MNQ' ? 'MNQ' : 'NQ';
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
    return contractSymbolFromId(contractId) === 'MNQ' ? 2 : 20;
}

function tickDollarValue(contractId, size) {
    return pointValueForContract(contractId) * 0.25 * normalizeContractSize(contractId, size);
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
    return allowed.includes(n) ? n : 3;
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
    const rr = rrEl ? (parseInt(rrEl.value, 10) || 2) : 2;
    const slTicks = slEl ? (Math.abs(parseInt(slEl.value, 10)) || 50) : 50;
    if (tpEl) tpEl.value = String(rr * slTicks);
    if (lbl) lbl.textContent = '1:' + rr;
    updateTrailBounds(mode);
}

// SINGLE vs OVERLAP method. Overlap reveals the timeframe multi-select and uses
// the merged synthetic zone (avg VAH/VAL/POC) only when all selected TFs' value
// areas overlap. Single uses one AREA TF zone.
// Timeframe checkbox changed → re-detect zones at the new area TF and redraw.
function onTfSelectionChange(mode) {
    updateOverlapTradeTfControl(mode);
    syncZoneFilterUI();
    onAreaConfigChange(mode);
    refreshTfZones(true);
}

function readOverlapTfCombo(mode) {
    const order = ['5m', '15m', '30m', '1h', '4h'];
    const checked = Array.from(document.querySelectorAll('.overlap-tf-chk-' + mode))
        .filter(c => c.checked).map(c => c.value);
    return order.filter(tf => checked.includes(tf));
}

function setOverlapTfCombo(mode, combo) {
    const set = new Set(Array.isArray(combo) ? combo : []);
    document.querySelectorAll('.overlap-tf-chk-' + mode).forEach(c => {
        c.checked = set.has(c.value);
    });
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
        : [String(p.area_timeframe || combo[0] || '5m')];
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
        area_timeframe: tfs[0] || '5m',
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
    if (v === 'confluence') return 'confluence';
    if (v === 'ml_consolidation_v2' || v === 'ml_consol_v2' || v === 'mlc2') return 'ml_consolidation_v2';
    return 'trend';
}

function strategyIdPrefix(kind) {
    return '';
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
    const isMLC2 = strategy === 'ml_consolidation_v2';
    const show = (id, on) => {
        const el = document.getElementById(id);
        if (el) el.style.display = on ? '' : 'none';
    };
    // trend-only controls — hidden in ML / MLC2 mode.
    show('tr-params-' + mode, !isML && !isMLC2);
    show('overlap-tf-row-' + mode, !isML && !isMLC2);
    // ML Confluence params — shown only in confluence mode
    show('ml-params-' + mode, isML);
    if (isML) onRrModeChange(mode);
    if (!isML && !isMLC2) {
        updateOverlapTradeTfControl(mode);
        updateTrailBounds(mode);
    }
}

// RR mode toggle: "固定" shows the single-RR select; "變動" shows the RR-grid
// (range) select and hides the fixed one. Only one is ever visible.
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

function onStrategyChange(mode) {
    updateStrategyParamVisibility(mode);
}

// Re-detect zones at the selected area timeframe + value-area % and redraw VAH/VAL/POC.
async function onAreaConfigChange(mode) {
    const sp = collectStrategyParams(mode);
    // Value-area width changed → refresh the all-timeframe zone cache for the filter.
    refreshTfZones(true);
    try {
        const resp = await fetch(API + '/data/detect-zones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                value_area_pct: sp.value_area_pct,
                area_timeframe: sp.area_timeframe,
            }),
        });
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.zones && data.zones.length > 0) {
            _cachedVPZones = data.zones;
            log('Area ' + sp.area_timeframe + ' / ' + Math.round(sp.value_area_pct * 100) + '%: ' + data.zones.length + ' zones', 'info');
        }
    } catch (e) {
        log('Area re-detect failed: ' + e.message, 'warn');
    }
}

// ML: overlay every timeframe's VAH/VAL/POC on the chart at once.
let _allTfZonesActive = false;

async function toggleAllTimeframeZones() {
    const btn = document.getElementById('btn-draw-all-tf');
    // Toggle OFF: revert to the single-timeframe view for the bt panel.
    if (_allTfZonesActive) {
        _allTfZonesActive = false;
        if (btn) {
            btn.classList.remove('btn-green');
            btn.textContent = 'SHOW ALL TF ZONES';
        }
        await onAreaConfigChange('bt');
        return;
    }
    const sp = collectStrategyParams('bt');
    try {
        if (btn) btn.textContent = 'LOADING...';
        const resp = await fetch(API + '/data/detect-zones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                value_area_pct: sp.value_area_pct,
                all_timeframes: true,
            }),
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        if (data.zones && data.zones.length > 0) {
            _cachedVPZones = data.zones;
            drawVolumeProfile(data.zones);
            drawBacktestZones(data.zones);
            _allTfZonesActive = true;
            if (btn) {
                btn.classList.add('btn-green');
                btn.textContent = 'HIDE ALL TF ZONES';
            }
            const tfs = (data.timeframes || []).join('/');
            log('All-timeframe zones (' + tfs + '): ' + data.zones.length + ' zones', 'info');
        } else {
            log('No zones detected across timeframes', 'warn');
            if (btn) btn.textContent = 'SHOW ALL TF ZONES';
        }
    } catch (e) {
        log('All-TF zone draw failed: ' + e.message, 'error');
        if (btn) btn.textContent = 'SHOW ALL TF ZONES';
    }
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
            // BREAKOUT control removed from the UI (sweep showed it's redundant at
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
    const cidEl = document.getElementById('contract-' + mode);
    const sizeEl = document.getElementById('size-' + mode);
    const contractId = (cidEl && cidEl.value) || DEFAULT_STRATEGY_PARAMS.contract_id;
    const strategy = normalizeStrategyName(_val('strategy-' + mode));
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
    const tfs = selTfs.length ? selTfs : ['5m'];
    const method = tfs.length >= 2 ? 'overlap' : 'single';
    const tfCombo = method === 'overlap' ? tfs : [];
    const areaTimeframe = tfs[0];
    const overlapTradeEl = document.getElementById('tr-overlap-trade-tf-' + mode);
    const overlapTradeTf = normalizeTrendOverlapTradeTf(
        (overlapTradeEl && overlapTradeEl.value) || applied.tr_overlap_trade_tf
    );
    const params = {
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
        rr_ratio: Math.max(1, Math.min(6, _int('rr-ratio-' + mode, 2))),
        full_tp_lock: primary.full_tp_lock,
        one_trade_per_session_direction: true,
        tr_one_trade_per_session: _int('tr-session-limit-' + mode, 1) === 1,
        tr_allowed_sessions: normalizeAllowedSessions(
            _mlSelectValue('tr-allowed-sessions-' + mode, 'ASIA')
        ),
        skip_zone_stability: false,
        breakout_confirm_bars: Math.max(1, Math.min(10, _int('confirm-bars-' + mode, 7))),
    };
    if (strategy === 'ml_consolidation_v2') {
        Object.assign(params, {
            mlc2_lookback: parseInt(applied.mlc2_lookback != null ? applied.mlc2_lookback : 30, 10) || 30,
            mlc2_band_ticks: Number(applied.mlc2_band_ticks != null ? applied.mlc2_band_ticks : 2.0) || 2.0,
            mlc2_sl_buffer_ticks: Number(applied.mlc2_sl_buffer_ticks != null ? applied.mlc2_sl_buffer_ticks : 4.0) || 4.0,
            mlc2_tp_mode: String(applied.mlc2_tp_mode || 'rr'),
            mlc2_rr: Number(applied.mlc2_rr != null ? applied.mlc2_rr : 4.0) || 4.0,
            mlc2_trail_trigger_pct: Number(applied.mlc2_trail_trigger_pct || 0),
            mlc2_trail_lock_pct: Number(applied.mlc2_trail_lock_pct || 0),
            mlc2_session_limit: !!applied.mlc2_session_limit,
            mlc2_min_score: Number(applied.mlc2_min_score || 0),
            mlc2_allowed_sessions: normalizeAllowedSessions(
                applied.mlc2_allowed_sessions != null ? applied.mlc2_allowed_sessions : ['ASIA', 'EURO']
            ),
            mlc2_shadow: !!applied.mlc2_shadow,
        });
    }
    return params;
}

function applyStrategyParams(mode, params) {
    const p = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params);
    const _set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
    const _setVal = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    const _ticks = (val, fallback) => Math.max(50, Math.min(200, parseInt(val != null ? val : fallback, 10) || fallback));
    p.strategy = normalizeStrategyName(p.strategy);
    _appliedStrategyParamsByMode[mode] = Object.assign({}, p);
    _set('strategy-' + mode, p.strategy);
    _set('area-pct-' + mode, (p.value_area_pct != null ? Number(p.value_area_pct) : 0.80).toFixed(2));
    _set('tr-overlap-trade-tf-' + mode, normalizeTrendOverlapTradeTf(p.tr_overlap_trade_tf));
    const cidEl = document.getElementById('contract-' + mode);
    if (cidEl) {
        const wanted = p.contract_id || DEFAULT_STRATEGY_PARAMS.contract_id;
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

    const rrVal = Math.max(1, Math.min(6, parseInt(p.rr_ratio != null ? p.rr_ratio : 2, 10) || 2));
    _set('rr-ratio-' + mode, String(rrVal));
    onRrChange(mode);

    _set('confirm-bars-' + mode, String(p.breakout_confirm_bars != null ? p.breakout_confirm_bars : 7));

    // ML (confluence) params — restored when the preset uses the ML strategy.
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
    _set('conf-allowed-sessions-' + mode, allowedSessionsSelectValue(
        p.conf_allowed_sessions != null ? p.conf_allowed_sessions : ['ASIA']
    ));
    _set('tr-allowed-sessions-' + mode, allowedSessionsSelectValue(
        p.tr_allowed_sessions != null ? p.tr_allowed_sessions : ['ASIA']
    ));
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
        : [String(p.area_timeframe || '5m')];
    setOverlapTfCombo(mode, selectedTfs);
    updateOverlapTradeTfControl(mode);

    updateStrategyParamVisibility(mode);
    syncZoneFilterUI();
    updateMlParamSummary(mode);
}

// CONTRACT preset dropdown in the connect panel — fills the contract-id text input.
function onContractPresetChange() {
    const sel = document.getElementById('contract-preset');
    const inp = document.getElementById('contract-id');
    if (!sel || !inp) return;
    if (sel.value) inp.value = sel.value;
    inp.focus();
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

function _namingDatePrefix(d) {
    const dt = d instanceof Date ? d : new Date();
    const mm = String(dt.getMonth() + 1).padStart(2, '0');
    const dd = String(dt.getDate()).padStart(2, '0');
    return mm + '.' + dd;
}

function _normalizeNamingAuthor(author) {
    const value = String(author || 'USER').trim().toUpperCase();
    return ['USER', 'CODEX', 'CLAUDE'].includes(value) ? value : 'USER';
}

function _sanitizePresetPurpose(value, fallback) {
    const clean = String(value || '').replace(/\s+/g, '').trim();
    return (clean || fallback || '手動保存').slice(0, 12);
}

function _nextPresetNumber(author, datePrefix) {
    const a = _normalizeNamingAuthor(author);
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
    if (normalizeStrategyName(p.strategy) === 'ml_consolidation_v2') {
        return [
            _contractPresetToken(p),
            'MLC2',
            'LB' + Number(p.mlc2_lookback != null ? p.mlc2_lookback : 30),
            'Band' + Number(p.mlc2_band_ticks != null ? p.mlc2_band_ticks : 2),
            'SLB' + Number(p.mlc2_sl_buffer_ticks != null ? p.mlc2_sl_buffer_ticks : 4),
            'RR1:' + _fmtConfRr(p.mlc2_rr != null ? p.mlc2_rr : 4),
            allowedSessionsLabel(p.mlc2_allowed_sessions != null ? p.mlc2_allowed_sessions : ['ASIA', 'EURO']),
            Number(p.mlc2_trail_trigger_pct || 0) > 0 ? 'TrailON' : 'TrailOFF',
        ].join(' ');
    }
    const vaPct = Math.round((p.value_area_pct != null ? Number(p.value_area_pct) : 0.80) * 100);
    const rr = Math.max(1, Math.min(6, parseInt(p.rr_ratio != null ? p.rr_ratio : 2, 10) || 2));
    const tfCombo = Array.isArray(p.tf_combo) ? p.tf_combo.filter(Boolean) : [];
    const method = (p.method || (tfCombo.length >= 2 ? 'overlap' : 'single')).toLowerCase();
    const tfs = (method === 'overlap' && tfCombo.length >= 2)
        ? tfCombo
        : (tfCombo.length ? [tfCombo[0]] : [p.area_timeframe || '5m']);
    const confirm = Math.max(1, Math.min(10, parseInt(p.breakout_confirm_bars != null ? p.breakout_confirm_bars : 7, 10) || 7));
    const market = allowedSessionsLabel(p.tr_allowed_sessions != null ? p.tr_allowed_sessions : ['ASIA']);
    const overlapTrade = method === 'overlap' && p.tr_overlap_trade_tf === 'smallest' ? 'TradeSmall' : '';
    return ['TR' + vaPct, tfs.join('/'), overlapTrade, 'RR1:' + rr, 'C' + confirm, market, _contractPresetToken(p)]
        .filter(Boolean).join(' ');
}

function suggestedPresetPurpose(params) {
    const p = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params || {});
    if (normalizeStrategyName(p.strategy) === 'ml_consolidation_v2') return '均值回歸';
    if (normalizeStrategyName(p.strategy) === 'confluence') {
        const risk = Number(p.conf_max_risk_ticks || 0);
        const prob = Number(p.conf_min_prob || 0);
        const rr = Number(p.conf_rr || 0);
        if (prob >= 0.6) return '回撤最低';
        if (risk <= 50) return 'PNL最高';
        if (rr >= 2.75) return '穩健測試';
        if (rr <= 1.75) return '卡瑪最佳';
        return '手動測試';
    }
    return '手動測試';
}

function buildPresetName(params, purpose, author) {
    const px = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params || {});
    const day = _namingDatePrefix();
    const a = _normalizeNamingAuthor(author || 'USER');
    const n = _nextPresetNumber(a, day);
    const use = _sanitizePresetPurpose(purpose, suggestedPresetPurpose(px));
    return day + ' ' + a + ' #' + n + ' ' + use + ' ' + buildPresetParamToken(px);

    const p = Object.assign({}, DEFAULT_STRATEGY_PARAMS, params || {});
    const vaPct = Math.round((p.value_area_pct != null ? Number(p.value_area_pct) : 0.80) * 100);
    const rr = Math.max(1, Math.min(6, parseInt(p.rr_ratio != null ? p.rr_ratio : 2, 10) || 2));
    const tfCombo = Array.isArray(p.tf_combo) ? p.tf_combo.filter(Boolean) : [];
    const method = (p.method || (tfCombo.length >= 2 ? 'overlap' : 'single')).toLowerCase();
    const tfs = (method === 'overlap' && tfCombo.length >= 2)
        ? tfCombo
        : (tfCombo.length ? [tfCombo[0]] : [p.area_timeframe || '5m']);
    const confirm = Math.max(1, Math.min(10, parseInt(p.breakout_confirm_bars != null ? p.breakout_confirm_bars : 7, 10) || 7));
    const contractLabel = contractLabelFromId(p.contract_id || DEFAULT_STRATEGY_PARAMS.contract_id);
    const contractSize = normalizeContractSize(
        p.contract_id || DEFAULT_STRATEGY_PARAMS.contract_id,
        p.contract_size != null ? p.contract_size : DEFAULT_STRATEGY_PARAMS.contract_size
    );
    if (normalizeStrategyName(p.strategy) === 'confluence') {
        const rrLabel = 'RR1:' + _fmtConfRr(p.conf_rr != null ? p.conf_rr : 1.0);
        const prob = _fmtMlProb(p.conf_min_prob != null ? p.conf_min_prob : 0.65).replace('.', '');
        const risk = p.conf_max_risk_ticks != null && Number(p.conf_max_risk_ticks) > 0
            ? ('R' + Number(p.conf_max_risk_ticks))
            : 'ROFF';
        const trail = Number(p.conf_trail_trigger_pct || 0) > 0 ? 'Trail50L5' : 'TrailOFF';
        const session = p.conf_session_limit === false ? 'SesOFF' : 'SesON';
        const market = allowedSessionsLabel(p.conf_allowed_sessions != null ? p.conf_allowed_sessions : ['ASIA']);
        const modelName = String(p.conf_model_name || _activeModelName || '').replace(/^20260618_codex_rr3-band4-mintf2-production-/, '');
        const modelLabel = modelName ? (' ' + modelName) : '';
        return 'ML' + modelLabel + ' ' + rrLabel +
            ' P' + prob +
            ' ' + risk +
            ' ' + trail +
            ' ' + session +
            ' ' + market +
            ' B' + Number(p.conf_band_ticks != null ? p.conf_band_ticks : 4) +
            ' TF' + Number(p.conf_min_distinct_tf != null ? p.conf_min_distinct_tf : 2) +
            ' W1m ' + contractLabel + '@' + contractSize;
    }
    // Naming: TR{VA%} {tf/tf/...} RR1:{rr} C{confirm} {contract}@{size}
    //   → e.g. "TR80 5m/1h/4h RR1:3 C7 MNQ@3"
    return 'TR' + vaPct +
        ' ' + tfs.join('/') +
        ' RR1:' + rr +
        ' C' + confirm +
        ' ' + contractLabel + '@' + contractSize;
}

async function fetchPresets() {
    try {
        const resp = await fetch(API + '/presets');
        if (resp.ok) _presetsCache = await resp.json();
    } catch(e) { /* server offline, use cache */ }
    return _presetsCache;
}

async function savePreset(mode) {
    const params = collectStrategyParams(mode);
    const confParams = collectConfluenceParams(mode);
    if (confParams) {
        const modelSel = document.getElementById('conf-model-' + mode);
        Object.assign(params, confParams, {
            strategy: 'confluence',
            conf_model_name: (modelSel && modelSel.value) || _activeModelName || null,
        });
    }
    const purpose = prompt('Preset purpose (4-5 chars):', suggestedPresetPurpose(params));
    if (purpose == null) return;
    const name = buildPresetName(params, purpose, 'USER');
    if (!name || !name.trim()) return;
    try {
        const saveResp = await fetch(API + '/presets/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), params: params }),
        });
        if (!saveResp.ok) throw new Error('HTTP ' + saveResp.status);
        await fetch(API + '/presets/use', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), mode: mode }),
        });
        await fetchPresets();
        refreshPresetDropdowns();
        document.getElementById('preset-' + mode).value = name.trim();
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
    } else if (_presetsCache.presets[name]) {
        const presetParams = _presetsCache.presets[name];
        applyStrategyParams(mode, presetParams);
        await activatePresetModel(mode, presetParams.conf_model_name);
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

function refreshPresetDropdowns() {
    const names = Object.keys(_presetsCache.presets || {});
    ['bt', 'live'].forEach(function(mode) {
        const sel = document.getElementById('preset-' + mode);
        if (!sel) return;
        const current = sel.value;
        sel.innerHTML = '<option value="default">Default</option>';
        names.forEach(function(n) {
            const opt = document.createElement('option');
            opt.value = n;
            opt.textContent = isFixedPreset(n) ? n + ' *' : n;
            sel.appendChild(opt);
        });
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
            await activatePresetModel('bt', _presetsCache.presets[lastBt].conf_model_name, { silent: true });
        } else {
            applyStrategyParams('bt', DEFAULT_STRATEGY_PARAMS);
        }
    }
    if (liveSel) {
        liveSel.value = lastLive;
        if (lastLive !== 'default' && _presetsCache.presets[lastLive]) {
            applyStrategyParams('live', _presetsCache.presets[lastLive]);
            await activatePresetModel('live', _presetsCache.presets[lastLive].conf_model_name, { silent: true });
        } else {
            applyStrategyParams('live', DEFAULT_STRATEGY_PARAMS);
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

async function loadModelRegistry() {
    try {
        const resp = await fetch(API + '/confluence/models');
        if (!resp.ok) return;
        const data = await resp.json();
        _modelRegistry = data.models || [];
        _activeModelName = data.active_model || '';
        ['bt', 'live'].forEach(_populateModelSelect);
    } catch (e) { /* registry is optional; manual model parameters still work */ }
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
        log('此模型尚未訓練', 'warn');
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
            log(`已啟用 ${name} (band ${Math.round(band)} · ${tf}TF · runtime RR 1:${_fmtConfRr(runRr)})`, 'success');
        } else {
            log('啟用模型失敗: ' + (data.detail || ('HTTP ' + resp.status)), 'error');
        }
    } catch (e) { log('啟用模型失敗: ' + e, 'error'); }
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
    if (!confirm(`新增模型版本\ntrainer = ${trainer.toUpperCase()}\ndescription = ${description}\n需已載入歷史數據，訓練可能需要一段時間。`)) return;
    log(`訓練新版本中 · ${trainer.toUpperCase()} · ${description}…`, 'info');
    try {
        const resp = await fetch(API + '/confluence/models/retrain', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ trainer, description, rr, band_ticks: band,
                                   min_distinct_tf: tf, enable_breakout: brk,
                                   loss_weight: lw, activate: true }),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok && data.success) {
            log(`✓ ${data.name} 訓練完成 · n=${data.n_samples} win=${(data.win_rate * 100).toFixed(0)}% `
                + `oos=${Number(data.oos_auc).toFixed(2)} · 已啟用`, 'success');
            await loadModelRegistry();
            if (sel) sel.value = data.name;
            if (descriptionEl) descriptionEl.value = '';
        } else {
            log('訓練失敗: ' + (data.detail || ('HTTP ' + resp.status)), 'error');
        }
    } catch (e) { log('訓練失敗: ' + e, 'error'); }
}

document.addEventListener('DOMContentLoaded', loadModelRegistry);

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

// -- Param help dots + hover tooltips (restored from 1.0.6, + ML) --

function addHelpDot(label, tip) {
    if (!label || !tip || label.querySelector('.help-dot')) return;
    const dot = document.createElement('span');
    dot.className = 'help-dot';
    dot.textContent = '?';
    dot.setAttribute('data-tip', tip);
    dot.addEventListener('mouseenter', () => showHelpTooltip(dot));
    dot.addEventListener('mouseleave', hideHelpTooltip);
    label.appendChild(dot);
}

function getHelpTooltip() {
    let tip = document.getElementById('global-help-tooltip');
    if (!tip) {
        tip = document.createElement('div');
        tip.id = 'global-help-tooltip';
        tip.className = 'help-tooltip';
        document.body.appendChild(tip);
    }
    return tip;
}

function showHelpTooltip(dot) {
    const text = dot ? dot.getAttribute('data-tip') : '';
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
}

function hideHelpTooltip() {
    const tip = document.getElementById('global-help-tooltip');
    if (tip) tip.classList.remove('open');
}

function decorateParamHelpDots() {
    // Applied to BOTH backtest (-bt) and live (-live) panels.
    const shared = {
        'strategy': '\u7b56\u7565\u908f\u8f2f\uff1aTREND = \u5340\u9593\u7a81\u7834\u8da8\u52e2\u55ae\uff1bML Confluence = \u591a\u6642\u9593\u6846\u6c34\u5e73\u532f\u805a\u8a55\u5206\uff1bML Consolidation V2 = \u6efe\u52d5\u5340\u9593 VAH/VAL \u5747\u503c\u56de\u6b78\u3002\nStrategy selector for trend, ML confluence, or ML Consolidation V2.',
        'contract': '\u4ea4\u6613 / \u56de\u6e2c\u4f7f\u7528\u7684\u671f\u8ca8\u5408\u7d04\uff0c\u4f8b\u5982 CON.F.US.MNQ.M26\u3002\nFutures contract used for data and orders.',
        'size': '\u6bcf\u7b46\u4ea4\u6613\u7684\u5408\u7d04\u53e3\u6578\u3002\nNumber of contracts per trade.',
        'preset': '\u8f09\u5165\u6216\u4fdd\u5b58\u76ee\u524d\u6240\u6709\u53c3\u6578\u8a2d\u5b9a\u3002\nLoad or save the current parameter set.',
        // ML CONFLUENCE
        'conf-minprob': 'ML \u6a5f\u7387\u9580\u6abb\uff1a\u53ea\u5728 scorer \u9810\u6e2c\u52dd\u7387 \u2265 \u6b64\u503c\u6642\u9032\u5834\u3002OFF = \u4e0d\u7528\u52dd\u7387\u9580\u6abb\u3002\nML win-probability gate.',
        'conf-rrmode': '\u56fa\u5b9a\u76c8\u8667\u6bd4\uff0c\u53ef\u9078 1:1 \u5230 1:6\u3002LATEST production scorer \u76ee\u524d\u7528 RR3 \u8a13\u7df4\u3002\nFixed reward:risk from 1:1 to 1:6.',
        'conf-band': '\u532f\u805a\u5e36\u5bec\uff08ticks\uff09\uff1a\u4e0d\u540c TF \u6c34\u5e73\u4f4d\u843d\u5728\u6b64\u7bc4\u570d\u5167\u8996\u70ba\u540c\u4e00\u532f\u805a\u5340\u3002\u8d8a\u5927 = \u8d8a\u5bb9\u6613\u5408\u4f75\uff0c\u8a0a\u865f\u66f4\u591a\u3002\nConfluence band in ticks.',
        'conf-mintf': '\u6700\u5c11\u4e0d\u540c\u6642\u9593\u6846\u6578\uff1a\u4e00\u500b\u532f\u805a\u5340\u81f3\u5c11\u9700\u8981\u591a\u5c11\u500b\u4e0d\u540c TF \u7684\u6c34\u5e73\u4f4d\u624d\u7b97\u6709\u6548\u3002\nMinimum distinct timeframes required.',
        'conf-evfloor': '\u671f\u671b\u503c\u9580\u6abb\uff0c\u512a\u5148\u65bc MIN PROB\u3002EV = prob \u00d7 RR \u2212 (1 \u2212 prob)\u3002\nExpected-value gate.',
        'conf-maxrisk': '\u6700\u5927 SL \u98a8\u96aa\uff08ticks\uff09\uff1aSL \u8ddd\u96e2\u8d85\u904e\u6b64\u503c\u7684\u8a0a\u865f\u6703\u88ab\u8df3\u904e\u3002\nMax allowed stop distance in ticks.',
        'conf-trail-trigger': 'ML \u9054\u5230 TP \u7684\u6307\u5b9a\u767e\u5206\u6bd4\u5f8c\uff0c\u5c07 SL \u79fb\u5230 +5% TP \u7684\u9396\u5229\u4f4d\u3002\nTrail trigger for moving stop after partial progress toward TP.',
        'conf-session-limit': 'Live parity \u9396\u5b9a\uff1a\u540c\u4e00 session / \u4e3b TF \u7246 / \u65b9\u5411\u53ea\u505a\u4e00\u6b21\u3002\nLive-style duplicate-entry lock.',
        'conf-allowed-sessions': '\u5e02\u5834\u76e4\u6bb5\u904e\u6ffe\uff1a\u53ea\u5728\u9078\u5b9a\u76e4\u6bb5\u958b\u65b0\u55ae\u3002ASIA \u662f\u76ee\u524d\u8f03\u7a69\u7684\u9810\u8a2d\u3002\nMarket segment filter.',
        'overlap-tf': '\u53c3\u8207\u532f\u805a\u7684\u6642\u9593\u6846\u3002\u9078 1 = \u55ae\u4e00\u6846\uff1b\u9078 2+ = \u8de8\u6846\u91cd\u758a\u532f\u805a\u3002\nTimeframes feeding confluence.',
        // TREND
        'tr-overlap-trade-tf': 'TREND overlap trade zone: merged = synthetic averaged overlap; smallest = trade the smallest selected timeframe zone.',
        'area-pct': 'TREND\uff1a\u5340\u9593\u5224\u5b9a\u7684\u9762\u7a4d\u6bd4\u4f8b\u9580\u6abb\uff0c\u8d8a\u9ad8\u8d8a\u56b4\u683c\u3002\nTREND range-area threshold.',
        'confirm-bars': 'TREND\uff1a\u7a81\u7834\u5f8c\u9700\u8981\u9023\u7e8c\u78ba\u8a8d\u7684 K \u7dda\u6578\uff0c\u8d8a\u591a\u8d8a\u4fdd\u5b88\u3002\nConfirmation bars after breakout.',
        'rr-ratio': 'TREND\uff1a\u6b62\u76c8\u8207\u6b62\u640d\u7684\u6bd4\u4f8b\uff08TP:SL\uff09\u3002\nTake-profit to stop-loss ratio.',
        'trail-trigger-pct': '\u50f9\u683c\u5230\u9054 TP \u7684\u6307\u5b9a\u767e\u5206\u6bd4\u5f8c\u958b\u59cb\u79fb\u52d5\u6b62\u640d\u3002OFF = \u4e0d\u79fb\u52d5\u3002\nTrail trigger percentage.',
        'trail-sl-pct': '\u89f8\u767c\u5f8c\u6b62\u640d\u8981\u79fb\u5230\u7684\u4f4d\u7f6e\uff0c\u76f8\u5c0d\u5165\u5834 / TP \u8a08\u7b97\u3002\nWhere the stop moves after trigger.',
        'full-tp-lock': '\u65e5\u5167\u9054\u5230\u6b64\u7372\u5229\u76ee\u6a19\u5f8c\u9396\u5b9a\uff0c\u4e0d\u518d\u958b\u65b0\u55ae\uff080 = OFF\uff09\u3002\nBlocks new entries after daily profit target.',
        'tr-session-limit': '\u540c\u4e00 Topstep session \u5167\uff0c\u540c\u4e00\u5340\u9593 / \u7a81\u7834\u65b9\u5411\u53ea\u5141\u8a31\u4e00\u6b21\u6210\u4ea4\u6a5f\u6703\uff1b\u672a\u6210\u4ea4\u53d6\u6d88\u6703\u91cb\u653e\u3002\nOne filled opportunity per zone/direction per session.',
        'tr-allowed-sessions': 'TREND \u5e02\u5834\u76e4\u6bb5\u904e\u6ffe\uff1a\u53ea\u5728\u9078\u5b9a\u76e4\u6bb5\u958b\u65b0\u55ae\uff1bpending \u8de8\u51fa\u76e4\u6bb5\u6703\u53d6\u6d88\u4e26\u91cb\u653e lock\u3002\nTrend market segment filter.',
    };
    const standalone = {
        'username': 'Topstep / ProjectX \u767b\u5165\u90f5\u7bb1\u3002\nTopstep login email.',
        'apikey': 'ProjectX API \u91d1\u9470\u3002\nProjectX API key.',
        'contract-preset': '\u5feb\u901f\u586b\u5165 contractId\u3002\nShortcut that fills the contractId.',
        'contract-id': '\u671f\u8ca8\u5408\u7d04 ID\uff0c\u4f8b\u5982 CON.F.US.MNQ.M26\u3002\nFutures contract ID.',
        'start-date': '\u6b77\u53f2\u8cc7\u6599\u958b\u59cb\u65e5\u671f\u3002\nStart date for historical data.',
        'end-date': '\u6b77\u53f2\u8cc7\u6599\u7d50\u675f\u65e5\u671f\u3002\nEnd date for historical data.',
        'data-count': '\u76ee\u524d\u8f09\u5165\u7684 1 \u5206\u9418 K \u7dda\u6578\u91cf\u3002\nLoaded 1-minute candle count.',
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
}

// -- Init ------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
    initChart();
    decorateParamHelpDots();
    checkHealth();
    loadEnvConfig();
    updateClock();
    setInterval(updateClock, 1000);

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
    setTimeout(() => {
        const username = document.getElementById('username').value.trim();
        if (username) {
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
            // Calendar is a full-page overlay; any non-calendar tab restores .main.
            if (tab === 'calendar') {
                if (mainEl) mainEl.style.display = 'none';
                if (calView) calView.classList.remove('hidden');
                liveTopBar.style.display = 'none';
                renderCalendar();
                return;
            }
            if (mainEl) mainEl.style.display = '';
            if (calView) calView.classList.add('hidden');
            if (tab === 'backtest') {
                backtestPanels.classList.remove('hidden');
                if (metricsPanel.style.display === 'block') metricsPanel.classList.remove('hidden');
                livePanel.classList.add('hidden');
                liveTopBar.style.display = 'none';
            } else if (tab === 'live') {
                backtestPanels.classList.add('hidden');
                metricsPanel.classList.add('hidden');
                livePanel.classList.remove('hidden');
                if (_liveInterval || _liveStatusInterval) liveTopBar.style.display = 'block';
                updateLiveTopBar();
            }
        };
    });
    document.querySelectorAll('.bottom-tab').forEach(t => {
        t.onclick = () => {
            document.querySelectorAll('.bottom-tab').forEach(x => x.classList.remove('active'));
            t.classList.add('active');
            const tab = t.dataset.btab;
            ['trades','execute','learn','pnl','log'].forEach(id => {
                const panel = document.getElementById('btab-' + id);
                if (panel) panel.classList.toggle('hidden', id !== tab);
            });
            if (tab === 'log') scrollSystemLogToBottom();
            if (tab === 'learn') loadLearnResult();
            if (tab === 'pnl') renderPnlCurve();
        };
    });
});

async function loadEnvConfig() {
    try {
        const resp = await fetch(API + '/config');
        const cfg = await resp.json();

        if (cfg.env_loaded) {
            document.getElementById('username').value = cfg.username;
            document.getElementById('apikey').placeholder = cfg.api_key_preview + ' (from .env)';
            document.getElementById('apikey').value = '';
            document.getElementById('contract-id').value = cfg.contract_id || '';
            log('.env loaded: username=' + cfg.username + ', key=' + cfg.api_key_preview, 'success');
            log('Credentials from .env -- click CONNECT to fetch data', 'info');

            // Accounts loaded after connect, not here
        } else {
            log('.env not configured -- enter credentials manually', 'warn');
        }
    } catch(e) {
        log('Could not load .env config: ' + e.message, 'warn');
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
        currentAccount = practice || data.accounts[0];
        updateAccountBadge();

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

function onLiveAccountSwitch() {
    const id = parseInt(document.getElementById('live-account-select').value);
    liveAccount = allAccounts.find(a => a.id === id) || null;
    // Save selection
    if (id) localStorage.setItem('ancser_live_account_id', id);
    // Sync header badge
    currentAccount = liveAccount;
    updateAccountBadge();

    const info = document.getElementById('live-account-info');
    if (liveAccount) {
        const type = liveAccount.is_practice ? 'PRACTICE' : 'FUNDED';
        info.innerHTML = '<span style="color:var(--text2);">' + type + '</span> | Balance: <span style="color:var(--green);">$' + liveAccount.balance.toLocaleString(undefined, {maximumFractionDigits: 0}) + '</span>';
        if (!liveAccount.is_practice) {
            info.innerHTML += '<br><span style="color:var(--red);">WARNING: FUNDED ACCOUNT</span>';
        }
    } else {
        info.innerHTML = '';
    }
}

let _liveInterval = null;
let _liveStatusInterval = null;
let _liveStartInProgress = false;

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
    const utcH = now.getUTCHours();
    const utcM = now.getUTCMinutes();
    const utcDay = now.getUTCDay(); // 0=Sun
    const month = now.getUTCMonth(); // 0=Jan

    // EDT: Mar(2)-Oct(9), EST: Nov(10)-Feb(1)
    const isDST = (month >= 2 && month <= 9);
    const etOffset = isDST ? 4 : 5; // UTC-4 or UTC-5

    // Convert to ET minutes since midnight
    let etMinutes = (utcH * 60 + utcM) - etOffset * 60;
    let etDay = utcDay;
    if (etMinutes < 0) { etMinutes += 1440; etDay = (etDay - 1 + 7) % 7; }

    // Weekend: Sat all day, Sun before 18:00 ET, Fri after 17:00 ET
    if (etDay === 6) return { label: 'CLOSED', color: 'var(--text3)' };
    if (etDay === 0 && etMinutes < 18 * 60) return { label: 'CLOSED', color: 'var(--text3)' };
    if (etDay === 5 && etMinutes >= 17 * 60) return { label: 'CLOSED', color: 'var(--text3)' };

    // 維護 17:00-18:00 ET
    if (etMinutes >= 17 * 60 && etMinutes < 18 * 60) {
        return { label: 'CLOSED', color: 'var(--text3)' };
    }

    // 開盤 09:30-16:00 ET
    if (etMinutes >= 9 * 60 + 30 && etMinutes < 16 * 60) {
        return { label: 'NORMAL', color: 'var(--green)' };
    }

    // 盤後 16:00-17:00 ET
    if (etMinutes >= 16 * 60 && etMinutes < 17 * 60) {
        return { label: 'AFTER', color: 'var(--cyan)' };
    }

    // 盤前 18:00-09:30 ET (overnight)
    return { label: 'PRE', color: 'var(--amber)' };
}

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

    if (currentAccount) {
        document.getElementById('lv-capital').textContent = '$' + currentAccount.balance.toLocaleString(undefined, {maximumFractionDigits: 0});
    }
}

async function refreshLiveZoneOverlay(stratParams) {
    try {
        const zoneResp = await fetch(API + '/data/detect-zones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                value_area_pct: stratParams.value_area_pct,
                area_timeframe: stratParams.area_timeframe,
            }),
        });
        if (zoneResp.ok) {
            const zoneData = await zoneResp.json();
            if (zoneData.zones && zoneData.zones.length > 0) {
                drawVolumeProfile(zoneData.zones);
                drawBacktestZones(zoneData.zones);
                log('偵測到 ' + zoneData.zones.length + ' 個盤整區間', 'success');
                if (window._lastChartData) {
                    applyDefaultChartView(window._lastChartData, zoneData.zones);
                }
            } else {
                log('未偵測到盤整區間', 'info');
            }
        }
    } catch(e) {
        log('Zone detection failed: ' + e.message, 'warn');
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
    statusEl.textContent = '啟動中...';
    log('GO LIVE: account=' + liveAccount.name + ' (' + (liveAccount.is_practice ? 'practice' : 'FUNDED') + ')', 'info');
    const stratParams = collectStrategyParams('live');

    // v1.0.6: ML (confluence, explainable) is selected via the STRATEGY dropdown.
    // No shadow mode in live — practice account places real orders.
    const confParams = collectConfluenceParams('live');
    if (confParams) {
        stratParams.strategy = 'confluence';
        stratParams.conf_shadow = false;
        Object.assign(stratParams, confParams);
        const gateTxt = (stratParams.conf_ev_floor != null)
            ? ('EV≥' + stratParams.conf_ev_floor + ' (EV優先)')
            : ('minProb=' + stratParams.conf_min_prob);
        const rrTxt = (Array.isArray(stratParams.conf_rr_grid) && stratParams.conf_rr_grid.length)
            ? ('rrGrid=' + stratParams.conf_rr_grid.join('/') + ' (EV挑選)')
            : ('rr=' + stratParams.conf_rr);
        log('ML CONFLUENCE: LIVE (places orders) base=1m ' + gateTxt
            + ' ' + rrTxt + ' band=' + stratParams.conf_band_ticks
            + ' minTF=' + stratParams.conf_min_distinct_tf
            + ' SLref=' + (stratParams.conf_sl_reference_tf || 'largest')
            + ' market=' + allowedSessionsLabel(stratParams.conf_allowed_sessions), 'info');
    }
    if (stratParams.strategy === 'ml_consolidation_v2') {
        stratParams.mlc2_shadow = false;
        log('ML CONSOLIDATION V2: LIVE (places orders) '
            + 'LB=' + stratParams.mlc2_lookback
            + ' band=' + stratParams.mlc2_band_ticks
            + ' rr=' + stratParams.mlc2_rr
            + ' market=' + allowedSessionsLabel(stratParams.mlc2_allowed_sessions), 'info');
    }
    if (stratParams.strategy === 'trend') {
        log('TREND: LIVE ' + trendTfUsageText(stratParams)
            + ' RR1:' + stratParams.rr_ratio
            + ' C=' + stratParams.breakout_confirm_bars
            + ' sessionLimit=' + (stratParams.tr_one_trade_per_session ? 'ON' : 'OFF')
            + ' market=' + allowedSessionsLabel(stratParams.tr_allowed_sessions), 'info');
    }

    // Switch the zone filter to LIVE and preselect the timeframe(s) being traded
    // (overlap combo, or the single area timeframe).
    _zoneFilter.mode = 'live';
    const liveTfs = (stratParams.method === 'overlap' && stratParams.tf_combo && stratParams.tf_combo.length)
        ? stratParams.tf_combo
        : [stratParams.area_timeframe];
    _zoneFilter.tfs = new Set(liveTfs);
    syncZoneFilterUI();

    // Show live top bar (chart data stays as-is from connect)
    document.getElementById('live-top-bar').style.display = 'block';
    updateLiveTopBar();

    // ── Start candle polling + status polling (always, even if engine fails) ──
    _cachedVPZones = null;  // clear backtest zones to avoid overlap with live zones
    _lastLiveCandleTime = '';  // reset
    if (_liveInterval) clearInterval(_liveInterval);
    _liveInterval = setInterval(pollLiveCandle, 1000); // every 1s (backend caches API calls)
    pollLiveCandle(); // immediate first poll

    if (_liveStatusInterval) clearInterval(_liveStatusInterval);
    _liveStatusInterval = setInterval(pollLiveStatus, 1000); // every 1s
    pollLiveStatus();

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
            log('交易引擎啟動失敗: ' + (data.detail || JSON.stringify(data)), 'warn');
            statusEl.style.color = 'var(--amber)';
            statusEl.textContent = '僅監控';
        } else {
            engineStarted = true;
            log('????????? ? ??????', 'success');
            refreshTfZones(true);
            setTimeout(() => refreshLiveZoneOverlay(stratParams), 0);
        }
    } catch(e) {
        log('交易引擎連線失敗: ' + e.message + ' (僅監控模式)', 'warn');
        statusEl.style.color = 'var(--amber)';
        statusEl.textContent = '僅監控';
    }

    if (!engineStarted) _liveStartInProgress = false;

    if (engineStarted) {
        statusEl.style.color = 'var(--amber)';
        statusEl.textContent = '啟動中...';
        const dot = document.getElementById('live-status-dot');
        if (dot) { dot.style.background = 'var(--amber)'; dot.style.boxShadow = '0 0 6px var(--amber)'; }
        const stopBtn = document.getElementById('btn-stop-live');
        if (stopBtn) stopBtn.disabled = true;
        const flattenBtn = document.getElementById('btn-flatten');
        if (flattenBtn) flattenBtn.disabled = true;
        log('????????? ? ??????', 'success');
    } else {
        const dot = document.getElementById('live-status-dot');
        if (dot) { dot.style.background = 'var(--amber)'; dot.style.boxShadow = '0 0 6px var(--amber)'; }
        goBtn.disabled = false;
        const stopBtn = document.getElementById('btn-stop-live');
        if (stopBtn) stopBtn.disabled = true;
        const flattenBtn = document.getElementById('btn-flatten');
        if (flattenBtn) flattenBtn.disabled = true;
        log('監控模式 — K線每秒更新 (交易引擎未啟動)', 'info');
    }

    // Auto-fetch real account state
    fetchRealState();
}

async function stopLive() {
    const statusEl = document.getElementById('live-status-text');
    _liveStartInProgress = false;
    try {
        const resp = await fetch(API + '/live/stop', { method: 'POST' });
        const data = await resp.json();
        log('引擎已停止', 'info');
    } catch(e) {
        log('Stop error: ' + e.message, 'error');
    }

    if (_liveInterval) { clearInterval(_liveInterval); _liveInterval = null; }
    if (_liveStatusInterval) { clearInterval(_liveStatusInterval); _liveStatusInterval = null; }

    document.getElementById('btn-go-live').disabled = false;
    document.getElementById('btn-stop-live').disabled = true;
    document.getElementById('btn-flatten').disabled = true;

    statusEl.style.color = 'var(--text3)';
    statusEl.textContent = 'STOPPED';
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
    if (!confirm('確認緊急平倉？')) return;
    try {
        const resp = await fetch(API + '/live/flatten', { method: 'POST' });
        const data = await resp.json();
        log('緊急平倉: ' + (data.message || 'OK'), 'warn');
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
                    html += '<div style="padding-left:8px;color:var(--text3);font-size:9px;">' + l + '</div>';
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

async function pollLiveStatus() {
    // Always update market session (even without engine)
    const session = getMarketSession();
    const elSession = document.getElementById('lv-session');
    if (elSession) { elSession.textContent = session.label; elSession.style.color = session.color; }
    const elMarket = document.getElementById('lv-market-session');
    if (elMarket) { elMarket.textContent = session.label; elMarket.style.color = session.color; }

    try {
        const resp = await fetch(API + '/live/status');
        if (!resp.ok) return;
        const st = await resp.json();
        if (!st.running) {
            if (_liveStartInProgress) {
                const statusEl = document.getElementById('live-status-text');
                if (statusEl) {
                    statusEl.style.color = 'var(--amber)';
                    statusEl.textContent = '啟動中...';
                }
                const dot = document.getElementById('live-status-dot');
                if (dot) {
                    dot.style.background = 'var(--amber)';
                    dot.style.boxShadow = '0 0 6px var(--amber)';
                }
                const phaseText = st.phase || '構建區間中...';
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
            const statusEl = document.getElementById('live-status-text');
            if (statusEl) {
                statusEl.style.color = 'var(--text3)';
                statusEl.textContent = 'STOPPED';
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

        _liveStartInProgress = false;
        const statusEl = document.getElementById('live-status-text');
        if (statusEl) {
            statusEl.style.color = 'var(--green)';
            statusEl.textContent = '交易中';
        }
        const dot = document.getElementById('live-status-dot');
        if (dot) {
            dot.style.background = 'var(--green)';
            dot.style.boxShadow = '0 0 6px var(--green)';
        }
        const stopBtn = document.getElementById('btn-stop-live');
        if (stopBtn) stopBtn.disabled = false;
        const flattenBtn = document.getElementById('btn-flatten');
        if (flattenBtn) flattenBtn.disabled = false;

        // Show engine version in console for debugging
        if (st.engine_version && !window._loggedVersion) {
            log('[ENGINE] ' + st.engine_version, 'info');
            window._loggedVersion = true;
        }

        // ── Top bar: Strategy ──
        const stratTopEl = document.getElementById('lv-strategy');
        if (stratTopEl) {
            const sn = (st.strategy_mode || collectStrategyParams('live').strategy || '--').toUpperCase();
            stratTopEl.textContent = sn;
            stratTopEl.style.color = 'var(--cyan)';
            window._liveStrategyName = sn;
        }

        // ── Top bar: ML (confluence) decision-basis banner ──
        const confRow = document.getElementById('lv-confluence-row');
        if (confRow) {
            const isMlc2 = !!st.mlc2_mode;
            const sigs = isMlc2 ? (st.mlc2_signals || []) : (st.confluence_signals || []);
            if ((st.confluence_mode || isMlc2) && sigs.length) {
                const last = sigs[sigs.length - 1];
                const basis = last.basis || (
                    (last.mode ? '[' + last.mode + '] ' : '') + (last.direction || '') + ' ' + (last.side || '')
                    + ' entry=' + last.entry + ' sl=' + last.sl + ' tp=' + last.tp
                    + ' prob=' + (last.prob != null ? last.prob.toFixed(2) : '?')
                );
                const tag = st.confluence_shadow ? 'SHADOW · ' : '';
                const scorer = st.confluence_scorer ? (' · scorer=' + st.confluence_scorer) : '';
                const bannerTag = (isMlc2 ? st.mlc2_shadow : st.confluence_shadow) ? 'SHADOW · ' : '';
                const bannerScorer = (!isMlc2 && st.confluence_scorer) ? (' · scorer=' + st.confluence_scorer) : '';
                document.getElementById('lv-conf-basis').textContent = bannerTag + (isMlc2 ? 'MLC2 · ' : '') + basis + bannerScorer;
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
            posEl.textContent = '掛單中(' + age + '/' + timeout + 'min)';
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
        const isMLmode = (st.strategy_mode === 'confluence') || st.confluence_mode || st.mlc2_mode;
        let modeText, modeColor;
        if (isMLmode) {
            modeText = st.confluence_shadow ? '影子(不下單)' : '實盤';
            modeColor = st.confluence_shadow ? 'var(--amber)' : 'var(--green)';
        } else {
            const am = (st.active_mode || '').toUpperCase();
            const sn = (st.strategy_mode || '').toUpperCase();
            modeText = (am && am !== sn) ? am : '實盤';
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
        const isMLStatus = (st.strategy_mode === 'confluence') || st.confluence_mode || st.mlc2_mode;
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
            statusLabelEl.textContent = isMLStatus ? 'ML 狀態' : 'TREND 狀態';
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
                        + '<span style="color:var(--text2);min-width:50px;text-align:right;">權' + (r.weight != null ? r.weight.toFixed(1) : '--') + '</span>'
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

        // ── Redraw zones from live status ──
        if (st.zones && st.zones.length > 0) {
            // Include POC/VAH/VAL + num_candles in key so VP redraws when zone updates
            const zoneKey = st.zones.map(z => z.zone_id + z.status + z.poc.toFixed(2) + z.num_candles).join('|');
            if (zoneKey !== window._lastLiveZoneKey) {
                window._lastLiveZoneKey = zoneKey;
                // Refresh the all-timeframe cache (throttled inside) so the LIVE
                // filter shows the freshest currently-using zone per timeframe.
                refreshTfZones();
            }
        }

        // ── Live realtime markers on chart (pending/open only) ──
        if (candleSeries) {
            if (st.trades) window._lastLiveTradeCount = st.trades.length;
            const liveMarkers = [];

            // Show pending signal as marker
            if (sig) {
                const sigDir = String(sig.direction || '').toLowerCase();
                const isLong = sigDir === 'buy' || sigDir === 'long';
                const localOffset = new Date().getTimezoneOffset() * -60;
                const pseudoTrade = {
                    direction: isLong ? 'buy' : 'sell',
                    entry_price: sig.entry_price,
                    mode: sig.mode,
                    side: sig.side,
                };
                const decision = _tradeDecisionPhrase(pseudoTrade);
                liveMarkers.push({
                    time: Math.floor(Date.now() / 1000) + localOffset,
                    position: isLong ? 'belowBar' : 'aboveBar',
                    color: '#ffa726',
                    shape: 'circle',
                    text: 'PENDING' + (decision ? '\n' + decision : ''),
                });
            }
            // Show filled position as marker
            if (st.position && st.fill_price) {
                const posIsLong = positionSideMeta(st.position).isLong;
                const localOffset = new Date().getTimezoneOffset() * -60;
                const pseudoTrade = {
                    direction: posIsLong ? 'buy' : 'sell',
                    entry_price: st.fill_price,
                    mode: sig && sig.mode,
                    side: sig && sig.side,
                };
                const decision = _tradeDecisionPhrase(pseudoTrade);
                liveMarkers.push({
                    time: Math.floor(Date.now() / 1000) + localOffset,
                    position: posIsLong ? 'belowBar' : 'aboveBar',
                    color: '#ffa726',
                    shape: 'circle',
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

async function pollLiveCandle() {
    // Fetch fresh candles from API and append new bars
    const session = getMarketSession();
    if (session && session.label === 'CLOSED') return;

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
            _refreshAllMarkers();
            log('K線更新: ' + newestC.close.toFixed(2) + ' (' + updated + ' bars)', 'info');
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

function initChart() {
    const container = document.getElementById('chart-container');
    chart = LightweightCharts.createChart(container, {
        width: container.clientWidth,
        height: container.clientHeight,
        layout: {
            background: { type: 'solid', color: '#08090d' },
            textColor: '#556178',
            fontSize: 11,
            fontFamily: 'IBM Plex Mono, monospace',
        },
        grid: {
            vertLines: { color: 'rgba(100, 220, 255, 0.03)' },
            horzLines: { color: 'rgba(100, 220, 255, 0.03)' },
        },
        crosshair: {
            mode: LightweightCharts.CrosshairMode.Normal,
            vertLine: { color: 'rgba(100, 220, 255, 0.2)', style: 0, width: 1 },
            horzLine: { color: 'rgba(100, 220, 255, 0.2)', style: 0, width: 1 },
        },
        rightPriceScale: {
            borderColor: 'rgba(100, 220, 255, 0.08)',
        },
        timeScale: {
            borderColor: 'rgba(100, 220, 255, 0.08)',
            timeVisible: true,
            secondsVisible: false,
            tickMarkFormatter: _chartTickMark,
        },
        localization: {
            // Crosshair tooltip shows the full "2026.04.15 15:30" stamp.
            timeFormatter: _chartStampFull,
        },
    });

    candleSeries = chart.addCandlestickSeries({
        upColor: '#888888',
        downColor: '#555555',
        borderDownColor: '#555555',
        borderUpColor: '#888888',
        wickDownColor: '#555555',
        wickUpColor: '#888888',
    });

    new ResizeObserver(() => {
        chart.applyOptions({
            width: container.clientWidth,
            height: container.clientHeight
        });
        // Redraw VP overlay on resize
        renderTfZones();
        redrawTradeDecisionOverlays();
        drawSessionDividers();
    }).observe(container);

    // Redraw VP overlay on scroll / zoom — continuous following via rAF
    let _vpRafId = null;
    const _redrawOverlays = () => {
        if (_vpRafId) return; // already scheduled
        _vpRafId = requestAnimationFrame(() => {
            _vpRafId = null;
            renderTfZones();
            redrawTradeDecisionOverlays();
            drawSessionDividers();
        });
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(_redrawOverlays);
    // Vertical zoom (wheel on price scale or chart body)
    container.addEventListener('wheel', _redrawOverlays, { passive: true });
    // Continuous drag redraw
    container.addEventListener('mousemove', (e) => { if (e.buttons) _redrawOverlays(); }, { passive: true });
    container.addEventListener('mouseup', _redrawOverlays);

    log('Chart initialized', 'info');
}

// -- Volume Profile Overlay (full-chart canvas) ---------------
// Draws VP histogram at each zone's formed_at position
// POC extends from zone start to the next session boundary.

let vpOverlayCanvas = null;
let positionLines = [];
let _cachedVPZones = null;  // cached for redraw on scroll/zoom

function createVPOverlay() {
    if (vpOverlayCanvas) return vpOverlayCanvas;
    const container = document.getElementById('chart-container');
    const canvas = document.createElement('canvas');
    canvas.id = 'vp-overlay';
    canvas.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:3;';
    container.appendChild(canvas);
    vpOverlayCanvas = canvas;
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

// Session boundaries in UTC (hour, minute, label)
// ASIA 22:00 → EURO 07:00 → PRE 11:00 → RTH 13:30 → AH 20:00 → ASIA 22:00
const SESSION_BOUNDARIES = [
    { h: 22, m: 0,  label: 'ASIA' },
    { h: 7,  m: 0,  label: 'EURO' },
    { h: 11, m: 0,  label: 'PRE'  },
    { h: 13, m: 30, label: 'RTH'  },
    { h: 20, m: 0,  label: 'AH'   },
];
const NO_TRADE_WINDOWS_UTC = [
    { startH: 19, startM: 30, endH: 22, endM: 0, label: 'NO TRADE' },
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
    const localOffset = new Date(ms).getTimezoneOffset() * -60;
    return Math.floor(ms / 1000) + localOffset;
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
    let offset = _timeZoneOffsetMs('America/New_York', guess);
    let utc = guess - offset;
    const offset2 = _timeZoneOffsetMs('America/New_York', utc);
    if (offset2 !== offset) utc = guess - offset2;
    return utc;
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
            const boundaryMs = Date.UTC(
                day.getUTCFullYear(),
                day.getUTCMonth(),
                day.getUTCDate(),
                b.h, b.m, 0
            );
            if (boundaryMs > formedMs + 1000 && (best === null || boundaryMs < best)) {
                best = boundaryMs;
            }
        });
    }

    return best;
}

function drawSessionDividers() {
    if (!candleSeries || !chart) return;
    const canvas = createSessionDividerCanvas();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth;
    const H = container.clientHeight;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
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

    // Sweep-only uses session high/low levels, so keep session dividers but do
    // not shade NY-open windows by default.

    ctx.strokeStyle = 'rgba(255, 255, 255, 0.25)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.font = '9px "IBM Plex Mono", monospace';
    ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
    ctx.textAlign = 'left';

    for (let d = startDay.getTime(); d <= endMs; d += dayMs) {
        const day = new Date(d);
        SESSION_BOUNDARIES.forEach(b => {
            const boundary = new Date(Date.UTC(
                day.getUTCFullYear(),
                day.getUTCMonth(),
                day.getUTCDate(),
                b.h, b.m, 0
            ));
            const bMs = boundary.getTime();
            if (bMs < fromMs - dayMs || bMs > toMs + dayMs) return;

            // Convert to lightweight-charts time (local offset hack used elsewhere)
            const chartTime = utcMsToChartTime(bMs);
            const x = chart.timeScale().timeToCoordinate(chartTime);
            if (x === null || x < 0 || x > W) return;

            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, H);
            ctx.stroke();

            // Label at top
            ctx.fillText(b.label, x + 3, 11);
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

    // Iterate a wider day span because chart timestamps are encoded in the
    // browser's wall-clock timezone while these windows are New York local time.
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
        NO_TRADE_WINDOWS_UTC.forEach(w => {
            const startMs = Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), w.startH, w.startM, 0);
            const endWindowMs = Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), w.endH, w.endM, 0);
            if (endWindowMs < fromMs || startMs > toMs) return;
            const x1 = chart.timeScale().timeToCoordinate(utcMsToChartTime(startMs));
            const x2 = chart.timeScale().timeToCoordinate(utcMsToChartTime(endWindowMs));
            if (x1 === null || x2 === null) return;
            const left = Math.max(0, Math.min(x1, x2));
            const right = Math.min(W, Math.max(x1, x2));
            if (right <= 0 || left >= W || right - left < 2) return;

            ctx.fillRect(left, 0, right - left, H);
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

// ── Timeframe zone filter state (bottom-left chart control) ──
// Draws clean VAH/VAL/POC LINES per timeframe (no VP histogram). Line width
// scales with timeframe size; colour = orange (live) / white (backtest).
const TF_ORDER = ['5m', '15m', '30m', '1h', '4h'];
const TF_LINE_WIDTH = { '5m': 1.0, '15m': 1.3, '30m': 1.7, '1h': 2.1, '4h': 2.6 };
let _zoneFilter = { tfs: new Set(['5m']), mode: 'backtest' };
let _tfAllZones = [];          // all-timeframe zones (each tagged with .timeframe)
let _tfZonesFetching = false;
let _tfZonesLastFetch = 0;

function _zoneFilterAreaPct() {
    const live = document.getElementById('area-pct-live');
    const bt = document.getElementById('area-pct-bt');
    const el = (_zoneFilter.mode === 'live' && live) ? live : (bt || live);
    const v = el ? parseFloat(el.value) : 0.80;
    return (v >= 0.50 && v <= 0.95) ? v : 0.80;
}

// Which param panel drives the chart zones (live when running, else backtest).
function _activeZonePanel() {
    return (_zoneFilter.mode === 'live') ? 'live' : 'bt';
}

// Pull the chart-zone timeframes from the active param panel's TF selection.
function syncZoneFilterUI() {
    let sel = [];
    try { sel = readOverlapTfCombo(_activeZonePanel()); } catch (e) {}
    _zoneFilter.tfs = new Set(sel.length ? sel : ['5m']);
}

// Kept for back-compat callers: re-sync TFs from params, then refetch + redraw.
function onZoneFilterChange() {
    syncZoneFilterUI();
    refreshTfZones(true);
}

// Fetch all-timeframe zones from the server (throttled), then render.
async function refreshTfZones(force) {
    const now = Date.now();
    if (_tfZonesFetching) return;
    if (!force && now - _tfZonesLastFetch < 15000) {
        // A recent request may legitimately return no zones (for example
        // before historical data is loaded). Do not bounce back into
        // renderTfZones(), which would immediately call this function again.
        if (_tfAllZones && _tfAllZones.length > 0) renderTfZones();
        return;
    }
    _tfZonesFetching = true;
    try {
        const resp = await fetch(API + '/data/detect-zones', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ all_timeframes: true, value_area_pct: _zoneFilterAreaPct() }),
        });
        if (resp.ok) {
            const data = await resp.json();
            _tfAllZones = data.zones || [];
        }
    } catch (e) {
        // silent — keep previous cache
    } finally {
        // Always advance throttle timestamp (even on error) so a failing
        // request doesn't bypass the 15s throttle and spam the backend.
        _tfZonesLastFetch = Date.now();
        _tfZonesFetching = false;
        renderTfZones();
    }
}

// Draw one zone's VAH/VAL (solid) + POC (dashed) horizontal lines.
// `op` is an opacity multiplier (backtest zones = 0.8, live/current zone = 1.0).
function _drawZoneLines(ctx, z, tf, lw, color, op, W, H, rightX, priceToY, tX) {
    const yVAH = priceToY(z.vah_80);
    const yVAL = priceToY(z.val_80);
    const yPOC = priceToY(z.poc);
    if ([yVAH, yVAL, yPOC].every(y => y < 0 || y > H)) return;

    // Always clamp the horizontal extent to the zone's own time range
    // (formed_at → left_at) so the line spans exactly the bucket — never the
    // full chart width — for both live and backtest.
    const a = tX(z.formed_at);
    const b = z.left_at ? tX(z.left_at) : null;
    let x0 = (a !== null) ? a : 0;
    let x1 = (b !== null) ? b : rightX;
    if (x1 < 0 || x0 > W) return;
    x0 = Math.max(0, x0);
    x1 = Math.min(rightX, x1);
    if (x1 <= x0 + 2) return;

    ctx.lineWidth = lw;
    // VAH / VAL — solid
    ctx.setLineDash([]);
    ctx.strokeStyle = `rgba(${color}, ${(0.9 * op).toFixed(3)})`;
    if (yVAH >= 0 && yVAH <= H) { ctx.beginPath(); ctx.moveTo(x0, yVAH); ctx.lineTo(x1, yVAH); ctx.stroke(); }
    if (yVAL >= 0 && yVAL <= H) { ctx.beginPath(); ctx.moveTo(x0, yVAL); ctx.lineTo(x1, yVAL); ctx.stroke(); }
    // POC — dashed
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = `rgba(${color}, ${(0.65 * op).toFixed(3)})`;
    if (yPOC >= 0 && yPOC <= H) { ctx.beginPath(); ctx.moveTo(x0, yPOC); ctx.lineTo(x1, yPOC); ctx.stroke(); }
    ctx.setLineDash([]);

    // No text labels here: zones are represented visually by the range lines.
}

// Render the selected-timeframe zones onto the VP overlay canvas.
function renderTfZones() {
    const canvas = createVPOverlay();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    const W = container.clientWidth;
    const H = container.clientHeight;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    // Once trade-decision overlays exist, suppress the generic bucket reference
    // lines (the tiny per-candle VAH/VAL segments). The decision overlay now
    // draws the exact primary zone used at entry.
    const _allDecisionTrades = [
        ...((backtestData && backtestData.trades) ? backtestData.trades : []),
        ...(window._liveCompletedTrades || []),
        ...(window._liveWorkingDecisionTrade ? [window._liveWorkingDecisionTrade] : []),
    ];
    if (_allDecisionTrades.length > 0) return;

    const tfs = _zoneFilter.tfs;
    if (!tfs || tfs.size === 0) return;
    if (!_tfAllZones || _tfAllZones.length === 0) {
        // Retry only after the throttle window. refreshTfZones() renders once
        // in its finally block, so an immediate retry here would recurse when
        // the response contains an empty zone list.
        if (!_tfZonesFetching && Date.now() - _tfZonesLastFetch >= 15000) {
            refreshTfZones();
        }
        return;
    }

    // Combined view (no filter): every completed reference zone is drawn as a
    // "backtest" line at 80% opacity, and the most-recent completed zone per TF
    // (the one the live engine is currently trading against) is redrawn on top
    // at 100% opacity in orange.
    const BT_COLOR = '255, 255, 255';   // backtest reference zones (dim)
    const LIVE_COLOR = '255, 165, 0';   // current/live zone (bright)
    const rightX = W - 60;

    const priceToY = (p) => { try { const y = candleSeries.priceToCoordinate(p); return y !== null ? y : -1; } catch (e) { return -1; } };
    const tX = (iso) => { if (!iso) return null; try { return chart.timeScale().timeToCoordinate(isoToChartTime(iso)); } catch (e) { return null; } };

    // Visible time range for viewport culling
    let vFrom = null, vTo = null;
    try { const vr = chart.timeScale().getVisibleRange(); if (vr) { vFrom = vr.from; vTo = vr.to; } } catch (e) {}

    TF_ORDER.forEach(tf => {
        if (!tfs.has(tf)) return;
        const zonesTf = _tfAllZones.filter(z => z.timeframe === tf);
        if (zonesTf.length === 0) return;
        const lw = TF_LINE_WIDTH[tf] || 1.5;

        // All completed zones in the viewport (backtest layer, 80% opacity).
        const completed = zonesTf.filter(z => z.status === 'left');
        const inView = [];
        completed.forEach(z => {
            const f = isoToChartTime(z.formed_at);
            const l = z.left_at ? isoToChartTime(z.left_at) : f;
            if (vFrom !== null && vTo !== null && (l < vFrom || f > vTo)) return;
            inView.push(z);
        });

        // Most-recent completed zone = current/live reference (100% opacity).
        let liveZone = null;
        if (completed.length) {
            const sorted = completed.slice().sort((a, b) => ((a.left_at || '') < (b.left_at || '')) ? 1 : -1);
            liveZone = sorted[0];
        }

        inView.forEach(z => {
            if (z === liveZone) return;   // drawn brighter below
            _drawZoneLines(ctx, z, tf, lw, BT_COLOR, 0.8, W, H, rightX, priceToY, tX);
        });
        if (liveZone) _drawZoneLines(ctx, liveZone, tf, lw, LIVE_COLOR, 1.0, W, H, rightX, priceToY, tX);
    });
}

// Back-compat shim: callers still pass single-TF zones; the actual rendering is
// filter-driven from the all-TF cache. Keep _cachedVPZones for legacy redraw paths.
function drawVolumeProfile(zones) {
    if (zones && zones.length) _cachedVPZones = zones;
    renderTfZones();
}


function clearVPOverlay() {
    if (vpOverlayCanvas) {
        const ctx = vpOverlayCanvas.getContext('2d');
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, vpOverlayCanvas.width, vpOverlayCanvas.height);
    }
}

// -- Decision-zone overlay --
// Draw only the primary VAH/VAL range used by each trade decision.

let posToolCanvas = null;

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
    clearPositionOverlay();
    if (!trades || trades.length === 0) return;

    const canvas = createPosToolCanvas();
    const container = document.getElementById('chart-container');
    const dpr = window.devicePixelRatio || 1;
    canvas.width = container.clientWidth * dpr;
    canvas.height = container.clientHeight * dpr;
    canvas.style.width = container.clientWidth + 'px';
    canvas.style.height = container.clientHeight + 'px';
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, container.clientWidth, container.clientHeight);

    const chartW = container.clientWidth;
    const chartH = container.clientHeight;

    // Only draw trades whose entry is visible in the current viewport
    let drawn = 0;
    const maxDraw = 25; // limit to avoid clutter

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

        let x0 = null, x1 = null;
        try { if (z.formed_at) x0 = chart.timeScale().timeToCoordinate(isoToChartTime(z.formed_at)); } catch(_) {}
        try { if (z.left_at) x1 = chart.timeScale().timeToCoordinate(isoToChartTime(z.left_at)); } catch(_) {}
        if (x0 === null) x0 = fallbackEntryX - 80;
        if (x1 === null || x1 <= x0) x1 = fallbackEntryX + 120;
        if (x1 < -20 || x0 > chartW + 20) return false;
        x0 = Math.max(0, x0);
        x1 = Math.min(chartW - 60, x1);
        if (x1 <= x0 + 4) return false;

        const top = Math.min(yVAH, yVAL);
        const h = Math.abs(yVAL - yVAH);
        ctx.fillStyle = 'rgba(255, 255, 255, 0.055)';
        ctx.fillRect(x0, top, x1 - x0, h);
        drawHLine(x0, x1, yVAH, 'rgba(255, 255, 255, 0.78)');
        drawHLine(x0, x1, yVAL, 'rgba(255, 255, 255, 0.78)');
        return true;
    };

    const tradePrice = (t, keys) => {
        for (const k of keys) {
            const n = Number(t && t[k]);
            if (Number.isFinite(n)) return n;
        }
        return NaN;
    };

    const drawRiskRewardBox = (t, entryX) => {
        const entry = tradePrice(t, ['entry_price', 'entry']);
        const sl = tradePrice(t, ['original_sl_price', 'sl_price', 'sl']);
        const tp = tradePrice(t, ['original_tp_price', 'tp_price', 'tp']);
        if (!Number.isFinite(entry) || !Number.isFinite(sl) || !Number.isFinite(tp)) return false;

        const yEntry = candleSeries.priceToCoordinate(entry);
        const ySL = candleSeries.priceToCoordinate(sl);
        const yTP = candleSeries.priceToCoordinate(tp);
        if (yEntry === null || ySL === null || yTP === null) return false;

        let x1 = null;
        try { if (t.exit_time) x1 = chart.timeScale().timeToCoordinate(isoToChartTime(t.exit_time)); } catch (_) {}
        if (x1 === null || x1 <= entryX + 3) x1 = entryX + 120;
        if (x1 < -20 || entryX > chartW + 20) return false;
        const x0 = Math.max(0, entryX);
        const xEnd = Math.min(chartW - 60, x1);
        if (xEnd <= x0 + 3) return false;

        const greenTop = Math.min(yEntry, yTP);
        const greenH = Math.abs(yTP - yEntry);
        const redTop = Math.min(yEntry, ySL);
        const redH = Math.abs(ySL - yEntry);

        ctx.save();
        ctx.setLineDash([]);
        if (greenH > 1) {
            ctx.fillStyle = 'rgba(0, 229, 160, 0.105)';
            ctx.fillRect(x0, greenTop, xEnd - x0, greenH);
        }
        if (redH > 1) {
            ctx.fillStyle = 'rgba(255, 64, 96, 0.115)';
            ctx.fillRect(x0, redTop, xEnd - x0, redH);
        }
        drawHLine(x0, xEnd, yEntry, 'rgba(255, 167, 38, 0.74)');
        drawHLine(x0, xEnd, yTP, 'rgba(0, 229, 160, 0.82)');
        drawHLine(x0, xEnd, ySL, 'rgba(255, 64, 96, 0.82)');
        ctx.restore();
        return true;
    };

    trades.forEach((t) => {
        if (drawn >= maxDraw) return;

        const entryTime = isoToChartTime(t.entry_time);
        const entryX = chart.timeScale().timeToCoordinate(entryTime);
        if (entryX === null) return;
        if (entryX < -200 || entryX > chartW + 50) return;

        const drewRisk = drawRiskRewardBox(t, entryX);
        const drewZone = drawPrimaryZone(t, entryX);
        if (!drewRisk && !drewZone) return;

        drawn++;
    });
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

            // Re-render metrics comparison if backtest already ran
            if (backtestData && backtestData.metrics) {
                renderMetrics(backtestData.metrics, backtestData.trades);
            }

            // setMarkers handles all viewports; the canvas overlay is redrawn with
            // both backtest and completed live decisions so SL/TP/zone style stays unified.
            drawLiveTradeMarkers(trades);
            if (_overlaySyncData) {
                if (_cachedVPZones) drawVolumeProfile(_cachedVPZones);
                redrawTradeDecisionOverlays();
                drawSessionDividers();
            }
        } else {
            window._liveCompletedTrades = [];
            _calLiveTrades = [];
            renderExecuteTrades([]);
            drawLiveTradeMarkers([]);
            if (_overlaySyncData) {
                redrawTradeDecisionOverlays();
                drawSessionDividers();
            }
            if (backtestData && backtestData.metrics) {
                renderMetrics(backtestData.metrics, backtestData.trades);
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

async function refreshTradeHistoryForCurrentAccount(refresh) {
    if (_tradeHistoryRefreshInFlight) return;
    _tradeHistoryRefreshInFlight = true;
    try {
        const accId = (liveAccount && liveAccount.id) ? liveAccount.id : (currentAccount ? currentAccount.id : 0);
        await fetchAndDrawTradeHistory(refresh, accId);
    } finally {
        _tradeHistoryRefreshInFlight = false;
    }
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
    // Clear old price lines
    zoneRectangles.forEach(l => { try { candleSeries.removePriceLine(l); } catch(e){} });
    zoneRectangles = [];

    if (!zones || zones.length === 0) return;

    // Draw VP histogram + POC/VAH/VAL lines on full-chart canvas overlay
    drawVolumeProfile(zones);
}

// -- API Calls -------------------------------------

async function checkHealth() {
    try {
        const resp = await fetch(API + '/health');
        const data = await resp.json();
        setStatus(data.status === 'ok' ? 'ok' : 'err', data.status === 'ok' ? 'ONLINE' : 'ERROR');
    } catch(e) {
        setStatus('err', 'OFFLINE');
    }
}

async function connectAPI() {
    const btn = document.getElementById('btn-connect');
    const username = document.getElementById('username').value.trim();
    const apikey = document.getElementById('apikey').value.trim();
    const contractId = document.getElementById('contract-id').value.trim();
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"><span></span><span></span><span></span><span></span></span> CONNECTING...';
    setStatus('loading', 'CONNECTING...');
    log('Connecting to TopstepX API...', 'info');

    // CONNECT loads only the recent warm-up window (CONNECT_WARMUP_DAYS) so the
    // app is interactive immediately. The full multi-year range is pulled lazily
    // on the first backtest / ML / LEARN & LIVE click (_ensureBacktestData).
    const _now = new Date();
    const startDate = new Date(_now.getTime() - CONNECT_WARMUP_DAYS * 86400000)
        .toISOString().slice(0, 10);
    const endDate   = _now.toISOString().slice(0, 10);

    // If CONTRACT ID is blank (auto-detect), fall back to the CONTRACT dropdown so
    // we fetch the right instrument (MNQ vs ENQ) instead of a generic guess.
    const presetSel = document.getElementById('contract-preset');
    let resolvedContract = contractId;
    if (!resolvedContract && presetSel && presetSel.value) resolvedContract = presetSel.value;

    const body = {
        unit: 2,
        unit_number: 1,
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
        document.getElementById('data-count').value = data.candles_count + ' bars';
        document.getElementById('btn-backtest').disabled = false;
        const btnRunAll = document.getElementById('btn-run-all');
        if (btnRunAll) btnRunAll.disabled = false;
        const btnFullFilter = document.getElementById('btn-full-filter');
        if (btnFullFilter) btnFullFilter.disabled = false;
        _updateDataInfo(data.first, data.last, 'conn', data.candles_count);
        // CONNECT only loaded the recent warm-up window → record it so the first
        // backtest / ML / LEARN sees the range mismatch and pulls the full history.
        _btDataRange = { start: startDate, end: endDate, contract: resolvedContract || '' };

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
        const accId = currentAccount ? currentAccount.id : 0;
        await fetchAndDrawTradeHistory(true, accId);

    } catch(e) {
        setStatus('err', 'FAILED');
        log('Connection failed: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'CONNECT';
    }
}

async function fetchAndShowChart(interval) {
    try {
        // Only fetch the most recent slice for charting — the full range can be
        // hundreds of thousands of 1m bars and rendering them all freezes the tab.
        // Backtest / machine learning still use the full backend dataset.
        const resp = await fetch(API + '/data/candles?limit=' + CHART_MAX_CANDLES);
        const data = await resp.json();
        if (data.candles && data.candles.length > 0) {
            showCandleData(data.candles);
            const shown = data.shown != null ? data.shown : data.candles.length;
            log('Chart showing ' + shown + ' / ' + data.count + ' candles (recent slice)', 'info');
            return;
        }
    } catch(e) {}

    log('No candle data available -- click CONNECT to fetch historical data', 'info');
}

function showCandleData(candles) {
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

    candleSeries.setData(chartData);
    window._lastChartData = chartData;

    applyDefaultChartView(chartData);
    drawSessionDividers();
    // Populate the all-timeframe zone cache so the filter draws lines immediately.
    refreshTfZones(true);
}

function applyDefaultChartView(chartData, zones) {
    if (!chartData || chartData.length === 0) {
        chart.timeScale().fitContent();
        return;
    }

    // Determine candle interval in seconds
    let intervalSec = 60; // default 1min
    if (chartData.length >= 2) {
        intervalSec = chartData[1].time - chartData[0].time;
        if (intervalSec <= 0) intervalSec = 60;
    }

    // 18 hours visible width (12h * 1.5), with 40% empty right
    const totalVisibleBars = Math.round((18 * 3600) / intervalSec);
    const dataBars = Math.round(totalVisibleBars * 0.6);
    const emptyRight = totalVisibleBars - dataBars;
    const totalBars = chartData.length;

    const fromIdx = Math.max(0, totalBars - dataBars);
    const toIdx = totalBars + emptyRight;

    chart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: toIdx });

    // Center on current price (last close)
    const centerPrice = chartData[chartData.length - 1].close;

    // Height = 4x of VAH-VAL range; fallback to 300 pts if no zone
    let halfRange = 150; // default fallback
    if (zones && zones.length > 0) {
        const activeZone = zones.find(z => z.status === 'active');
        const refZone = activeZone || zones[zones.length - 1];
        if (refZone && refZone.vah_80 && refZone.val_80) {
            const vahValRange = Math.abs(refZone.vah_80 - refZone.val_80);
            if (vahValRange > 0) {
                halfRange = (vahValRange * 4) / 2; // 4x range, half on each side
            }
        }
    }

    candleSeries.applyOptions({
        autoscaleInfoProvider: () => ({
            priceRange: {
                minValue: centerPrice - halfRange,
                maxValue: centerPrice + halfRange,
            },
        }),
    });
}

function buildBacktestBody() {
    const params = collectStrategyParams('bt');
    // v1.0.6: ML (confluence, explainable) backtest is selected via the STRATEGY dropdown.
    const confParams = collectConfluenceParams('bt');
    if (confParams) {
        params.strategy = 'confluence';
        Object.assign(params, confParams);
    }
    return {
        initial_capital: 50000,
        ...params,
    };
}

// ── Data range indicator ───────────────────────────
// Shows what's actually in _historical_candles: source (CONN=14d, BT=full range), dates, bar count.
function _updateDataInfo(first, last, source, barCount) {
    const el = document.getElementById('data-range-info');
    if (!el || !first || !last) return;
    const fmt = iso => {
        const d = new Date(iso);
        return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    };
    const days  = Math.max(1, Math.round((new Date(last) - new Date(first)) / 86400000));
    const color = source === 'conn' ? 'var(--cyan)' : 'var(--green)';
    const label = source === 'conn' ? 'CONN' : 'BT';
    const bars  = barCount ? ` · ${barCount.toLocaleString()} bars` : '';
    el.style.display = 'block';
    el.innerHTML = `<span style="color:${color};font-weight:600">${label}</span>&nbsp;·&nbsp;${fmt(first)}&nbsp;→&nbsp;${fmt(last)}&nbsp;·&nbsp;${days}d${bars}`;
}

// ── Backtest data lazy-loader ──────────────────────
// Tracks which date range is currently loaded in the backend.
// CONNECT only loads 14 days (fast); full range is fetched on first backtest or Machine Learning click.
let _btDataRange = null;  // { start, end, contract } once loaded for backtest

function _profitLockBoundaryISO(dateStr) {
    const parts = String(dateStr || '').split('-').map(Number);
    if (parts.length !== 3 || parts.some(n => !Number.isFinite(n))) return dateStr + 'T00:00:00Z';
    // Follow Topstep/CME trading-day boundary: California 3:15pm (a ~1h
    // maintenance gap follows, so the prior trading day is complete by then).
    return new Date(parts[0], parts[1] - 1, parts[2], 15, 15, 0, 0).toISOString();
}

async function _ensureBacktestData(btn, overrideStart, overrideEnd, force) {
    // overrideStart / overrideEnd let callers use a fixed window.
    // instead of the date pickers. Falls back to date pickers when omitted.
    // force=true → re-pull the WHOLE range even if already loaded (used by the
    // FETCH FULL DATA button to recover candles dropped by a wifi disconnect).
    const startDate = overrideStart || document.getElementById('start-date').value;
    const endDate   = overrideEnd   || document.getElementById('end-date').value;
    if (force) _btDataRange = null;
    const username   = document.getElementById('username').value.trim();
    const apikey     = document.getElementById('apikey').value.trim();
    // Fall back to the CONTRACT dropdown when the ID box is blank (auto-detect),
    // so merge chains the right instrument (MNQ vs ENQ).
    const _presetSel = document.getElementById('contract-preset');
    const contractId = document.getElementById('contract-id').value.trim()
        || (_presetSel && _presetSel.value) || '';
    const sameContract = _btDataRange && (_btDataRange.contract || '') === (contractId || '');

    // Already loaded for this exact range → skip fetch
    if (sameContract && _btDataRange.start === startDate && _btDataRange.end === endDate) {
        return true;
    }

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

    const body = { unit: 2, unit_number: 1,   // always 1m bars for backtest / machine learning
        start_time: fetchStartTime,
        end_time:   endDate   + 'T23:59:59Z',
        append: appendFetch,
        continuous_contract: true,
        force_full: !!force };
    if (username)    body.username    = username;
    if (apikey)      body.api_key     = apikey;
    if (contractId)  body.contract_id = contractId;

    try {
        const resp = await fetch(API + '/data/fetch-historical', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) { const e = await resp.json(); throw new Error(e.detail || resp.statusText); }
        const data = await resp.json();
        document.getElementById('data-count').value = data.candles_count + ' bars';
        _btDataRange = { start: startDate, end: endDate, contract: contractId || '' };
        _updateDataInfo(data.first, data.last, 'bt', data.candles_count);
        if (data.contracts && data.contracts.length > 1) {
            log('Continuous contract: ' + data.contracts.map(contractLabelFromId).join(' + '), 'info');
        }
        if (data.continuous && data.continuous.roll_at) {
            const adj = Number(data.continuous.price_adjustment || 0);
            log('Roll adjusted @ ' + data.continuous.roll_at + ' | old contract offset ' + adj.toFixed(2), 'info');
        }
        const storeTag = data.from_store ? ' [local store + incremental]' : '';
        log('Backtest data ready: ' + data.candles_count + ' bars' + (data.fetched_count != null ? ' (' + data.fetched_count + ' fetched)' : '') + storeTag, 'success');
        // Refresh chart to show the full loaded range
        await fetchAndShowChart('1m');
        return true;
    } catch(e) {
        log('Data fetch failed: ' + e.message, 'error');
        return false;
    }
}

// FETCH FULL DATA button — force a complete re-pull of the whole range. Use
// this when a wifi drop left holes in the data (incremental sync only adds the
// tail, so it never backfills an interior gap; a full re-pull does).
async function fetchFullData() {
    const btn = document.getElementById('btn-fetch-full');
    if (!btn || btn.disabled) return;
    const orig = btn.textContent;
    btn.disabled = true;
    log('Force-fetching FULL data range (recovering any missing candles)...', 'info');
    try {
        await _ensureBacktestData(btn, FULL_RANGE_START,
            document.getElementById('end-date').value, true);
    } finally {
        btn.disabled = false;
        btn.textContent = orig;
    }
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

async function runBacktest() {
    const btn = document.getElementById('btn-backtest');
    btn.disabled = true;
    let succeeded = false;

    // Lazy-load full date range before running
    const dataOk = await _ensureBacktestData(btn);
    if (!dataOk) { btn.disabled = false; btn.textContent = 'EXECUTE BACKTEST'; return; }

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

    try {
        const resp = await fetch(API + '/backtest/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(btBody)
        });

        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || resp.statusText);
        }

        backtestData = await resp.json();

        log('Backtest complete // ' + backtestData.metrics.total_trades + ' trades // ' +
            'Win rate: ' + (backtestData.metrics.win_rate * 100).toFixed(1) + '% // ' +
            'PnL: $' + backtestData.metrics.total_pnl.toFixed(0), 'success');

        // Clear old cached zone overlay before rendering new backtest
        _cachedVPZones = null;

        renderChart(backtestData);
        renderMetrics(backtestData.metrics, backtestData.trades);
        renderTrades(backtestData.trades);
        _saveBacktestCache(backtestData);   // persist for next app restart
        if (!document.getElementById('btab-pnl').classList.contains('hidden')) renderPnlCurve();
        await refreshTradeHistoryForCurrentAccount(true);
        succeeded = true;

    } catch(e) {
        log('Backtest failed: ' + e.message, 'error');
    } finally {
        _stopBacktestProgress(succeeded);
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
let _backtestMarkers = [];
let _liveMarkers = [];
let _liveRealtimeMarkers = [];

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
}

function _setLiveRealtimeMarkers(markers) {
    _liveRealtimeMarkers = (markers || []).filter(m => m && m.time && !isNaN(m.time));
    _refreshAllMarkers();
}

function renderChart(data) {
    if (!data) { log('No backtest data to render', 'warn'); return; }

    // Backtest finished → switch the zone filter to BT and refresh the all-TF cache.
    _zoneFilter.mode = 'backtest';
    syncZoneFilterUI();
    refreshTfZones(true);

    // Draw zones (VP overlay + legend)
    drawBacktestZones(data.zones);

    // Draw decision overlays (entry marker + primary VAH/VAL zone)
    drawPositionTools([...(data.trades || []), ...(window._liveCompletedTrades || [])]);
    drawTradeMarkers(data.trades);

    // Apply default chart view with POC centering from zones
    // Reconstruct chartData from the stored candle data
    if (window._lastChartData) {
        applyDefaultChartView(window._lastChartData, data.zones);
    } else {
        chart.timeScale().fitContent();
    }

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
            const zonesToDraw = _cachedVPZones || data.zones;
            if (zonesToDraw) drawVolumeProfile(zonesToDraw);
            redrawTradeDecisionOverlays();
            drawSessionDividers();
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
    const localOffsetSec = d.getTimezoneOffset() * -60;
    return Math.floor(d.getTime() / 1000) + localOffsetSec;
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
        shape: 'circle',
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
    renderTfZones();
    redrawTradeDecisionOverlays();
}

function clearLiveWorkingDecision() {
    if (!window._liveWorkingDecisionTrade && !window._liveWorkingDecisionKey) return;
    window._liveWorkingDecisionTrade = null;
    window._liveWorkingDecisionKey = null;
    window._liveWorkingDecisionTs = null;
    renderTfZones();
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

const SESSION_CODES = ['ASIA', 'EURO', 'PRE', 'RTH', 'AH'];
const TOPSTEP_TRADE_TZ = 'America/Chicago';
const TOPSTEP_TRADE_DAY_START_HOUR_CT = 17;

function _dateKeyFromUtcParts(y, m, d) {
    return y + '-' + String(m).padStart(2, '0') + '-' + String(d).padStart(2, '0');
}

function _timePartsInZone(date, timeZone) {
    const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: timeZone,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        hour12: false,
        hourCycle: 'h23',
    }).formatToParts(date);
    const out = {};
    parts.forEach(p => { if (p.type !== 'literal') out[p.type] = p.value; });
    return {
        year: parseInt(out.year, 10),
        month: parseInt(out.month, 10),
        day: parseInt(out.day, 10),
        hour: parseInt(out.hour, 10),
    };
}

function topstepTradeDateKey(value) {
    const d = value instanceof Date ? value : new Date(value);
    if (!d || isNaN(d.getTime())) return null;
    const p = _timePartsInZone(d, TOPSTEP_TRADE_TZ);
    if (![p.year, p.month, p.day, p.hour].every(Number.isFinite)) return null;
    const shifted = new Date(Date.UTC(
        p.year,
        p.month - 1,
        p.day + (p.hour >= TOPSTEP_TRADE_DAY_START_HOUR_CT ? 1 : 0)
    ));
    return _dateKeyFromUtcParts(
        shifted.getUTCFullYear(),
        shifted.getUTCMonth() + 1,
        shifted.getUTCDate()
    );
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
    const h = d.getUTCHours();
    const m = d.getUTCMinutes();
    if (h >= 22 || h < 7) return 'ASIA';
    if (h < 11) return 'EURO';
    if (h < 13 || (h === 13 && m < 30)) return 'PRE';
    if (h < 20) return 'RTH';
    return 'AH';
}

function getTradeExitBucket(trade) {
    const reason = ((trade && trade.exit_reason) || '').toLowerCase();
    if (reason === 'tp' || reason === 'tp_4r') return 'tp';
    if (reason === 'trail_sl') return 'trail_sl';
    if (reason === 'sl' || reason === 'be_sl') return 'sl';
    return 'other';
}

function summarizeTradeOutcomes(trades) {
    const summary = {
        total: 0,
        tp: 0, sl: 0, trail_sl: 0, other: 0,
        // Sum of PnL per bucket — used to compute avg $ per exit type
        tp_pnl: 0, sl_pnl: 0, trail_sl_pnl: 0, other_pnl: 0,
        sessions: {},
    };
    SESSION_CODES.forEach(code => {
        summary.sessions[code] = { total: 0, tp: 0, sl: 0, trail_sl: 0 };
    });

    (trades || []).forEach(trade => {
        summary.total += 1;
        const bucket = getTradeExitBucket(trade);
        const pnl = +(trade && trade.pnl) || 0;
        if (bucket === 'tp')            { summary.tp += 1;        summary.tp_pnl += pnl; }
        else if (bucket === 'sl')       { summary.sl += 1;        summary.sl_pnl += pnl; }
        else if (bucket === 'trail_sl') { summary.trail_sl += 1;  summary.trail_sl_pnl += pnl; }
        else                            { summary.other += 1;     summary.other_pnl += pnl; }

        const refIso = trade && (trade.entry_time || trade.exit_time);
        if (!refIso) return;
        const sessionCode = getSessionCodeFromDate(new Date(refIso));
        if (!sessionCode || !summary.sessions[sessionCode]) return;
        summary.sessions[sessionCode].total += 1;
        if (bucket === 'tp') summary.sessions[sessionCode].tp += 1;
        else if (bucket === 'sl') summary.sessions[sessionCode].sl += 1;
        else if (bucket === 'trail_sl') summary.sessions[sessionCode].trail_sl += 1;
    });

    return summary;
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
            tp: 0, sl: 0, trail_sl: 0, other: 0, session_tp: {},
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
    const exitSummary = summarizeTradeOutcomes(sorted);
    const count = exitSummary.total;
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
        tp: exitSummary.tp,
        sl: exitSummary.sl,
        trail_sl: exitSummary.trail_sl,
        other: exitSummary.other,
        // Avg PnL per exit bucket — exposes the huge $ difference between TP / TRAIL / SL
        avg_tp_pnl:       exitSummary.tp       ? exitSummary.tp_pnl       / exitSummary.tp       : 0,
        avg_sl_pnl:       exitSummary.sl       ? exitSummary.sl_pnl       / exitSummary.sl       : 0,
        avg_trail_sl_pnl: exitSummary.trail_sl ? exitSummary.trail_sl_pnl / exitSummary.trail_sl : 0,
        session_tp: exitSummary.sessions,
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

function renderMetrics(m, backtestTrades) {
    const panel = document.getElementById('metrics-panel');
    // Only show the metrics panel when the BACKTEST tab is active.
    // If user is on LIVE, keep it hidden — data still gets rendered into the
    // grid below and will appear next time they switch back to BACKTEST.
    const backtestActive = document.querySelector('.tab.active')?.dataset?.tab === 'backtest';
    panel.style.display = 'block';
    if (backtestActive) {
        panel.classList.remove('hidden');
        // Scroll the sidebar so the newly-rendered panel is actually in view
        setTimeout(() => {
            try { panel.scrollIntoView({behavior: 'smooth', block: 'nearest'}); } catch(_){}
        }, 0);
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
    const total_pnl    = windowed ? backtestStats.total_pnl  : m.total_pnl;
    const total_gain   = windowed ? backtestStats.total_gain : (m.total_gain != null ? m.total_gain : backtestStats.total_gain);
    const total_loss   = windowed ? backtestStats.total_loss : (m.total_loss != null ? m.total_loss : backtestStats.total_loss);
    const total_trades = windowed ? backtestStats.trades     : m.total_trades;
    const rr_ratio     = windowed ? backtestStats.rr_ratio   : m.avg_rr_ratio;
    const max_dd       = windowed ? backtestStats.max_dd     : m.max_drawdown;
    const calmarPrimary = windowed ? (backtestStats.calmar || 0) : (m.calmar_ratio || 0);
    const activeDaily  = windowed ? (backtestStats.daily_pnl || {}) : (m.daily_pnl || {});

    // Day span of the active window (drives the FINAL PNL card label)
    const daySpan = backtestStats.days;
    const totalPnlLabel = daySpan > 0 ? ('FINAL PNL (' + daySpan + 'd)') : 'FINAL PNL';

    const paren = (v) => liveStats ? ' <span class="metric-real">(' + v + ')</span>' : '';
    const fmtSessionTriple = (stats, code) => {
        const s = stats && stats.session_tp ? stats.session_tp[code] : null;
        if (!s || !s.total) return '--';
        return fmtTpSlTrail(
            ((s.tp / s.total) * 100).toFixed(0) + '%',
            ((s.sl / s.total) * 100).toFixed(0) + '%',
            ((s.trail_sl / s.total) * 100).toFixed(0) + '%'
        );
    };

    // Profit Factor = gross gain / gross loss. PF>1 profitable, >2 strong.
    const profitFactorOf = (gain, loss) => Math.abs(loss || 0) > 0 ? Math.abs(gain || 0) / Math.abs(loss) : (gain > 0 ? Infinity : 0);
    const pfPrimary = windowed ? profitFactorOf(total_gain, total_loss)
                               : ((m.profit_factor != null) ? m.profit_factor : profitFactorOf(total_gain, total_loss));
    const pfLive = liveStats ? (liveStats.profit_factor != null ? liveStats.profit_factor : profitFactorOf(liveStats.total_gain, liveStats.total_loss)) : null;
    const fmtPF = (v) => (Number.isFinite(v) && v < 999) ? v.toFixed(2) : '∞';

    // Exit-path distribution — % of trades that exited via each reason (NOT win rate)
    const fmtTpSlTrail = (tp, sl, trail) => (
        '<span style="color:var(--green);">' + tp + '</span>/' +
        '<span style="color:var(--red);">' + sl + '</span>/' +
        '<span style="color:var(--white);">' + trail + '</span>'
    );
    const fmtPctTriple = (s) => {
        const t = s && s.trades ? s.trades : 0;
        if (!t) return '--';
        const tp = ((s.tp / t) * 100).toFixed(0);
        const sl = ((s.sl / t) * 100).toFixed(0);
        const tr = ((s.trail_sl / t) * 100).toFixed(0);
        return fmtTpSlTrail(tp + '%', sl + '%', tr + '%');
    };
    // Avg $ per exit bucket — shows the magnitude gap between full TP, trail SL, full SL
    const fmtAvgTriple = (s) => {
        if (!s || !s.trades) return '--';
        const sign = (v) => (v >= 0 ? '+' : '') + Math.round(v);
        return fmtTpSlTrail(sign(s.avg_tp_pnl || 0), sign(s.avg_sl_pnl || 0), sign(s.avg_trail_sl_pnl || 0));
    };
    const fmtZoneExitBuckets = (stats) => {
        if (!stats || !stats.trades) return '--';
        return fmtPctTriple(stats);
    };
    const currentZoneStats = _computeTradeStats((windowed ? windowedTrades : allTrades).filter(t => t.zone_source === 'current'));

    // Week-to-week variation: σ of weekly PnL, with consistency (% of green weeks).
    // Lower CV + higher consistency = steadier equity curve, less luck-dependent.
    const wk = windowed ? _weeklyFromDaily(activeDaily) : (m.weekly_stats || {});
    const wkCount = wk.weekly_count || 0;
    const wkStd = wk.weekly_std != null ? wk.weekly_std : 0;
    const wkCv = wk.weekly_cv != null ? wk.weekly_cv : 0;
    const wkConsist = wk.weekly_consistency != null ? wk.weekly_consistency : 0;
    const weeklyVarItem = {
        label: 'WEEKLY VARIATION (σ/CV)',
        value: wkCount > 0
            ? ('$' + Math.round(wkStd) + ' / ' + wkCv.toFixed(2) +
               ' <span class="metric-real">(' + Math.round(wkConsist * 100) + '% green, ' +
               wkCount + 'w)</span>')
            : '--',
        // Steady = low CV. Flag green when weekly CV < 1 and most weeks positive.
        cls: wkCount > 0 ? ((wkCv < 1 && wkConsist >= 0.6) ? 'pos' : (wkCv > 2 ? 'neg' : '')) : '',
    };

    const items = [
        { label: totalPnlLabel,
          value: '$' + total_pnl.toFixed(0) + paren(liveStats ? '$' + liveStats.total_pnl.toFixed(0) : ''),
          cls: total_pnl >= 0 ? 'pos' : 'neg' },
        { label: 'MAX DD',
          value: '$' + max_dd.toFixed(0) + paren(liveStats ? '$' + liveStats.max_dd.toFixed(0) : ''),
          cls: max_dd > 0 ? 'neg' : '' },
        { label: 'TOTAL GAIN',
          value: '$' + total_gain.toFixed(0) + paren(liveStats ? '$' + liveStats.total_gain.toFixed(0) : ''),
          cls: total_gain > 0 ? 'pos' : '' },
        { label: 'TOTAL LOSS',
          value: '$' + total_loss.toFixed(0) + paren(liveStats ? '$' + liveStats.total_loss.toFixed(0) : ''),
          cls: total_loss < 0 ? 'neg' : '' },
        { label: 'PROFIT FACTOR',
          value: fmtPF(pfPrimary) + paren(pfLive != null ? fmtPF(pfLive) : ''),
          cls: pfPrimary >= 1 ? 'pos' : 'neg' },
        { label: 'CALMAR',
          value: calmarPrimary.toFixed(2) + paren(liveStats ? (liveStats.calmar != null ? liveStats.calmar : 0).toFixed(2) : ''),
          cls: calmarPrimary >= 1 ? 'pos' : '' },
        { label: 'TRADE COUNTS',
          value: String(total_trades || 0) + paren(liveStats ? String(liveStats.trades || 0) : ''),
          cls: '' },
        { label: 'WIN RATE',
          value: ((backtestStats.win_rate || 0) * 100).toFixed(1) + '%' + paren(liveStats ? ((liveStats.win_rate || 0) * 100).toFixed(1) + '%' : ''),
          cls: (backtestStats.win_rate || 0) >= 0.5 ? 'pos' : '' },
        weeklyVarItem,
        { label: 'RR RATIO',
          value: rr_ratio.toFixed(2) + paren(liveStats ? liveStats.rr_ratio.toFixed(2) : ''),
          cls: rr_ratio > 1 ? 'pos' : 'neg' },
        { label: 'EXIT % TP/SL/TRAIL',
          value: fmtPctTriple(backtestStats) + paren(liveStats ? fmtPctTriple(liveStats) : ''),
          cls: '' },
        { label: 'AVG $ TP/SL/TRAIL',
          value: fmtAvgTriple(backtestStats) + paren(liveStats ? fmtAvgTriple(liveStats) : ''),
          cls: '' },
        { label: 'CURRENT ZONE TP/SL/TRAIL',
          value: fmtZoneExitBuckets(currentZoneStats),
          cls: '' },
        { label: 'ASIA TP/SL/TRAIL', value: fmtSessionTriple(backtestStats, 'ASIA'), cls: '' },
        { label: 'EURO TP/SL/TRAIL', value: fmtSessionTriple(backtestStats, 'EURO'), cls: '' },
        { label: 'PRE TP/SL/TRAIL',  value: fmtSessionTriple(backtestStats, 'PRE'), cls: '' },
        { label: 'RTH TP/SL/TRAIL',  value: fmtSessionTriple(backtestStats, 'RTH'), cls: '' },
    ];

    grid.innerHTML = items.map(i => `
        <div class="metric-card">
            <div class="label">${i.label}</div>
            <div class="value ${i.cls}">${i.value}</div>
        </div>
    `).join('');
}

function renderTrades(trades) {
    const tbody = document.getElementById('trades-tbody');
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;color:var(--text2);padding:20px;">NO TRADE DATA</td></tr>';
        return;
    }

    // Sort newest → oldest, like TopstepX journal
    const sorted = [...trades].sort((a, b) => {
        const ta = a.entry_time ? new Date(a.entry_time).getTime() : 0;
        const tb = b.entry_time ? new Date(b.entry_time).getTime() : 0;
        return tb - ta;
    });
    const totalN = sorted.length;

    const fmtTime = (iso) => {
        if (!iso) return '--';
        const d = new Date(iso);
        // YYYY-MM-DD HH:MM:SS (drop microseconds)
        const p = (n) => n < 10 ? '0' + n : '' + n;
        return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) +
            ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    };

    const fmtDuration = (entryIso, exitIso) => {
        if (!entryIso || !exitIso) return '--';
        const diffMs = new Date(exitIso).getTime() - new Date(entryIso).getTime();
        if (diffMs < 0) return '--';
        if (diffMs === 0) return '<1m';
        const totalSec = Math.floor(diffMs / 1000);
        const hh = Math.floor(totalSec / 3600);
        const mm = Math.floor((totalSec % 3600) / 60);
        const ss = totalSec % 60;
        const p = (n) => n < 10 ? '0' + n : '' + n;
        return p(hh) + ':' + p(mm) + ':' + p(ss);
    };

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
        // Short trade ID (last 6 chars) so column stays narrow
        const id = t.trade_id ? String(t.trade_id).slice(-6) : (totalN - i);
        const grossStr = '$' + (grossPnl >= 0 ? '+' : '-') + Math.abs(grossPnl).toFixed(2);

        return '<tr>' +
            '<td style="color:var(--text2);">' + id + '</td>' +
            '<td style="width:48px;">' + symbol + '</td>' +
            '<td style="width:36px;text-align:right;">' + size + '</td>' +
            '<td style="font-family:\'IBM Plex Mono\',monospace;">' + fmtTime(t.entry_time) + '</td>' +
            '<td style="font-family:\'IBM Plex Mono\',monospace;">' + fmtTime(t.exit_time) + '</td>' +
            '<td>' + fmtDuration(t.entry_time, t.exit_time) + '</td>' +
            '<td>' + t.entry_price.toFixed(2) + '</td>' +
            '<td>' + (t.exit_price ? t.exit_price.toFixed(2) : '--') + '</td>' +
            '<td class="' + pnlClass + '">' + grossStr + '</td>' +
            '<td class="pnl-neg">$-' + commission.toFixed(2) + '</td>' +
            '<td class="pnl-neg">$-' + fees.toFixed(2) + '</td>' +
            '<td style="color:' + dirColor + ';">' + dirLabel + '</td>' +
        '</tr>';
    }).join('');
}

// Render real TopstepX trade history in the EXECUTE TRADES bottom tab
function renderExecuteTrades(trades) {
    const tbody = document.getElementById('execute-tbody');
    if (!tbody) return;
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;color:var(--text2);padding:20px;">NO EXECUTE TRADE DATA</td></tr>';
        return;
    }

    const sorted = [...trades].sort((a, b) => {
        const ta = a.entry_time ? new Date(a.entry_time).getTime() : 0;
        const tb = b.entry_time ? new Date(b.entry_time).getTime() : 0;
        return tb - ta;
    });

    const fmtTime = (iso) => {
        if (!iso) return '--';
        const d = new Date(iso);
        const p = (n) => n < 10 ? '0' + n : '' + n;
        return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) +
            ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    };
    const fmtDuration = (entryIso, exitIso) => {
        if (!entryIso || !exitIso) return '--';
        const diffMs = new Date(exitIso).getTime() - new Date(entryIso).getTime();
        if (diffMs < 0) return '--';
        if (diffMs === 0) return '<1m';
        const totalSec = Math.floor(diffMs / 1000);
        const hh = Math.floor(totalSec / 3600);
        const mm = Math.floor((totalSec % 3600) / 60);
        const ss = totalSec % 60;
        const p = (n) => n < 10 ? '0' + n : '' + n;
        return p(hh) + ':' + p(mm) + ':' + p(ss);
    };

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
        const id = t.trade_id ? String(t.trade_id).split('_')[0].slice(-6) : '--';
        const grossStr = '$' + (grossPnl >= 0 ? '+' : '-') + Math.abs(grossPnl).toFixed(2);

        return '<tr>' +
            '<td style="color:var(--text2);">' + id + '</td>' +
            '<td style="width:48px;">' + symbol + '</td>' +
            '<td style="width:36px;text-align:right;">' + size + '</td>' +
            '<td style="font-family:\'IBM Plex Mono\',monospace;">' + fmtTime(t.entry_time) + '</td>' +
            '<td style="font-family:\'IBM Plex Mono\',monospace;">' + fmtTime(t.exit_time) + '</td>' +
            '<td>' + fmtDuration(t.entry_time, t.exit_time) + '</td>' +
            '<td>' + (t.entry_price != null ? Number(t.entry_price).toFixed(2) : '--') + '</td>' +
            '<td>' + (t.exit_price != null ? Number(t.exit_price).toFixed(2) : '--') + '</td>' +
            '<td class="' + pnlClass + '">' + grossStr + '</td>' +
            '<td class="pnl-neg">$-' + commission.toFixed(2) + '</td>' +
            '<td class="pnl-neg">$-' + fees.toFixed(2) + '</td>' +
            '<td style="color:' + dirColor + ';">' + dirLabel + '</td>' +
        '</tr>';
    }).join('');
}

function classifyZoneType(z) {
    if (!z.formed_at) return '-';
    const code = getSessionCodeFromDate(new Date(z.formed_at));
    if (code === 'ASIA') return '亞盤';
    if (code === 'EURO') return '歐盤';
    if (code === 'PRE') return '盤前';
    if (code === 'RTH') return '早盤';
    if (code === 'AH') return '盤後';
    return '-';
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

function setStatus(type, text) {
    const dot = document.getElementById('api-status');
    const label = document.getElementById('api-status-text');
    dot.className = 'status-dot ' + type;
    label.textContent = text;
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

function _renderScorerCard(title, model) {
    if (!model) return '<div style="margin-bottom:14px;color:var(--text3);">' + title + ': （尚未訓練）</div>';
    if (model.error) return '<div style="margin-bottom:14px;color:var(--red);">' + title + ' 讀取失敗: ' + model.error + '</div>';
    const m = model.meta || {};
    const dropped = m.dropped_features || [];
    const top = model.weights || [];
    const rrIsTop = top.length && top[0].name === 'rr';
    const warn = rrIsTop
        ? '<div style="color:var(--red);font-weight:600;margin:4px 0;">⚠ rr 是最大權重 → 模型異常（server 可能用到舊程式碼，請重啟後重新 LEARN）</div>'
        : '';
    const kv = (label, val, cls) =>
        '<span style="margin-right:14px;">' + label + ': <b style="color:' + (cls || 'var(--text1)') + ';">' + val + '</b></span>';

    let meta = '<div style="margin:4px 0 6px;line-height:1.7;">';
    meta += kv('è¨“ç·´æ™‚é–“', _fmtTs(m.trained_at));
    meta += kv('來源', m.source || '--');
    meta += '<br>';
    meta += kv('æ•¸æ“šæ™‚é–“æ®µ', _fmtTs(m.data_start) + ' → ' + _fmtTs(m.data_end));
    meta += '<br>';
    meta += kv('樣本', (m.n_samples != null ? m.n_samples.toLocaleString() : '--'));
    meta += kv('勝率', (m.train_win_rate != null ? (m.train_win_rate * 100).toFixed(1) + '%' : '--'));
    meta += kv('train AUC', (m.train_auc != null ? m.train_auc.toFixed(3) : '--'));
    meta += kv('OOS AUC', (m.oos_auc != null ? m.oos_auc.toFixed(3) : '--'), 'var(--cyan)');
    meta += kv('OOS Brier', (m.oos_brier != null ? m.oos_brier.toFixed(3) : '--'));
    meta += kv('C', (m.C != null ? (+m.C).toFixed(3) : '--'));
    meta += '<br>';
    meta += kv('RR', (m.rr_grid ? ('grid ' + m.rr_grid.join('/')) : (m.cfg && m.cfg.rr != null ? m.cfg.rr : '--')));
    meta += kv('TFs', (m.timeframes || []).join('/'));
    meta += kv('dropped', dropped.length ? dropped.join(', ') : '無', dropped.length ? 'var(--amber)' : 'var(--text3)');
    meta += '</div>';

    let rows = '';
    for (const w of top) {
        const pos = w.weight >= 0;
        const isRr = w.name === 'rr';
        rows += '<tr style="' + (isRr && rrIsTop ? 'background:rgba(255,80,80,0.12);' : '') + '">'
            + '<td style="padding:2px 10px 2px 0;color:var(--text2);">' + w.name + '</td>'
            + '<td style="padding:2px 0;text-align:right;font-weight:600;color:' + (pos ? 'var(--green)' : 'var(--red)') + ';">'
            + (pos ? '+' : '') + w.weight.toFixed(4) + '</td></tr>';
    }
    const biasRow = '<tr><td style="padding:2px 10px 2px 0;color:var(--text3);">(bias)</td>'
        + '<td style="padding:2px 0;text-align:right;color:var(--text3);">'
        + (model.bias != null ? (model.bias >= 0 ? '+' : '') + (+model.bias).toFixed(4) : '--') + '</td></tr>';

    return '<div style="margin-bottom:18px;">'
        + '<div style="color:var(--cyan);font-weight:600;margin-bottom:2px;">' + title + '</div>'
        + warn + meta
        + '<table style="border-collapse:collapse;min-width:240px;">' + rows + biasRow + '</table>'
        + '<div style="color:var(--text3);font-size:10px;margin-top:4px;">' + (model.path || '') + '</div>'
        + '</div>';
}

async function loadLearnResult() {
    const body = document.getElementById('learn-result-body');
    const hint = document.getElementById('learn-result-hint');
    if (!body) return;
    if (hint) hint.textContent = '載入中…';
    try {
        const resp = await fetch(API + '/confluence/scorer');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        body.innerHTML = _renderScorerCard('LATEST production model', data.fixed);
        if (hint) hint.textContent = '';
    } catch (e) {
        body.innerHTML = '<div style="color:var(--red);">讀取失敗: ' + e.message + '</div>';
        if (hint) hint.textContent = '';
    }
}

// ── PNL CURVE tab: cumulative equity + Topstep $2K trailing-DD line ──
// The DD line starts $2000 below break-even and trails UP only as each day's
// settled PnL sets a new equity high ("increase as income settles every day"),
// then LOCKS at break-even (0) once it has climbed from -2000 to 0 — i.e. once
// cumulative profit reaches +$2000. Mirrors Topstep's EOD trailing drawdown.
function renderPnlCurve() {
    const host = document.getElementById('pnl-curve-body');
    const hint = document.getElementById('pnl-curve-hint');
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

    const content = host.closest('.bottom-content');
    const headerH = 30;
    const W = Math.max(320, host.clientWidth || (content ? content.clientWidth : 600));
    const H = Math.max(220, (content ? content.clientHeight : 300) - headerH - 6);
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
        ctx.fillText('尚無回測成交 — 先跑一次 BACKTEST', W / 2, H / 2);
        if (hint) hint.textContent = '';
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

    let lo = -DD, hi = DD * 0.25;
    for (const p of pts) { if (p.cum < lo) lo = p.cum; if (p.cum > hi) hi = p.cum; }
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
    ctx.fillStyle = C.cyan; ctx.fillText('— equity', padL + 4, padT + 2);
    ctx.fillStyle = locked ? C.green : C.amber;
    ctx.fillText(locked ? '— $2K DD (locked)' : '— $2K trailing DD', padL + 66, padT + 2);

    if (hint) {
        let peak = 0, maxDDfp = 0;
        pts.forEach(p => { peak = Math.max(peak, p.cum); maxDDfp = Math.max(maxDDfp, peak - p.cum); });
        hint.textContent = [
            'final $' + Math.round(last.cum), 'peak $' + Math.round(peak),
            'maxDD(峰) $' + Math.round(maxDDfp),
            (breachIdx >= 0 ? '⚠ 觸及 $2K 線 (爆帳)' : '未觸及 $2K 線'),
            (locked ? 'DD線已鎖定 break-even' : 'DD線在 -$' + Math.round(-finalThr)),
        ].join('  ·  ');
        hint.style.color = breachIdx >= 0 ? C.red : C.text3;
    }
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
function calShiftMonth(delta) {
    _calMonth = new Date(_calMonth.getFullYear(), _calMonth.getMonth() + delta, 1);
    renderCalendar();
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
    backtestData = d;                       // feeds the Data calendar (uses .trades)
    try { renderMetrics(d.metrics, d.trades || []); } catch (e) {}
    try { renderTrades(d.trades || []); } catch (e) {}
    try {
        log('Restored last backtest (' + (d.metrics.total_trades || (d.trades || []).length) +
            ' trades) from cache · ' + (d.saved_at ? d.saved_at.slice(0, 16).replace('T', ' ') : ''), 'info');
    } catch (e) {}
}
// Script tag is at end of <body>, so the DOM is already parsed here.
_restoreBacktestCache();

