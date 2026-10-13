#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=== Starting Confluence Ticketing Platform ==="

# Check virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r backend/requirements.txt
fi

# Check frontend build
if [ ! -d "frontend/dist" ]; then
    echo "Building frontend..."
    cd frontend
    npm install
    npm run build
    cd "$DIR"
fi

# Run database migrations
echo "Running database migrations..."
(cd backend && ../.venv/bin/alembic upgrade head)

# Start backend server
PORT="${APP_PORT:-8000}"
echo "Starting server on http://127.0.0.1:${PORT} ..."
exec .venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port "$PORT"
