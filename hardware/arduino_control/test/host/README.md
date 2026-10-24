# Host-side firmware test

Runs `Motor.cpp` and `CommandHandler.cpp` on a PC against a fake `Arduino.h` (simulated pins, clock and serial port).
No PlatformIO and no hardware needed; only `g++` with C++17.

```bash
cd hardware/arduino_control/test/host
./run.sh
```

`run.sh` is equivalent to:

```bash
g++ -std=c++17 -Wall -Wextra -O1 \
    -I . -I ../../lib/Motor -I ../../lib/CommandHandler -I ../../lib/Protocol \
    motor_host_test.cpp ../../lib/Motor/Motor.cpp ../../lib/CommandHandler/CommandHandler.cpp \
    -o /tmp/motor_host_test && /tmp/motor_host_test
```

The process exit code is non-zero if any check fails.

What it covers: EN is HIGH after `CONFIG`; `disable()` always writes the pin; `0xFF` at idle and mid-move releases the
motors; abort inside a 30 ms step wait is honoured immediately (`rotate` and `rotateRamp`); first pulse is immediate;
`end_delay` reaches the last step period; `micros()` wrap-around.

Limits: the fake clock does not model CPU cost (`sqrtf`, serial interrupts), so it cannot validate timing at very short
step periods; that needs a hardware test.
