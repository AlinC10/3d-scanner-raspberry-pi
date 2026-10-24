#!/usr/bin/env bash
# Build and run the host-side firmware test (no PlatformIO, no hardware needed).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FW="$(cd "$HERE/../.." && pwd)"
OUT="${TMPDIR:-/tmp}/motor_host_test"

# -I "$HERE" first so that <Arduino.h> resolves to the fake in this folder.
g++ -std=c++17 -Wall -Wextra -O1 \
    -I "$HERE" -I "$FW/lib/Motor" -I "$FW/lib/CommandHandler" -I "$FW/lib/Protocol" \
    "$HERE/motor_host_test.cpp" \
    "$FW/lib/Motor/Motor.cpp" \
    "$FW/lib/CommandHandler/CommandHandler.cpp" \
    -o "$OUT"

"$OUT"
