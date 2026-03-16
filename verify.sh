#!/usr/bin/env bash
set -e

echo "=== 1. Installing dependencies ==="
uv sync

echo ""
echo "=== 2. Starting Agent A (background) ==="
uv run uvicorn agent_a_server:app --port 8000 &
SERVER_PID=$!
sleep 3

echo ""
echo "=== 3. Checking Agent Card ==="
curl -s http://localhost:8000/.well-known/agent.json | python -m json.tool

echo ""
echo "=== 4. Running Agent B ==="
uv run python agent_b_client.py

echo ""
echo "=== 5. Cleanup ==="
kill $SERVER_PID 2>/dev/null || true
echo "Done!"
