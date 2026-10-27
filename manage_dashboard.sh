#!/usr/bin/env bash
# =============================================================================
# JobAutomation Mission Control Dashboard Manager (macOS)
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8765
LABEL="com.amrit.jobautomation.dashboard"
PLIST_SRC="${SCRIPT_DIR}/${LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

# Fallback to which python3 if 3.14 binary does not exist
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="$(which python3)"
fi

case "$1" in
    install)
        echo "⚙️  Installing Mission Control Dashboard background service..."
        mkdir -p "${HOME}/Library/LaunchAgents"
        cp "$PLIST_SRC" "$PLIST_DST"
        launchctl unload "$PLIST_DST" 2>/dev/null || true
        launchctl load -w "$PLIST_DST"
        echo "✅ Mission Control background service installed and started!"
        echo "🌐 Accessible anytime at: http://localhost:${PORT}"
        ;;

    uninstall)
        echo "🛑 Uninstalling Mission Control Dashboard service..."
        launchctl unload -w "$PLIST_DST" 2>/dev/null || true
        rm -f "$PLIST_DST"
        echo "✅ Mission Control background service uninstalled."
        ;;

    start)
        echo "🚀 Starting Mission Control service..."
        if [ -f "$PLIST_DST" ]; then
            launchctl load -w "$PLIST_DST" 2>/dev/null || launchctl start "$LABEL" 2>/dev/null || true
        else
            nohup "$PYTHON_BIN" "${SCRIPT_DIR}/dashboard.py" --no-browser --port "$PORT" >> "${SCRIPT_DIR}/logs/dashboard_service.log" 2>&1 &
        fi
        echo "✅ Dashboard service started on port ${PORT}."
        ;;

    stop)
        echo "🛑 Stopping Mission Control service..."
        if [ -f "$PLIST_DST" ]; then
            launchctl unload "$PLIST_DST" 2>/dev/null || true
        fi
        # Also kill any manual instances on port 8765
        PID=$(lsof -ti :${PORT} 2>/dev/null || true)
        if [ -n "$PID" ]; then
            kill "$PID" 2>/dev/null || true
            echo "Killed process $PID listening on port ${PORT}."
        fi
        echo "✅ Dashboard service stopped."
        ;;

    restart)
        "$0" stop
        sleep 1
        "$0" start
        ;;

    status)
        "$PYTHON_BIN" "${SCRIPT_DIR}/dashboard.py" --service-status --port "$PORT"
        ;;

    open)
        open "http://localhost:${PORT}"
        ;;

    app)
        echo "🖥️  Opening Job Automation Dashboard.app..."
        open "${SCRIPT_DIR}/Job Automation Dashboard.app"
        ;;

    *)
        echo "================================================================="
        echo "      🚀 JOB AUTOMATION MISSION CONTROL DASHBOARD MANAGER        "
        echo "================================================================="
        echo "Usage: ./manage_dashboard.sh [command]"
        echo ""
        echo "Commands:"
        echo "  install    - Install LaunchAgent daemon (runs 24/7 on Mac startup)"
        echo "  uninstall  - Remove LaunchAgent daemon cleanly"
        echo "  start      - Start background server"
        echo "  stop       - Stop background server"
        echo "  restart    - Restart background server"
        echo "  status     - Show health, PID, and port status"
        echo "  open       - Open dashboard in default browser"
        echo "  app        - Launch the native desktop .app"
        echo "================================================================="
        exit 1
        ;;
esac
