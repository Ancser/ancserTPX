# ancserTPX

NQ (Nasdaq 100 E-mini) futures auto-trading system on **TopstepX** (ProjectX API).

1-minute candle session-based trend-follow strategy with automated SL/TP management.

---

## Strategy

**Session Trend Follow** — detects consolidation zones (Volume Profile: VAH/VAL/POC) across 4 sessions, then trades breakouts.

| Session | Time (ET) | Time (UTC) |
|---------|-----------|------------|
| ASIA | 6:00 PM - 3:00 AM | 22:00 - 07:00 |
| PRE (Europe/Pre-market) | 3:00 AM - 9:30 AM | 07:00 - 13:30 |
| RTH (Regular Trading Hours) | 9:30 AM - 4:00 PM | 13:30 - 20:00 |
| AH (After Hours) | 4:00 PM - 6:00 PM | 20:00 - 22:00 |

- **Entry**: 5 consecutive 1m closes outside VAH/VAL → limit order at 50% retracement
- **Stop Loss**: 50 ticks (12.5 pts = $250)
- **Take Profit**: 3x SL (150 ticks = 37.5 pts = $750)
- **Flatten**: All positions closed at 12:45 PM PT daily

---

## Setup

### 1. Prerequisites

- **Python 3.10+** — [python.org/downloads](https://www.python.org/downloads/)
  - Check "Add Python to PATH" during install
- **Git** (optional) — for cloning the repo

### 2. Get ProjectX API Key

1. Go to **[TopstepX API Portal](https://platform.projectx.com)**
2. Sign in with your TopstepX account
3. Navigate to **API Keys** section
4. Click **Generate API Key**
5. Copy the key (you won't see it again)

> ProjectX API docs: [https://projectxapi.com](https://projectxapi.com)

### 3. Configure `.env`

Create a `.env` file in the project root:

```env
TOPSTEPX_USERNAME=your_email@example.com
TOPSTEPX_API_KEY=your_api_key_here
```

Optional (auto-detected if not set):

```env
TOPSTEPX_ACCOUNT_ID=0
TOPSTEPX_CONTRACT_ID=CON.F.US.ENQ.M26
```

### 4. Install & Run

**Windows:**

```
install.bat        # first time — installs Python dependencies
start.bat          # launches server + opens browser
```

**macOS / Linux:**

Open **Terminal** (Spotlight search "Terminal" or `Cmd+Space` → type "Terminal"), `cd` to the project folder, then:

```bash
make install       # first time — installs Python dependencies
make start         # launches server + opens browser
```

---

## Usage

### Backtest

1. Open browser (auto-opens at `http://localhost:8001`)
2. Click **CONNECT** (loads .env credentials)
3. Select date range (default: last 10 trading days)
4. Click **RUN BACKTEST** — draws zones + trade signals on chart

### Live Trading

1. Connect to API (same as above)
2. Switch to **LIVE MONITOR** tab
3. Select your trading account from the dropdown
4. Click **START LIVE** — engine monitors market and auto-trades
5. **Ctrl+C** in terminal to stop (do NOT close with X button)

---

## Project Structure

```
ancserTPX/
├── backend/
│   ├── main.py              # FastAPI server entry
│   ├── api/routes.py        # REST API endpoints
│   ├── broker/topstepx.py   # ProjectX API client (REST + SignalR)
│   ├── live/engine.py       # Live trading engine
│   └── strategy/
│       ├── consolidation.py # Volume Profile zone detection
│       └── trend_follow.py  # Session trend-follow strategy
├── frontend/
│   └── static/index.html    # Single-page trading UI
├── .env                     # Credentials (not committed)
├── .env.example             # Template for .env
├── install.bat / install.sh # One-click environment setup
├── start.bat / start.sh     # One-click launch
└── kill_old.ps1             # Cleanup zombie processes (Windows)
```

---

## License

See [LICENSE](LICENSE).
