# Hardware Diagnostic & Testing Endpoints (`/test`)

This module provides isolated HTTP endpoints to test, calibrate, and verify individual hardware subsystems on the 3D scanner without running a full scan sequence.

Interactive OpenAPI docs are accessible at: `http://<pi-ip>:8000/docs#/Hardware%20Diagnostics%20%26%20Tests`

---

## Quick Reference Table

| Category | Method | Endpoint | Description |
|---|---|---|---|
| **Limit Switches** | `GET` | `/test/endstops` | Read top and bottom endstop electrical states |
| **Lighting** | `GET` | `/test/lights` | Get lighting relay state |
| **Lighting** | `POST` | `/test/lights/on` | Turn illumination LEDs ON |
| **Lighting** | `POST` | `/test/lights/off` | Turn illumination LEDs OFF |
| **Lighting** | `POST` | `/test/lights/toggle` | Toggle illumination LEDs state |
| **Homing** | `POST` | `/test/motor/home-bottom` | Move carriage down until bottom limit switch triggers |
| **Homing** | `POST` | `/test/motor/move-to-top` | Move carriage up until top limit switch triggers |
| **Motor Movement** | `POST` | `/test/motor/z-move` | Move Z-axis by distance in millimeters (`up` or `down`) |
| **Motor Movement** | `POST` | `/test/turntable/rotate` | Rotate turntable by degrees (`clockwise` or counter) |
| **Camera & Video** | `POST` | `/test/stream/start` | Launch RTSP live stream and return stream URLs |
| **Camera & Video** | `POST` | `/test/stream/stop` | Stop RTSP stream and release camera devices |
| **Camera & Video** | `GET` | `/test/stream/status` | Read streaming status and active stream URLs |
| **Camera & Video** | `POST` | `/test/camera/snap` | Capture a synchronized test photo pair from both cameras |
| **Safety & Reset** | `POST` | `/test/cleanup` | Emergency cut on motor torque, lights, and cameras |

---

## 1. Limit Switch Diagnostics

### `GET /test/endstops`
Reads the live electrical status of both physical limit switches.

* **Response (`200 OK`)**:
  ```json
  {
    "top_endstop": false,
    "bottom_endstop": true,
    "status": "triggered"
  }
  ```
  * `top_endstop`: `true` if carriage is contacting the top limit switch.
  * `bottom_endstop`: `true` if carriage is contacting the bottom limit switch.
  * `status`: `"triggered"` if either switch is active; `"clear"` if neither is active.

* **Example `curl`**:
  ```bash
  curl -X GET http://localhost:8000/test/endstops
  ```

---

## 2. Illumination Relay

### `GET /test/lights`
* **Response (`200 OK`)**:
  ```json
  {
    "is_on": true
  }
  ```

### `POST /test/lights/on`
Powers the relay ON to illuminate the scan chamber.
* **Response (`200 OK`)**: `{"status": "success", "is_on": true}`

### `POST /test/lights/off`
Powers the relay OFF.
* **Response (`200 OK`)**: `{"status": "success", "is_on": false}`

### `POST /test/lights/toggle`
Flips the current lighting state.
* **Response (`200 OK`)**: `{"status": "success", "is_on": true}`

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/lights/toggle
  ```

---

## 3. Motor Homing & Limit Seeking

### `POST /test/motor/home-bottom`
Moves the carriage downward in small increments (20 steps at 0.0005s delay) until the bottom limit switch activates. When triggered, the motor automatically disables torque.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "message": "Carriage successfully homed to bottom endstop.",
    "bottom_endstop": true
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/motor/home-bottom
  ```

---

### `POST /test/motor/move-to-top`
Moves the carriage upward until the top limit switch activates.

* **Query Parameters**:
  * `delay` (optional float, default `0.0005`, min `0.0001`, max `0.01`): Step pulse delay.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "message": "Carriage successfully reached top endstop.",
    "top_endstop": true
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST "http://localhost:8000/test/motor/move-to-top?delay=0.0005"
  ```

---

## 4. Manual Incremental Movement

### `POST /test/motor/z-move`
Moves the Z-axis carriage by a specified distance in millimeters. Automatically halted if an endstop is reached.

* **Request Body**:
  ```json
  {
    "distance_mm": 15.0,
    "direction": "up",
    "delay": 0.0005
  }
  ```
  * `distance_mm` (float, default `10.0`, range `0.1`–`200.0`): Travel distance.
  * `direction` (`"up"` | `"down"`, default `"up"`).
  * `delay` (float, default `0.0005`): Step pulse delay.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "direction": "up",
    "distance_mm": 15.0
  }
  ```

* **Error Response (`400 Bad Request`)**:
  Occurs if the destination direction hits a limit switch.
  ```json
  {
    "detail": "Cannot move UP: Top limit switch reached!"
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/motor/z-move \
       -H "Content-Type: application/json" \
       -d '{"distance_mm": 20.0, "direction": "up"}'
  ```

---

### `POST /test/turntable/rotate`
Rotates the turntable platter by a specific angle.

* **Request Body**:
  ```json
  {
    "angle": 18.0,
    "clockwise": true,
    "delay": 0.0005
  }
  ```
  * `angle` (float, default `18.0`, range `0.1`–`360.0`).
  * `clockwise` (bool, default `true`).
  * `delay` (float, default `0.0005`).

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "angle": 18.0,
    "clockwise": true
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/turntable/rotate \
       -H "Content-Type: application/json" \
       -d '{"angle": 90.0, "clockwise": true}'
  ```

---

## 5. Camera & Livestream Testing

### `POST /test/stream/start`
Initializes the dual camera array and starts the H.264 RTSP livestream without moving any motors.

* **Request Body** (optional):
  ```json
  {
    "bitrate": 2000000
  }
  ```

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "stream_urls": [
      "rtsp://localhost:8554/cam0",
      "rtsp://localhost:8554/cam1"
    ],
    "is_streaming": true
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/stream/start \
       -H "Content-Type: application/json" \
       -d '{"bitrate": 2000000}'
  ```

---

### `POST /test/stream/stop`
Stops the RTSP livestream encoder and releases camera hardware locks.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "is_streaming": false,
    "message": "Livestream stopped and cameras closed."
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/stream/stop
  ```

---

### `GET /test/stream/status`
Returns whether the stream is active and its RTSP URLs.

* **Response (`200 OK`)**:
  ```json
  {
    "is_streaming": true,
    "stream_urls": [
      "rtsp://localhost:8554/cam0",
      "rtsp://localhost:8554/cam1"
    ]
  }
  ```

---

### `POST /test/camera/snap`
Takes a single synchronized test capture from both cameras. If cameras are not currently initialized, it initializes them first.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "photo_paths": [
      "/home/alin/Desktop/3d-scanner-raspberry-pi/input_images/cam0_test.jpg",
      "/home/alin/Desktop/3d-scanner-raspberry-pi/input_images/cam1_test.jpg"
    ],
    "count": 2
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/camera/snap
  ```

---

## 6. Full Hardware Reset & Cleanup

### `POST /test/cleanup`
Powers down all hardware cleanly:
- Cuts torque on both stepper motors (`release_torque=True`).
- Powers off the lighting relay.
- Terminates RTSP live stream and closes camera handles.
- Resets hardware state to `IDLE`.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "message": "All hardware safely stopped, torques cut, and cameras closed.",
    "state": "idle",
    "is_streaming": false,
    "lights_on": false
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/cleanup
  ```

---

## Recommended Hardware Verification Checklist

When first bringing up or troubleshooting the physical rig:

1. **Verify Endstops**:
   - Call `GET /test/endstops` while carriage is in mid-air -> should return `top_endstop: false, bottom_endstop: false`.
   - Manually press the bottom switch with your finger -> `bottom_endstop` should flip to `true`.
   - Manually press the top switch -> `top_endstop` should flip to `true`.
2. **Verify Lighting**:
   - Call `POST /test/lights/toggle` -> chamber LEDs should turn on.
3. **Verify Turntable**:
   - Call `POST /test/turntable/rotate` with `angle: 18` -> turntable should rotate smoothly 18 degrees and release holding current.
4. **Verify Z-Axis Homing**:
   - Call `POST /test/motor/home-bottom` -> carriage should move downward until clicking the bottom switch, then stop immediately.
5. **Verify Camera Stream**:
   - Call `POST /test/stream/start` -> open `rtsp://<pi-ip>:8554/cam0` in VLC media player to verify video feed and focus.
6. **Verify Clean Shutdown**:
   - Call `POST /test/cleanup` -> lights turn off, video stream terminates, motors release.
