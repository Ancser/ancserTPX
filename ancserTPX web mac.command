#!/bin/bash
echo ""
echo "  ========================================"
echo "   ancserTPX web"
echo "  ========================================"
echo ""

cd "$(dirname "$0")"

if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "  [ERROR] Python not found! Run ancserTPX install mac.command first."
    exit 1
fi

echo "  Stopping old ancserTPX instances..."
pkill -f "uvicorn backend.main:app" 2>/dev/null || true
pkill -f "backend.terminal_live" 2>/dev/null || true
pkill -f "terminal_live.py" 2>/dev/null || true
for PORT_TO_KILL in $(seq 8000 8010); do
    PIDS=$(lsof -tiTCP:$PORT_TO_KILL -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "  Killing PID(s) $PIDS on port $PORT_TO_KILL"
        kill -9 $PIDS 2>/dev/null || true
    fi
done
sleep 1

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

echo "  Clearing bytecode cache..."
find backend -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null

if [ -f "data/live_zones.json" ]; then
    echo "  Resetting zone cache..."
    echo '{"saved_at":"","active_zone_id":null,"zones":[]}' > data/live_zones.json
fi

echo ""
echo "  ============================================"
echo "   ancserTPX web starting on port $PORT"
echo "   Web UI: http://localhost:$PORT"
echo "   Use Ctrl+C to stop"
echo "  ============================================"
echo ""

if command -v open &>/dev/null; then
    open "http://localhost:$PORT"
elif command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:$PORT"
fi

$PY -m uvicorn backend.main:app --host 127.0.0.1 --port $PORT

echo ""
echo "  ancserTPX web stopped."
