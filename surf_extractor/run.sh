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

# ── 1. Prefer uv (works in the Ona devcontainer where no system python is set up)
if command -v uv >/dev/null 2>&1; then
    echo "Using uv to manage Python environment…"

    # Create a virtual-env in .venv if it does not exist yet
    if [ ! -d ".venv" ]; then
        echo "Creating virtual environment with uv…"
        uv venv .venv --python 3.14 2>/dev/null || uv venv .venv
    fi

    # Install / sync dependencies into the venv
    echo "Installing dependencies with uv pip…"
    uv pip install --quiet -r requirements.txt

    echo "Starting SURF Extractor on http://0.0.0.0:${PORT:-8000} …"
    uv run uvicorn backend.main:app \
        --host 0.0.0.0 \
        --port "${PORT:-8000}" \
        --reload
    exit 0
fi

# ── 2. Fall back to any available Python 3 binary
PYTHON=$(command -v python3.14 \
      || command -v python3.13 \
      || command -v python3.12 \
      || command -v python3.11 \
      || command -v python3.10 \
      || command -v python3 \
      || command -v python \
      || echo "")

if [ -z "$PYTHON" ]; then
    echo "ERROR: No Python interpreter found."
    echo "  Install uv:      curl -Lsf https://astral.sh/uv/install.sh | sh"
    echo "  Or install Python 3.10+: https://www.python.org/downloads/"
    exit 1
fi

echo "Using Python at: $PYTHON"

# Install Python dependencies (skip if already installed)
if ! "$PYTHON" -c "import fastapi" 2>/dev/null; then
    echo "Installing Python dependencies…"
    "$PYTHON" -m pip install --quiet -r requirements.txt
fi

echo "Starting SURF Extractor on http://0.0.0.0:${PORT:-8000} …"
"$PYTHON" -m uvicorn backend.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --reload
