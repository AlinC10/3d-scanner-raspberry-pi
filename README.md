# 3D Scanner - Raspberry Pi 5 & Arduino Uno

A high-precision, multi-tier photogrammetry 3D scanner combining a Raspberry Pi 5 brain with a dedicated Arduino Uno execution unit for real-time stepper motor kinematics.

---

## 📌 Active Development: Arduino Motor Controller Migration

Physical pulse generation for NEMA 17 stepper motors (turntable & vertical Z-axis) is offloaded to an Arduino Uno running bare-metal C++ via PlatformIO in [`hardware/arduino_control/`](hardware/arduino_control/).


## 📚 Documentation & Reference Links

* **Detailed Firmware Architecture & Feedback**: [`hardware/arduino_control/README.md`](hardware/arduino_control/README.md)
* **Arduino Migration Plan**: [`plans/implemented/arduino_motor_migration_plan.md`](plans/implemented/arduino_motor_migration_plan.md)
* **Dual Camera Synchronous Capture**: [`hardware/docs/DUAL_CAMERAS.md`](hardware/docs/DUAL_CAMERAS.md)
* **Original Motor Documentation**: [`hardware/docs/MOTOR.md`](hardware/docs/MOTOR.md)
* **Scanner State Machine**: [`hardware/docs/SCANNER.md`](hardware/docs/SCANNER.md)
