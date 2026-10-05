#include "Motor.h"
#include "Protocol.h"

static inline void safeDelayMicroseconds(uint32_t us) {
    if (us > 16000) {
        delay(us / 1000);
        delayMicroseconds(us % 1000);
    } else if (us > 0) {
        delayMicroseconds(us);
    }
}

Motor::Motor():
    id(0),
    dirPin(-1),
    pulsePin(-1),
    enPin(-1),
    configured(false),
    enableStatus(false)
{
}

Motor::Motor(const uint8_t id, const int8_t pulsePin, const int8_t dirPin, const int8_t enPin):
    id(id),
    dirPin(dirPin),
    pulsePin(pulsePin),
    enPin(enPin),
    configured(false),
    enableStatus(enPin != -1)
{

}

void Motor::pinSetup(const uint8_t id , const int8_t pulsePin, const int8_t dirPin, const int8_t enPin) {
    this->id = id;
    this->dirPin = dirPin;
    this->pulsePin = pulsePin;
    this->enPin = enPin;

    pinMode(this->dirPin, OUTPUT);
    pinMode(this->pulsePin, OUTPUT);

    if (this->enPin >= 0) {
        pinMode(this->enPin, OUTPUT);
        this->disable();
    }
    this->configured = true;
}

void Motor::enable() {
    if (this->enPin == -1 || this->isEnabled())
        return;

    digitalWrite(this->enPin, LOW);
    this->enableStatus = true;
}

void Motor::disable() {
    if (this->enPin == -1 || !this->isEnabled())
        return;

    digitalWrite(this->enPin, HIGH);
    this->enableStatus = false;
}

uint8_t Motor::rotate(const bool clockwise, const uint32_t steps, const uint32_t delayMicros) {
    if (!this->isConfigured())
        return STATUS_ERR_NOT_CONFIGURED; // motor is not configured

    if (steps == 0)
        return STATUS_ERR_INVALID_ARG; // steps needs to be > 0

    this->enable();

    digitalWrite(this->dirPin, clockwise ? HIGH : LOW);
    delayMicroseconds(20); // TB6600 direction setup time

    constexpr uint8_t TB6600_REQ_MICROS_DELAY = 10;
    const uint32_t lowDelay = (delayMicros > TB6600_REQ_MICROS_DELAY) ? (delayMicros - TB6600_REQ_MICROS_DELAY) : 1;

    for (uint32_t i = 0; i < steps; i++) {
        if (Serial.available() > 0) {
            const uint8_t b = Serial.peek();

            if (b == CMD_EMERGENCY_STOP || b == CMD_EMERGENCY_ALIAS) {
                Serial.read();
                this->disable();
                return STATUS_ERR_ABORTED;
            }
        }

        digitalWrite(this->pulsePin, HIGH);
        delayMicroseconds(TB6600_REQ_MICROS_DELAY);
        digitalWrite(this->pulsePin, LOW);
        safeDelayMicroseconds(lowDelay);
    }

    return STATUS_OK;
}

void Motor::stop() {
    this->disable();
}

bool Motor::isConfigured() const {
    return this->configured;
}

bool Motor::isEnabled() const {
    return this->enableStatus;
}
inline uint8_t Motor::stepMotorWithCheck(uint32_t delayUs) {
    if (Serial.available() > 0) {
        const uint8_t b = Serial.peek();
        if (b == CMD_EMERGENCY_STOP || b == CMD_EMERGENCY_ALIAS) {
            Serial.read(); // Consume opcode
            this->disable();
            return STATUS_ERR_ABORTED;
        }
    }
    digitalWrite(this->pulsePin, HIGH);
    delayMicroseconds(5); // TB6600 requirement
    digitalWrite(this->pulsePin, LOW);
    
    safeDelayMicroseconds(delayUs > 5 ? delayUs - 5 : 5);
    return STATUS_OK;
}

uint8_t Motor::rotateRamp(const bool clockwise, const uint32_t steps, const uint32_t targetDelayUs, uint32_t startDelayUs, uint32_t accelSteps, uint32_t decelSteps) {
    if (!this->isConfigured()) return STATUS_ERR_NOT_CONFIGURED;
    if (steps == 0) return STATUS_ERR_INVALID_ARG;
    
    this->enable();
    
    digitalWrite(this->dirPin, clockwise ? HIGH : LOW);
    delayMicroseconds(20);

    if (startDelayUs < targetDelayUs) {
        startDelayUs = targetDelayUs;
    }

    if (accelSteps + decelSteps > steps) {
        uint32_t total = accelSteps + decelSteps;
        accelSteps = (uint32_t)(((uint64_t)accelSteps * steps) / total);
        decelSteps = steps - accelSteps;
    }

    uint32_t cruiseSteps = steps - (accelSteps + decelSteps);
    uint32_t delayDiff = startDelayUs - targetDelayUs;

    // 1. Acceleration Phase
    for (uint32_t i = 0; i < accelSteps; i++) {
        uint32_t currentDelay = startDelayUs - (uint32_t)(((uint64_t)delayDiff * i) / accelSteps);
        uint8_t status = this->stepMotorWithCheck(currentDelay);
        if (status != STATUS_OK) return status;
    }

    // 2. Cruise Phase
    for (uint32_t i = 0; i < cruiseSteps; i++) {
        uint8_t status = this->stepMotorWithCheck(targetDelayUs);
        if (status != STATUS_OK) return status;
    }

    // 3. Deceleration Phase
    for (uint32_t i = 0; i < decelSteps; i++) {
        uint32_t currentDelay = targetDelayUs + (uint32_t)(((uint64_t)delayDiff * i) / decelSteps);
        uint8_t status = this->stepMotorWithCheck(currentDelay);
        if (status != STATUS_OK) return status;
    }

    return STATUS_OK;
}
