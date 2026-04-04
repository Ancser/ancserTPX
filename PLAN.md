# ancserTPX 0.6 — Strategy Parameter Control System

## Overview

Add configurable strategy parameter panels for both backtest and live trading modes.
Both panels share the same parameter schema and preset system, but operate independently
(changing backtest params does NOT affect live, and vice versa).

---

## 1. Current State Analysis

### How Parameters Are Currently Wired

| Parameter | Current Value | Location | Used By |
|-----------|--------------|----------|---------|
| BREAKOUT_CONFIRM_CANDLES | 5 | `SessionTrendFollow` class constant | Backtest + Live |
| ENTRY_RATIO | 0.5 (50% RE) | `SessionTrendFollow` class constant | Backtest + Live |
| SL_TICKS | 50 (12.5 pts) | `SessionTrendFollow` class constant | Backtest + Live |
| TP_MULTIPLIER | 3 | `SessionTrendFollow` class constant | Backtest + Live |
| PENDING_TIMEOUT_CANDLES | 30 (30 min) | `SessionTrendFollow` class constant | Backtest + Live |
| value_area_pct | 0.80 | `BacktestConfig` / `LiveStartRequest` | Backtest + Live |
| max_daily_trades | 5 | `BacktestEngine.__init__` / `LiveStartRequest` | Backtest + Live |

### Are Backtest and Live Connected?

**No.** They create separate instances:
- Backtest: `BacktestEngine` → creates own `SessionTrendFollow()`
- Live: `LiveTradingEngine` → creates own `SessionTrendFollow()`

Changing one does NOT affect the other. This isolation is preserved in the new design.

### Current Entry/SL/TP Formula (SessionTrendFollow._generate_signal)

**UP breakout (BUY):**
```
entry = VAH + ENTRY_RATIO × (H100 - VAH)
sl    = VAH - SL_TICKS × 0.25
tp    = entry + |entry - sl| × TP_MULTIPLIER
```

**DOWN breakout (SELL):**
```
entry = VAL - ENTRY_RATIO × (VAL - L100)
sl    = VAL + SL_TICKS × 0.25
tp    = entry - |entry - sl| × TP_MULTIPLIER
```

Where H100/L100 = zone.high_100/low_100 (full range of the volume profile zone).

---

## 2. Decision Point — TP Formula

The current TP formula uses risk-reward ratio: `TP = entry ± |entry - sl| × multiplier`

The user describes: `TP = entry ± tp_factor × (breakout_extreme - VAH/VAL)`

These produce **different results**. For example with up breakout where H100-VAH = 20 pts:
- Current (multiplier=3): TP distance = |entry-sl| × 3 = (10 + 12.5) × 3 = 67.5 pts
- User's (factor=4): TP distance = 4 × 20 = 80 pts

**Plan: Change to the user's described formula:**
```
breakout_range = H100 - VAH  (for BUY)  or  VAL - L100  (for SELL)
tp = entry + tp_factor × breakout_range  (BUY)
tp = entry - tp_factor × breakout_range  (SELL)
```

This makes TP scale with the zone's breakout range rather than SL distance.
Default tp_factor = 4 (per user instruction).

> **Note:** The "breakout extreme" reference currently uses `zone.high_100` / `zone.low_100`
> (the full range boundaries of the zone). This is what the current code already uses for
> entry calculation. We keep using these same values for consistency.

---

## 3. New Parameters (StrategyParams)

| # | Parameter | UI Control | Options | Default | Maps To |
|---|-----------|-----------|---------|---------|---------|
| 1 | Strategy | Dropdown | Trend / ~~Reversion~~ | Trend | strategy type |
| 2 | Entry Mode | Dropdown | 50% RE / 100% RE | 50% RE | ENTRY_RATIO → 0.5 / 0.0 |
| 3 | TP Factor | Dropdown | 1 / 2 / 3 / 4 | 3 | TP_MULTIPLIER |
| 4 | SL Buffer (ticks) | Dropdown | 0 / 25 / 50 / 75 / 100 | 50 | SL_TICKS |
| 5 | Entry Timeout | Dropdown | 10 / 20 / 30 min | 30 | PENDING_TIMEOUT_CANDLES |
| 6 | TP Timeout | Dropdown | OFF / 30 / 60 min | OFF | NEW: position hold timeout |
| 7 | TP Timeout Action | Dropdown | Flat / 3 / 2 / 1 | Flat | NEW: action on timeout |

**Edge case notes:**
- SL Buffer = 0 means SL sits exactly at VAH/VAL (no buffer)
- With 100% RE + SL Buffer = 0: entry = VAH and SL = VAH → zero risk distance (bad).
  The UI should warn or prevent this combination.
- TP Timeout "Flat" = close position immediately at market price
- TP Timeout action dropdown only visible when TP Timeout is not OFF

---

## 4. Implementation Steps

### Step 1: Add `StrategyParams` dataclass to `models.py`

```python
@dataclass
class StrategyParams:
    """Configurable strategy parameters for SessionTrendFollow"""
    strategy: str = "trend"              # "trend" | "reversion"
    entry_mode: str = "50RE"             # "50RE" | "100RE"
    tp_factor: int = 4                   # 1, 2, 3, 4
    sl_ticks: int = 50                   # 0, 25, 50, 75, 100
    entry_timeout_minutes: int = 30      # 10, 20, 30
    tp_timeout_minutes: int = 0          # 0 (OFF), 30, 60
    tp_timeout_action: str = "flat"      # "flat", "3", "2", "1"
```

### Step 2: Modify `SessionTrendFollow` to accept params

Change from class constants to instance variables set from `StrategyParams`:

```python
class SessionTrendFollow:
    def __init__(self, params: Optional[StrategyParams] = None):
        p = params or StrategyParams()
        self.ENTRY_RATIO = 0.5 if p.entry_mode == "50RE" else 0.0
        self.SL_TICKS = p.sl_ticks
        self.TP_FACTOR = p.tp_factor
        self.PENDING_TIMEOUT_CANDLES = p.entry_timeout_minutes  # 1:1 for 1m bars
        self.TP_TIMEOUT_CANDLES = p.tp_timeout_minutes           # 1:1 for 1m bars
        self.TP_TIMEOUT_ACTION = p.tp_timeout_action
        self.BREAKOUT_CONFIRM_CANDLES = 5  # stays fixed
        self.TICK_SIZE = 0.25              # stays fixed
```

**Change TP formula in `_generate_signal`:**
```python
if direction == "up":
    breakout_range = zone.high_100 - zone.vah_80
    entry = zone.vah_80 + self.ENTRY_RATIO * breakout_range
    sl = zone.vah_80 - self.SL_TICKS * self.TICK_SIZE
    tp = entry + self.TP_FACTOR * breakout_range
else:
    breakout_range = zone.val_80 - zone.low_100
    entry = zone.val_80 - self.ENTRY_RATIO * breakout_range
    sl = zone.val_80 + self.SL_TICKS * self.TICK_SIZE
    tp = entry - self.TP_FACTOR * breakout_range
```

### Step 3: Implement TP Timeout in `BacktestEngine`

New logic in `_process_candle` (after SL/TP check, before strategy eval):

```python
# ── TP Timeout check ──
if self._open_position and self._tp_timeout_candles > 0:
    hold_minutes = self._position_age  # incremented each candle
    if hold_minutes >= self._tp_timeout_candles:
        if self._tp_timeout_action == "flat":
            self._force_exit(candle, ExitReason.FLATTEN)
        else:
            # Reduce TP factor and recalculate TP
            new_factor = int(self._tp_timeout_action)
            self._recalc_tp(new_factor)
        self._tp_timeout_triggered = True  # only trigger once
```

Need to add:
- `self._position_age: int = 0` — increment each candle while position open
- `self._tp_timeout_triggered: bool = False` — prevent re-triggering
- `_recalc_tp(new_factor)` — recalculate TP price using new factor and stored breakout_range

### Step 4: Implement TP Timeout in `LiveTradingEngine`

Similar logic but with real order management:
- Track position age from fill time
- On timeout with "flat": market close (cancel TP, send market order)
- On timeout with factor: cancel existing TP order, place new TP limit at recalculated price

### Step 5: Update API endpoints

**`BacktestRequest`** — add strategy params:
```python
class BacktestRequest(BaseModel):
    initial_capital: float = 50000.0
    max_daily_trades: int = 5
    # Strategy params
    strategy: str = "trend"
    entry_mode: str = "50RE"
    tp_factor: int = 4
    sl_ticks: int = 50
    entry_timeout_minutes: int = 30
    tp_timeout_minutes: int = 0
    tp_timeout_action: str = "flat"
```

**`LiveStartRequest`** — add same params:
```python
class LiveStartRequest(BaseModel):
    account_id: int
    contract_id: str = "CON.F.US.ENQ.M26"
    max_daily_trades: int = 5
    value_area_pct: float = 0.80
    # Strategy params
    strategy: str = "trend"
    entry_mode: str = "50RE"
    tp_factor: int = 4
    sl_ticks: int = 50
    entry_timeout_minutes: int = 30
    tp_timeout_minutes: int = 0
    tp_timeout_action: str = "flat"
```

**`/backtest/run`** — pass params to engine:
```python
params = StrategyParams(
    strategy=req.strategy,
    entry_mode=req.entry_mode,
    tp_factor=req.tp_factor,
    sl_ticks=req.sl_ticks,
    entry_timeout_minutes=req.entry_timeout_minutes,
    tp_timeout_minutes=req.tp_timeout_minutes,
    tp_timeout_action=req.tp_timeout_action,
)
engine = BacktestEngine(config, max_daily_trades=req.max_daily_trades, strategy_params=params)
```

### Step 6: Frontend — Parameter Panel (shared component)

Add identical parameter panels to both backtest sidebar and live sidebar.
All dropdowns styled with existing GFL2 theme (var(--bg), var(--border), var(--cyan)).

```html
<!-- Strategy Params Panel (appears in both backtest and live sections) -->
<div class="panel" id="strategy-params-{mode}">
    <div class="panel-title">STRATEGY PARAMS</div>

    <!-- Preset selector -->
    <div class="form-row">
        <div class="form-group" style="flex:2">
            <label>PRESET</label>
            <select id="preset-{mode}">
                <option value="default">Default</option>
                <!-- dynamically populated -->
            </select>
        </div>
        <div class="form-group" style="flex:1">
            <button class="btn btn-outline" onclick="savePreset('{mode}')">SAVE</button>
        </div>
    </div>

    <!-- Strategy type -->
    <div class="form-group">
        <label>STRATEGY</label>
        <select id="strategy-{mode}">
            <option value="trend" selected>Trend Follow</option>
            <option value="reversion" disabled>Reversion (coming soon)</option>
        </select>
    </div>

    <!-- Entry mode -->
    <div class="form-group">
        <label>ENTRY</label>
        <select id="entry-mode-{mode}">
            <option value="50RE" selected>50% Retracement</option>
            <option value="100RE">100% Retracement (at VAH/VAL)</option>
        </select>
    </div>

    <!-- TP Factor -->
    <div class="form-group">
        <label>TP FACTOR</label>
        <select id="tp-factor-{mode}">
            <option value="1">1x breakout range</option>
            <option value="2">2x breakout range</option>
            <option value="3">3x breakout range</option>
            <option value="4" selected>4x breakout range</option>
        </select>
    </div>

    <!-- SL Buffer -->
    <div class="form-group">
        <label>SL BUFFER (TICKS FROM VAH/VAL)</label>
        <select id="sl-ticks-{mode}">
            <option value="0">0 ticks (at VAH/VAL)</option>
            <option value="25">25 ticks (6.25 pts)</option>
            <option value="50" selected>50 ticks (12.5 pts)</option>
            <option value="75">75 ticks (18.75 pts)</option>
            <option value="100">100 ticks (25 pts)</option>
        </select>
    </div>

    <!-- Entry Timeout -->
    <div class="form-group">
        <label>ENTRY TIMEOUT</label>
        <select id="entry-timeout-{mode}">
            <option value="10">10 minutes</option>
            <option value="20">20 minutes</option>
            <option value="30" selected>30 minutes</option>
        </select>
    </div>

    <!-- TP Timeout -->
    <div class="form-row">
        <div class="form-group">
            <label>TP TIMEOUT</label>
            <select id="tp-timeout-{mode}" onchange="toggleTpAction('{mode}')">
                <option value="0" selected>OFF</option>
                <option value="30">30 minutes</option>
                <option value="60">60 minutes</option>
            </select>
        </div>
        <div class="form-group" id="tp-action-group-{mode}" style="display:none">
            <label>ACTION</label>
            <select id="tp-action-{mode}">
                <option value="flat" selected>Flat (close now)</option>
                <option value="3">TP → 3x</option>
                <option value="2">TP → 2x</option>
                <option value="1">TP → 1x</option>
            </select>
        </div>
    </div>
</div>
```

Replace `{mode}` with `bt` for backtest and `live` for live trading.

### Step 7: Frontend — Preset System

Presets stored in **localStorage** as JSON. Both panels share the same preset list.

```javascript
const DEFAULT_PARAMS = {
    strategy: 'trend',
    entry_mode: '50RE',
    tp_factor: 4,
    sl_ticks: 50,
    entry_timeout_minutes: 30,
    tp_timeout_minutes: 0,
    tp_timeout_action: 'flat',
};

function getPresets() {
    return JSON.parse(localStorage.getItem('ancserTPX_presets') || '{}');
}

function savePreset(mode) {
    const name = prompt('Preset name:');
    if (!name) return;
    const presets = getPresets();
    presets[name] = collectParams(mode);
    localStorage.setItem('ancserTPX_presets', JSON.stringify(presets));
    refreshPresetDropdowns();
}

function loadPreset(mode, name) {
    const presets = getPresets();
    if (presets[name]) applyParams(mode, presets[name]);
}

function collectParams(mode) {
    return {
        strategy: document.getElementById(`strategy-${mode}`).value,
        entry_mode: document.getElementById(`entry-mode-${mode}`).value,
        tp_factor: parseInt(document.getElementById(`tp-factor-${mode}`).value),
        sl_ticks: parseInt(document.getElementById(`sl-ticks-${mode}`).value),
        entry_timeout_minutes: parseInt(document.getElementById(`entry-timeout-${mode}`).value),
        tp_timeout_minutes: parseInt(document.getElementById(`tp-timeout-${mode}`).value),
        tp_timeout_action: document.getElementById(`tp-action-${mode}`).value,
    };
}
```

### Step 8: Wire Frontend to API

**Backtest** — `buildBacktestBody()`:
```javascript
function buildBacktestBody() {
    const params = collectParams('bt');
    return {
        initial_capital: parseFloat(document.getElementById('initial-capital').value),
        max_daily_trades: 5,
        ...params,
    };
}
```

**Live** — `goLive()`:
```javascript
const liveParams = {
    account_id: liveAccount.id,
    contract_id: liveAccount.contract_id || 'CON.F.US.ENQ.M26',
    max_daily_trades: parseInt(document.getElementById('live-max-trades').value),
    value_area_pct: parseFloat(document.getElementById('live-value-area').value),
    ...collectParams('live'),
};
```

---

## 5. Files Modified

| File | Changes |
|------|---------|
| `backend/db/models.py` | Add `StrategyParams` dataclass |
| `backend/strategy/trend_follow.py` | `SessionTrendFollow.__init__` accepts params; change TP formula |
| `backend/backtest/engine.py` | Accept `StrategyParams`; implement TP timeout; track position age |
| `backend/live/engine.py` | Accept `StrategyParams`; implement TP timeout with order management |
| `backend/api/routes.py` | Update `BacktestRequest`, `LiveStartRequest`; pass params through |
| `frontend/static/index.html` | Add param panels to both modes; preset JS; wire to API |

No new files needed. All changes in existing files.

---

## 6. Answers to User's Questions

### Q: Will changing backtest params affect live trading?
**No.** Backtest and live create completely separate engine instances. Each gets its own
copy of `StrategyParams`. Changing one panel does not touch the other.

### Q: How do presets work between backtest and live?
Presets are **shared** — the same preset list appears in both panels. But **selecting** a
preset only applies to that panel. You can:
1. Set params in backtest, iterate until happy
2. Save as preset "aggressive-v1"
3. In live panel, select "aggressive-v1" to use the same params

### Q: Will the changes cause errors?
The changes are backward-compatible. All new params have defaults that match current
behavior (except TP_MULTIPLIER changing from 3 → 4, and TP formula change).
If no params are passed, the system behaves as before with the new defaults.

### Q: What about the TP formula change?
**This is the biggest algorithm change.** Current: `TP = entry ± |entry-sl| × multiplier`
New: `TP = entry ± tp_factor × breakout_range` where breakout_range = |H100 - VAH| or |VAL - L100|.
With default tp_factor=4, TP targets will generally be larger than before.

---

## 7. Computation Examples

### Example: UP breakout, VAH=21000, H100=21020, L100=20980

**50% RE, TP Factor=4, SL Buffer=50 ticks:**
```
breakout_range = 21020 - 21000 = 20 pts
entry = 21000 + 0.5 × 20 = 21010
sl    = 21000 - 50 × 0.25 = 20987.5
tp    = 21010 + 4 × 20     = 21090
risk  = |21010 - 20987.5|   = 22.5 pts = $450
reward = |21090 - 21010|     = 80 pts   = $1600
RR    = 1:3.56
```

**100% RE, TP Factor=4, SL Buffer=50 ticks:**
```
entry = 21000 + 0 × 20 = 21000 (at VAH)
sl    = 20987.5
tp    = 21000 + 4 × 20 = 21080
risk  = 12.5 pts = $250
reward = 80 pts  = $1600
RR    = 1:6.4
```

**50% RE, TP Factor=2, SL Buffer=25 ticks:**
```
entry = 21010
sl    = 21000 - 25 × 0.25 = 20993.75
tp    = 21010 + 2 × 20     = 21050
risk  = 16.25 pts = $325
reward = 40 pts   = $800
RR    = 1:2.46
```

---

## 8. Implementation Order

1. **`models.py`** — Add `StrategyParams` dataclass
2. **`trend_follow.py`** — Parameterize `SessionTrendFollow`, change TP formula
3. **`backtest/engine.py`** — Accept params, implement TP timeout, track position age
4. **`routes.py`** — Update request models, pass params through
5. **`index.html`** — Add param panels (backtest), preset system
6. **`index.html`** — Add param panels (live), wire goLive()
7. **`live/engine.py`** — Accept params, implement TP timeout with order management
8. **Test** — Run backtest with default params, verify results match expectations

---

## 9. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|-----------|
| TP formula change alters all backtest results | HIGH | Run before/after comparison; user explicitly requested this formula |
| TP Timeout "flat" in live could close at bad price | MEDIUM | Market close is standard; user explicitly requested |
| SL=0 + 100%RE = zero risk distance | LOW | UI warning when this combo selected |
| TP Timeout order management complexity in live | MEDIUM | Test thoroughly in practice account |
| localStorage presets lost if browser cleared | LOW | Acceptable; can add export/import later |
