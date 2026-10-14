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

    bool abortRequested();
    uint8_t waitUntil(uint32_t deadline);
    uint8_t stepAt(uint32_t& nextStepAt, uint32_t periodUs);

public:
    Motor();
    Motor(uint8_t id, int8_t pulsePin, int8_t dirPin, int8_t enPin = -1);
    void pinSetup(uint8_t id, int8_t pulsePin, int8_t dirPin, int8_t enPin = -1);
    void enable();
    void disable();
    uint8_t rotate(bool clockwise, uint32_t steps, uint32_t delayMicros);
    // endDelayUs == 0 -> decelerate back down to startDelayUs (no slow tail).
    uint8_t rotateRamp(bool clockwise, uint32_t steps, uint32_t targetDelayUs, uint32_t startDelayUs, uint32_t accelSteps, uint32_t decelSteps, uint32_t endDelayUs = 0);
    void stop();
    bool isConfigured() const;
    bool isEnabled() const;
};