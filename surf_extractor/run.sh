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

# Install Python dependencies (skip if already installed)
if ! python -c "import fastapi" 2>/dev/null; then
    echo "Installing Python dependencies …"
    pip install -r requirements.txt
fi

echo "Starting SURF Extractor on http://0.0.0.0:${PORT:-8000} …"
python -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --reload
