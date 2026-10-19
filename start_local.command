#!/bin/zsh
PROJECT_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd -- "$PROJECT_ROOT"
exec "$PROJECT_ROOT/backend/.venv/bin/python" "$PROJECT_ROOT/scripts/start_local.py"
