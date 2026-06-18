# ancserTPX

English | [繁體中文](README_ZH.md)

ancserTPX is an NQ (Nasdaq 100 E-mini) futures auto-trading system built on **TopstepX (ProjectX API)**.

The system is centered around **1-minute candles** and supports both backtesting and live monitoring / trading.

---

## Strategy Overview

The main strategy is **Session Trend Follow** — a Volume-Profile breakout system built on 1-minute candles.

### Value Area zones

Price is grouped into **fixed clock buckets** of a selectable timeframe (5m / 15m / 30m / 1h / 4h), aligned to clock boundaries — e.g. 4h buckets start at 00:00 / 04:00 / 08:00 / 12:00 / 16:00 / 20:00. Each **completed** bucket becomes a reference Value Area carrying **VAH / VAL / POC** plus its full volume-profile histogram. Only completed buckets are used as references; the in-progress bucket is never traded.

Two zone methods:

- **Single** — break out of one timeframe's Value Area.
- **Overlap** — require the breakout to clear the *overlapping* VAH / VAL of 2–5 timeframes at once (stricter, fewer but higher-quality signals).

### Entry / Exit (RR-based)

- **Entry**: after `CONFIRM` consecutive 1-minute closes fully outside the Value Area (default **7**), arm a limit order at the value-area edge — **VAH** for an upside breakout, **VAL** for a downside breakout. The limit order is valid for **1 candle (1 minute)**; if unfilled it is cancelled and re-evaluated against the latest VAH / VAL.
- **Stop Loss**: the **lowest-volume price node** inside the value area (POC→VAH for longs, POC→VAL for shorts); falls back to a fixed tick distance if no valid node exists.
- **Take Profit**: `RR × |entry − SL|`, where the reward-to-risk ratio **RR** is selectable from **1:1 to 1:6**.
- **Trail SL**: once price reaches the configured trigger %, the stop is moved up to lock in profit.
- **Flatten**: all positions are closed daily at **12:45 PM PT**.

### Backtest & Machine Learning

- **Backtest** runs the chosen settings over the full loaded history (~60 days, the TopstepX data-retention limit), reports performance metrics, and writes a per-trade CSV under `data/backtest/`.
- **Machine Learning** sweeps every timeframe combination (single + overlap of 5m / 15m / 30m / 1h / 4h = 31) × every RR (1:1 … 1:6) = **186 combinations**, then ranks them by **Calmar ratio** (Profit Factor, max drawdown and weekly variation are also reported). `AREA %` and `CONFIRM` are held fixed across the sweep; STRATEGY / METHOD / AREA TF do not affect the sweep.

### Trading sessions

Sessions are used for flatten timing and per-session trade limits:

| Session | Time (ET) | UTC |
|---------|-----------|-----|
| ASIA | 6:00 PM - 3:00 AM | 22:00 - 07:00 |
| EURO | 3:00 AM - 7:00 AM | 07:00 - 11:00 |
| PRE | 7:00 AM - 9:30 AM | 11:00 - 13:30 |
| RTH | 9:30 AM - 4:00 PM | 13:30 - 20:00 |
| AH | 4:00 PM - 6:00 PM | 20:00 - 22:00 |

---

## Architecture

### Project structure

```
ancserTPX/
├── backend/
│   ├── api/routes.py              # FastAPI router — all endpoints
│   ├── backtest/
│   │   ├── confluence_backtest.py  # ConfluenceBacktester + zone timeline builder
│   │   ├── confluence_worker.py    # Child-process backtest runner (own GIL)
│   │   ├── engine.py              # Trend backtest engine
│   │   └── metrics.py             # Shared metrics calculator
│   ├── broker/topstepx.py         # TopstepX API client (auth, bars, orders, websocket)
│   ├── data/
│   │   └── candle_store.py        # Persistent candle accumulator + gap detection
│   ├── db/models.py               # Candle, Trade, contract specs
│   ├── live/engine.py             # Live trading engine
│   └── strategy/
│       ├── confluence.py           # Multi-TF weighted confluence signal engine
│       ├── confluence_scorer.py    # Logistic regression scorer (explainable ML)
│       ├── consolidation.py        # ClockBucketZoneDetector, penta-session
│       ├── exit_policy.py          # Trail trigger / lock / full-TP-lock
│       └── trend_follow.py         # Trend strategy
├── frontend/static/
│   ├── ancserTPX.html             # Single-page app
│   ├── ancserTPX.css              # Dark theme + reusable classes
│   └── ancserTPX.js               # UI logic, chart, presets, LATEST model
├── data/
│   ├── store/                     # Persistent 1m candle accumulator (.pkl)
│   ├── models/confluence_scorer.json # single LATEST production model
│   └── presets.json               # User presets
└── scripts/
    ├── accumulate_history.py      # CLI: grow the persistent candle store
    └── train_confluence.py        # CLI: retrain the single LATEST model
```

### Backtest process model

Web backtests run in a **dedicated child process** (`ProcessPoolExecutor`) with its own GIL, so the web server stays fully responsive during computation. The child process caches the candle set and zone timeline internally — repeated runs on the same data with different model / RR / band / trail parameters skip the slow detector pass entirely.

### Data persistence

Historical 1-minute candles are persisted to `data/store/MNQ_accumulated_1m.pkl` — an append-only store that never truncates. On fetch:
1. Load from local store (instant, no API call)
2. Incremental API fetch (only the tail since last stored bar)
3. Merge + auto gap-detection + recovery of wifi-dropped bars
4. Save back to store

The store survives server restarts, so subsequent launches need only a few hundred bars of incremental data instead of the full 60-day re-download.

### ML production model

The system keeps one interpretable logistic-regression model at `data/models/confluence_scorer.json`. Retraining replaces this canonical LATEST model, ensuring live == backtest == train parity without stale model selection.

### Session timing

| Event | ET | UTC | PT (summer) |
|---|---|---|---|
| New session (ASIA start) | 6:00 PM | 22:00 | 3:00 PM |
| Daily maintenance gap | 5:00–6:00 PM | 22:00–23:00 | 3:00–4:00 PM |
| RTH open | 9:30 AM | 13:30 | 6:30 AM |
| Bot flatten | 3:45 PM | 19:45 | 12:45 PM |
| AH end | ~5:59 PM | ~21:59 | ~2:59 PM |

---

## Before You Start

### 1. ProjectX API Setup

1. Go to https://dashboard.projectx.com/dashboard and enable ProjectX API Access
2. Link your TopstepX account
3. Go to https://topstepx.com/settings
4. Open the **API** page
5. Copy your API key

### 2. Credentials

Enter your **email** and **API key** in the Web UI's top-right **CONNECT** panel. Credentials are saved to `.env` automatically on first connect — no manual file editing needed.

### 3. TopstepX Auto OCO Preset

Live trading requires TopstepX **Auto OCO Brackets** to be enabled. The bot sends `stopLossBracket` and `takeProfitBracket` fields with each API entry order; the preset screen alone does not attach SL/TP to naked API orders.

Recommended setup in TopstepX:

- Open [TopstepX Risk Settings](https://topstepx.com/settings?tab=risk-settings)
- Enable **Auto OCO Brackets**
- Create a preset for this bot
- **Stop Loss Order Type**: `Stop Market`
- **Take Profit Order Type**: `Limit`
- The preset's tick distances are only a fallback/default; the bot sends strategy-specific SL/TP ticks in the API order
- Do not use `Trailing Stop Market` for the preset SL; trailing is handled by the bot by modifying the existing Auto OCO stop order

Runtime behavior:

1. The bot sends the entry order with attached SL/TP bracket ticks.
2. After the entry fills, TopstepX creates the Auto OCO SL/TP child orders from those bracket fields.
3. The bot waits for those child orders, selects the correct exit direction, and confirms/modifies SL/TP to the strategy-calculated prices.
4. When Trail SL triggers, the bot modifies the existing Auto OCO SL instead of placing a new stop order.

If the Auto OCO child orders are not created, live logs will show an `[AUTO OCO]` warning and the bot will not fall back to manual bracket orders. If SL/TP are still missing 5 minutes after fill, the bot flattens the position, pauses the engine, and logs the Risk Settings link above.

---

## Install & Start

Run the matching files for your operating system:

### Windows 11

- First-time install: double-click `ancserTPX install win.bat`
- Web app: double-click `ancserTPX web win.bat`
- Terminal-only LIVE: double-click `ancserTPX terminal win.bat`

> If Windows shows a SmartScreen warning, click **More info → Run anyway**.

### macOS

- First-time install: double-click `ancserTPX install mac.command`
- Web app: double-click `ancserTPX web mac.command`
- Terminal-only LIVE: double-click `ancserTPX terminal mac.command`

> **First launch only:** macOS will block the file with "cannot be opened because it is from an unidentified developer".
> Fix: **right-click** the file → **Open** → click **Open** in the dialog. Only needed once per file.
>
> A Terminal window will open and run automatically — you don't need to type anything.
> Keep the Terminal window open while using ancserTPX. Closing it stops the server.

---

## Usage

### Backtest

1. Choose a strategy or preset
2. Click **EXECUTE BACKTEST**

### Live Trading

1. Select a trading account
2. Confirm the TopstepX Auto OCO preset is enabled
3. Click **GO LIVE**
4. Use **STOP** or **FLATTEN** when you want to stop trading or close positions manually

### Terminal-Only LIVE

The terminal launcher skips the web UI and starts LIVE directly. It uses `.env`
credentials, `TOPSTEPX_ACCOUNT_ID` when set, otherwise the first practice
account, and the last used live preset from `data/presets.json`.

Starting either Web or Terminal first stops any older ancserTPX Web/Terminal
process and clears app ports `8000-8010`, so only one trading engine can run.

---

## License

See [LICENSE](LICENSE).
