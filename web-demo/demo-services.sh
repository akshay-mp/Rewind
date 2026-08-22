#!/usr/bin/env bash
# demo-services.sh — manage the Agent Timetravel demo services as macOS LaunchAgents.
#
# This is the FIX for "macOS keeps killing localhost". launchd owns the
# processes, so they survive: terminal/IDE quits, App Nap, screen sleep, and
# reboots — and auto-restart if they crash.
#
# Usage:
#   ./demo-services.sh start     load + launch both agents (idempotent)
#   ./demo-services.sh stop      unload both agents (kills the processes)
#   ./demo-services.sh status    show running state + port health
#   ./demo-services.sh restart   stop then start
#   ./demo-services.sh logs [r|w]  tail logs: r=receiver, w=web (default: both)
#
# Agents:
#   com.akshaymp.rewind.receiver  → agent-timetravel serve on :4318
#   com.akshaymp.rewind.webdemo   → next dev on :3000
#
# NOTE: this does NOT manage the model (Unsloth Studio / llama-server).
# Start that separately — the web UI auto-discovers its port.

set -euo pipefail

RECEIVER=com.akshaymp.rewind.receiver
WEBDEMO=com.akshaymp.rewind.webdemo
PLIST_DIR="$HOME/Library/LaunchAgents"

# --- launchd 101 -----------------------------------------------------------
# `launchctl bootstrap gui/$UID <plist>` loads + starts an agent on modern
# macOS (Sequoia+). `bootout` stops + unloads it. `enable`/`disable` control
# whether it auto-starts at login. We bootstrap (not `load`, which is legacy).
uid_gui() { echo "gui/$(id -u)"; }

do_start() {
  echo "▶ starting Agent Timetravel receiver (:4318)…"
  launchctl bootstrap "$(uid_gui)" "$PLIST_DIR/$RECEIVER.plist" 2>/dev/null \
    && echo "  loaded" \
    || echo "  already loaded (or rerun stop first)"
  launchctl enable "$(uid_gui)/$RECEIVER" 2>/dev/null || true

  echo "▶ starting web UI (:3000)…"
  launchctl bootstrap "$(uid_gui)" "$PLIST_DIR/$WEBDEMO.plist" 2>/dev/null \
    && echo "  loaded" \
    || echo "  already loaded (or rerun stop first)"
  launchctl enable "$(uid_gui)/$WEBDEMO" 2>/dev/null || true

  echo "  waiting for ports to come up…"
  sleep 6
  do_status
}

do_stop() {
  echo "▶ stopping web UI…"
  launchctl bootout "$(uid_gui)/$WEBDEMO" 2>/dev/null && echo "  stopped" || echo "  not loaded"
  echo "▶ stopping Agent Timetravel receiver…"
  launchctl bootout "$(uid_gui)/$RECEIVER" 2>/dev/null && echo "  stopped" || echo "  not loaded"
}

port_up() { curl -s -o /dev/null -m 2 "$1" 2>/dev/null; }

do_status() {
  # launchctl print returns nonzero if the agent isn't loaded.
  if launchctl print "$(uid_gui)/$RECEIVER" >/dev/null 2>&1; then
    if port_up http://127.0.0.1:4318/healthz; then
      printf '  \033[1;32m✓\033[0m receiver  :4318  (launchd-managed, healthy)\n'
    else
      printf '  \033[1;33m~\033[0m receiver  :4318  (loaded, port not responding yet)\n'
    fi
  else
    printf '  \033[1;31m✗\033[0m receiver  not loaded — run: %s start\n' "$0"
  fi

  if launchctl print "$(uid_gui)/$WEBDEMO" >/dev/null 2>&1; then
    if port_up http://localhost:3000/; then
      printf '  \033[1;32m✓\033[0m web UI    :3000  (launchd-managed, healthy) → http://localhost:3000\n'
    else
      printf '  \033[1;33m~\033[0m web UI    :3000  (loaded, compiling — wait ~10s)\n'
    fi
  else
    printf '  \033[1;31m✗\033[0m web UI    not loaded — run: %s start\n' "$0"
  fi

  # Model is NOT managed by us — just report it.
  local port
  port=$(ps aux | grep 'llama-server' | grep -v grep | grep -oE -- '--port [0-9]+' | head -1 | grep -oE '[0-9]+$' || true)
  if [ -n "$port" ]; then
    printf '  \033[1;32m✓\033[0m model     :%s  (Unsloth, auto-discovered)\n' "$port"
  else
    printf '  \033[1;31m✗\033[0m model     DOWN — load Qwen3.6 in Unsloth Studio\n'
  fi
}

do_logs() {
  local which="${1:-both}"
  case "$which" in
    r|receiver) tail -n 50 -f /tmp/rewind-serve.log ;;
    w|web)      tail -n 50 -f /tmp/web-demo-dev.log ;;
    both|"")    tail -n 50 -f /tmp/rewind-serve.log /tmp/web-demo-dev.log ;;
    *) echo "usage: $0 logs [r|w|both]"; exit 2 ;;
  esac
}

case "${1:-status}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 2; do_start ;;
  status)  do_status ;;
  logs)    do_logs "${2:-}" ;;
  *) echo "usage: $0 {start|stop|restart|status|logs [r|w]}"; exit 2 ;;
esac
