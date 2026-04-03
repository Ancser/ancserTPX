#!/bin/bash
echo ""
echo "  ========================================"
echo "   ancserTPX - One-Click Install"
echo "  ========================================"
echo ""

cd "$(dirname "$0")"

# ── Check Python ──
echo "  [1/4] Checking Python..."
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "  [ERROR] Python not found!"
    echo "  Install: brew install python3  (macOS)"
    echo "       or: sudo apt install python3 python3-pip  (Linux)"
    exit 1
fi
echo "        $($PY --version)"

# ── Check pip ──
echo "  [2/4] Checking pip..."
$PY -m pip --version &>/dev/null
if [ $? -ne 0 ]; then
    echo "  [ERROR] pip not found!"
    echo "  Install: $PY -m ensurepip --upgrade"
    exit 1
fi
echo "        pip OK"

# ── Install dependencies ──
echo "  [3/4] Installing dependencies..."
$PY -m pip install -r backend/requirements.txt --quiet
if [ $? -ne 0 ]; then
    echo "  [ERROR] Failed to install dependencies!"
    exit 1
fi
echo "        All packages installed"

# ── Check .env ──
echo "  [4/4] Checking .env..."
if [ -f ".env" ]; then
    echo "        .env found"
else
    cp .env.example .env 2>/dev/null
    echo "        .env created from template"
    echo ""
    echo "  ============================================"
    echo "   IMPORTANT: Edit .env with your credentials"
    echo "     nano .env"
    echo "  ============================================"
    echo ""
fi

echo ""
echo "  ========================================"
echo "   Setup complete!"
echo "   Run: ./start.sh"
echo "  ========================================"
echo ""
