# VCM driver IC (DW9714-compatible) on Raspberry Pi CSI camera I2C bus
VCM_I2C_BUS      = 10          # /dev/i2c-10  (CSI camera bus on RPi 5)
VCM_I2C_ADDR     = 0x0C        # Default address for Arducam B0272 VCM
VCM_MIN_POS      = 0           # Far / infinity
VCM_MAX_POS      = 1023        # Near / macro
VCM_STEP_SMALL   = 10
VCM_STEP_LARGE   = 50
VCM_MOVE_DELAY_S = 0.06        # ~60 ms settling time per step