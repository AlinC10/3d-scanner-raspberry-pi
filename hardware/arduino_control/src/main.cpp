#include <Arduino.h>
#include <Motor.h>
#include <CommandHandler.h>

constexpr uint8_t motorNumber = 2;

Motor motors[motorNumber];
CommandHandler commandHandler(motors, motorNumber);

void setup() {
    commandHandler.begin(115200);
}

void loop() {
    commandHandler.update();
}