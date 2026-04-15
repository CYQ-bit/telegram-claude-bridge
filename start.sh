#!/usr/bin/env bash
# start.sh — 前台运行（用于调试）
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export http_proxy="http://127.0.0.1:7897"
export https_proxy="http://127.0.0.1:7897"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"

if [ ! -f ".env" ]; then
    echo "ERROR: .env not found."
    exit 1
fi

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

echo "Starting Telegram-Claude Bridge…"
python app.py
