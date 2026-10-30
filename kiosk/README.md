# Kiosk start (Raspberry Pi 5, Raspberry Pi OS Trixie)

Goal (KIOSK-1): no desktop. At boot the FastAPI server starts as a systemd service; the console logs in by itself on
tty1 and starts `cage` (a Wayland compositor that shows one application) with the browser in kiosk mode. The browser
shows `kiosk/splash.html` (a local page, dark, with a spinner) at once and opens `http://127.0.0.1:8000` as soon as the
server answers.

| File | Role |
|---|---|
| `kiosk/3d-scanner.service` | systemd unit of the server (`main_prod.py`, `127.0.0.1:8000`, restarts on failure) |
| `rpi_startup.sh` | kiosk session: `cage` + browser on the splash page (falls back to starting the server itself) |
| `kiosk/splash.html` | loading page, polls the server every 0.3 s |
| `kiosk/50-3d-scanner-udisks.rules` | polkit: the service may mount / unmount USB sticks (USB export) |

The paths assume user `ducks` and the project in `/home/ducks/3d-scanner-raspberry-pi`; change them in
`3d-scanner.service` otherwise.

## Setup (once, on the Pi)

```bash
# 1. Packages: cage (kiosk compositor), udisks2 (USB export), firefox already installed
sudo apt update && sudo apt install -y cage udisks2

# 2. Hardware groups for the user the service runs as
sudo usermod -aG gpio,i2c,dialout,video,render,input ducks

# 3. Server as a service
cd /home/ducks/3d-scanner-raspberry-pi
chmod +x rpi_startup.sh
sudo cp kiosk/3d-scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now 3d-scanner.service
systemctl status 3d-scanner.service          # active (running)
curl -s http://127.0.0.1:8000/scanner/status  # answers

# 4. USB export without a desktop
sudo cp kiosk/50-3d-scanner-udisks.rules /etc/polkit-1/rules.d/
sudo systemctl restart polkit

# 5. Boot to the console with autologin (no desktop): raspi-config, System Options > Boot / Auto Login >
#    "Console Autologin", or:
sudo raspi-config nonint do_boot_behaviour B2

# 6. Start the kiosk on tty1 after the autologin (only there: an SSH login keeps a normal shell)
cat >> ~/.bash_profile <<'PROFILE'
if [ -z "$WAYLAND_DISPLAY" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec /home/ducks/3d-scanner-raspberry-pi/rpi_startup.sh
fi
PROFILE

# 7. Remove the old autostart of rpi_startup.sh from the desktop, if there was one
#    (~/.config/autostart/*.desktop or ~/.config/labwc/autostart / wayfire.ini), then:
sudo reboot
```

## Day to day

| What | Command |
|---|---|
| Server log | `journalctl -u 3d-scanner -f` |
| Restart the server (after a `git pull`) | `sudo systemctl restart 3d-scanner` |
| Leave the kiosk | `Ctrl+Alt+F2` (another console; `cage -s` allows it), log in, work, `Ctrl+Alt+F1` back |
| Restart the kiosk | from SSH or tty2: `sudo systemctl restart getty@tty1` |
| Back to the desktop | `sudo raspi-config nonint do_boot_behaviour B4` and remove the lines from `~/.bash_profile` |

Never run a second server by hand (`python main_prod.py`, `fastapi dev`) while the service runs: the cameras, the
serial port and the GPIO can only be owned by one process. Stop the service first (`sudo systemctl stop 3d-scanner`).

## Not verified

Written on a PC, not tried on the Pi. If Firefox shows a first-run page or a "restore session" bar in the kiosk,
start it once normally (`firefox`) and close it, or switch `rpi_startup.sh` to Chromium.
