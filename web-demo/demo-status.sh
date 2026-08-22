#!/usr/bin/env bash
# demo-status.sh — print the health of every service the demo needs.
#
# Usage:  ./demo-status.sh
#
# Green ✓ = up, red ✗ = down. Use this before/after each demo step to confirm
# nothing died (the macOS-detached processes can be flaky between reboots).
set -euo pipefail

c() { printf '\033[1;32m✓\033[0m'; }
x() { printf '\033[1;31m✗\033[0m'; }

# 1. Model backend — the llama-server subprocess Unsloth Studio spawned.
LLAMA_PORT=$(ps aux | grep 'llama-server' | grep -v grep | grep -oE -- '--port [0-9]+' | head -1 | grep -oE '[0-9]+$' || true)
if [ -n "$LLAMA_PORT" ]; then
  echo "$(c) model backend    : llama-server :$LLAMA_PORT  (auto-discovered)"
else
  echo "$(x) model backend    : DOWN — start Unsloth Studio / load Qwen3.6"
fi

# 2. Agent Timetravel OTLP receiver.
if curl -s -o /dev/null -m 2 http://127.0.0.1:4318/healthz 2>/dev/null; then
  echo "$(c) Agent Timetravel receiver : :4318  (captures spans → /tmp/rewind-demo.db)"
else
  echo "$(x) Agent Timetravel receiver : DOWN — run: source .venv/bin/activate && agent-timetravel serve"
fi

# 3. Web demo UI.
if curl -s -o /dev/null -m 2 http://localhost:3000/ 2>/dev/null; then
  echo "$(c) web demo UI      : http://localhost:3000"
else
  echo "$(x) web demo UI      : DOWN — run: ./run-demo.sh   (or bun run dev in web-demo/)"
fi
