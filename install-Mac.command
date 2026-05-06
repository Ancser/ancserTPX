#!/bin/bash
set -e
echo ""
echo "  ========================================"
echo "   ancserTPX - One-Click Install"
echo "  ========================================"
echo ""

cd "$(dirname "$0")"

PY=""
PY_VERSION="3.13"

# ── [1/4] Locate or auto-install Python ──
echo "  [1/4] Checking Python..."

find_python() {
    for cand in python3.13 python3 python; do
        if command -v "$cand" &>/dev/null; then
            local minor
            minor=$("$cand" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
            local major
            major=$("$cand" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
            if [ "$major" = "3" ] && [ "$minor" -ge 9 ] && [ "$minor" -le 13 ]; then
                PY="$cand"
                return 0
            fi
        fi
    done
    return 1
}

if find_python; then
    echo "        $($PY --version) ready"
else
    echo "        Python 3.9-3.13 not found - auto-installing..."
    OS="$(uname -s)"

    case "$OS" in
        Darwin*)
            # macOS: try Homebrew
            if command -v brew &>/dev/null; then
                echo "        Using Homebrew..."
                brew install python@3.13 || true
            else
                echo "        Homebrew not found - installing Homebrew first..."
                /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || {
                    echo ""
                    echo "  [ERROR] Could not install Homebrew automatically."
                    echo "  Please install Python manually: https://www.python.org/downloads/"
                    exit 1
                }
                # Try to source brew env
                if [ -x /opt/homebrew/bin/brew ]; then
                    eval "$(/opt/homebrew/bin/brew shellenv)"
                elif [ -x /usr/local/bin/brew ]; then
                    eval "$(/usr/local/bin/brew shellenv)"
                fi
                brew install python@3.13 || true
            fi
            ;;
        Linux*)
            # Linux: try apt / dnf / yum / pacman
            if command -v apt-get &>/dev/null; then
                echo "        Using apt..."
                sudo apt-get update -qq
                sudo apt-get install -y python3.13 python3.13-venv python3-pip 2>/dev/null \
                    || sudo apt-get install -y python3 python3-venv python3-pip
            elif command -v dnf &>/dev/null; then
                echo "        Using dnf..."
                sudo dnf install -y python3.13 python3-pip 2>/dev/null \
                    || sudo dnf install -y python3 python3-pip
            elif command -v yum &>/dev/null; then
                echo "        Using yum..."
                sudo yum install -y python3 python3-pip
            elif command -v pacman &>/dev/null; then
                echo "        Using pacman..."
                sudo pacman -Sy --noconfirm python python-pip
            else
                echo ""
                echo "  [ERROR] No supported package manager found (apt/dnf/yum/pacman)."
                echo "  Please install Python 3.13 manually."
                exit 1
            fi
            ;;
        *)
            echo ""
            echo "  [ERROR] Unsupported OS: $OS"
            echo "  Please install Python 3.13 manually: https://www.python.org/downloads/"
            exit 1
            ;;
    esac

    if find_python; then
        echo "        $($PY --version) installed"
    else
        echo ""
        echo "  [ERROR] Python install ran but python3 not found in PATH."
        echo "  Please open a NEW terminal and run install-Mac.command again."
        exit 1
    fi
fi

# ── [2/4] pip ──
echo "  [2/4] Checking pip..."
if ! $PY -m pip --version &>/dev/null; then
    echo "        pip missing - bootstrapping with ensurepip..."
    $PY -m ensurepip --upgrade &>/dev/null || {
        echo "  [ERROR] pip bootstrap failed."
        exit 1
    }
fi
echo "        pip OK"

# ── [3/4] Dependencies ──
echo "  [3/4] Installing dependencies..."
$PY -m pip install --upgrade pip --quiet
$PY -m pip install -r backend/requirements.txt --quiet || {
    echo "  [ERROR] Failed to install dependencies!"
    exit 1
}
echo "        All packages installed"

# ── [4/4] .env ──
echo "  [4/4] Checking .env..."
if [ -f ".env" ]; then
    echo "        .env found"
else
    echo "        .env not found - creating blank..."
    printf 'TOPSTEPX_USERNAME=\nTOPSTEPX_API_KEY=\n' > .env
    echo ""
    echo "  ============================================"
    echo "   Credentials will be saved from the Web UI"
    echo "   Click CONNECT and enter your email + API key"
    echo "  ============================================"
    echo ""
fi

echo ""
echo "  ========================================"
echo "   Setup complete!"
echo "   Double-click start-Mac.command to launch ancserTPX"
echo "  ========================================"
echo ""
