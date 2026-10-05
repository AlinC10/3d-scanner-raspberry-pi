#include "CommandHandler.h"
#include "Protocol.h"

CommandHandler::CommandHandler(Motor* motors, const uint8_t motorCount):
    motors(motors),
    motorCount(motorCount),
    bufferPos(0)
{}

void CommandHandler::begin(const uint32_t baudRate) {
    Serial.begin(baudRate);
    Serial.println(STATUS_OK);
}

void CommandHandler::update() {
    while (Serial.available() > 0) {
        const uint8_t c = Serial.read();

        // Immediate universal single-byte emergency stop intercept:
        if (c == CMD_EMERGENCY_STOP || c == CMD_EMERGENCY_ALIAS) {
            for (uint8_t i = 0; i < this->motorCount; i++) {
                this->motors[i].stop();
                this->motors[i].disable(); // Cut power to all coils!
            }
            continue;
        }

        // Newline-terminated string commands:
        if (c == '\n' || c == '\r') {
            if (this->bufferPos > 0) {
                this->inputBuffer[this->bufferPos] = '\0';
                parseAndExecute(this->inputBuffer);
                this->bufferPos = 0;
            }
        } else if (this->bufferPos < sizeof(this->inputBuffer) - 1) {
            this->inputBuffer[this->bufferPos++] = static_cast<char>(c);
        }
    }
}

void CommandHandler::parseAndExecute(const char* cmd) {
    char* p = strchr(cmd, ' '); // Pointer to arguments following the command token
    if (p) p++;                 // Advance past delimiter space

    // Instant O(1) dispatch on opcode digit or initial ASCII letter (zero strcmp!)
    switch (cmd[0]) {
        case '1':
            if (cmd[1] == '6') { // 16
                if (!p) { Serial.println(STATUS_ERR_INVALID_ARG); return; }
                const uint8_t id = strtol(p, &p, 10);
                const uint32_t steps = strtoul(p, &p, 10);
                const bool cw = strtol(p, &p, 10) == 1;
                const uint32_t target_delay = strtoul(p, &p, 10);
                const uint32_t start_delay = strtoul(p, &p, 10);
                const uint32_t accel = strtoul(p, &p, 10);
                const uint32_t decel = strtoul(p, &p, 10);
                
                if (id < this->motorCount && this->motors[id].isConfigured()) {
                    const uint8_t status = this->motors[id].rotateRamp(cw, steps, target_delay, start_delay, accel, decel);
                    Serial.println(status);
                } else {
                    Serial.println(id >= this->motorCount ? STATUS_ERR_INVALID_ARG : STATUS_ERR_NOT_CONFIGURED);
                }
                break;
            }
            // Fallthrough to PING if it's '1 '
            Serial.println(STATUS_OK); // 0
            break;
        case 'A':
        case 'a': { // ACCEL_ROTATE Alias
            if (!p) { Serial.println(STATUS_ERR_INVALID_ARG); return; }
            const uint8_t id = strtol(p, &p, 10);
            const uint32_t steps = strtoul(p, &p, 10);
            const bool cw = strtol(p, &p, 10) == 1;
            const uint32_t target_delay = strtoul(p, &p, 10);
            const uint32_t start_delay = strtoul(p, &p, 10);
            const uint32_t accel = strtoul(p, &p, 10);
            const uint32_t decel = strtoul(p, &p, 10);
            
            if (id < this->motorCount && this->motors[id].isConfigured()) {
                const uint8_t status = this->motors[id].rotateRamp(cw, steps, target_delay, start_delay, accel, decel);
                Serial.println(status);
            } else {
                Serial.println(id >= this->motorCount ? STATUS_ERR_INVALID_ARG : STATUS_ERR_NOT_CONFIGURED);
            }
            break;
        }

        case 'P':
        case 'p': // PING
            Serial.println(STATUS_OK); // 0
            break;

        case '2':
        case 'C':
        case 'c': { // CONFIG: 2 <id> <step> <dir> <en>
            if (!p) { Serial.println(STATUS_ERR_INVALID_ARG); return; }
            const uint8_t id = strtol(p, &p, 10);
            const int8_t step = strtol(p, &p, 10);
            const int8_t dir = strtol(p, &p, 10);
            const int8_t en = strtol(p, &p, 10);
            if (id < this->motorCount) {
                this->motors[id].pinSetup(id, step, dir, en);
                Serial.println(STATUS_OK);
            } else {
                Serial.println(STATUS_ERR_INVALID_ARG);
            }
            break;
        }

        case '3':
        case 'E':
        case 'e': { // ENABLE: 3 <id>
            if (!p) { Serial.println(STATUS_ERR_INVALID_ARG); return; }
            const uint8_t id = strtol(p, &p, 10);
            if (id < this->motorCount && this->motors[id].isConfigured()) {
                this->motors[id].enable();
                Serial.println(STATUS_OK);
            } else {
                Serial.println(id >= this->motorCount ? STATUS_ERR_INVALID_ARG : STATUS_ERR_NOT_CONFIGURED);
            }
            break;
        }

        case '4':
        case 'D':
        case 'd': { // DISABLE: 4 <id>
            if (!p) { Serial.println(STATUS_ERR_INVALID_ARG); return; }
            const uint8_t id = strtol(p, &p, 10);
            if (id < this->motorCount && this->motors[id].isConfigured()) {
                this->motors[id].disable();
                Serial.println(STATUS_OK);
            } else {
                Serial.println(id >= this->motorCount ? STATUS_ERR_INVALID_ARG : STATUS_ERR_NOT_CONFIGURED);
            }
            break;
        }

        case '5':
        case 'R':
        case 'r': { // ROTATE: 5 <id> <steps> <dir> <delay_us>
            if (!p) { Serial.println(STATUS_ERR_INVALID_ARG); return; }
            const uint8_t id     = strtol(p, &p, 10);
            const uint32_t steps = strtoul(p, &p, 10);
            const bool cw        = strtol(p, &p, 10) == 1;
            const uint32_t delay = strtoul(p, &p, 10);
            if (id < this->motorCount && this->motors[id].isConfigured()) {
                const uint8_t status = this->motors[id].rotate(cw, steps, delay);
                Serial.println(status);
            } else {
                Serial.println(id >= this->motorCount ? STATUS_ERR_INVALID_ARG : STATUS_ERR_NOT_CONFIGURED);
            }
            break;
        }

        case '6':
        case 'S':
        case 's': { // STOP: 6 <id>
            if (!p) { Serial.println(STATUS_ERR_INVALID_ARG); return; }
            const uint8_t id = strtol(p, &p, 10);
            if (id < this->motorCount) {
                this->motors[id].stop();
                Serial.println(STATUS_OK);
            } else {
                Serial.println(STATUS_ERR_INVALID_ARG);
            }
            break;
        }

        default:
            Serial.println(STATUS_ERR_UNKNOWN_CMD); // 1
            break;
    }
}