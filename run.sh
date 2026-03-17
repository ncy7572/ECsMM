#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
MENUBAR_SCRIPT="$ROOT_DIR/menubar.py"
CLI_SCRIPT="$ROOT_DIR/macmonitor.py"
MENUBAR_LOG_FILE="${MENUBAR_LOG_FILE:-$ROOT_DIR/menubar.log}"
MENUBAR_PID_FILE="${MENUBAR_PID_FILE:-$ROOT_DIR/menubar.pid}"

ensure_sudo() {
  sudo -v
}

menubar_is_running() {
  if [[ -f "$MENUBAR_PID_FILE" ]]; then
    local pid
    pid="$(cat "$MENUBAR_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

start_menubar() {
  if menubar_is_running; then
    echo "menubar.py is already running (pid $(cat "$MENUBAR_PID_FILE"))."
    return 0
  fi

  ensure_sudo
  nohup "$PYTHON_BIN" "$MENUBAR_SCRIPT" >>"$MENUBAR_LOG_FILE" 2>&1 &
  local pid=$!
  disown "$pid" 2>/dev/null || true
  echo "$pid" >"$MENUBAR_PID_FILE"
  echo "Started menubar.py (pid $pid)"
  echo "Log: $MENUBAR_LOG_FILE"
}

stop_menubar() {
  if ! menubar_is_running; then
    rm -f "$MENUBAR_PID_FILE"
    echo "menubar.py is not running."
    return 0
  fi

  local pid
  pid="$(cat "$MENUBAR_PID_FILE")"
  kill "$pid" 2>/dev/null || true
  rm -f "$MENUBAR_PID_FILE"
  echo "Stopped menubar.py (pid $pid)"
}

status_menubar() {
  if menubar_is_running; then
    echo "menubar.py is running (pid $(cat "$MENUBAR_PID_FILE"))."
    echo "Log: $MENUBAR_LOG_FILE"
  else
    echo "menubar.py is not running."
  fi
}

logs_menubar() {
  touch "$MENUBAR_LOG_FILE"
  tail -f "$MENUBAR_LOG_FILE"
}

start_cli() {
  ensure_sudo
  exec sudo "$PYTHON_BIN" "$CLI_SCRIPT"
}

usage() {
  cat <<'EOF'
Usage: ./run.sh <command>

Commands:
  start_menubar     Start menubar.py in the background
  stop_menubar      Stop the background menubar.py process
  restart_menubar   Restart the background menubar.py process
  status_menubar    Show whether menubar.py is running
  logs_menubar      Follow the menubar log file
  start_cli         Run macmonitor.py in the current terminal

Tip:
  This script will request sudo by default for GPU access.
EOF
}

cmd="${1:-start_menubar}"

case "$cmd" in
  start_menubar)
    start_menubar
    ;;
  stop_menubar)
    stop_menubar
    ;;
  restart_menubar)
    stop_menubar
    start_menubar
    ;;
  status_menubar)
    status_menubar
    ;;
  logs_menubar)
    logs_menubar
    ;;
  start_cli)
    start_cli
    ;;
  *)
    usage
    exit 1
    ;;
esac
