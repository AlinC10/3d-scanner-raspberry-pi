// Minimal fake of the Arduino core for running Motor.cpp / CommandHandler.cpp on a PC (see README.md).
// Time is simulated: micros() advances by 1 us per call, delayMicroseconds()/delay() advance it by the requested
// amount, so busy-wait loops terminate and every pin write is stamped with a reproducible time.
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <math.h>
#include <string.h>
#include <stdlib.h>
#include <string>
#include <utility>
#include <vector>

constexpr uint8_t LOW = 0;
constexpr uint8_t HIGH = 1;
constexpr uint8_t INPUT = 0;
constexpr uint8_t OUTPUT = 1;

#define F(x) (x)

struct PinEvent {
    uint32_t t;      // simulated time (us)
    uint8_t pin;
    bool isMode;     // true: pinMode(), false: digitalWrite()
    uint8_t value;   // new mode, or new port value
    uint8_t driven;  // level on the pin right after the event (meaningful when isOutput)
    bool isOutput;   // pin is in OUTPUT mode right after the event
};

namespace fake {
struct State {
    uint32_t now = 0;
    uint8_t port[64];
    uint8_t mode[64];
    std::vector<PinEvent> events;
    std::vector<std::pair<uint32_t, uint8_t>> pendingRx;  // (time, byte): delivered when the clock reaches `time`
    std::vector<uint8_t> rx;
    std::string tx;

    State() { reset(); }

    void reset() {
        now = 0;
        memset(port, 0, sizeof(port));  // AVR PORT registers reset to 0
        memset(mode, 0, sizeof(mode));  // all pins start as INPUT
        events.clear();
        pendingRx.clear();
        rx.clear();
        tx.clear();
    }

    void pump() {
        for (size_t i = 0; i < pendingRx.size();) {
            if ((int32_t)(now - pendingRx[i].first) >= 0) {
                rx.push_back(pendingRx[i].second);
                pendingRx.erase(pendingRx.begin() + (long)i);
            } else {
                i++;
            }
        }
    }
};

inline State& st() {
    static State s;
    return s;
}
inline void reset() { st().reset(); }
// Level currently driven on a pin (what the TB6600 input sees). INPUT pins read as LOW here.
inline uint8_t level(uint8_t pin) { return st().mode[pin] == OUTPUT ? st().port[pin] : 0; }
inline void injectAt(uint32_t t, uint8_t byte) { st().pendingRx.push_back({t, byte}); }
inline void feedLine(const char* s) {
    for (; *s; s++) st().rx.push_back((uint8_t)*s);
}
}  // namespace fake

inline void pinMode(uint8_t pin, uint8_t m) {
    auto& s = fake::st();
    s.mode[pin] = m;
    s.events.push_back({s.now, pin, true, m, s.port[pin], m == OUTPUT});
}

inline void digitalWrite(uint8_t pin, uint8_t v) {
    auto& s = fake::st();
    s.port[pin] = v;
    s.events.push_back({s.now, pin, false, v, v, s.mode[pin] == OUTPUT});
}

inline int digitalRead(uint8_t pin) { return fake::level(pin); }
inline uint32_t micros() { return fake::st().now++; }
inline void delayMicroseconds(uint32_t us) { fake::st().now += us; }
inline void delay(uint32_t ms) { fake::st().now += ms * 1000u; }

struct FakeSerial {
    void begin(uint32_t) {}
    int available() {
        fake::st().pump();
        return (int)fake::st().rx.size();
    }
    int peek() {
        fake::st().pump();
        return fake::st().rx.empty() ? -1 : fake::st().rx.front();
    }
    int read() {
        fake::st().pump();
        if (fake::st().rx.empty()) return -1;
        const int b = fake::st().rx.front();
        fake::st().rx.erase(fake::st().rx.begin());
        return b;
    }
    void println(const char* s) {
        fake::st().tx += s;
        fake::st().tx += '\n';
    }
    void println(long v) {
        fake::st().tx += std::to_string(v);
        fake::st().tx += '\n';
    }
};
inline FakeSerial Serial;
