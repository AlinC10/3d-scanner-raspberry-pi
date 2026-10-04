#pragma once
#include <Arduino.h>

class Motor {
private:
    uint8_t id;
    int8_t dirPin;
    int8_t pulsePin;
    int8_t enPin;
    bool configured;
    bool enableStatus;

public:
    Motor();
    Motor(uint8_t id, int8_t pulsePin, int8_t dirPin, int8_t enPin = -1);
    void pinSetup(uint8_t id, int8_t pulsePin, int8_t dirPin, int8_t enPin = -1);
    void enable();
    void disable();
    uint8_t rotate(bool clockwise, uint32_t steps, uint32_t delayMicros);
    void stop();
    bool isConfigured() const;
    bool isEnabled() const;
};