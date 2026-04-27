#!/usr/bin/env bash
# Run the AQ-IPTV test suite locally.
# Usage:  bash scripts/run-tests.sh [pytest args...]
# Example: bash scripts/run-tests.sh -v -k "test_unit"
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== AQ-IPTV Test Suite ==="
echo "Installing test dependencies..."
pip install -q pytest pytest-asyncio bcrypt httpx fastapi psutil pydantic python-multipart uvicorn 2>/dev/null || true

echo "Running tests..."
python -m pytest tests/ -v --tb=short "$@"
