# ancserTPX

English | [繁體中文](README_ZH.md)

ancserTPX is an NQ (Nasdaq 100 E-mini) futures auto-trading system built on **TopstepX (ProjectX API)**.

The system is centered around **1-minute candles** and supports both backtesting and live monitoring / trading.

---

## Strategy Choices

The Web Backtest and Web LIVE panels provide these strategy choices:

- **FACTOR** (`EMAPMO`, `KDJMA`, `MREV`)
- **MOMENTUM** — intraday momentum continuation
- **BETAFIB** — fib retracement of the RTH impulse leg
- **PI** — external Discord signal source
- **FADE** — prior-day value-area fade (`OR15` variant available)
- **SIGMA** — rolling sigma-band fade

`CONFLUENCE` (ML) is live-only and does not go through the same dispatch.

> The authoritative list is enforced by
> `tests/test_strategy_pipeline_classification.py`, not by this README.
> **TREND, DAY ZONE, DISTRIBUTION and standalone PMO were removed** (1.0.9/1.0.10);
> `pmo` survives only as a FACTOR-family alias. Do not reintroduce them
> from this document — see `docs/1.0.9_DELETE_LIST.md` and
> `docs/1.0.10_REMOVE_PMO.md` for why they were cut.

The preset selector includes **Default** and user-saved presets. Terminal-only LIVE has no separate strategy selector; it follows the saved or assigned LIVE preset.

Strategy rules, formulas, thresholds, SL/TP internals, and preset parameters are intentionally not documented here.

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

### 3. Attached Auto OCO (API Entry Contract)

> **2026-08-17 correction:** the plain-entry decision introduced in `1.0.10n`
> is superseded. Never remove the API bracket fields or treat an account/UI
> preset as the bot's protection source.

The bot's live protection source is one attached pair in every API entry request:
`stopLossBracket` and `takeProfitBracket`. A TopstepX OCO template selected in
the website may apply to manual website orders, but it does not replace these
API fields and the engine must not wait for a preset to protect a naked entry.

API protection contract:

- **Stop Loss Order Type**: `Stop Market`
- **Take Profit Order Type**: `Limit`
- The bot sends strategy-specific bracket tick offsets with both limit and market entries
- The engine must not place a second independent SL/TP pair after fill
- Trailing is handled by modifying the attached Auto OCO stop order

Runtime behavior:

1. The bot sends the entry order with attached SL/TP bracket ticks.
2. After the entry fills, TopstepX creates the Auto OCO SL/TP child orders from those bracket fields.
3. The bot waits for those child orders, selects the correct exit direction, and confirms/modifies SL/TP to the strategy-calculated prices.
4. When Trail SL triggers, the bot modifies the existing Auto OCO SL instead of placing a new stop order.

If the attached child orders are not created, live logs show an `[AUTO OCO]`
warning and the bot does not fall back to a second bracket pair. If SL/TP are
still missing 5 minutes after fill, the bot flattens the position and pauses the
engine. Diagnose the entry payload/API response and broker open orders; changing
an account preset is not a substitute for the attached fields.

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
2. Confirm the account is flat before starting a new engine build
3. Click **GO LIVE**
4. Use **STOP** or **FLATTEN** when you want to stop trading or close positions manually

### Terminal-Only LIVE

The terminal launcher skips the web UI and starts LIVE directly. It uses `.env`
credentials, `TOPSTEPX_ACCOUNT_ID` when set, otherwise the first practice
account, and the last used live preset from `data/presets.json`.

Starting either Web or Terminal first stops any older ancserTPX Web/Terminal
process and clears app ports `8000-8010`, so only one trading engine can run.

### EMAPMO Discord signal chart

Web LIVE and Terminal LIVE share one notifier. It posts one text message and PNG
only when the live engine produces an actionable EMAPMO `TradeSignal`; warm-up,
backtests, and ordinary status updates never post.

Configure one Discord channel in the root `.env`. An official channel webhook is
the preferred transport:

```env
EMAPMO_MESSENGER_ENABLED=true
EMAPMO_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
EMAPMO_SIGNAL_HISTORY_DAYS=30
```

For compatibility with `ancserMessenger`, you may instead set `DISCORD_TOKEN`,
`EMAPMO_DISCORD_CHANNEL_ID`, and `EMAPMO_DISCORD_AUTH_MODE`. Automating a personal
user token may violate Discord's terms; prefer a webhook or bot token. Metadata-only
history is stored in `data/messenger/emapmo_signals.sqlite3`, deduplicated across
Web/Terminal/accounts, and retained for 30 days. PNG files and full history are not
kept in memory.

---

## License

See [LICENSE](LICENSE).
