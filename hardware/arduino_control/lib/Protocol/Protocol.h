#pragma once
#include <Arduino.h>

// Command Opcodes (Wire Protocol)
enum CommandOpcode : uint8_t {
    CMD_PING            = 0x01, // Handshake / liveness check
    CMD_CONFIG          = 0x02, // Configure motor pins (id, step, dir, en)
    CMD_ENABLE          = 0x03, // Energize motor coils (active holding torque)
    CMD_DISABLE         = 0x04, // De-energize motor coils (release torque)
    CMD_ROTATE          = 0x05, // Execute microsecond stepping loop
    CMD_STOP            = 0x06, // In-band stop for specific motor

    // Reserved Future Features (Roadmap)
    // CMD_ACCEL_ROTATE    = 0x10, // Trapezoidal velocity ramping move
    // CMD_SET_ACCEL       = 0x11, // Set acceleration rate & jerk limits
    CMD_GET_STATUS      = 0x20, // Query motor state & step position
    CMD_GET_ENDSTOPS    = 0x21, // Read hardware limit switch states

    // Out-of-band Universal Immediate Emergency Stop
    CMD_EMERGENCY_STOP  = 0xFF, // Universal single-byte abort (<1us check)
    CMD_EMERGENCY_ALIAS = '!'   // ASCII convenience alias for Serial Monitor
};

// Status / Return Codes (POSIX standard: 0 = Success, non-zero = specific error)
enum StatusCode : uint8_t {
    STATUS_OK                 = 0, // Success / Target reached without error
    STATUS_ERR_UNKNOWN_CMD    = 1, // Opcode or command unrecognized
    STATUS_ERR_INVALID_ARG    = 2, // Arguments missing or out of valid range
    STATUS_ERR_NOT_CONFIGURED = 3, // Motor not initialized via CONFIG
    STATUS_ERR_ABORTED        = 4, // Aborted mid-motion by emergency stop
    STATUS_ERR_BUSY           = 5  // Controller busy executing operation
};