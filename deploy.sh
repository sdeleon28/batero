#!/bin/bash
# Rebuild /Applications/drumhero.app and relaunch it if it was running, so the
# rcmd shortcut always opens the latest code. Run after every change.
set -e
cd "$(dirname "$0")"
was_running=0
if pgrep -f "drumhero.app/Contents/MacOS/drumhero" >/dev/null; then
    was_running=1
    osascript -e 'tell application "drumhero" to quit' >/dev/null 2>&1 || true
    for _ in $(seq 1 20); do
        pgrep -f "drumhero.app/Contents/MacOS/drumhero" >/dev/null || break
        sleep 0.25
    done
    pkill -f "drumhero.app/Contents/MacOS/drumhero" 2>/dev/null || true
fi
.venv/bin/python install_app.py "${1:-/Applications}" 2>&1 | grep -v "^pygame-ce"
if [ "$was_running" = 1 ]; then
    open -a drumhero
    echo "relaunched drumhero"
fi
