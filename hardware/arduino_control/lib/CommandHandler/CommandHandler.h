#pragma once
#include <Arduino.h>
#include "Motor.h"
#include "Protocol.h"

class CommandHandler {
private:
    Motor* motors;
    uint8_t motorCount;
    char inputBuffer[64];
    uint8_t bufferPos;


public:
    CommandHandler(Motor* motors, uint8_t motorCount);
    void begin(uint32_t baudRate = 115200);
    void update();
    void parseAndExecute(const char* cmd);
};