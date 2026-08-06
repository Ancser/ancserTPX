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

// 1.0.8: 目前前月季約(鏡像後端 current_quarterly_contract_id):
// CME 股指 H/M/U/Z,到期=季月第3個週五,前 8 天視為換月。
function currentQuarterlyContractId(sym) {
    const now = new Date();
    const codes = { 3: 'H', 6: 'M', 9: 'U', 12: 'Z' };
    const thirdFriday = (y, m) => {
        let count = 0;
        for (let d = 1; d <= 21; d++) {
            if (new Date(Date.UTC(y, m - 1, d)).getUTCDay() === 5) {
                count++;
                if (count === 3) return Date.UTC(y, m - 1, d);
            }
        }
        return Date.UTC(y, m - 1, 21);
    };
    for (const y of [now.getUTCFullYear(), now.getUTCFullYear() + 1]) {
        for (const m of [3, 6, 9, 12]) {
            if (now.getTime() < thirdFriday(y, m) - 8 * 86400000) {
                return 'CON.F.US.' + String(sym || 'MNQ').toUpperCase() + '.' + codes[m] + String(y).slice(-2);
            }
        }
    }
    return 'CON.F.US.' + String(sym || 'MNQ').toUpperCase() + '.H' + String(now.getUTCFullYear() + 2).slice(-2);
}

// 1.0.8: 開機把所有寫死到期月的合約選項/輸入改成目前前月(auto-renew)
function refreshContractOptions() {
    const quarterly = new Set(['NQ', 'ENQ', 'MNQ', 'ES', 'MES']);
    const rewrite = (v) => {
        const m = /^CON\.F\.US\.([A-Z]+)\./.exec(String(v || '').toUpperCase());
        return (m && quarterly.has(m[1])) ? currentQuarterlyContractId(m[1]) : v;
    };
    document.querySelectorAll('select option').forEach(o => {
        const nv = rewrite(o.value);
        if (nv !== o.value) o.value = nv;
    });
    const cidInput = document.getElementById('contract-id');
    if (cidInput && cidInput.value) cidInput.value = rewrite(cidInput.value);
}
document.addEventListener('DOMContentLoaded', refreshContractOptions);

const DEFAULT_STRATEGY_PARAMS = {
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
    contract_id: currentQuarterlyContractId('MNQ'),  // 1.0.8: 自動換月
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
    pmo_timeframe_minutes: 5,
    pmo_signal_mode: 'normal',
    pmo_sl_atr: 1.0,
    pmo_tp_atr: 1.0,
    pmo_max_hold_bars: 0,   // 1.0.9: HOLD 5m system removed → SL/TP-only exits
    pmo_max_trades_per_day: 3,
    pmo_warmup_bars: 150,
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
// Timeframe checkbox changed → re-detect zones at the new area TF and redraw.
function onTfSelectionChange(mode) {
    enforceSessionTfExclusive(mode);   // 1.0.8: SESSION 與其他 TF 互斥
    updateOverlapTradeTfControl(mode);
    syncZoneFilterUI();
    onAreaConfigChange(mode);
    refreshTfZones(true);
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
    if (v === 'pmo') return 'pmo';
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
    const v = normalizeStrategyName(value);
    if (v === 'fade') return 'DAY ZONE';
    if (v === 'sigma') return 'DISTRIBUTION';
    if (v === 'pmo') return 'PMO';
    if (v === 'factor') return 'FACTOR';
    if (v === 'momentum') return 'MOMENTUM';
    if (v === 'betafib') return 'BETA FIB';
    return 'FACTOR';   // 1.0.9: TREND 已移除,未知值顯示為預設策略
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
    const isPmo = strategy === 'pmo';
    const isFactor = strategy === 'factor';
    const show = (id, on) => {
        const el = document.getElementById(id);
        if (el) el.style.display = on ? '' : 'none';
    };
    const showControl = (id, on) => _showParamControl(id + '-' + mode, on);
    // trend-only controls — hidden in ML mode.
    show('tr-params-' + mode, !isML && !isPmo);
    show('overlap-tf-row-' + mode, !isML && isTrend);
    show('tr-overlap-trade-row-' + mode, !isML && isTrend);
    showControl('area-pct', !isML && (isTrend || isFade));
    showControl('confirm-bars', !isML && isTrend);
    showControl('tr-exit-mode', !isML && (isTrend || isFactor));
    showControl('rr-ratio', !isML && (isTrend || isFactor));
    showControl('trail-trigger-pct', !isML && (isTrend || isFactor));
    showControl('trail-sl-pct', !isML && (isTrend || isFactor));
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
    show('factor-params-' + mode, isFactor || isIntramom || isSessfib);
    show('momentum-params-' + mode, isIntramom);
    show('betafib-params-' + mode, isSessfib);
    ['factor-family-', 'factor-pmo-mode-', 'factor-va-filter-'].forEach((id) => {
        const el = document.getElementById(id + mode);
        const row = el && el.closest ? el.closest('.form-group') : null;
        if (row) row.style.display = (isIntramom || isSessfib) ? 'none' : '';
    });
    syncEmapmoThresholdRow(mode);   // 1.0.9: 門檻滑桿只在 EMAPMO 顯示
    let slText;
    if (isFade) {
        const fem = _mlSelectValue('fade-entry-mode-' + mode, 'limit');
        slText = (fem === 'or15')
            ? 'OR15: entry ± 0.2×前日VA幅(雙向假突破·TP 1×幅)'
            : 'DAY ZONE: 前日 VAL - 120 tick 固定緩衝';
    } else if (isSigma) {
        slText = 'DISTRIBUTION: preset rolling sigma SL / center TP';
    } else if (isPmo) {
        slText = 'PMO: completed 5m EMAPMO signal, ATR SL / ATR TP, market entry';
    } else if (isFactor) {
        slText = 'FACTOR: completed 5m signal, market entry; side/signal/SL/TP use FACTOR controls';
    } else {
        slText = 'TREND: POC↔VAH/VAL 間最低量節點 SL';
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
    } else if (isPmo) {
        slText = 'PMO: completed 5m EMAPMO signal; next available market entry';
    } else if (isFactor) {
        slText = 'FACTOR: completed 5m factor signal; live/backtest use last completed candle only';
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
    if (strategy !== 'trend' && strategy !== 'factor') return;
    const isLadder = !!(sel && sel.value === 'ladder');
    const dim = (id, off) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.disabled = off;
        const grp = el.closest('.form-group');
        if (grp) grp.style.opacity = off ? '0.35' : '';
        if (grp) grp.title = off ? 'LADDER 模式不使用(無 TP;階梯固定 +2R 保本、每 +1R 跟 1R)' : '';
    };
    dim('rr-ratio-' + mode, isLadder);
    dim('trail-trigger-pct-' + mode, isLadder);
    dim('trail-sl-pct-' + mode, isLadder);
}

// RR mode toggle: "固定" shows the single-RR select; "變動" shows the RR-grid
// (range) select and hides the fixed one. Only one is ever visible.
function onExitModeChange(mode) {
    const sel = document.getElementById('tr-exit-mode-' + mode);
    const strategy = normalizeStrategyName(
        (document.getElementById('strategy-' + mode) || {}).value
    );
    if (strategy !== 'trend' && strategy !== 'factor') return;
    const isLadder = !!(sel && sel.value === 'ladder');
    const setGroupVisible = (id, on) => {
        const el = document.getElementById(id);
        if (!el) return;
        const grp = el.closest('.form-group');
        if (grp) grp.style.display = on ? '' : 'none';
        el.disabled = !on;
    };
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
    return [['1', '1 x ATR'], ['1.5', '1.5 x ATR'], ['2', '2 x ATR'], ['2.5', '2.5 x ATR'], ['3', '3 x ATR']];
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
        opt.textContent = current;
        valEl.appendChild(opt);
    }
    if (current !== '') valEl.value = current;
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
    const el = document.getElementById('strategy-' + mode);
    _setStrategySelect(mode, el ? el.value : DEFAULT_STRATEGY_PARAMS.strategy);
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
    const factorSlRule = _factorRule(_paramVal('factor-sl-rule', 'factor_sl_rule', 'atr_blend'), 'atr_blend');
    const factorSlValue = _paramNum('factor-sl-value', 'factor_sl_value', 2.5) || 2.5;
    // 1.0.9: HOLD 5m-candle system removed — FACTOR exits are SL/TP only, always,
    // for every current and future preset. Pinned 0 regardless of any stored value.
    const factorHoldBars = 0;
    const dailyMaxTrades = Math.max(0, _paramInt('factor-max-trades', 'factor_max_trades_per_day', 3));
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
        pmo_timeframe_minutes: parseInt(applied.pmo_timeframe_minutes != null ? applied.pmo_timeframe_minutes : 5, 10) || 5,
        pmo_signal_mode: String(applied.pmo_signal_mode || 'normal') === 'early' ? 'early' : 'normal',
        pmo_sl_atr: Number(applied.pmo_sl_atr != null ? applied.pmo_sl_atr : 1.0) || 1.0,
        pmo_tp_atr: Number(applied.pmo_tp_atr != null ? applied.pmo_tp_atr : 1.0) || 1.0,
        pmo_max_hold_bars: parseInt(applied.pmo_max_hold_bars != null ? applied.pmo_max_hold_bars : 24, 10) || 24,
        pmo_max_trades_per_day: dailyMaxTrades,
        pmo_warmup_bars: parseInt(applied.pmo_warmup_bars != null ? applied.pmo_warmup_bars : 150, 10) || 150,
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
        betafib_risk_basis: _mlSelectValue('betafib-basis-' + mode, 'atr_blend'),
        betafib_min_move_pct: _float('betafib-minpct-' + mode, 0),
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
    _setStrategySelect(mode, p.strategy);
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
    _set('betafib-basis-' + mode, String(p.betafib_risk_basis || 'atr_blend'));
    _set('betafib-minpct-' + mode, String(p.betafib_min_move_pct != null ? p.betafib_min_move_pct : 0));
    // 1.0.9: preset 存的是價距 ticks —— 直接寫進唯一真相,
    // setCapTicks 會把滑桿位置與文字一起重畫,兩者不可能不同步。
    setCapTicks(mode, parseInt(p.max_profit_ticks != null ? p.max_profit_ticks : 0, 10) || 0);
    updateRiskCapHint(mode);
    _setChoice('factor-sl-rule-' + mode, _factorRuleValue(p.factor_sl_rule, 'atr_blend'));
    _setChoice('factor-sl-value-' + mode, String(p.factor_sl_value != null ? p.factor_sl_value : 2.5));
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

const PRESET_MODEL_ORDER = ['TREND', 'DAY ZONE', 'DISTRIBUTION', 'PMO', 'FACTOR'];

function _presetNameMeta(name) {
    const raw = String(name || '');
    const fixed = /\s+\*$/.test(raw);
    const s = raw.replace(/\s+\*$/, '').replace(/^SWEEP\s+/i, '').trim();
    const compactDated = s.match(/^(\d{4})\s+(TREND|DAY ZONE|DISTRIBUTION|PMO|FACTOR)\s+#(\d+)\s*(.*)$/i);
    const dottedDated = s.match(/^(\d{2}\.\d{2})(?:\s+(\d{2}:\d{2}))?\s+(TREND|DAY ZONE|DISTRIBUTION|PMO|FACTOR)\s+#(\d+)\s*(.*)$/i);
    const legacy = s.match(/^(TREND|DAY ZONE|DISTRIBUTION|PMO|FACTOR)\s+#(\d+)\s*(.*)$/i);
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
    const strategy = normalizeStrategyName((params || {}).strategy);
    if (strategy === 'fade') return 'DAY ZONE';
    if (strategy === 'sigma') return 'DISTRIBUTION';
    if (strategy === 'pmo') return 'PMO';
    if (strategy === 'factor') return 'FACTOR';
    return 'TREND';
}

function _normalizeNamingModel(model) {
    const value = String(model || '').trim().toUpperCase();
    return ['TREND', 'DAY ZONE', 'DISTRIBUTION', 'PMO', 'FACTOR'].includes(value) ? value : 'TREND';
}

function _sanitizePresetPurpose(value, fallback) {
    const clean = String(value || '').replace(/\s+/g, '').trim();
    return (clean || fallback || '手動保存').slice(0, 12);
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
    if (normalizeStrategyName(p.strategy) === 'pmo') {
        const market = allowedSessionsLabel(p.tr_allowed_sessions != null ? p.tr_allowed_sessions : null);
        return [
            'PMO',
            String(p.pmo_signal_mode || 'normal').toUpperCase(),
            String(p.pmo_timeframe_minutes || 5) + 'm',
            'SL' + String(p.pmo_sl_atr != null ? p.pmo_sl_atr : 1) + 'ATR',
            'TP' + String(p.pmo_tp_atr != null ? p.pmo_tp_atr : 1) + 'ATR',
            'H' + String(p.pmo_max_hold_bars != null ? p.pmo_max_hold_bars : 24),
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
    if (normalizeStrategyName(p.strategy) === 'pmo') return 'PMO';
    if (normalizeStrategyName(p.strategy) === 'factor') return 'Icefishball';
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
        'strategy': '策略邏輯：TREND = 區間突破趨勢單；DAY ZONE = 前日 VA 均值回歸；DISTRIBUTION = rolling distribution resting fade；PMO = 5m EMAPMO ATR market entry。\nStrategy selector for trend, day-zone, distribution, or PMO.',
        'contract': '\u4ea4\u6613 / \u56de\u6e2c\u4f7f\u7528\u7684\u671f\u8ca8\u5408\u7d04\uff0c\u4f8b\u5982 CON.F.US.MNQ.U26\u3002\nFutures contract used for data and orders.',
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
        'contract-id': '\u671f\u8ca8\u5408\u7d04 ID\uff0c\u4f8b\u5982 CON.F.US.MNQ.U26\u3002\nFutures contract ID.',
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
    initPresetDirtyTracking();
    decorateParamHelpDots();
    checkHealth();
    const envConfigReady = loadEnvConfig();
    updateClock();
    setInterval(updateClock, 1000);

    ['username', 'apikey'].forEach(id => {
        const input = document.getElementById(id);
        if (!input) return;
        input.addEventListener('input', () => {
            _refreshConnectionState({ credentialsChanged: true });
        });
    });

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
            } else if (tab === 'live') {
                backtestPanels.classList.add('hidden');
                try { refreshCapsForContract('live'); } catch (e) {}
                metricsPanel.classList.add('hidden');
                livePanel.classList.remove('hidden');
                if (_liveInterval || _liveStatusInterval) liveTopBar.style.display = 'block';
                updateLiveTopBar();
                // 1.0.9: 兩帳號槽 —— 填入 + 每 2s 更新各槽狀態
                try { initLiveSlots(); } catch (e) {}
                if (!_liveSlotInterval) _liveSlotInterval = setInterval(pollLiveSlots, 2000);
            }
        };
    });
    document.querySelectorAll('.bottom-tab').forEach(t => {
        t.onclick = () => {
            document.querySelectorAll('.bottom-tab').forEach(x => x.classList.remove('active'));
            t.classList.add('active');
            const tab = t.dataset.btab;
            ['presets','trades','execute','pnl','log'].forEach(id => {
                const panel = document.getElementById('btab-' + id);
                if (panel) panel.classList.toggle('hidden', id !== tab);
            });
            if (tab === 'presets') renderSweepTable();
            if (tab === 'log') scrollSystemLogToBottom();
            if (tab === 'pnl') renderPnlCurve();
        };
    });
});

async function loadEnvConfig() {
    try {
        const resp = await fetch(API + '/config');
        const cfg = await resp.json();
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
            document.getElementById('contract-id').value = cfg.contract_id || '';
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
const LIVE_MINOR_SLOT = 2;

function liveSlotLabel(slot) {
    return Number(slot) === LIVE_MAIN_SLOT ? 'ACCOUNT MAIN' : 'ACCOUNT MINOR';
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
                log('Detected ' + zoneData.zones.length + ' consolidation zone(s)', 'success');
                if (window._lastChartData) {
                    applyDefaultChartView(window._lastChartData, zoneData.zones);
                }
            } else {
                log('No consolidation zones detected', 'info');
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
            log('Trading engine start failed: ' + (data.detail || JSON.stringify(data)), 'warn');
            statusEl.style.color = 'var(--amber)';
            statusEl.textContent = 'MONITOR ONLY';
        } else {
            engineStarted = true;
            log('Trading engine started successfully ✓', 'success');
            refreshTfZones(true);
            setTimeout(() => refreshLiveZoneOverlay(stratParams), 0);
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
    const statusEl = document.getElementById('live-status-text');
    _liveStartInProgress = false;
    try {
        const resp = await fetch(API + '/live/stop', { method: 'POST' });
        const data = await resp.json();
        log('Trading engine stopped', 'info');
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

async function pollLiveStatus() {
    const statusAccount = _focusMainLiveAccount() || liveAccount;
    // Always update market session (even without engine)
    const session = getMarketSession();
    const elSession = document.getElementById('lv-session');
    if (elSession) { elSession.textContent = session.label; elSession.style.color = session.color; }
    const elMarket = document.getElementById('lv-market-session');
    if (elMarket) { elMarket.textContent = session.label; elMarket.style.color = session.color; }

    try {
        // 1.0.9: 頂欄/圖表跟隨目前聚焦的帳號(liveAccount);未設則走 primary 引擎
        const resp = await fetch(API + '/live/status' + (statusAccount && statusAccount.id ? ('?account_id=' + statusAccount.id) : ''));
        if (!resp.ok) return;
        const st = await resp.json();
        renderLiveRiskGates(st);   // 1.0.9: 風控閘狀態列(running / stopped 皆更新)
        if (!st.running) {
            if (_liveStartInProgress) {
                const statusEl = document.getElementById('live-status-text');
                if (statusEl) {
                    statusEl.style.color = 'var(--amber)';
                    statusEl.textContent = 'STARTING...';
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
            statusEl.textContent = 'RUNNING';
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
            statusLabelEl.textContent = isMLStatus
                ? 'ML STATUS'
                : (strategyDisplayName(st.strategy_mode || collectStrategyParams('live').strategy) + ' STATUS');
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
                    shape: isLong ? 'arrowUp' : 'arrowDown',
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
            refreshTfZones(!(_tfAllZones && _tfAllZones.length));
            refreshIndicatorSignalMarkers(false);
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
        // 1.0.9: 自動居中(⌖ 鈕)— 開啟時鎖定縱向比例,中心跟隨中央 K 線 EMA200
        autoscaleInfoProvider: (original) => _autoCenterProvider(original),
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
        drawIndicatorSignalOverlay();
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
            drawIndicatorSignalOverlay();
            window.TpxGlass?.sync?.();
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
let fadeLevelsCanvas = null;
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

    ctx.strokeStyle = 'rgba(247, 239, 224, 0.25)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.font = '9px "IBM Plex Mono", monospace';
    ctx.fillStyle = 'rgba(247, 239, 224, 0.5)';
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

// ── Timeframe zone filter state (bottom-left chart control) ──
// Draws clean VAH/VAL/POC LINES per timeframe (no VP histogram). Line width
// scales with timeframe size; colour = orange (live) / white (backtest).
const TF_ORDER = ['15m', '30m', '1h', '4h'];
const TF_LINE_WIDTH = { '15m': 1.3, '30m': 1.7, '1h': 2.1, '4h': 2.6 };
let _zoneFilter = { tfs: new Set(['15m']), mode: 'backtest' };
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
    _zoneFilter.tfs = new Set(sel.length ? sel : ['15m']);
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

function _drawSessionDevelopingVa(ctx, W, H, priceToY, tX, vFrom, vTo) {
    // 1.0.9: 畫「每一個 session」的生長 VA 白線(波浪曲線),整段歷史都顯示;
    // 移除延伸到圖表右邊的水平直線(使用者要求)。已完成但無曲線的 session,
    // 只在它自己的時間範圍內畫一段(不延伸到右邊)。
    const sessions = (_tfAllZones || [])
        .filter(z => String(z.timeframe || '').toLowerCase() === 'session')
        .filter(z => Number.isFinite(Number(z.vah_80)) && Number.isFinite(Number(z.val_80)));
    if (!sessions.length) return;

    const rightX = Math.max(0, W - 60);
    const makePts = (z, key) => (z.va_curve || []).map(p => {
        const ts = p.ts || p.time || p.timestamp;
        const value = Number(p[key]);
        if (!ts || !Number.isFinite(value)) return null;
        const time = isoToChartTime(ts);
        if (vFrom !== null && vTo !== null && (time < vFrom || time > vTo)) return null;
        const x = tX(ts);
        const y = priceToY(value);
        if (x === null || y === null || y < -80 || y > H + 80) return null;
        return { x, y };
    }).filter(Boolean);

    const drawPts = (pts) => {
        if (pts.length < 2) return;
        ctx.beginPath();
        pts.forEach((p, i) => { if (i === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y); });
        ctx.stroke();
    };

    ctx.save();
    ctx.lineWidth = 1.25;
    ctx.strokeStyle = 'rgba(247, 239, 224, 0.90)';
    ctx.setLineDash([]);
    sessions.forEach(z => {
        const vah = makePts(z, 'vah');
        const val = makePts(z, 'val');
        if (vah.length >= 2 || val.length >= 2) {
            drawPts(vah);
            drawPts(val);
            return;
        }
        // 無生長曲線:進行中的 session 直接跳過(避免右延直線);
        // 已完成的 session 只在 formed_at→left_at 之間畫一段。
        if (!z.left_at) return;
        const a = tX(z.formed_at);
        const b = tX(z.left_at);
        if (a === null || b === null) return;
        const x0 = Math.max(0, a);
        const x1 = Math.min(rightX, b);
        if (x1 <= x0 + 2) return;
        [Number(z.vah_80), Number(z.val_80)].forEach(v => {
            const y = priceToY(v);
            if (y === null || y < -80 || y > H + 80) return;
            ctx.beginPath();
            ctx.moveTo(x0, y);
            ctx.lineTo(x1, y);
            ctx.stroke();
        });
    });
    ctx.restore();
}

// Render the selected-timeframe zones onto the VP overlay canvas.
// 1.0.9: SESSFIB —— 每晚一條 fib 掛單線,橫跨該夜盤時段。
// 琥珀色虛線 + 左端標籤,與 VAH/VAL(白/藍)和成交決策疊圖區隔。
function _drawBetafibLevels(ctx, H, priceToY, tX) {
    ctx.save();
    _betafibLevels.forEach((lv) => {
        const y = priceToY(Number(lv.level));
        if (!(y >= 0 && y <= H)) return;
        const x0 = tX(lv.t_from);
        const x1 = tX(lv.t_to);
        if (x0 === null || x1 === null || x1 <= x0) return;
        ctx.strokeStyle = 'rgba(251, 191, 36, 0.85)';
        ctx.lineWidth = 1.2;
        ctx.setLineDash([5, 4]);
        ctx.beginPath();
        ctx.moveTo(x0, y);
        ctx.lineTo(x1, y);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(251, 191, 36, 0.95)';
        ctx.font = '10px ui-monospace, monospace';
        ctx.textBaseline = 'bottom';
        const tag = 'fib ' + Number(lv.entry_fib).toFixed(3)
            + ' ' + (lv.direction === 'long' ? '↑' : '↓')
            + ' ' + Number(lv.move_pct).toFixed(2) + '%';
        ctx.fillText(tag, x0 + 3, y - 2);
    });
    ctx.restore();
}

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

    const priceToY = (p) => { try { const y = candleSeries.priceToCoordinate(p); return y !== null ? y : -1; } catch (e) { return -1; } };
    const tX = (iso) => { if (!iso) return null; try { return chart.timeScale().timeToCoordinate(isoToChartTime(iso)); } catch (e) { return null; } };

    // Visible time range for viewport culling.
    let vFrom = null, vTo = null;
    try { const vr = chart.timeScale().getVisibleRange(); if (vr) { vFrom = vr.from; vTo = vr.to; } } catch (e) {}

    // Always show the current session developing VA80 boundary in white.
    if (_tfAllZones && _tfAllZones.length > 0) {
        _drawSessionDevelopingVa(ctx, W, H, priceToY, tX, vFrom, vTo);
    }

    // 1.0.9: SESSFIB 掛單線。畫在早退(有成交決策疊圖時)之前,
    // 否則一旦跑過回測就永遠看不到掛單位。
    if (_betafibLevels.length) {
        _drawBetafibLevels(ctx, H, priceToY, tX);
    }

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
    const BT_COLOR = '247, 239, 224';   // backtest reference zones (milk-white)
    const LIVE_COLOR = '255, 165, 0';   // current/live zone (bright)
    const rightX = W - 60;

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

function clearFadeDailyLevels() {
    if (fadeLevelsCanvas) {
        const ctx = fadeLevelsCanvas.getContext('2d');
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, fadeLevelsCanvas.width, fadeLevelsCanvas.height);
    }
}

function drawFadeDailyLevels(zones) {
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
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';

    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
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

// -- Decision-zone overlay --
// Draw only the primary VAH/VAL range used by each trade decision.

// ── 1.0.9: 自動居中(EMA200 錨定)+ 一鍵到最新 ──────────────────
// ⌖ 開啟時:縱向比例(價格/像素)鎖定在開啟當下的值;左右拖拽瀏覽歷史時
// 視窗中心自動平滑跟隨「畫面中央 K 線的 EMA200」,不需要手動上下拉。
// 縱向並沒有鎖死:拖拽的上下分量會即時調整相對 EMA 的偏移(_acOffset),
// 之後繼續左右平移時偏移保留;candle ratio 不會被 autoscale 改變。
let _autoCenterOn = false;
let _acSpan = null;    // 鎖定的可視價格跨度(= 開啟當下的縱向比例)
let _acMid = null;     // 平滑後的中心價
let _acOffset = 0;     // 手動上下拖拽的偏移(相對 EMA200 錨點)
let _acKickRAF = null;
let _acEma = { src: null, n: 0, arr: null };   // EMA200 快取(資料變更時重算)

function _acEmaArr() {
    const cd = window._lastChartData || [];
    if (!cd.length) return null;
    if (_acEma.src === cd && _acEma.n === cd.length) return _acEma.arr;
    const k = 2 / (200 + 1);
    const arr = new Float64Array(cd.length);
    let prev = cd[0].close;
    for (let i = 0; i < cd.length; i++) {
        prev = cd[i].close * k + prev * (1 - k);
        arr[i] = prev;
    }
    _acEma = { src: cd, n: cd.length, arr };
    return arr;
}

function _centerEmaPrice() {
    try {
        const vr = chart.timeScale().getVisibleRange();
        const ema = _acEmaArr();
        if (!vr || !ema) return null;
        const i0 = _nearestBarIndex(vr.from);
        const i1 = _nearestBarIndex(vr.to);
        if (i0 === null || i1 === null) return null;
        const mid = Math.min(ema.length - 1, Math.max(0, Math.round((i0 + i1) / 2)));
        const v = ema[mid];
        return Number.isFinite(v) ? v : null;
    } catch (_) { return null; }
}

function _autoCenterProvider(original) {
    const base = original();
    if (!_autoCenterOn || !_acSpan) return base;
    const anchor = _centerEmaPrice();
    if (anchor === null) return base;
    const target = anchor + _acOffset;
    // 平滑滑動:每次重算向目標移動 35% — 拖拽時有緩動的「slide」感
    _acMid = (_acMid === null) ? target : _acMid + (target - _acMid) * 0.35;
    // 尚未收斂 → 排一次重算,放開拖拽後會自己滑到定位
    if (Math.abs(target - _acMid) > _acSpan * 0.002) _acScheduleKick();
    const half = _acSpan / 2;
    return { priceRange: { minValue: _acMid - half, maxValue: _acMid + half } };
}

// 縱向手動偏移:拖拽的上下分量調整 _acOffset(橫向平移仍由圖表庫處理)。
// 只攔 pane 區域 — 價格軸/時間軸上的拖拽(縮放)不干預。
let _acDragBound = false;
let _acDrag = null;   // { y0, off0 }

function _acBindDrag() {
    if (_acDragBound) return;
    const el = document.getElementById('chart-container');
    if (!el) return;
    _acDragBound = true;
    el.addEventListener('pointerdown', (e) => {
        if (!_autoCenterOn || e.button !== 0) return;
        const r = el.getBoundingClientRect();
        let axW = 0;
        try { axW = chart.priceScale('right').width() || 0; } catch (_) {}
        if (e.clientX > r.right - axW) return;
        if (e.clientY > r.bottom - _timeAxisHeight()) return;
        _acDrag = { y0: e.clientY, off0: _acOffset };
    });
    window.addEventListener('pointermove', (e) => {
        if (!_autoCenterOn || !_acDrag || !_acSpan) return;
        const paneH = el.clientHeight - _timeAxisHeight();
        if (paneH <= 0) return;
        _acOffset = _acDrag.off0 + (e.clientY - _acDrag.y0) * (_acSpan / paneH);
        _acScheduleKick();
    });
    const end = () => { _acDrag = null; };
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
}

function _acScheduleKick() {
    if (_acKickRAF) return;
    _acKickRAF = requestAnimationFrame(() => {
        _acKickRAF = null;
        // 重套 autoScale 觸發重算(provider 會再被呼叫一次)
        try { candleSeries.priceScale().applyOptions({ autoScale: true }); } catch (_) {}
        try { chart.timeScale().applyOptions({}); } catch (_) {}
        requestAnimationFrame(() => {
            drawIndicatorSignalOverlay();
            window.TpxGlass?.sync?.();
        });
    });
}

function toggleAutoCenter() {
    _autoCenterOn = !_autoCenterOn;
    const btn = document.getElementById('btn-auto-center');
    if (_autoCenterOn) {
        // 鎖定當下的縱向比例:用座標反推目前可視價格跨度
        _acSpan = null;
        try {
            const container = document.getElementById('chart-container');
            const paneH = container.clientHeight - _timeAxisHeight();
            const pTop = candleSeries.coordinateToPrice(0);
            const pBot = candleSeries.coordinateToPrice(paneH);
            if (pTop != null && pBot != null && Number.isFinite(pTop) && Number.isFinite(pBot)) {
                _acSpan = Math.abs(pTop - pBot);
            }
        } catch (_) {}
        _acMid = null;
        _acOffset = 0;
        if (!_acSpan) {
            _autoCenterOn = false;
            log('Auto-center could not start (current vertical scale unavailable)', 'warn');
        } else {
            _acBindDrag();
            try { candleSeries.priceScale().applyOptions({ autoScale: true }); } catch (_) {}
            log('Auto-center ON — following the center candle EMA200; drag vertically to adjust offset', 'info');
        }
    } else {
        // 回復預設 autoscale(範圍重新由資料決定)
        try { candleSeries.priceScale().applyOptions({ autoScale: true }); } catch (_) {}
        log('Auto-center OFF — default autoscale restored', 'info');
    }
    if (btn) btn.classList.toggle('active', _autoCenterOn);
    _acScheduleKick();
}

function scrollToLatest() {
    try { chart.timeScale().scrollToRealTime(); } catch (_) {}
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
            if (Number.isFinite(n)) return n;
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

        // 1.0.9: SL/TP 價格在可視範圍外時,該側底色會撐滿整個豎列
        // (zoom out 時看起來像灰白色大柱)→ 邊界不在窗格內就不畫該側。
        // 高度 < 6px 的一側也不畫:zoom out 價格軸壓縮後,底色會退化成
        // 超長的細條(使用者回報「高度不足時超長延伸」)。
        const paneH = chartH - _timeAxisHeight();
        const inPane = (yy) => yy >= -20 && yy <= paneH + 20;
        const MIN_FILL_H = 6;

        ctx.save();
        ctx.setLineDash([]);
        if (greenH >= MIN_FILL_H && inPane(yEntry) && inPane(yTP)) {
            ctx.fillStyle = 'rgba(0, 229, 160, 0.105)';
            ctx.fillRect(x0, greenTop, xEnd - x0, greenH);
        }
        if (redH >= MIN_FILL_H && inPane(yEntry) && inPane(ySL)) {
            ctx.fillStyle = 'rgba(255, 64, 96, 0.115)';
            ctx.fillRect(x0, redTop, xEnd - x0, redH);
        }
        // 1.0.8: 依使用者要求移除 entry/TP/SL 三條橫線,只保留紅綠底
        ctx.restore();
        return true;
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
        const mainAcc = _focusMainLiveAccount() || getMainLiveAccount();
        const accId = mainAcc ? mainAcc.id : (currentAccount ? currentAccount.id : 0);
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
    clearFadeDailyLevels();

    if (!zones || zones.length === 0) return;

    // Draw VP histogram + POC/VAH/VAL lines on full-chart canvas overlay
    drawVolumeProfile(zones);
    drawFadeDailyLevels(zones);
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
        const btnSweep = document.getElementById('btn-sweep');   // 1.0.9: 連線後啟用 SWEEP
        if (btnSweep) btnSweep.disabled = false;
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

        // 1.0.9: 連線後立刻抓多 TF + session 生長區,讓「白色 VAH/VAL」在啟動就畫出來。
        // 先前只有跑完 backtest 才 refreshTfZones → 啟動看不到白線(codex 沒修到的點)。
        try { refreshTfZones(true); } catch (e) {}

        // Fetch actual trades from TopstepX (refresh cache) for the active account
        const mainAcc = _focusMainLiveAccount() || getMainLiveAccount() || currentAccount;
        const accId = mainAcc ? mainAcc.id : 0;
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
    refreshIndicatorSignalMarkers(true);
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

// ── 1.0.8: 高效參數掃描(SWEEP 分頁)─────────────────────────
let _sweepData = null;
let _sweepSortKey = 'pf';        // 1.0.9: 預設依 PF 排序
let _sweepRenderedRows = [];     // 目前排序後渲染的列(供 + 存 preset)

async function loadSweepResults() {
    // 啟動時自動載入上一次 sweep 結果
    try {
        const resp = await fetch(API + '/backtest/sweep/results');
        if (!resp.ok) return;
        const data = await resp.json();
        if (data && data.results && data.results.length) {
            _sweepData = data;
            renderSweepTable();
        }
    } catch (_) { /* server 未起或無結果 — 靜默 */ }
}

function renderSweepTable(sortKey) {
    if (sortKey) _sweepSortKey = sortKey;
    const wrap = document.getElementById('sweep-results-wrap');
    const meta = document.getElementById('sweep-meta');
    if (!wrap || !_sweepData || !(_sweepData.results || []).length) return;
    const onlyAcc = !!((document.getElementById('sweep-filter-acc') || {}).checked);   // 1.0.9: 只顯示 ACC ★
    let rows = [..._sweepData.results];
    if (onlyAcc) rows = rows.filter(r => r.accept);
    const k = _sweepSortKey;
    rows.sort((a, b) => (Number(b[k]) || 0) - (Number(a[k]) || 0));
    _sweepRenderedRows = rows;   // 1.0.9: 供 + 存 preset(index 對齊渲染順序)
    if (!rows.length) {
        wrap.innerHTML = '<div style="color:var(--text3);padding:16px;">' + t('No ACC ★ pass variants — untick the filter to see all.') + '</div>';
        if (meta) meta.textContent = '0 / ' + _sweepData.results.length + ' ' + t('pass ACC ★');
        return;
    }

    if (meta) {
        const created = _sweepData.created_at ? String(_sweepData.created_at).slice(0, 16).replace('T', ' ') : '?';
        const byModel = _sweepData.qualified_by_model || {};
        const modelSummary = Object.keys(byModel).length
            ? (' | qualified: ' + Object.keys(byModel).map(m => m + '=' + ((byModel[m] || []).length)).join(' '))
            : '';
        meta.textContent = 'sweep @ ' + created + ' UTC | ' + rows.length +
            (onlyAcc ? ' ' + t('★ pass') + ' / ' + (_sweepData.results || []).length + ' ' + t('all') : ' ' + t('variants')) +
            ' | ' + t('sort') + ': ' + k + ' (' + t('click column header to change') + ') | PF first' + modelSummary;
    }
    const th = (key, label, tip) => '<th style="cursor:pointer;' +
        (key === k ? 'color:var(--amber);' : '') +
        '"' + (tip ? ' title="' + tip + '"' : '') +
        ' onclick="renderSweepTable(\'' + key + '\')">' + label + '</th>';
    const money = (v, pos) => '<span style="color:var(--' + (v >= 0 ? (pos || 'green') : 'red') + ');">' +
        (v >= 0 ? '+' : '') + Math.round(v) + '</span>';
    // 1.0.9: params 拆成 model / risk 兩欄(risk = 封鎖型設定;sweep 不掃 risk 變體)
    const RISK_KEYS = ['tr_daily_loss_stop', 'tr_daily_win_stop', 'tr_daily_profit_stop',
        'tr_allowed_sessions', 'tr_one_trade_per_session', 'factor_max_trades_per_day',
        'pmo_max_trades_per_day'];
    const fmtParams = (p, riskSide) => Object.keys(p || {})
        .filter(kk => kk !== 'strategy' && (RISK_KEYS.includes(kk) === riskSide))
        .map(kk => kk.replace(/^(tr_|factor_|sigma_|fade_|pmo_)/, '') + '=' +
            (Array.isArray(p[kk]) ? p[kk].join('+') : p[kk]))
        .join(' ') || '—';
    const factorsOf = (r) => {
        const p = r.params || {};
        return p.factor_signal_family ? String(p.factor_signal_family).toUpperCase()
            : (p.sigma_method ? ('ROLL' + (p.sigma_window_minutes || '') + ' ' + String(p.sigma_method).toUpperCase())
            : (p.fade_entry_mode ? String(p.fade_entry_mode).toUpperCase() : (r.model || '—')));
    };
    wrap.innerHTML = '<table><thead><tr>' +
        '<th>#</th><th title="縮放測試通過時顯示 MNQx3 / NQx1">CONTRACT</th><th>MODEL</th><th>FACTORS</th>' +
        '<th>MODEL PARAMS</th><th>RISK PARAMS</th><th>TRADES</th>' +
        th('monthly_avg', 'M-PNL', '月均 PnL(30.44 天歸一)') + th('pf', 'PF') +
        th('max_dd', 'MAXDD') + th('worst_day', 'WORST-D') +
        th('weekly_cv', 'W-VAR', '週變異 CV = 週PnL std / |mean|,<1 為穩') +
        '<th title="walk-forward 三段各正">WF</th>' +
        '<th title="ACC: 月PnL>3k · PF>1.5 · 月20筆 · DD<1k · 週CV<1(或縮放後通過)">ACC</th>' +
        '<th title="存成結構化 preset(命名規則自動)">+</th>' +
        '</tr></thead><tbody>' +
        rows.slice(0, 80).map((r, i) => {
            const segs = (r.seg_pnls || []).map(v => Math.round(v)).join(' / ');
            const sc = r.scaled || null;
            const scale = r.contract_scale || 'MNQx1';
            return '<tr' + (r.accept ? ' style="background:rgba(0,229,160,0.06);"' : '') + '>' +
                '<td style="color:var(--text2);">' + (i + 1) + '</td>' +
                '<td style="color:' + (scale !== 'MNQx1' ? 'var(--amber)' : 'var(--text2)') + ';font-weight:600;">' + scale + '</td>' +
                '<td style="color:var(--cyan);font-weight:600;">' + (r.model || 'TREND') + '</td>' +
                '<td style="color:var(--text2);">' + factorsOf(r) + '</td>' +
                '<td style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;">' + fmtParams(r.params, false) + '</td>' +
                '<td style="font-family:\'IBM Plex Mono\',monospace;font-size:10px;color:var(--text3);">' + fmtParams(r.params, true) + '</td>' +
                '<td>' + r.trades + ' <span style="color:var(--text3);font-size:9px;">(' + (r.trades_per_month || 0) + '/mo)</span></td>' +
                '<td>' + money(sc ? sc.monthly_avg : (r.monthly_avg || 0)) + '</td>' +
                '<td style="color:' + (((sc ? sc.pf : r.pf) || 0) >= 1.5 ? 'var(--green)' : 'var(--red)') + ';">' + ((sc ? sc.pf : r.pf) || 0).toFixed(2) + '</td>' +
                '<td style="color:var(--red);">' + Math.round(sc ? sc.max_dd : r.max_dd) + '</td>' +
                '<td style="color:var(--red);">' + Math.round(r.worst_day) + '</td>' +
                '<td style="color:' + ((r.weekly_cv || 99) < 1 ? 'var(--green)' : 'var(--text2)') + ';">' + (r.weekly_cv != null ? r.weekly_cv.toFixed(2) : '—') + '</td>' +
                '<td title="' + segs + '" style="color:' + (r.wf_pass ? 'var(--green)' : 'var(--red)') + ';">' + (r.wf_pass ? '✓' : '✗') + '</td>' +
                '<td style="color:' + (r.accept ? 'var(--green)' : 'var(--text3)') + ';font-weight:bold;">' + (r.accept ? '★' : '—') + '</td>' +
                '<td><button class="btn btn-outline btn-mini" style="padding:0 7px;font-size:12px;line-height:1.5;" onclick="saveSweepPreset(' + i + ')" title="存成 preset">+</button></td>' +
                '</tr>';
        }).join('') + '</tbody></table>';
}

// 1.0.9: 程式化切換底部分頁
function _showBottomTab(name) {
    document.querySelectorAll('.bottom-tab').forEach(x => x.classList.toggle('active', x.dataset.btab === name));
    ['presets', 'trades', 'execute', 'pnl', 'log'].forEach(id => {
        const p = document.getElementById('btab-' + id);
        if (p) p.classList.toggle('hidden', id !== name);
    });
    if (name === 'presets') renderSweepTable();
    if (name === 'log') scrollSystemLogToBottom();
    if (name === 'pnl') renderPnlCurve();
}

// 1.0.9: 全策略參數掃描(TREND + DAY ZONE + DISTRIBUTION)→ 結果進 PRESETS 分頁,依 PF 排序
async function runBacktestSweep() {
    const sweepBtn = document.getElementById('btn-sweep');
    const btBtn = document.getElementById('btn-backtest');
    if (sweepBtn && sweepBtn.disabled) return;
    const _resetSweepBtn = () => { if (sweepBtn) sweepBtn.textContent = 'SWEEP'; };
    // 1.0.9: SWEEP 很吃 CPU/記憶體;和 live 引擎同時跑易造成卡頓/當機 → 先警告
    try {
        const lr = await fetch(API + '/live/status-all');
        if (lr.ok) {
            const ld = await lr.json();
            const running = (ld.engines || []).filter(e => e.status && e.status.running).length;
            if (running > 0 && !confirm('Detected ' + running + ' running live engine(s).\nSWEEP is resource intensive and may make live trading unresponsive. Stop live engines first when possible.\nContinue anyway?')) { _resetSweepBtn(); return; }
        }
    } catch (e) {}
    const dataOk = await _ensureBacktestData(sweepBtn || btBtn);
    if (!dataOk) { _resetSweepBtn(); return; }
    if (!confirm('Start parameter sweep for the selected models (MNQx1, risk settings locked, about 5–25 minutes)?')) { _resetSweepBtn(); return; }

    if (sweepBtn) { sweepBtn.disabled = true; sweepBtn.textContent = 'SWEEPING…'; }
    if (btBtn) btBtn.disabled = true;
    _showBottomTab('presets');
    const body = buildBacktestBody();
    // 1.0.9: run/lock 面板 — 只掃勾選的 model(全勾 = 不帶參數,後端跑全部)
    const scopeEl = document.getElementById('sweep-model-scope-bt');
    const scope = scopeEl ? String(scopeEl.value || 'ALL').toUpperCase() : 'ALL';
    const _mm = [];
    if (scope === 'ALL') {
        _mm.push('TREND', 'DAY ZONE', 'DISTRIBUTION', 'FACTOR');
    } else if (['TREND', 'DAY ZONE', 'DISTRIBUTION', 'FACTOR'].includes(scope)) {
        _mm.push(scope);
    } else {
        if ((document.getElementById('sweep-m-trend') || {}).checked) _mm.push('TREND');
        if ((document.getElementById('sweep-m-dayzone') || {}).checked) _mm.push('DAY ZONE');
        if ((document.getElementById('sweep-m-dist') || {}).checked) _mm.push('DISTRIBUTION');
        if ((document.getElementById('sweep-m-factor') || {}).checked) _mm.push('FACTOR');
    }
    if (!_mm.length) { log('Select at least one model before starting SWEEP', 'warn'); _resetSweepBtn(); if (sweepBtn) sweepBtn.disabled = false; if (btBtn) btBtn.disabled = false; return; }
    if (_mm.length < 4) body.sweep_models = _mm;
    log('SWEEP started: ' + _mm.join(' + ') + ' (MNQx1 locked, sorted by PF)…', 'info');
    _startBacktestProgress();

    let ok = false;
    try {
        const resp = await fetch(API + '/backtest/sweep', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        if (!resp.ok) { const e = await resp.json().catch(() => ({})); throw new Error(e.detail || resp.statusText); }
        await resp.json();                 // sweep 已持久化結果
        await loadSweepResults();
        renderSweepTable('pf');
        ok = true;
        const n = (_sweepData && (_sweepData.results || []).length) || 0;
        log('SWEEP complete // ' + n + ' variants // sorted by PF; click + to save a preset', 'success');
    } catch (e) {
        log('SWEEP failed: ' + e.message, 'error');
    } finally {
        _stopBacktestProgress(ok);
        if (sweepBtn) { sweepBtn.disabled = false; sweepBtn.textContent = 'SWEEP'; }
        if (btBtn) btBtn.disabled = false;
    }
}

// 1.0.9: 把某個 sweep 結果列存成結構化命名 preset(base = 當前 backtest 表單,overlay = 該列掃出的參數)
async function saveSweepPreset(i) {
    const r = (_sweepRenderedRows || [])[i];
    if (!r) { log('Sweep result row not found', 'warn'); return; }
    const rowStrat = (r.params && r.params.strategy)
        || (r.model === 'DAY ZONE' ? 'fade' : (r.model === 'DISTRIBUTION' ? 'sigma' : (r.model === 'FACTOR' ? 'factor' : 'trend')));
    // 1.0.9 FIX: 一律用 sweep 存下的完整參數快照(preset_params)—— 逐位重現掃描條件。
    // 舊法用「當前表單」當 base,表單的 session/size/trail/exit 會污染 preset,
    // 導致回測結果與 sweep 榜單完全對不上(0708 事件)。
    let params;
    if (r.preset_params && Object.keys(r.preset_params).length) {
        params = Object.assign({}, r.preset_params, { strategy: rowStrat });
    } else {
        log('This legacy sweep row has no parameter snapshot; falling back to form values, so results may differ', 'warn');
        params = Object.assign({}, collectStrategyParams('bt'), r.params, { strategy: rowStrat });
    }
    const defaultName = buildPresetName(params, suggestedPresetPurpose(params));
    const name = prompt('Preset name:', defaultName);
    if (!name || !name.trim()) return;
    try {
        const resp = await fetch(API + '/presets/save', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), params: params }),
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        await fetch(API + '/presets/use', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name.trim(), mode: 'bt' }),
        });
        await fetchPresets();
        refreshPresetDropdowns();
        const btPreset = document.getElementById('preset-bt');
        if (btPreset) btPreset.value = name.trim();
        applyStrategyParams('bt', params);
        log('Sweep preset "' + name.trim() + '" saved ✓ (' + r.model + ' ' + r.label + ')', 'success');
    } catch (e) {
        log('Preset save error: ' + e.message, 'error');
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
        try {
            setPerfSource({
                preset: backtestData.preset_name,
                strategy: normalizeStrategyName(_mlSelectValue('strategy-bt', 'factor')),
                saved_at: new Date().toISOString(), stale: false,
            });
        } catch (e) {}
        if (!document.getElementById('btab-pnl').classList.contains('hidden')) renderPnlCurve();
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
// 1.0.9: SESSFIB 掛單線。點狀 marker 走 _indicatorSignalRows,
// 水平線走這裡 —— 兩者的繪製路徑完全不同。
let _betafibLevels = [];
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
    let logicalRange = null;
    try { logicalRange = chart && chart.timeScale().getVisibleLogicalRange(); } catch (_) {}
    window._lastChartData = nextData;
    try { candleSeries.setData(nextData); } catch (_) {}
    if (logicalRange) {
        try { chart.timeScale().setVisibleLogicalRange(logicalRange); } catch (_) {}
    }
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
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';

    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, W, H);

    if (!_indicatorSignalRows || _indicatorSignalRows.length === 0) return;

    // 1.0.9: 夾在價格窗格內 — 信號不再蓋住底部時間軸
    ctx.beginPath();
    ctx.rect(0, 0, W, H - _timeAxisHeight());
    ctx.clip();

    let visibleRange = null;
    try { visibleRange = chart.timeScale().getVisibleRange(); } catch (_) {}

    for (const row of _indicatorSignalRows) {
        const spec = INDICATOR_SIGNAL_TYPES[row.type];
        if (!spec) continue;
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
            _betafibLevels = [];
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
        _betafibLevels = Array.isArray(data.betafib_levels) ? data.betafib_levels : [];
        applyIndicatorSignalCandleColors();
        _refreshAllMarkers();
        if (typeof renderTfZones === 'function') renderTfZones();

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
        }
    }
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
            drawFadeDailyLevels(data.zones);
            redrawTradeDecisionOverlays();
            drawSessionDividers();
            drawIndicatorSignalOverlay();
            window.TpxGlass?.sync?.();
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
            ? ('(價距 ' + ticks + 't · ' + c.size + ' 口 → $' + c.tv.toFixed(2) + '/tick)')
            : '(單筆獲利上限 · 0=不限)';
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
    const consistOver = consistPct != null && consistPct > 0.40;
    // 超標時要再賺多少才合格:bestDay / 0.40 - 目前總淨利
    const consistNeed = consistOver ? (bestDay / 0.40 - netTotal) : 0;
    const consistTip = consistPct == null
        ? 'Topstep XFA consistency: largest single trade-day net / total net must be <= 40%.'
        : (consistOver
            ? ('Topstep XFA consistency ' + (consistPct * 100).toFixed(0) + '% — OVER the 40% limit. '
               + 'Payout is blocked until total net profit reaches $'
               + (bestDay / 0.40).toFixed(0) + ' (earn $' + consistNeed.toFixed(0) + ' more). '
               + 'The best day is locked in until you take a payout — a NEW record day raises the bar again. '
               + 'Use the PDPT slider in RISK MANAGEMENT to cap daily gains.')
            : ('Topstep XFA consistency ' + (consistPct * 100).toFixed(0) + '% — within the 40% limit. '
               + 'Any single trade-day above $' + (netTotal * 0.40).toFixed(0)
               + ' would push you over.'));

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

    // 1.0.8: 佈局重排 — WORST DAY 接在 TOTAL LOSS 後;WIN RATE 全寬置於
    // EXIT % 之前;CURRENT ZONE 全寬獨立一行 → ASIA..RTH 兩兩自動對齊。
    const items = [
        { label: totalPnlLabel,
          value: '$' + total_pnl.toFixed(0) + paren(liveStats ? '$' + liveStats.total_pnl.toFixed(0) : ''),
          cls: total_pnl >= 0 ? 'pos' : 'neg' },
        { label: 'MAX DD',
          value: '$' + max_dd.toFixed(0) + paren(liveStats ? '$' + liveStats.max_dd.toFixed(0) : ''),
          cls: max_dd > 0 ? 'neg' : '' },
        // 1.0.9: BEST DAY —— Topstep XFA 一致性的分子。超過 40% 時掛黃色警告三角。
        { label: 'BEST DAY'
                 + (consistOver
                    ? ' <span class="tpx-warn" title="' + _attr(consistTip) + '">&#9888;</span>'
                    : ''),
          value: (bestDay != null ? '$' + bestDay.toFixed(0) : '--')
                 + (consistPct != null
                    ? ' <span class="metric-real' + (consistOver ? ' tpx-warn' : '')
                      + '" title="' + _attr(consistTip) + '">('
                      + (consistPct * 100).toFixed(0) + '% consist)</span>'
                    : ''),
          cls: consistOver ? 'neg' : (bestDay > 0 ? 'pos' : '') },
        { label: 'TOTAL GAIN',
          value: '$' + total_gain.toFixed(0) + paren(liveStats ? '$' + liveStats.total_gain.toFixed(0) : ''),
          cls: total_gain > 0 ? 'pos' : '' },
        { label: 'TOTAL LOSS',
          value: '$' + total_loss.toFixed(0) + paren(liveStats ? '$' + liveStats.total_loss.toFixed(0) : ''),
          cls: total_loss < 0 ? 'neg' : '' },
        { label: 'MONTHLY GAIN AVG',
          value: (monthlyAvg != null ? '$' + monthlyAvg.toFixed(0) : '--')
                 + paren(liveMonthlyAvg != null ? '$' + liveMonthlyAvg.toFixed(0) : ''),
          cls: (monthlyAvg != null && monthlyAvg >= 0) ? 'pos' : 'neg' },
        { label: 'WORST DAY',
          value: (worstDay != null ? '$' + worstDay.toFixed(0) : '--')
                 + paren(liveWorst != null ? '$' + liveWorst.toFixed(0) : ''),
          cls: (worstDay != null && worstDay < 0) ? 'neg' : '' },
        { label: 'PROFIT FACTOR',
          value: fmtPF(pfPrimary) + paren(pfLive != null ? fmtPF(pfLive) : ''),
          cls: pfPrimary >= 1 ? 'pos' : 'neg' },
        { label: 'CALMAR',
          value: calmarPrimary.toFixed(2) + paren(liveStats ? (liveStats.calmar != null ? liveStats.calmar : 0).toFixed(2) : ''),
          cls: calmarPrimary >= 1 ? 'pos' : '' },
        { label: 'TRADE COUNTS',
          value: String(total_trades || 0) + paren(liveStats ? String(liveStats.trades || 0) : ''),
          cls: '' },
        weeklyVarItem,
        { label: 'RR RATIO',
          value: rr_ratio.toFixed(2) + paren(liveStats ? liveStats.rr_ratio.toFixed(2) : ''),
          cls: rr_ratio > 1 ? 'pos' : 'neg' },
        // 1.0.9: WIN RATE 拆成 LONG / SHORT 兩卡(自動兩兩對齊)
        (function () {
            let ln = 0, lw = 0;
            (backtestTrades || []).forEach(t => {
                const d = String(t.direction || '').toUpperCase();
                if (d.includes('BUY') || d.includes('LONG')) { ln++; if ((t.pnl || 0) > 0) lw++; }
            });
            return { label: 'LONG WIN RATE',
                value: (ln ? (lw / ln * 100).toFixed(1) : '--') + '%' + paren(ln + ' tr'),
                cls: ln && lw / ln >= 0.5 ? 'pos' : '' };
        })(),
        (function () {
            let sn = 0, sw = 0;
            (backtestTrades || []).forEach(t => {
                const d = String(t.direction || '').toUpperCase();
                if (!(d.includes('BUY') || d.includes('LONG'))) { sn++; if ((t.pnl || 0) > 0) sw++; }
            });
            return { label: 'SHORT WIN RATE',
                value: (sn ? (sw / sn * 100).toFixed(1) : '--') + '%' + paren(sn + ' tr'),
                cls: sn && sw / sn >= 0.5 ? 'pos' : '' };
        })(),
        { label: 'EXIT % TP/SL/TRAIL',
          value: fmtPctTriple(backtestStats) + paren(liveStats ? fmtPctTriple(liveStats) : ''),
          cls: '' },
        { label: 'AVG $ TP/SL/TRAIL',
          value: fmtAvgTriple(backtestStats) + paren(liveStats ? fmtAvgTriple(liveStats) : ''),
          cls: '' },
        { label: 'CURRENT ZONE TP/SL/TRAIL', full: true,
          value: fmtZoneExitBuckets(currentZoneStats),
          cls: '' },
        { label: 'ASIA TP/SL/TRAIL', value: fmtSessionTriple(backtestStats, 'ASIA'), cls: '' },
        { label: 'EURO TP/SL/TRAIL', value: fmtSessionTriple(backtestStats, 'EURO'), cls: '' },
        { label: 'PRE TP/SL/TRAIL',  value: fmtSessionTriple(backtestStats, 'PRE'), cls: '' },
        { label: 'RTH TP/SL/TRAIL',  value: fmtSessionTriple(backtestStats, 'RTH'), cls: '' },
    ];

    grid.innerHTML = items.map(i => `
        <div class="metric-card"${i.full ? ' style="grid-column:1 / -1;"' : ''}>
            <div class="label">${i.label}</div>
            <div class="value ${i.cls}">${i.value}</div>
        </div>
    `).join('');
}

function renderTrades(trades) {
    const tbody = document.getElementById('trades-tbody');
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="13" style="text-align:center;color:var(--text2);padding:20px;">NO TRADE DATA</td></tr>';
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
    const esc = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    const explainTrade = (t) => {
        if (t.reason) return String(t.reason);
        const labels = Array.isArray(t.labels) ? t.labels.join(', ') : '';
        if (t.or_range) {
            const r = t.or_range || {};
            const side = _tradeIsBuy(t) ? 'OR low fake -> long' : 'OR high fake -> short';
            return 'OR15 ' + side + ' | OR ' + Number(r.or_low).toFixed(2) + '~' + Number(r.or_high).toFixed(2);
        }
        return labels || t.zone_id || '--';
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
        const why = explainTrade(t);
        const whyShort = why.length > 96 ? why.slice(0, 93) + '...' : why;

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
            '<td title="' + esc(why) + '" style="color:var(--text2);font-family:\'IBM Plex Mono\',monospace;max-width:360px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(whyShort) + '</td>' +
        '</tr>';
    }).join('');
}

// Render real TopstepX trade history in the EXECUTE TRADES bottom tab
function renderExecuteTrades(trades) {
    const tbody = document.getElementById('execute-tbody');
    if (!tbody) return;
    if (!trades || trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="13" style="text-align:center;color:var(--text2);padding:20px;">NO EXECUTE TRADE DATA</td></tr>';
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
    const esc = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    const explainTrade = (t) => {
        if (t.reason) return String(t.reason);
        const labels = Array.isArray(t.labels) ? t.labels.join(', ') : '';
        if (t.or_range) {
            const r = t.or_range || {};
            const side = _tradeIsBuy(t) ? 'OR low fake -> long' : 'OR high fake -> short';
            return 'OR15 ' + side + ' | OR ' + Number(r.or_low).toFixed(2) + '~' + Number(r.or_high).toFixed(2);
        }
        return labels || t.zone_id || '--';
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
        const why = explainTrade(t);
        const whyShort = why.length > 96 ? why.slice(0, 93) + '...' : why;

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
            '<td title="' + esc(why) + '" style="color:var(--text2);font-family:\'IBM Plex Mono\',monospace;max-width:360px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + esc(whyShort) + '</td>' +
        '</tr>';
    }).join('');
}

function classifyZoneType(z) {
    if (!z.formed_at) return '-';
    const code = getSessionCodeFromDate(new Date(z.formed_at));
    // 1.0.9 i18n: EN 顯示代碼,繁中經 t() 對照(亞盤/歐盤/盤前/早盤/盤後)
    return ['ASIA', 'EURO', 'PRE', 'RTH', 'AH'].indexOf(code) >= 0 ? t(code) : '-';
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

// ── PNL CURVE tab: cumulative equity + Topstep $2K trailing-DD line ──
// The DD line starts $2000 below break-even and trails UP only as each day's
// settled PnL sets a new equity high ("increase as income settles every day"),
// then LOCKS at break-even (0) once it has climbed from -2000 to 0 — i.e. once
// cumulative profit reaches +$2000. Mirrors Topstep's EOD trailing drawdown.
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

    const content = host.closest('.bottom-content');
    const headerH = 0; // 1.0.9: 標題列已移除
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
        ctx.fillText(t('No trades yet — run BACKTEST or load LIVE trades'), W / 2, H / 2);
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
    // 1.0.9: 標題列統計 hint 已移除(final/peak/maxDD 文字)
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

function _calCurveSeries(map) {
    const y = _calMonth.getFullYear();
    const m = _calMonth.getMonth();
    const days = new Date(y, m + 1, 0).getDate();
    let equity = 0;
    const out = [];
    for (let day = 1; day <= days; day++) {
        const key = _calDateKey(new Date(y, m, day));
        equity += Number((map[key] || {}).pnl || 0);
        out.push({ day, value: equity });
    }
    return out;
}

function _svgPath(points, xScale, yScale) {
    if (!points.length) return '';
    return points.map((p, i) => (i ? 'L' : 'M') + xScale(p.day).toFixed(1) + ' ' + yScale(p.value).toFixed(1)).join(' ');
}

function renderWeeklyIncomeCurve(btMap, liveMap) {
    const wrap = document.getElementById('cal-income-curve');
    const status = document.getElementById('cal-curve-status');
    if (!wrap) return;
    const btSeries = _calCurveSeries(btMap || {});
    const liveSeries = _calCurveSeries(liveMap || {});
    const vals = btSeries.concat(liveSeries).map(p => p.value);
    const minV = Math.min(0, ...vals);
    const maxV = Math.max(0, ...vals);
    const pad = Math.max(100, (maxV - minV) * 0.12);
    const lo = minV - pad;
    const hi = maxV + pad;
    const w = 900, h = 168, l = 40, r = 16, t = 14, b = 24;
    const days = btSeries.length || 1;
    const x = day => l + (day - 1) * ((w - l - r) / Math.max(1, days - 1));
    const y = val => t + (hi - val) * ((h - t - b) / Math.max(1, hi - lo));
    const zeroY = y(0);
    const weekLines = [];
    for (let d = 1; d <= days; d++) {
        const dt = new Date(_calMonth.getFullYear(), _calMonth.getMonth(), d);
        if (dt.getDay() === 0 && d !== 1) {
            const xx = x(d);
            weekLines.push(`<line x1="${xx.toFixed(1)}" y1="${t}" x2="${xx.toFixed(1)}" y2="${h - b}" stroke="rgba(247,239,224,0.08)"/>`);
        }
    }
    const btPath = _svgPath(btSeries, x, y);
    const livePath = _svgPath(liveSeries, x, y);
    const btLast = btSeries.length ? btSeries[btSeries.length - 1].value : 0;
    const liveLast = liveSeries.length ? liveSeries[liveSeries.length - 1].value : 0;
    wrap.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
        <rect x="0" y="0" width="${w}" height="${h}" fill="transparent"/>
        ${weekLines.join('')}
        <line x1="${l}" y1="${zeroY.toFixed(1)}" x2="${w - r}" y2="${zeroY.toFixed(1)}" stroke="rgba(247,239,224,0.18)"/>
        <path d="${btPath}" fill="none" stroke="rgba(0,229,160,0.95)" stroke-width="2.4"/>
        <path d="${livePath}" fill="none" stroke="rgba(255,176,32,0.95)" stroke-width="2.4"/>
        <text x="${l}" y="11" fill="rgba(0,229,160,0.95)" font-size="10" font-family="IBM Plex Mono">BT ${_calFmtMoney(btLast)}</text>
        <text x="${w - 190}" y="11" fill="rgba(255,176,32,0.95)" font-size="10" font-family="IBM Plex Mono">LIVE ${_calFmtMoney(liveLast)}</text>
        <text x="${l}" y="${h - 6}" fill="rgba(247,239,224,0.35)" font-size="9" font-family="IBM Plex Mono">daily cumulative, week separators shown</text>
    </svg>`;
    if (status) {
        status.textContent = 'BT ' + _calFmtMoney(btLast) + ' | LIVE ' + _calFmtMoney(liveLast) + ' | visible month';
    }
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

function _orderCard(label, value, sub, cls) {
    return '<div class="order-compare-card">'
        + '<div class="k">' + label + '</div>'
        + '<div class="v ' + (cls || '') + '">' + value + '</div>'
        + '<div class="s">' + (sub || '') + '</div>'
        + '</div>';
}

function renderOrderComparison() {
    const wrap = document.getElementById('order-compare-cards');
    const status = document.getElementById('order-compare-status');
    if (!wrap) return;
    const btTrades = _calTradesInVisibleMonth(backtestData ? backtestData.trades : []);
    const liveTrades = _calTradesInVisibleMonth(_calLiveTrades || []);
    const btFree = btTrades.map((_, i) => i);
    const pairs = [];
    const timeLimitMs = 5 * 60 * 1000;
    const tick = 0.25;
    for (const lv of liveTrades) {
        const lt = _tradeTs(lv, 'entry_time');
        const lp = _tradeEntry(lv);
        const ld = _tradeDir(lv);
        if (!lt || lp == null || !ld) continue;
        let best = -1, bestCost = Infinity;
        for (const idx of btFree) {
            const bt = btTrades[idx];
            if (_tradeDir(bt) !== ld) continue;
            const btTime = _tradeTs(bt, 'entry_time');
            const bp = _tradeEntry(bt);
            if (!btTime || bp == null) continue;
            const dt = Math.abs(lt - btTime);
            if (dt > timeLimitMs) continue;
            const ticks = Math.abs(lp - bp) / tick;
            const cost = dt / 60000 + ticks * 0.15;
            if (cost < bestCost) {
                best = idx;
                bestCost = cost;
            }
        }
        if (best >= 0) {
            btFree.splice(btFree.indexOf(best), 1);
            const bt = btTrades[best];
            const btTime = _tradeTs(bt, 'entry_time');
            const bp = _tradeEntry(bt);
            const dtMin = (lt - btTime) / 60000;
            const diffTicks = (lp - bp) / tick;
            const intended = Number(lv.intended_entry_price != null ? lv.intended_entry_price : lv.signal_entry_price);
            const slipTicks = Number.isFinite(intended) ? ((lp - intended) / tick) : diffTicks;
            pairs.push({ live: lv, bt, dtMin, diffTicks, slipTicks });
        }
    }
    const matched = pairs.length;
    const liveOnly = liveTrades.filter(lv => !pairs.some(p => p.live === lv));
    const btOnly = btFree.map(i => btTrades[i]);
    const sameLevel = pairs.filter(p => Math.abs(p.diffTicks) <= 2).length;
    const avgDiff = matched ? pairs.reduce((a, p) => a + Math.abs(p.diffTicks), 0) / matched : 0;
    const slipRows = pairs.filter(p => Math.abs(p.slipTicks) > 0.5);
    const avgSlip = slipRows.length ? slipRows.reduce((a, p) => a + Math.abs(p.slipTicks), 0) / slipRows.length : 0;
    const liveOnlyLoss = liveOnly.filter(t => Number(t.pnl || t.topstep_pnl || 0) < 0);
    const liveOnlyGain = liveOnly.filter(t => Number(t.pnl || t.topstep_pnl || 0) > 0);
    const blocked = btOnly.filter(bt => {
        const btTime = _tradeTs(bt, 'entry_time');
        if (!btTime) return false;
        return liveTrades.some(lv => {
            const a = _tradeTs(lv, 'entry_time');
            const z = _tradeTs(lv, 'exit_time');
            return a && z && btTime >= a && btTime <= z;
        });
    });
    const presetSel = document.getElementById('preset-bt');
    const presetName = presetSel ? presetSel.value : '';
    const matchRate = Math.max(btTrades.length, liveTrades.length) ? matched / Math.max(btTrades.length, liveTrades.length) : 0;
    wrap.innerHTML =
        _orderCard('BT ORDERS', String(btTrades.length), presetName && presetName !== 'default' ? _presetDisplayName(presetName) : 'current backtest') +
        _orderCard('LIVE ORDERS', String(liveTrades.length), 'deduped visible month') +
        _orderCard('TIME MATCH ≤5M', _fmtPct(matchRate), matched + ' matched / max(BT,LIVE)', matchRate >= 0.9 ? 'institution-pos' : 'institution-neg') +
        _orderCard('SAME LEVEL ≤2T', _fmtPct(matched ? sameLevel / matched : 0), sameLevel + '/' + matched + ' matched orders') +
        _orderCard('AVG ENTRY DIFF', avgDiff.toFixed(1) + 't', 'abs live fill vs BT entry') +
        _orderCard('FILL SLIP', slipRows.length + ' orders', 'avg ' + avgSlip.toFixed(1) + 't; future ledger exact') +
        _orderCard('EXTRA LIVE LOSS', String(liveOnlyLoss.length), 'not shown in BT result', liveOnlyLoss.length ? 'institution-neg' : '') +
        _orderCard('ACCIDENT GAIN', String(liveOnlyGain.length), 'live gain not expected by BT', liveOnlyGain.length ? 'institution-pos' : '') +
        _orderCard('MISSED BT', String(btOnly.length), 'BT orders not in live') +
        _orderCard('BLOCKED BY LIVE', String(blocked.length), 'BT signal while live trade still open');
    if (status) {
        status.textContent = 'pairing: same direction, entry time within 5m; historical preset attribution is approximate until new ledger fields accumulate.';
    }
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

// ════════════════════════════════════════════════════════════════════════
// RESEARCH robustness (1.0.9) — Monte Carlo · Walk-Forward · Slippage.
// Replaces the old Hunter/Sweep/Liquidity summary. Runs entirely client-side
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

const _ROB_POINT_VALUE = { MNQ: 2, NQ: 20, ENQ: 20, MES: 5, ES: 50, MGC: 10, GC: 100, ZL: 600 };
const _ROB_TICK = 0.25;
// Documented EMAPMO market fill 2026-07-23 14:31Z: +3.5 pts vs strategy price.
const _ROB_SLIP_ANCHOR_TICKS = 14;

function _robTickValue(trades) {
    const sym = String((trades[0] || {}).symbol || '/MNQ').replace('/', '').toUpperCase();
    return (_ROB_POINT_VALUE[sym] || 2) * _ROB_TICK;
}

function _robSeriesStats(pnls) {
    let gain = 0, loss = 0, eq = 0, peak = 0, dd = 0, wins = 0;
    for (const p of pnls) {
        if (p > 0) { gain += p; wins++; } else loss += -p;
        eq += p;
        if (eq > peak) peak = eq;
        if (peak - eq > dd) dd = peak - eq;
    }
    return {
        pnl: eq,
        pf: loss > 0 ? gain / loss : (gain > 0 ? 999 : 0),
        maxDd: dd,
        win: pnls.length ? wins / pnls.length : 0,
        n: pnls.length,
    };
}

function _robMonteCarlo(pnls, iters) {
    const n = pnls.length;
    if (n < 10) return null;
    iters = iters || 1000;
    const totals = [], dds = [], pfs = [];
    for (let i = 0; i < iters; i++) {
        let eq = 0, peak = 0, dd = 0, gain = 0, loss = 0;
        for (let j = 0; j < n; j++) {
            const p = pnls[(Math.random() * n) | 0];
            if (p > 0) gain += p; else loss += -p;
            eq += p;
            if (eq > peak) peak = eq;
            if (peak - eq > dd) dd = peak - eq;
        }
        totals.push(eq);
        dds.push(dd);
        pfs.push(loss > 0 ? gain / loss : (gain > 0 ? 999 : 0));
    }
    totals.sort((a, b) => a - b);
    dds.sort((a, b) => a - b);
    pfs.sort((a, b) => a - b);
    const q = (arr, p) => arr[Math.min(arr.length - 1, Math.max(0, Math.round(p * (arr.length - 1))))];
    return {
        iters, totals,
        pnlP5: q(totals, 0.05), pnlP50: q(totals, 0.50), pnlP95: q(totals, 0.95),
        pLoss: totals.filter(v => v <= 0).length / iters,
        ddP50: q(dds, 0.50), ddP95: q(dds, 0.95),
        pDd2k: dds.filter(v => v > 2000).length / iters,
        pfP5: q(pfs, 0.05),
    };
}

function _robMcPass(mc) {
    return !!mc && mc.pLoss <= 0.05 && mc.ddP95 < 2000 && mc.pfP5 > 1.0;
}

let _robLegacyMcCache = null;
let _robTopstepCache = null;

function _robTradesCacheKey(trades) {
    return (trades || []).map(tr => [
        tr.trade_id || '', tr.exit_time || tr.entry_time || '',
        Number(tr.pnl || 0).toFixed(4), Number(tr.size || 1).toFixed(2), tr.symbol || '',
    ].join(':')).join('|');
}

function _robCachedMonteCarlo(trades, pnls, force) {
    const cacheKey = _robTradesCacheKey(trades);
    if (!force && _robLegacyMcCache && _robLegacyMcCache.cacheKey === cacheKey) {
        return _robLegacyMcCache.value;
    }
    const value = _robMonteCarlo(pnls, 1000);
    _robLegacyMcCache = { cacheKey: cacheKey, value: value };
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

function _robTopstepOutcomeText(result, contracts, limit) {
    if (!result || !Array.isArray(result.observed)) return contracts + ' MNQ —';
    const row = result.observed.find(r => r.contracts === contracts && r.consistencyLimit === limit);
    if (!row) return contracts + ' MNQ —';
    const label = row.status === 'pass' ? 'met' : (row.status === 'fail' ? 'MLL fail' : 'not yet');
    return contracts + ' MNQ ' + label + ' d' + row.days
        + ' · equity ' + _robUsd(row.equity) + ' / required ' + _robUsd(row.target);
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
        sizes: [1, 2],
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
    const c1 = _robTopstepRow(combine, 1, 0.5);
    const c2 = _robTopstepRow(combine, 2, 0.5);
    const x1 = _robTopstepRow(xfa, 1, 0.4);
    const x2 = _robTopstepRow(xfa, 2, 0.4);
    const winner = combine.recommendation ? combine.recommendation.contracts : 1;
    const xfaWinner = xfa.recommendation ? xfa.recommendation.contracts : 1;
    const winRow = winner === 1 ? c1 : c2;
    const loseRow = winner === 1 ? c2 : c1;
    const passEdge = winRow && loseRow ? Math.abs(winRow.passRate - loseRow.passRate) * 100 : 0;
    const faster = c1 && c2 && c1.medianPassDays != null && c2.medianPassDays != null
        ? Math.abs(c1.medianPassDays - c2.medianPassDays) : null;
    const fasterSize = c1 && c2 && c1.medianPassDays <= c2.medianPassDays ? 1 : 2;
    const xfaWinRow = xfaWinner === 1 ? x1 : x2;
    const xfaLoseRow = xfaWinner === 1 ? x2 : x1;
    const xfaEdge = xfaWinRow && xfaLoseRow
        ? Math.abs(xfaWinRow.passRate - xfaLoseRow.passRate) * 100 : 0;
    const daysText = row => row && row.medianPassDays != null && row.p90PassDays != null
        ? row.medianPassDays + ' / ' + row.p90PassDays : '—';

    const combineRow = row => {
        if (!row) return '';
        return '<tr class="' + (row.contracts === winner ? 'rob-size-winner' : '') + '">'
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
        return '<tr class="' + (row.contracts === xfaWinner ? 'rob-size-winner' : '') + '">'
            + '<td>' + row.contracts + ' MNQ</td>'
            + '<td class="institution-pos">' + _robPct(row.passRate, 2) + '</td>'
            + '<td class="' + (row.failRate > 0.02 ? 'institution-neg' : '') + '">' + _robPct(row.failRate, 2) + '</td>'
            + '<td>' + _robPct(row.openRate, 2) + '</td>'
            + '<td>' + daysText(row) + '</td>'
            + '<td>' + _robUsd(row.medianPassTarget) + '</td>'
            + '</tr>';
    };

    const source = combine.source || {};
    const slipLabel = Number(slipTicks || 0) > 0
        ? Math.round(slipTicks) + 't (' + _robUsd(analysis.slipPerContract) + '/contract) entry-slip stress'
        : 'no added slippage';
    return '<div class="institution-card institution-wide rob-topstep-card">'
        + '<h3>TOPSTEP 50K · PAIRED EVALUATION MONTE CARLO · 10,000×</h3>'
        + '<div class="rob-topstep-verdict"><strong>COMBINE · ' + winner + ' MNQ = HIGHER PASS ODDS</strong>'
        + '<span>within 60 active days · +' + passEdge.toFixed(2) + ' percentage points vs ' + (winner === 1 ? 2 : 1) + ' MNQ'
        + (faster != null && faster > 0 ? ' · ' + fasterSize + ' MNQ is ' + faster + ' active day(s) faster at median' : '')
        + '</span></div>'
        + '<div class="rob-topstep-rules">'
        + '<section class="rob-topstep-rule"><h4>TRADING COMBINE · 50% <span>pass evaluation</span></h4>'
        + '<table class="institution-table"><thead><tr><th>SIZE</th><th>PASS</th><th>MLL FAIL</th><th>OPEN@60</th><th>DAYS P50/P90</th><th>TARGET ↑</th><th>REQ P50</th></tr></thead><tbody>'
        + combineRow(c1) + combineRow(c2) + '</tbody></table>'
        + '<div class="rob-topstep-observed">Observed sequence · '
        + _robTopstepOutcomeText(combine, 1, 0.5) + '<br>'
        + _robTopstepOutcomeText(combine, 2, 0.5) + '</div>'
        + '<div class="rob-note">$2,500 best day ⇒ $5,000 evaluation target (best day ÷ 50%). Minimum 2 active days.</div>'
        + '</section>'
        + '<section class="rob-topstep-rule"><h4>XFA CONSISTENCY · 40% <span>after pass · first payout eligibility</span></h4>'
        + '<div class="rob-topstep-program-verdict"><strong>' + xfaWinner + ' MNQ</strong> = higher first-payout eligibility within 60 active days · +'
        + xfaEdge.toFixed(2) + ' percentage points</div>'
        + '<table class="institution-table"><thead><tr><th>SIZE</th><th>ELIGIBLE</th><th>MLL FAIL</th><th>OPEN@60</th><th>DAYS P50/P90</th><th>MIN PAYOUT BAL P50</th></tr></thead><tbody>'
        + xfaRow(x1) + xfaRow(x2) + '</tbody></table>'
        + '<div class="rob-topstep-observed">Observed sequence · '
        + _robTopstepOutcomeText(xfa, 1, 0.4) + '<br>'
        + _robTopstepOutcomeText(xfa, 2, 0.4) + '</div>'
        + '<div class="rob-note">$2,500 best day ⇒ $6,250 net balance needed (best day ÷ 40%). Minimum 3 active days. The starting $250 here is not a profit target: $125 minimum request ÷ 50% payoutable balance = $250. This does not change the Combine target.</div>'
        + '</section></div>'
        + '<div class="rob-note">' + source.tradeCount + ' settled MNQ trades · ' + source.days.length
        + ' Topstep active days · 60-active-day horizon · same sampled days for both sizes · ' + slipLabel
        + '. MLL is replayed from closed-trade P&amp;L; intratrade unrealized drawdown is unavailable, so probabilities are optimistic.</div>'
        + '</div>';
}

function _robWalkForward(trades) {
    const rows = trades.filter(tr => tr.entry_time);
    if (rows.length < 6) return null;
    const times = rows.map(tr => new Date(tr.entry_time).getTime());
    const t0 = Math.min(...times), t1 = Math.max(...times);
    const span = Math.max(1, t1 - t0);
    const segs = [[], [], []];
    rows.forEach((tr, i) => {
        const seg = Math.min(2, Math.floor((times[i] - t0) * 3 / span));
        segs[seg].push(Number(tr.pnl) || 0);
    });
    const stats = segs.map(_robSeriesStats);
    return { stats, pass: stats.every(s => s.n > 0 && s.pnl > 0 && s.pf > 1.0) };
}

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

function _robHistogram(totals) {
    const bins = 18;
    const min = totals[0], max = totals[totals.length - 1];
    const w = (max - min) || 1;
    const counts = new Array(bins).fill(0);
    for (const v of totals) counts[Math.min(bins - 1, Math.floor((v - min) / w * bins))]++;
    const peak = Math.max(...counts, 1);
    let html = '<div class="rob-hist">';
    for (let i = 0; i < bins; i++) {
        const lo = min + w * i / bins;
        const hi = min + w * (i + 1) / bins;
        html += '<i class="' + (hi <= 0 ? 'neg' : 'pos') + '" style="height:'
            + Math.max(4, counts[i] / peak * 100).toFixed(1) + '%" title="$'
            + Math.round(lo) + ' .. $' + Math.round(hi) + ' : ' + counts[i] + '"></i>';
    }
    return html + '</div>';
}

function _robBadge(pass, passText, failText) {
    return '<span class="rob-badge ' + (pass ? 'institution-pos' : 'institution-neg') + '">'
        + t(pass ? passText : failText) + '</span>';
}

function renderResearchRobustness(force) {
    const status = document.getElementById('robustness-status');
    const content = document.getElementById('robustness-content');
    if (!status || !content) return;
    const trades = (backtestData && backtestData.trades)
        ? backtestData.trades.filter(tr => tr.pnl != null) : [];
    if (!trades.length) {
        status.textContent = t('Run a backtest first — analysis uses the latest backtest trades.');
        content.innerHTML = '';
        return;
    }
    const pnls = trades.map(tr => Number(tr.pnl) || 0);
    const base = _robSeriesStats(pnls);
    const tickVal = _robTickValue(trades);
    const times = trades.map(tr => new Date(tr.entry_time).getTime()).filter(Number.isFinite);
    const d0 = times.length ? new Date(Math.min(...times)) : null;
    const d1 = times.length ? new Date(Math.max(...times)) : null;
    status.textContent = trades.length + ' ' + t('trades') + ' · '
        + String((trades[0] || {}).symbol || '')
        + (d0 ? (' · ' + d0.toISOString().slice(0, 10) + ' → ' + d1.toISOString().slice(0, 10)) : '')
        + ' · PF ' + _researchNum(base.pf, 2)
        + ' · PnL $' + Math.round(base.pnl)
        + ' · maxDD $' + Math.round(base.maxDd);

    const slip = _robMeasureSlip();
    const topstepSlipPerContract = Math.max(0, Number(slip.usedTicks) || 0) * tickVal;
    const topstep = _robTopstepAnalysis(trades, topstepSlipPerContract, !!force);
    const topstepHtml = _robTopstepHtml(topstep, slip.usedTicks);

    // ── Monte Carlo ──────────────────────────────────────────
    const mc = _robCachedMonteCarlo(trades, pnls, !!force);
    let mcHtml;
    if (!mc) {
        mcHtml = '<div class="institution-status">' + t('Not enough trades (need ≥10).') + '</div>';
    } else {
        const row = (k, v, cls) => '<tr><td>' + t(k) + '</td><td class="' + (cls || '') + '">' + v + '</td></tr>';
        mcHtml =
            _robHistogram(mc.totals)
            + '<table class="institution-table"><tbody>'
            + row('Total PnL P5 / P50 / P95',
                '$' + Math.round(mc.pnlP5) + ' / $' + Math.round(mc.pnlP50) + ' / $' + Math.round(mc.pnlP95),
                _researchClass(mc.pnlP5))
            + row('P(total loss)', _researchNum(mc.pLoss * 100, 1) + '%', mc.pLoss > 0.05 ? 'institution-neg' : 'institution-pos')
            + row('maxDD P50 / P95', '$' + Math.round(mc.ddP50) + ' / $' + Math.round(mc.ddP95),
                mc.ddP95 >= 2000 ? 'institution-neg' : 'institution-pos')
            + row('P(maxDD > $2k)', _researchNum(mc.pDd2k * 100, 1) + '%', mc.pDd2k > 0.05 ? 'institution-neg' : 'institution-pos')
            + row('PF P5', _researchNum(mc.pfP5, 2), mc.pfP5 > 1 ? 'institution-pos' : 'institution-neg')
            + '</tbody></table>';
    }

    // ── Walk-forward ─────────────────────────────────────────
    const wf = _robWalkForward(trades);
    let wfHtml;
    if (!wf) {
        wfHtml = '<div class="institution-status">' + t('Not enough trades (need ≥6).') + '</div>';
    } else {
        wfHtml = '<table class="institution-table"><thead><tr><th>' + t('Segment')
            + '</th><th>N</th><th>PnL</th><th>PF</th><th>' + t('Win%') + '</th></tr></thead><tbody>'
            + wf.stats.map((s, i) => '<tr>'
                + '<td>' + (i + 1) + '/3</td>'
                + '<td>' + s.n + '</td>'
                + '<td class="' + _researchClass(s.pnl) + '">$' + Math.round(s.pnl) + '</td>'
                + '<td class="' + (s.pf > 1 ? 'institution-pos' : 'institution-neg') + '">' + _researchNum(s.pf, 2) + '</td>'
                + '<td>' + _researchNum(s.win * 100, 1) + '%</td>'
                + '</tr>').join('')
            + '</tbody></table>';
    }

    // ── Slippage injection ───────────────────────────────────
    const sizes = trades.map(tr => Number(tr.size) || 1);
    const used = Math.max(1, Math.round(slip.usedTicks));
    const levels = [1, 2, 4, 8];
    if (levels.indexOf(used) < 0) levels.push(used);
    levels.sort((a, b) => a - b);
    const slipRow = (lvl, stats, highlight) =>
        '<tr' + (highlight ? ' class="rob-slip-used"' : '') + '>'
        + '<td>' + (lvl === 0 ? t('original') : lvl + 't' + (highlight ? ' ★' : '')) + '</td>'
        + '<td class="' + _researchClass(stats.pnl) + '">$' + Math.round(stats.pnl) + '</td>'
        + '<td class="' + (stats.pf >= 1.5 ? 'institution-pos' : 'institution-neg') + '">' + _researchNum(stats.pf, 2) + '</td>'
        + '<td>' + (lvl === 0 ? '—' : _researchNum((stats.pf / Math.max(base.pf, 1e-9) - 1) * 100, 1) + '%') + '</td>'
        + '<td>$' + Math.round(stats.maxDd) + '</td>'
        + '</tr>';
    let slipHtml = '<div class="institution-status" style="margin-bottom:6px;">'
        + t('Measured market-entry slip') + ': '
        + (slip.anchor
            ? (used + 't (' + t('anchor — documented EMAPMO fill; market-like live sample') + ' n=' + slip.n + ')')
            : (used + 't (' + t('median of live market-like fills') + ' n=' + slip.n + ')'))
        + ' · 1t = $' + _researchNum(tickVal, 2) + '/ct'
        + '</div>'
        + '<table class="institution-table"><thead><tr><th>' + t('RT slip')
        + '</th><th>PnL</th><th>PF</th><th>ΔPF</th><th>maxDD</th></tr></thead><tbody>'
        + slipRow(0, base, false)
        + levels.map(lvl => slipRow(
            lvl,
            _robSeriesStats(pnls.map((p, i) => p - lvl * tickVal * sizes[i])),
            lvl === used)).join('')
        + '</tbody></table>'
        + '<div class="rob-note">' + t('Market entries pay the full slip each round turn (bracket follows the fill). Limit entries skip entry slip but miss fills instead. Small-SL variants lose PF fastest — check the 8t+ rows before moving a model to market entry.') + '</div>';

    const usedStats = _robSeriesStats(pnls.map((p, i) => p - used * tickVal * sizes[i]));
    content.innerHTML =
        '<div class="institution-grid">'
        + topstepHtml
        + '<div class="institution-card"><h3>' + t('MONTE CARLO') + ' · 1000×'
        + (mc ? ' ' + _robBadge(_robMcPass(mc), 'PASS', 'FAIL') : '') + '</h3>' + mcHtml + '</div>'
        + '<div class="institution-card"><h3>' + t('WALK-FORWARD') + ' · 3 ' + t('segments')
        + (wf ? ' ' + _robBadge(wf.pass, 'PASS', 'FAIL') : '') + '</h3>' + wfHtml + '</div>'
        + '<div class="institution-card institution-wide"><h3>' + t('SLIPPAGE INJECTION')
        + ' ' + _robBadge(usedStats.pf >= 1.5, 'PF OK AFTER SLIP', 'PF DEGRADES BELOW 1.5') + '</h3>'
        + slipHtml + '</div>'
        + '</div>';
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
    renderWeeklyIncomeCurve(bt, live);
    renderOrderComparison();
    renderResearchRobustness();   // 1.0.9: Monte Carlo / Walk-Forward / Slippage
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
        setPerfSource({ preset: d.preset_name, saved_at: d.saved_at, stale: true });
        log('Restored last backtest (' + (d.metrics.total_trades || (d.trades || []).length) +
            ' trades) from cache · ' + (d.preset_name || '?') + ' · ' +
            (d.saved_at ? d.saved_at.slice(0, 16).replace('T', ' ') : ''), 'info');
    } catch (e) {}
}
// Script tag is at end of <body>, so the DOM is already parsed here.
_restoreBacktestCache();
// 1.0.9: 啟動即載入上一次 sweep 結果 → PRESETS 分頁一開就有可排序/可加入的榜單
try { loadSweepResults(); } catch (e) {}

// ════════════════════════════════════════════════════════════════════════
// 1.0.9: Live account slots - ACCOUNT MAIN / ACCOUNT MINOR.
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
    [1, 2].forEach(s => {
        o['acct' + s] = (document.getElementById('live-acct-select-' + s) || {}).value || '';
        o['preset' + s] = (document.getElementById('live-acct-preset-' + s) || {}).value || '';
    });
    try { localStorage.setItem(_LIVE_SLOTS_KEY, JSON.stringify(o)); } catch (e) {}
}
function _defaultSlotAccount(slot, accts) {
    const express = accts.find(a => a.account_type === 'express');
    const practice = accts.find(a => a.account_type === 'practice');
    const main = accts.find(a => a.is_main);
    const s1 = (main || express || accts[0] || {}).id || '';
    if (slot === 1) return s1;
    const s2 = practice && practice.id !== s1 ? practice.id : (accts.find(a => a.id !== s1) || {}).id;
    return s2 || '';
}

async function initLiveSlots() {
    // presets 需在快取(供 preset 下拉);若空則抓一次
    if (!_presetsCache || !Object.keys(_presetsCache.presets || {}).length) {
        try { const pr = await fetch(API + '/presets'); if (pr.ok) { const pd = await pr.json(); if (pd && pd.presets) _presetsCache = pd; } } catch (e) {}
    }
    const accts = allAccounts || [];
    const presetNames = Object.keys((_presetsCache && _presetsCache.presets) || {}).sort(_comparePresetNames);
    const saved = _loadLiveSlots();
    [1, 2].forEach(slot => {
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
    pollLiveSlots();
}

// 1.0.9: GO LIVE 成功後把槽位指派寫進 data/account_roles.json —
// MAIN 槽帳號 = main_account_id,每帳號記 preset + live 旗標。
// terminal 模式(backend.terminal_live)靠這個檔案自動跟隨 main 帳號與其 preset。
async function _persistLiveRolesFromSlots() {
    try {
        const r = await fetch(API + '/accounts/roles');
        const cur = r.ok ? (((await r.json()) || {}).roles || {}) : {};
        const accounts = Object.assign({}, cur.accounts || {});
        [LIVE_MAIN_SLOT, LIVE_MINOR_SLOT].forEach(s => {
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
    const slotNum = Number(slot);
    if (!accId) { log(slotName + ': select account first', 'warn'); return; }
    if (!presetName || !(_presetsCache.presets || {})[presetName]) { log(slotName + ': select preset first', 'warn'); return; }
    if (!accId) { log('ACCOUNT ' + slot + ': select an account first', 'warn'); return; }
    if (!presetName || !(_presetsCache.presets || {})[presetName]) { log('ACCOUNT ' + slot + ': select a preset first', 'warn'); return; }
    // 兩槽不可選同一帳號
    const other = parseInt((document.getElementById('live-acct-select-' + (slotNum === LIVE_MAIN_SLOT ? LIVE_MINOR_SLOT : LIVE_MAIN_SLOT)) || {}).value);
    slot = slotNum === LIVE_MAIN_SLOT ? 'MAIN' : 'MINOR';
    if (other && other === accId) { log('MAIN and MINOR cannot use the same account', 'warn'); return; }
    const acc = (allAccounts || []).find(a => a.id === accId);
    const warn = (acc && acc.account_type === 'express') ? '\n⚠ EXPRESS FUNDED ACCOUNT: REAL ORDERS WILL BE PLACED!' : '';
    if (!confirm('GO LIVE (ACCOUNT ' + slot + ')\nAccount: ' + (acc ? acc.name : accId) + '\nPreset: ' + presetName + warn)) return;
    const preset = _presetsCache.presets[presetName];
    const body = Object.assign({}, preset, { account_id: accId });
    body.strategy = normalizeStrategyName(body.strategy);
    if (!body.contract_id) body.contract_id = fv('contract-id', 'CON.F.US.MNQ.U26');
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
    setTimeout(pollLiveSlots, 400);
}

async function liveSlotStop(slot) {
    const accId = parseInt((document.getElementById('live-acct-select-' + slot) || {}).value);
    slot = Number(slot) === LIVE_MAIN_SLOT ? 'MAIN' : 'MINOR';
    if (!accId) return;
    try { const r = await fetch(API + '/live/stop?account_id=' + accId, { method: 'POST' }); const d = await r.json(); log('ACCOUNT ' + slot + ' STOP:' + _acctEsc(d.message || ''), 'info'); }
    catch (e) { log('ACCOUNT ' + slot + ' STOP failed: ' + e.message, 'warn'); }
    setTimeout(pollLiveSlots, 300);
}

async function liveSlotFlatten(slot) {
    const accId = parseInt((document.getElementById('live-acct-select-' + slot) || {}).value);
    slot = Number(slot) === LIVE_MAIN_SLOT ? 'MAIN' : 'MINOR';
    if (!accId) return;
    if (!confirm('Emergency flatten ACCOUNT ' + slot + ' (' + accId + ')?')) return;
    try { const r = await fetch(API + '/live/flatten?account_id=' + accId, { method: 'POST' }); const d = await r.json(); log('ACCOUNT ' + slot + ' FLATTEN:' + _acctEsc(d.message || ''), 'warn'); }
    catch (e) { log('ACCOUNT ' + slot + ' FLATTEN failed: ' + e.message, 'warn'); }
    setTimeout(pollLiveSlots, 300);
}

// 帶動圖表/頂欄跟隨指定帳號(沿用既有 live chart machinery)
function _startLiveChartForAccount(acc, stratParams) {
    const mainAcc = _focusMainLiveAccount() || acc;
    if (mainAcc) liveAccount = mainAcc;
    stratParams = getMainLivePresetParams(stratParams);
    syncMainAccountPresetToPanels(true);
    try {
        _zoneFilter.mode = 'live';
        const tfs = (stratParams.method === 'overlap' && stratParams.tf_combo && stratParams.tf_combo.length)
            ? stratParams.tf_combo : [stratParams.area_timeframe];
        _zoneFilter.tfs = new Set(tfs);
        syncZoneFilterUI();
    } catch (e) {}
    const topBar = document.getElementById('live-top-bar');
    if (topBar) topBar.style.display = 'block';
    try { updateLiveTopBar(); } catch (e) {}
    _lastLiveCandleTime = '';
    if (_liveInterval) clearInterval(_liveInterval);
    _liveInterval = setInterval(pollLiveCandle, 1000); pollLiveCandle();
    if (_liveStatusInterval) clearInterval(_liveStatusInterval);
    _liveStatusInterval = setInterval(pollLiveStatus, 1000); pollLiveStatus();
    try { refreshTfZones(true); } catch (e) {}
    setTimeout(() => { try { refreshLiveZoneOverlay(stratParams); } catch (e) {} }, 0);
}

function _liveSlotRenderStatus(slot, statusMap, sess) {
    const accId = String((document.getElementById('live-acct-select-' + slot) || {}).value || '');
    const st = statusMap[accId];
    const dot = document.getElementById('live-slot-dot-' + slot);
    const set = (base, txt, color) => { const el = document.getElementById(base + '-' + slot); if (el) { el.textContent = txt; if (color) el.style.color = color; } };
    set('live-slot-mkt', sess.label, sess.color);
    if (!accId) {
        set('live-slot-status', '—', 'var(--text3)');
        if (dot) { dot.style.background = 'var(--text3)'; dot.style.boxShadow = 'none'; }
        ['live-slot-phase', 'live-slot-mode', 'live-slot-dl', 'live-slot-rv', 'live-slot-pnl'].forEach(b => set(b, '--', 'var(--text3)'));
        return;
    }
    if (st && st.running) {
        set('live-slot-status', 'RUNNING', 'var(--green)');
        if (dot) { dot.style.background = 'var(--green)'; dot.style.boxShadow = '0 0 6px var(--green)'; }
        set('live-slot-phase', st.phase || '--', 'var(--text2)');
        const activeModeName = st.active_mode ? strategyDisplayName(st.active_mode) : '';
        const strategyModeName = st.strategy_mode ? strategyDisplayName(st.strategy_mode) : 'TREND';
        const mode = st.confluence_shadow ? 'SHADOW (NO ORDERS)'
            : ((activeModeName && activeModeName !== strategyModeName) ? activeModeName : 'LIVE');
        set('live-slot-mode', mode, 'var(--green)');
        const pnl = st.daily_pnl || 0;
        set('live-slot-pnl', (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(0), pnl >= 0 ? 'var(--green)' : 'var(--red)');
        const g = st.risk_gates || {}, dl = g.daily_loss || {}, rv = g.prev_rv || {};
        set('live-slot-dl', dl.limit ? (dl.resting ? ('LOCKED ' + (dl.count || 0) + '/' + dl.limit) : ((dl.count || 0) + '/' + dl.limit)) : 'OFF', dl.resting ? 'var(--red)' : 'var(--text2)');
        set('live-slot-rv', rv.lookback ? (rv.blocking ? 'BLOCKED' : rv.lookback + 'D PASS') : 'OFF', rv.blocking ? 'var(--red)' : 'var(--text2)');
    } else {
        set('live-slot-status', st ? 'STOPPED' : 'NOT STARTED', 'var(--text3)');
        if (dot) { dot.style.background = 'var(--text3)'; dot.style.boxShadow = 'none'; }
        ['live-slot-phase', 'live-slot-mode', 'live-slot-dl', 'live-slot-rv', 'live-slot-pnl'].forEach(b => set(b, '--', 'var(--text3)'));
    }
}

async function pollLiveSlots() {
    const lp = document.getElementById('live-panel');
    if (!lp || lp.classList.contains('hidden')) return;   // 只在 Live 分頁輪詢
    const sess = getMarketSession();
    let statusMap = {};
    try {
        const r = await fetch(API + '/live/status-all');
        if (r.ok) { const d = await r.json(); (d.engines || []).forEach(e => { statusMap[String(e.account_id)] = e.status || {}; }); }
    } catch (e) {}
    [1, 2].forEach(slot => _liveSlotRenderStatus(slot, statusMap, sess));
}


// ════════════════════════════════════════════════════════════════════════
// UI language switch (1.0.9) — EN ⇄ 繁體中文.
// Static chrome is translated by walking text nodes against I18N_ZH (original
// English kept on each node, so switching back is lossless). Dynamic strings
// rendered by JS go through t(). A MutationObserver re-translates any DOM the
// app renders later (tables, panels) while Chinese is active.
// ════════════════════════════════════════════════════════════════════════

const UI_LANG_KEY = 'ancserTPX.uiLang';
let UI_LANG = 'en';
try { UI_LANG = localStorage.getItem(UI_LANG_KEY) === 'zh' ? 'zh' : 'en'; } catch (e) {}

const I18N_ZH = {
    // header / tabs
    'Research': '研究', 'Backtest': '回測', 'Live': '實盤',
    'USERNAME': '帳號', 'API KEY': 'API 金鑰', 'CONTRACT': '合約', 'CONTRACT ID': '合約代碼',
    'INTERVAL': '週期', 'BARS': 'K棒數', 'FETCH FULL DATA': '抓取完整數據', 'CONNECT': '連線',
    'MNQ (Micro NQ — $2/pt)': 'MNQ(微型 NQ — $2/點)', 'NQ (Mini NQ — $20/pt)': 'NQ(迷你 NQ — $20/點)',
    'CUSTOM…': '自訂…', 'MNQ ($2/pt)': 'MNQ($2/點)', 'NQ ($20/pt)': 'NQ($20/點)',
    'DISCONNECTED': '未連線', 'CONNECTED': '已連線',
    // sidebar
    'ENVIRONMENT': '環境', 'PRESET': '預設組', 'SAVE': '儲存', 'DEL': '刪除',
    'SIZE': '手數', 'MODEL': '模型', 'RETRAIN': '重新訓練',
    'TREND': 'TREND 趨勢突破',
    'DAY ZONE Prev-Day VA Revert': 'DAY ZONE 前日VA回歸',
    'DISTRIBUTION Rolling Fade': 'DISTRIBUTION 滾動分佈回歸',
    'MIN PROB': '最低勝率', 'EV FLOOR': 'EV 下限',
    'OFF (use win-rate gate)': 'OFF(用勝率門檻)', '≥0 (all positive EV)': '≥0(所有正期望值)',
    'BAND (ticks)': '帶寬(ticks)', 'MIN DISTINCT TF': '最少獨立TF',
    '(library · select = active)': '(版本庫 · 選擇即啟用)',
    '(fixed 1–6 · 0.25 step)': '(固定 1–6 · 支援 0.25)',
    'RISK MANAGEMENT': '風險管理', 'MAX RISK': '最大風險',
    'TRAIL TP TRIGGER': '移動停利觸發', 'SL REF TF': 'SL 參考TF',
    'LARGEST': '最大', 'SMALLEST': '最小',
    'SESSION MAX TRADE LIMIT': '單一時段限單', 'MARKET LIMIT': '交易時段',
    'ALL': '全部', 'ASIA + PRE': '亞盤+盤前',
    'ASIA': '亞盤', 'EURO': '歐盤', 'PRE': '盤前', 'RTH': '早盤', 'AH': '盤後',
    'MODEL SETTINGS': '模型設定', 'ENTRY TRIGGER': '進場觸發',
    '(fixed by model)': '(底層模型決定,不可改)',
    'TIMEFRAMES': '時間框架', '(pick 1 = single; pick 2+ = overlap)': '(選1=單一;選2+=重疊)',
    'TRADE ZONE': '交易區間', 'Merged overlap': '合併重疊區', 'Smallest selected TF': '最小已選TF',
    'AREA %': '區間 %', 'CONFIRM': '確認K數',
    'DAY ZONE ENTRY MODE': 'DAY ZONE 進場模式',
    'LIMIT resting at VAL (safest)': 'LIMIT 直接掛 VAL(最穩)',
    'REJECTION sweep-back market': 'REJECTION 掃回後市價',
    'OR15 open fake-break (2-way market)': 'OR15 開盤假突破(雙向·市價)',
    'FACTOR FAMILY': '因子族', 'SIDE': '方向', 'BOTH': '雙向',
    'LONG ONLY': '只做多', 'SHORT ONLY': '只做空',
    'SIGNAL MODE': '訊號模式', 'NORMAL': '標準', 'EARLY': '提早',
    'VA FILTER': 'VA 過濾', 'OUTSIDE VA80': 'VA80 之外',
    'SL ANCHOR': 'SL 錨點', 'SL INPUT': 'SL 參數',
    'TP ANCHOR': 'TP 錨點', 'FIXED RATIO': '固定比例', 'LADDER RATIO': '階梯比例',
    'TP INPUT': 'TP 參數', 'LADDER INPUT': '階梯參數', '(engine fixed)': '(引擎固定)',
    'TRAIL SL': '移動停損',
    'DAILY MAX TRADE LIMIT': '每日最大交易數',
    'FULL LOSS LOCK': '日虧鎖單',
    '(bot only · N daily losses stop new orders, 0=OFF)': '(僅程序交易;當日虧 N 單停新單,0=OFF)',
    'FULL WIN LOCK': '日贏落袋',
    '(bank N daily wins then stop, 0=OFF)': '(當日贏 N 單落袋停手,0=OFF)',
    'HIGH VOLATILITY LOCK': '高波動鎖',
    '(prev-day high vol pauses today, 0=OFF)': '(前日高波動→今日停手,0=OFF)',
    'last 10d': '近10日', 'last 15d': '近15日', 'last 20d': '近20日',
    'EXECUTE BACKTEST': '執行回測', 'SWEEP': '掃描', 'SWEEP MODEL': '掃描模型',
    'MNQx1 + risk locked': 'MNQx1 + 風控鎖定',
    'ALL MODELS': '全部模型', 'FACTOR ONLY': '只掃 FACTOR', 'TREND ONLY': '只掃 TREND',
    'DAY ZONE ONLY': '只掃 DAY ZONE', 'DISTRIBUTION ONLY': '只掃 DISTRIBUTION',
    'PERFORMANCE': '績效', 'BACKTEST': '回測',
    // live panel
    'ACCOUNT MAIN': '主帳號', 'ACCOUNT MINOR': '副帳號',
    'GO LIVE': '啟動實盤', 'STOP': '停止', 'FLAT': '平倉',
    '-- SELECT ACCOUNT --': '── 選擇帳號 ──', '-- SELECT PRESET --': '── 選擇預設組 ──',
    'STATUS:': '狀態:', 'PHASE:': '階段:', 'MODE:': '模式:', 'MARKET:': '時段:',
    'BOT LOSS LOCK:': '程序虧損鎖:', 'VOLATILITY GATE:': '波動閘:', 'DAILY PNL:': '當日損益:',
    'STRAT:': '策略:', 'POSITION:': '持倉:', 'CAPITAL:': '資金:',
    'RISK GATES': '風控閘', 'STATUS': '狀態', 'ACTIVE ZONE': '活躍區間',
    'ML DECISION BASIS': 'ML 決策依據',
    // bottom panel
    'PRESETS': '預設組', 'BACKTEST TRADES': '回測交易', 'EXECUTE TRADES': '實盤成交',
    'PNL CURVE': '損益曲線', 'SYSTEM LOG': '系統日誌',
    'SWEEP RESULTS · ALL MODELS · SORTED BY PF': '掃描結果 · 全模型 · 依 PF 排序',
    'ACC ★ pass only': '只顯示 ACC ★ 通過',
    'No results yet — run sidebar': '尚無結果 — 用側欄', '(~10–15 min).': '(約 10–15 分鐘)。',
    'No ACC ★ pass variants — untick the filter to see all.': '沒有通過 ACC ★ 的變體 — 取消勾選以看全部。',
    'pass ACC ★': '通過 ACC ★', '★ pass': '★通過', 'all': '全部', 'variants': '變體',
    'sort': '排序', 'click column header to change': '點欄位標題換排序',
    'SYMBOL': '商品', 'ENTRY TIME': '進場時間', 'EXIT TIME': '出場時間', 'DURATION': '持倉時長',
    'ENTRY': '進場價', 'EXIT': '出場價', 'P&L': '損益', 'COMMISSION': '佣金', 'FEES': '費用',
    'DIR': '方向', 'WHY': '原因',
    'No trades yet — run BACKTEST or load LIVE trades': '尚無成交 — 先跑回測或載入實盤交易',
    // research view
    'Today': '今天', '⟳ Live': '⟳ 實盤',
    'BACKTEST P/L': '回測損益', 'LIVE P/L': '實盤損益', 'DIFF vs BT': '實盤 vs 回測',
    'Sun': '日', 'Mon': '一', 'Tue': '二', 'Wed': '三', 'Thu': '四', 'Fri': '五', 'Week': '週',
    'WEEKLY INCOME': '週收益', 'Backtest vs Live Curve': '回測 vs 實盤曲線',
    'Run a backtest to compare curves.': '先跑回測以比較曲線。',
    'ORDER COMPARISON': '訂單比對', 'Selected Preset vs Live Execution': '選定預設組 vs 實盤執行',
    'Historical live rows may not include preset names.': '歷史實盤列可能沒有預設組名稱。',
    'RESEARCH': '研究',
    'Robustness — Topstep · Monte Carlo · Walk-Forward · Slippage': '穩健性 — Topstep · 蒙地卡羅 · 走查 · 滑價',
    'Refresh': '刷新',
    'Run a backtest first — analysis uses the latest backtest trades.': '先跑回測 — 分析使用最近一次回測交易。',
    'trades': '筆交易',
    'Not enough trades (need ≥10).': '交易數不足(需 ≥10)。',
    'Not enough trades (need ≥6).': '交易數不足(需 ≥6)。',
    'MONTE CARLO': '蒙地卡羅', 'WALK-FORWARD': '走查驗證', 'segments': '段',
    'SLIPPAGE INJECTION': '滑價注入',
    'PASS': '通過', 'FAIL': '未過',
    'PF OK AFTER SLIP': '滑價後 PF 合格', 'PF DEGRADES BELOW 1.5': '滑價後 PF < 1.5',
    'Total PnL P5 / P50 / P95': '總損益 P5 / P50 / P95',
    'P(total loss)': 'P(總體虧損)', 'maxDD P50 / P95': '最大回撤 P50 / P95',
    'P(maxDD > $2k)': 'P(回撤 > $2k)', 'PF P5': 'PF P5',
    'Segment': '分段', 'Win%': '勝率', 'original': '原始',
    'Measured market-entry slip': '實測市價進場滑價',
    'anchor — documented EMAPMO fill; market-like live sample': '錨點 — 有據 EMAPMO 成交;市價特徵樣本',
    'median of live market-like fills': '實盤市價特徵成交中位數',
    'RT slip': '往返滑價',
    'Market entries pay the full slip each round turn (bracket follows the fill). Limit entries skip entry slip but miss fills instead. Small-SL variants lose PF fastest — check the 8t+ rows before moving a model to market entry.':
        '市價進場每筆承擔全額往返滑價(bracket 跟隨成交價);限價進場無進場滑價但會漏單。小 SL 變體 PF 掉最快 — 改市價進場前先看 8t 以上列。',
};

function t(s) {
    return (UI_LANG === 'zh' && I18N_ZH[s]) || s;
}

function _i18nTranslateTextNode(n) {
    if (UI_LANG === 'zh') {
        const raw = n.__i18nEn != null ? n.__i18nEn : n.nodeValue;
        const key = String(raw).trim();
        if (!key) return;
        const zh = I18N_ZH[key];
        if (zh) {
            if (n.__i18nEn == null) n.__i18nEn = n.nodeValue;
            n.nodeValue = String(raw).replace(key, zh);
        }
    } else if (n.__i18nEn != null) {
        n.nodeValue = n.__i18nEn;
        n.__i18nEn = null;
    }
}

function _i18nTranslateTree(root) {
    if (!root) return;
    if (root.nodeType === 3) { _i18nTranslateTextNode(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9) return;
    if (root.id === 'log-container' || (root.closest && root.closest('#log-container'))) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
        acceptNode(n) {
            const p = n.parentNode;
            if (!p) return NodeFilter.FILTER_REJECT;
            if (p.nodeName === 'SCRIPT' || p.nodeName === 'STYLE') return NodeFilter.FILTER_REJECT;
            if (p.closest && p.closest('#log-container')) return NodeFilter.FILTER_REJECT;
            return NodeFilter.FILTER_ACCEPT;
        }
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(_i18nTranslateTextNode);
}

function applyLanguage() {
    _i18nTranslateTree(document.body);
    const btn = document.getElementById('lang-toggle');
    if (btn) {
        btn.textContent = UI_LANG === 'zh' ? 'EN' : '中';
        btn.title = UI_LANG === 'zh' ? 'Switch to English' : '切換為繁體中文';
    }
    document.documentElement.lang = UI_LANG === 'zh' ? 'zh-TW' : 'en';
}

function toggleLanguage() {
    UI_LANG = UI_LANG === 'zh' ? 'en' : 'zh';
    try { localStorage.setItem(UI_LANG_KEY, UI_LANG); } catch (e) {}
    applyLanguage();
    // re-render views whose strings are built in JS with t()
    try { renderSweepTable(); } catch (e) {}
    try {
        const cal = document.getElementById('calendar-view');
        if (cal && !cal.classList.contains('hidden')) renderCalendar();
    } catch (e) {}
}

// 動態渲染(表格/面板)在中文模式下持續翻譯;nodeValue 變更不觸發 childList,
// 不會自迴圈。
const _i18nObserver = new MutationObserver(muts => {
    if (UI_LANG !== 'zh') return;
    for (const m of muts) {
        if (m.addedNodes) m.addedNodes.forEach(node => _i18nTranslateTree(node));
    }
});
_i18nObserver.observe(document.body, { childList: true, subtree: true });
applyLanguage();


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
    if (!risk && !prof) { hint.textContent = '(兩者皆 OFF — 無上限)'; return; }
    const cEl = document.getElementById('contract-' + mode);
    const cid = String((cEl && cEl.value) || '');
    // CON.F.US.<SYM>.<expiry> — ENQ = 迷你 NQ($20/pt),MNQ 微型($2),MES 微型 ES($5)
    const sym = (cid.split('.')[3] || 'MNQ').toUpperCase();
    const pv = { MNQ: 2, ENQ: 20, NQ: 20, MES: 5, ES: 50 }[sym] || 2;
    const sEl = document.getElementById('size-' + mode);
    const size = (sEl ? parseInt(sEl.value, 10) : 1) || 1;
    const tv = 0.25 * pv * size;
    const parts = [];
    // 1.0.9: 兩個上限各自獨立夾,不再等比縮放 —— 壓 TP 不會動到 SL
    if (risk) parts.push('風險 ≤ $' + Math.round(risk * tv));
    if (prof) parts.push('獲利 ≤ $' + Math.round(prof * tv));
    hint.textContent = '(' + parts.join(' · ') + ' @ ' + size + ' 口 · SL/TP 各自獨立)';
}


// 1.0.9: 標示 PERFORMANCE 面板顯示的是哪一次回測的結果。
// 啟動時會從 localStorage 還原上次結果,不標示的話使用者會誤以為那是
// 當前 preset / 策略跑出來的(實際可能是好幾天前、別的策略的)。
function setPerfSource(info) {
    const el = document.getElementById('perf-source');
    if (!el) return;
    if (!info) { el.textContent = ''; el.classList.remove('stale'); return; }
    const who = info.preset || info.strategy || '?';
    const when = info.saved_at ? info.saved_at.slice(0, 16).replace('T', ' ') : '';
    if (info.stale) {
        el.textContent = '⚠ 快取 · ' + who + (when ? ' · ' + when : '');
        el.classList.add('stale');
        el.title = '這是上次回測的結果,不是當前設定跑出來的。按 EXECUTE BACKTEST 重跑。';
    } else {
        el.textContent = who + (when ? ' · ' + when : '');
        el.classList.remove('stale');
        el.title = '';
    }
}
