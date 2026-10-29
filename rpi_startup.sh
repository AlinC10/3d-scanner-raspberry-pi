#!/usr/bin/env bash

# Calea către folderul proiectului tău
PROJECT_DIR="/home/ducks/3d-scanner-raspberry-pi"
URL="http://127.0.0.1:8000"

cd "$PROJECT_DIR" || exit 1

# 1. start FastAPI server
if [ -d ".venv" ]; then
    ./.venv/bin/python main_prod.py &
else
    python3 main_prod.py &
fi

SERVER_PID=$!

# 2. wait for FastAPI server start
while ! curl -s -o /dev/null -w "%{http_code}" "$URL" | grep -qE "200|404|307"; do
    sleep 0.5
done
echo "FastAPI server launched!"

# 3. Deschide Firefox în mod Kiosk
#    --kiosk: fullscreen mode
firefox --kiosk "$URL"

# if Firefox is closed, kill the server
kill $SERVER_PID 2>/dev/null