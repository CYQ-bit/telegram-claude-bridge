#!/usr/bin/env bash
# daemon.sh — 后台守护运行，崩了自动重启，日志写 logs/daemon.log
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export http_proxy="http://127.0.0.1:7897"
export https_proxy="http://127.0.0.1:7897"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

PIDFILE="$SCRIPT_DIR/logs/bot.pid"
LOGFILE="$SCRIPT_DIR/logs/daemon.log"
mkdir -p "$SCRIPT_DIR/logs"

# ── stop ──────────────────────────────────────────────────────────────────────
if [ "$1" = "stop" ]; then
    if [ -f "$PIDFILE" ]; then
        PID=$(cat "$PIDFILE")
        echo "Stopping bot (pid=$PID)…"
        kill "$PID" 2>/dev/null && echo "Stopped." || echo "Process not found."
        rm -f "$PIDFILE"
    else
        echo "Bot is not running (no pidfile)."
    fi
    exit 0
fi

# ── status ────────────────────────────────────────────────────────────────────
if [ "$1" = "status" ]; then
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "Bot is running (pid=$(cat "$PIDFILE"))."
    else
        echo "Bot is NOT running."
    fi
    exit 0
fi

# ── start ─────────────────────────────────────────────────────────────────────
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Bot is already running (pid=$(cat "$PIDFILE")). Use: ./daemon.sh stop"
    exit 1
fi

echo "Starting bot in background (logs → $LOGFILE)…"

# Restart loop: if python exits, wait 5s and restart
(
    while true; do
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting python app.py" >> "$LOGFILE"
        python app.py >> "$LOGFILE" 2>&1
        EXIT_CODE=$?
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Exited with code $EXIT_CODE. Restarting in 5s…" >> "$LOGFILE"
        sleep 5
    done
) &

LOOP_PID=$!
echo $LOOP_PID > "$PIDFILE"
echo "Bot started (loop pid=$LOOP_PID). Logs: tail -f $LOGFILE"
