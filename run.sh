#!/usr/bin/env bash
# Sobe o High em modo desenvolvimento: ./run.sh [porta]
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt; }
[ -f .env ] || cp .env.example .env
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "${1:-8000}" --reload
