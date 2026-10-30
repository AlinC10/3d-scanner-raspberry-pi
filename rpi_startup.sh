#!/usr/bin/env bash
# Kiosk session: one fullscreen browser in `cage` (a Wayland compositor for a single app: no desktop, no panels).
# Started from ~/.bash_profile on tty1 after the console autologin (see kiosk/README.md).
# The FastAPI server runs as the systemd service 3d-scanner.service; the browser shows kiosk/splash.html at once and
# that page opens the scanner UI as soon as the server answers (no white screen).

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
URL="http://127.0.0.1:8000"
SPLASH="file://$PROJECT_DIR/kiosk/splash.html"
SERVER_PID=""

cd "$PROJECT_DIR" || exit 1

# Fallback when the service is not installed: start the server from here (as the old script did)
if ! systemctl is-active --quiet 3d-scanner.service; then
    if [ -x ".venv/bin/python" ]; then ./.venv/bin/python main_prod.py & else python3 main_prod.py & fi
    SERVER_PID=$!
fi

# Browser: Firefox (as before) or Chromium when Firefox is missing
if command -v firefox >/dev/null 2>&1; then
    BROWSER=(firefox --kiosk --new-instance "$SPLASH")
else
    BROWSER=(chromium --kiosk --noerrdialogs --disable-infobars --no-first-run --ozone-platform=wayland "$SPLASH")
fi

if command -v cage >/dev/null 2>&1 && [ -z "$WAYLAND_DISPLAY" ] && [ -z "$DISPLAY" ]; then
    # -d: no client-side decorations; -s: allow switching to another tty (Ctrl+Alt+F2) for maintenance
    cage -d -s -- "${BROWSER[@]}"
else
    # Already inside a desktop session (or cage missing): plain fullscreen browser
    "${BROWSER[@]}"
fi

# The kiosk was closed: stop a server started by this script (the systemd one keeps running)
[ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
