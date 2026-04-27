#!/bin/bash
echo ""
echo "  ========================================"
echo "   ancserTPX - NQ Futures Trading System"
echo "  ========================================"
echo ""

cd "$(dirname "$0")"

# ── Check Python ──
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "  [ERROR] Python not found! Run install-Mac.command first."
    exit 1
fi

# ── Kill old uvicorn processes ──
echo "  Killing old uvicorn processes..."
pkill -f "uvicorn backend.main:app" 2>/dev/null
sleep 1

# ── Find a free port ──
PORT=8001
while [ $PORT -le 8010 ]; do
    if ! lsof -iTCP:$PORT -sTCP:LISTEN &>/dev/null; then
        break
    fi
    echo "  Port $PORT occupied, trying next..."
    PORT=$((PORT + 1))
done

if [ $PORT -gt 8010 ]; then
    echo "  [ERROR] Ports 8001-8010 all occupied!"
    exit 1
fi
echo "  [OK] Using port $PORT"

# ── Clear cache ──
echo "  Clearing bytecode cache..."
find backend -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null

# ── Clear zone data ──
if [ -f "data/live_zones.json" ]; then
    echo "  Resetting zone cache..."
    echo '{"saved_at":"","active_zone_id":null,"zones":[]}' > data/live_zones.json
fi

echo ""
echo "  ============================================"
echo "   Server starting on port $PORT"
echo "   Web UI: http://localhost:$PORT"
echo "   Use Ctrl+C to stop"
echo "  ============================================"
echo ""

# ── Open browser ──
if command -v open &>/dev/null; then
    open "http://localhost:$PORT"
elif command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:$PORT"
fi

$PY -m uvicorn backend.main:app --host 0.0.0.0 --port $PORT

echo ""
echo "  Server stopped."
