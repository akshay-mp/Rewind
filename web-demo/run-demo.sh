#!/usr/bin/env bash
# run-demo.sh — start the Rewind × Deep Research demo, robustly.
#
# Usage:  ./run-demo.sh
#
# Starts (if not already running):
#   1. The Rewind OTLP receiver on :4318  (captures spans into /tmp/rewind-demo.db)
#   2. The Next.js web-demo UI on :3000   (auto-discovers the llama-server port)
#
# It does NOT start the model server — that's Unsloth Studio / Ollama, which
# you run separately. The demo auto-discovers the llama-server subprocess
# spawned by Unsloth Studio, so no port config is needed.
#
# Each service is detached with ( nohup ... & ) so it survives this script
# exiting — macOS has no `setsid`, so we use the nohup-in-a-subshell pattern.
set -euo pipefail

REPO=/Users/akshaymp/Projects/Agentic_AI/rewind
VENV=/Users/akshaymp/Projects/Agentic_AI/.venv
DB=/tmp/rewind-demo.db

echo "▶ checking model backend (llama-server)…"
LLAMA_PORT=$(ps aux | grep 'llama-server' | grep -v grep | grep -oE -- '--port [0-9]+' | head -1 | grep -oE '[0-9]+$' || true)
if [ -z "$LLAMA_PORT" ]; then
  echo "  ⚠ no llama-server found. Start Unsloth Studio first:"
  echo "      unsloth studio run --model unsloth/Qwen3.6-27B-MTP-GGUF --port 8888"
  echo "  (the demo will fall back to the Studio proxy on :8888, which may 401)"
else
  echo "  ✓ llama-server on :$LLAMA_PORT (auto-discovered)"
fi

echo "▶ starting Rewind receiver on :4318…"
if curl -s -o /dev/null -w '' -m 2 http://127.0.0.1:4318/healthz 2>/dev/null; then
  echo "  ✓ already running"
else
  # shellcheck disable=SC1090
  source "$VENV/bin/activate"
  rm -f "$DB" "$DB-wal" "$DB-shm"
  ( nohup rewind serve --port 4318 --db "$DB" >/tmp/rewind-serve.log 2>&1 & ) >/dev/null 2>&1
  sleep 2
  curl -s -o /dev/null -w "  receiver=%{http_code}\n" -m 3 http://127.0.0.1:4318/healthz
fi

echo "▶ starting web-demo UI on :3000…"
if curl -s -o /dev/null -w '' -m 2 http://localhost:3000/ 2>/dev/null; then
  echo "  ✓ already running"
else
  cd "$REPO/web-demo"
  ( nohup ./node_modules/.bin/next dev -H 127.0.0.1 -p 3000 >/tmp/web-demo-dev.log 2>&1 & ) >/dev/null 2>&1
  sleep 8
  curl -s -o /dev/null -w "  web=%{http_code}\n" -m 8 http://localhost:3000/
fi

echo
echo "✓ demo ready → http://localhost:3000"
echo "  (ignore 401s from :8888 — the demo talks to the llama-server directly)"
