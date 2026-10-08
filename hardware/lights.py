import atexit
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from hardware.relay import Relay

log = logging.getLogger(__name__)

RP1_PWM_MARKERS = ["1f00098000", "1f0009c000", "1000120000.pcie"]


class LightLockedError(RuntimeError):
    """Raised when an operation is attempted while light brightness is locked by an active scan."""
    pass


class DimmableLight:
    """
    Controls 12V LED illumination using a mechanical Relay for power cut
    and Raspberry Pi 5 RP1 Hardware PWM (via D4184 MOSFET) for brightness.
    """

    def __init__(
        self,
        relay_pin: int = 11,
        pwm_channel: int = 0,
        pwm_chip: Optional[int] = None,
        frequency: int = 10000,
        initial_brightness: float = 1.0,
        active_high_relay: bool = True,
    ):
        self._lock = threading.RLock()
        self._closed = False
        self._is_on = False
        self._locked = False
        self._brightness = max(0.0, min(1.0, float(initial_brightness)))

        self._relay = Relay(pin=relay_pin, active_high=active_high_relay, initial_value=False)

        try:
            if pwm_chip is None:
                import os
                if "PWM_CHIP" in os.environ:
                    pwm_chip = int(os.environ["PWM_CHIP"])
                else:
                    pwm_chip = self._find_pwm_chip()
            log.info(
                "[Lights] Initializing hardware PWM: chip=%d, channel=%d, frequency=%d Hz",
                pwm_chip,
                pwm_channel,
                frequency,
            )

            from rpi_hardware_pwm import HardwarePWM

            import subprocess
            try:
                # Force pin multiplexing for Raspberry Pi 5 RP1 PWM 
                # (since dtoverlay=pwm-2chan doesn't always mux GPIO 12 automatically on Pi 5)
                # Hardcoded to GPIO 12 for PWM Channel 0
                if pwm_channel == 0:
                    subprocess.run(["pinctrl", "set", "12", "a0"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log.warning("[Lights] Failed to run pinctrl for pin muxing: %s", e)
                
            self._pwm = HardwarePWM(pwm_channel=pwm_channel, hz=frequency, chip=pwm_chip)
            self._pwm.start(0)
        except ImportError:
            self._relay.close()
            raise RuntimeError("Please install 'rpi-hardware-pwm' via pip.")
        except Exception as e:
            self._relay.close()
            raise RuntimeError(
                f"Failed to open hardware PWM (chip={pwm_chip}, channel={pwm_channel}). "
                f"Is 'dtoverlay=pwm-2chan' enabled in /boot/firmware/config.txt? Error: {e}"
            )

        atexit.register(self.close)

    @staticmethod
    def _find_pwm_chip() -> int:
        chips = []
        for p in Path("/sys/class/pwm").glob("pwmchip*"):
            try:
                idx = int(p.name.replace("pwmchip", ""))
                chips.append((idx, p))
            except ValueError:
                continue

        chips.sort(key=lambda x: x[0])
        if not chips:
            raise RuntimeError(
                "No /sys/class/pwm/pwmchip* found. Is 'dtoverlay=pwm-2chan' enabled in /boot/firmware/config.txt? A reboot is required after adding it."
            )

        discovered_info = []

        # 1. Match by RP1 base address marker (PWM0: 1f00098000, PWM1: 1f0009c000)
        for index, chip in chips:
            try:
                device = str((chip / "device").resolve()).lower()
                npwm = (chip / "npwm").read_text().strip() if (chip / "npwm").exists() else "?"
                discovered_info.append(f"pwmchip{index} (npwm={npwm}, device={device})")
                for marker in RP1_PWM_MARKERS:
                    if marker.lower() in device:
                        log.info("[Lights] Identified RP1 PWM chip%d (%s, npwm=%s)", index, marker, npwm)
                        return index
            except OSError as e:
                discovered_info.append(f"pwmchip{index} (read error: {e})")

        # 2. Defensive fallback: match by channel count (RP1 PWM has 2 or 4 channels; CPU cooler fan has 1)
        for index, chip in chips:
            try:
                npwm_str = (chip / "npwm").read_text().strip()
                npwm_val = int(npwm_str)
                if npwm_val >= 2:
                    log.warning("[Lights] RP1 address marker not matched, but pwmchip%d has %d channels. Using it.", index, npwm_val)
                    return index
            except (OSError, ValueError):
                pass

        details = "; ".join(discovered_info)
        raise RuntimeError(
            f"Could not identify the RP1 PWM chip. Detected PWM chips: [{details}]. "
            "If only the active cooler fan chip (1 channel) is listed, make sure 'dtoverlay=pwm-2chan' is in /boot/firmware/config.txt and you have REBOOTED the Pi."
        )

    def _target_duty(self) -> float:
        if self._brightness <= 0.0:
            return 0.0
        return self._brightness * 100.0

    def on(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Light device is closed.")

            # If brightness was previously 0, default back to 100% on power on
            if self._brightness <= 0.0:
                self._brightness = 1.0

            if not getattr(self._relay, "is_active", False):
                self._relay.on()
                time.sleep(0.05)  # Relay contact bounce settle

            self._is_on = True
            duty = self._target_duty()
            self._pwm.change_duty_cycle(duty)
            log.info("[Lights] ON (Brightness: %.0f%%, Duty: %.1f%%)", self._brightness * 100, duty)

    def off(self):
        with self._lock:
            if self._closed:
                return

            # Instantly drop PWM to zero before killing power
            try:
                self._pwm.change_duty_cycle(0.0)
            except Exception:
                pass

            try:
                if not getattr(self._relay, "closed", False):
                    self._relay.off()
            except Exception:
                pass

            self._is_on = False
            self._locked = False
            log.info("[Lights] OFF")

    def toggle(self):
        with self._lock:
            if self._locked:
                raise LightLockedError("Brightness is locked (scan in progress).")
            if self._is_on:
                self.off()
            else:
                self.on()

    def set_brightness(self, value: float):
        with self._lock:
            if self._locked:
                raise LightLockedError("Brightness is locked (scan in progress).")

            self._brightness = max(0.0, min(1.0, float(value)))
            if self._is_on:
                if self._brightness > 0:
                    self._pwm.change_duty_cycle(self._target_duty())
                else:
                    self.off()

    @property
    def brightness(self) -> float:
        return self._brightness

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def is_active(self) -> bool:
        """Alias for gpiozero output device compatibility."""
        return self._is_on

    def lock_brightness(self):
        with self._lock:
            self._locked = True

    def unlock_brightness(self):
        with self._lock:
            self._locked = False

    @property
    def locked(self) -> bool:
        return self._locked

    def close(self):
        with self._lock:
            if self._closed:
                return
            try:
                self.off()
            except Exception as e:
                log.warning("[Lights] Error turning off during close: %s", e)

            self._closed = True

            try:
                self._pwm.stop()
            except Exception as e:
                log.warning("[Lights] Error stopping PWM: %s", e)

            try:
                if not getattr(self._relay, "closed", False):
                    self._relay.close()
            except Exception as e:
                log.warning("[Lights] Error closing relay: %s", e)

            try:
                atexit.unregister(self.close)
            except Exception:
                pass
