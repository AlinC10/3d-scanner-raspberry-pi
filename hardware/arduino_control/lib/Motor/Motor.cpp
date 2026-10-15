#include "Motor.h"
#include "Protocol.h"

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
    enableStatus(false) // EN is only driven by pinSetup(); until then the motor is not known to be energized
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
        // EN is active LOW (LOW = driver energized). Latch HIGH in the port register BEFORE switching the pin to
        // OUTPUT, otherwise it would come up LOW (energized) while enableStatus is false, and DISABLE / 0xFF would
        // be no-ops until the first ENABLE or move.
        digitalWrite(this->enPin, HIGH);
        pinMode(this->enPin, OUTPUT);
    }
    this->enableStatus = false;
    this->configured = true;
}

void Motor::enable() {
    if (this->enPin < 0 || this->isEnabled())
        return;

    digitalWrite(this->enPin, LOW);
    this->enableStatus = true;
}

void Motor::disable() {
    // Always write the pin (idempotent): it must not depend on enableStatus, which can disagree with the real EN level.
    if (this->enPin < 0)
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

    // Same absolute-time scheduling and 0xFF polling as rotateRamp(): the first pulse is emitted immediately and a stop
    // is honoured within ~1 ms even with step periods above 16 ms. stepAt() enforces the 60 us period floor.
    uint32_t nextStepAt = micros();
    for (uint32_t i = 0; i < steps; i++) {
        const uint8_t status = this->stepAt(nextStepAt, delayMicros);
        if (status != STATUS_OK) return status;
    }

    // Let the final step period elapse before reporting completion (same as rotateRamp).
    return this->waitUntil(nextStepAt);
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
// Returns true (and releases the coils) if an emergency stop byte is waiting at the head of the serial buffer.
bool Motor::abortRequested() {
    if (Serial.available() > 0) {
        const uint8_t b = Serial.peek();
        if (b == CMD_EMERGENCY_STOP || b == CMD_EMERGENCY_ALIAS) {
            Serial.read(); // Consume opcode
            this->disable();
            return true;
        }
    }
    return false;
}

// Busy-waits until micros() reaches `deadline` (wrap-safe), polling for an emergency stop the whole time.
uint8_t Motor::waitUntil(const uint32_t deadline) {
    while ((int32_t)(micros() - deadline) < 0) {
        if (this->abortRequested()) return STATUS_ERR_ABORTED;
    }
    return STATUS_OK;
}

// Emits one STEP pulse at the absolute time `nextStepAt`, then schedules the following step `periodUs` later.
// Scheduling against an absolute time means the cost of computing the next period is hidden inside the wait.
uint8_t Motor::stepAt(uint32_t& nextStepAt, const uint32_t periodUs) {
    const uint8_t status = this->waitUntil(nextStepAt);
    if (status != STATUS_OK) return status;
    if (this->abortRequested()) return STATUS_ERR_ABORTED;

    digitalWrite(this->pulsePin, HIGH);
    delayMicroseconds(50); // TB6600 requirement
    digitalWrite(this->pulsePin, LOW);

    nextStepAt += (periodUs < 60) ? 60 : periodUs;
    return STATUS_OK;
}

// Step period (us) for a squared speed v2 (steps^2 / s^2).
// NOTE: sqrtf + a float divide cost roughly 200-400 us on the ATmega328P (estimate, not measured). Because steps are
// scheduled against an absolute deadline this is hidden inside the wait, but below a ~400 us step period it can exceed
// the period and make ramp steps run late. The firmware does not guard against this; the Python side enforces
// STEP_DELAY_MIN = 0.0001 s (hardware/motor.py) and recommends >= 0.0005 s for scans.
static inline uint32_t periodFromSpeedSquared(const float v2) {
    return (uint32_t)(1000000.0f / sqrtf(v2) + 0.5f);
}

uint8_t Motor::rotateRamp(const bool clockwise, const uint32_t steps, const uint32_t targetDelayUs, uint32_t startDelayUs, uint32_t accelSteps, uint32_t decelSteps, uint32_t endDelayUs) {
    if (!this->isConfigured()) return STATUS_ERR_NOT_CONFIGURED;
    if (steps == 0 || targetDelayUs == 0) return STATUS_ERR_INVALID_ARG;

    this->enable();

    digitalWrite(this->dirPin, clockwise ? HIGH : LOW);
    delayMicroseconds(20);

    if (startDelayUs < targetDelayUs) {
        startDelayUs = targetDelayUs;
    }
    // endDelayUs == 0 means "no slow tail": decelerate back down to the start speed.
    if (endDelayUs == 0) {
        endDelayUs = startDelayUs;
    }
    if (endDelayUs < targetDelayUs) {
        endDelayUs = targetDelayUs;
    }

    if (accelSteps + decelSteps > steps) {
        uint32_t total = accelSteps + decelSteps;
        accelSteps = (uint32_t)(((uint64_t)accelSteps * steps) / total);
        decelSteps = steps - accelSteps;
    }

    const uint32_t cruiseSteps = steps - (accelSteps + decelSteps);

    // Constant acceleration: v^2 is linear in the number of steps travelled (v^2 = v0^2 + 2*a*s),
    // i.e. speed is a straight ramp in time.
    const float vStart = 1000000.0f / (float)startDelayUs;
    const float vCruise = 1000000.0f / (float)targetDelayUs;
    const float vEnd = 1000000.0f / (float)endDelayUs;
    const float vStart2 = vStart * vStart;
    const float vCruise2 = vCruise * vCruise;
    const float vEnd2 = vEnd * vEnd;

    uint32_t nextStepAt = micros();

    // 1. Acceleration Phase: vStart -> vCruise
    for (uint32_t i = 0; i < accelSteps; i++) {
        const float v2 = vStart2 + (vCruise2 - vStart2) * (float)(i + 1) / (float)accelSteps;
        const uint8_t status = this->stepAt(nextStepAt, periodFromSpeedSquared(v2));
        if (status != STATUS_OK) return status;
    }

    // 2. Cruise Phase
    for (uint32_t i = 0; i < cruiseSteps; i++) {
        const uint8_t status = this->stepAt(nextStepAt, targetDelayUs);
        if (status != STATUS_OK) return status;
    }

    // 3. Deceleration Phase: vCruise -> vEnd (the last step is emitted at vEnd)
    for (uint32_t i = 0; i < decelSteps; i++) {
        const float v2 = vCruise2 - (vCruise2 - vEnd2) * (float)(i + 1) / (float)decelSteps;
        const uint8_t status = this->stepAt(nextStepAt, periodFromSpeedSquared(v2));
        if (status != STATUS_OK) return status;
    }

    // Let the final step period elapse before reporting completion, so the rotor has actually settled on the last step.
    return this->waitUntil(nextStepAt);
}
