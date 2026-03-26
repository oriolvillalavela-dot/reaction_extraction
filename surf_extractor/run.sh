#!/usr/bin/env bash
# ─── SURF Extractor – startup script ─────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if it exists
if [ -f .env ]; then
    echo "Loading environment from .env …"
    set -a; source .env; set +a
fi

# Detect python and pip executables
PYTHON=$(command -v python3 || command -v python)
PIP=$(command -v pip3 || command -v pip)

if [ -z "$PYTHON" ]; then
    echo "ERROR: Python not found. Please install Python 3.10+."
    exit 1
fi

# Install Python dependencies (skip if already installed)
if ! "$PYTHON" -c "import fastapi" 2>/dev/null; then
    echo "Installing Python dependencies …"
    if [ -n "$PIP" ]; then
        "$PIP" install -r requirements.txt
    else
        "$PYTHON" -m pip install -r requirements.txt
    fi
fi

echo "Starting SURF Extractor on http://0.0.0.0:${PORT:-8000} …"
"$PYTHON" -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --reload
