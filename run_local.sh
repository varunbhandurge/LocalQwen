#!/usr/bin/env bash
# run_local.sh  -- one-command start for ba-account-swarm (Linux / macOS / WSL)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 1. Create or activate venv
if [ ! -d "venv" ]; then
    echo "[start] Creating Python virtualenv ..."
    python3 -m venv venv
fi
source venv/bin/activate

# 2. Install dependencies
echo "[start] Installing deps ..."
pip install -q -r requirements.txt

# 3. Seed .env from example (if missing)
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    cp .env.example .env
    echo "[start] Created .env from .env.example -- edit it for your credentials."
fi

# 4. Bootstrap DB
python3 -c "from app.db import init_db; init_db()"

# 5. Start uvicorn (port 8000)
echo "[start] Launching at http://localhost:8000"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
