# Hardware Diagnostic & Testing Endpoints (`/test`)

This module provides isolated HTTP endpoints to test, calibrate, and verify individual hardware subsystems on the 3D scanner without running a full scan sequence.

Interactive OpenAPI docs are accessible at: `http://<pi-ip>:8000/docs#/Hardware%20Diagnostics%20%26%20Tests`

> [!NOTE]
> `/test/turntable/rotate` and `/test/motor/z-move` answer `422` when the angle / distance is not a whole number of steps (`0.1125°` multiples on the turntable, `0.01 mm` multiples on Z) and `500` when the move was aborted before it completed (for example by `POST /scanner/cancel`); they no longer report `success` for an aborted move.

> [!NOTE]
> **Limits** (all rejected with `422`): `delay` and `start_delay` must be within `0.0001`-`0.05` s (also for the `home-bottom` / `move-to-top` query parameters, where `ramp_percent` must be `0.0`-`1.0`); `z-move` `distance_mm` is at most `150` (the Z travel). Below `delay = 0.0004` s the Arduino may not keep the step period during ramps: for manual tests prefer `acceleration=false` there.

> [!WARNING]
> **Overlapping motions**: `/test/motor/z-move`, `/test/turntable/rotate`, `/test/motor/home-bottom` and `/test/motor/move-to-top` run inside the scanner motion guard. A second motion request that arrives while another motion is running is answered `409 Conflict` ("Another motion is in progress...") and its motor is not touched; wait for the running move to finish or stop it with `POST /scanner/cancel`.

> [!WARNING]
> **Busy guard**: the endpoints that move or (de)energize the motors (`/test/motor/*`, `/test/turntable/rotate`, `/test/cleanup`) answer `409 Conflict` unless the scanner is `idle`, `completed`, `cancelled` or `error`. During `preparing`, `prepared`, `running`, `processing` and `downloading` use `POST /scanner/cancel` first. Read-only endpoints (endstops, ToF, status) are always available.

---

## Quick Reference Table

| Category | Method | Endpoint | Description |
|---|---|---|---|
| **Limit Switches** | `GET` | `/test/endstops` | Read top and bottom endstop states |
| **Sensors** | `GET` | `/test/tof` | Read the VL53L1X time-of-flight distance (mm) |
| **Lighting** | `POST` | `/test/lights/enable` | Turn illumination LEDs ON (optional JSON body `{"brightness": 0.0-1.0}`) |
| **Lighting** | `POST` | `/test/lights/disable` | Turn illumination LEDs OFF |
| **Motor State** | `GET` | `/test/motor/status` | Torque state (`enabled` / `disabled`) of both steppers |
| **Motor State** | `POST` | `/test/motor/enable` | Energize the coils (`?target=turntable\|z_axis\|all`) |
| **Motor State** | `POST` | `/test/motor/disable` | Release the coils (`?target=turntable\|z_axis\|all`) |
| **Homing** | `POST` | `/test/motor/home-bottom` | Move carriage down until the bottom limit switch triggers (0.5s settling delay) |
| **Homing** | `POST` | `/test/motor/move-to-top` | Move carriage up until the top limit switch triggers (0.5s settling delay) |
| **Motor Movement** | `POST` | `/test/motor/z-move` | Move Z-axis by distance in millimeters with 0.5s coil de-energization settling delay |
| **Motor Movement** | `POST` | `/test/turntable/rotate` | Rotate turntable by degrees with 0.5s coil de-energization settling delay |
| **Camera & Video** | `POST` | `/test/stream/start` | Start the MJPEG live preview (no cloud needed) and return its URLs |
| **Camera & Video** | `POST` | `/test/stream/stop` | Stop the live preview and release camera devices |
| **Camera & Video** | `GET` | `/test/stream/status` | Read streaming status and active stream URLs |
| **Camera & Video** | `GET` | `/test/stream/mjpeg` | Direct multipart MJPEG stream for browser `<img>` tags (`?camera=1\|2`) with on-demand encoding |
| **Camera & Video** | `POST` | `/test/stream/active` | Instantaneous zero-latency active preview camera switch (`?camera_id=1\|2`) |
| **Camera & Video** | `POST` | `/test/stream/quality` | Dynamically updates live stream JPEG compression quality (`1..100`, default 80) |
| **Camera & Video** | `GET` | `/test/stream/sync-status` | Returns real-time libcamera `rpi.sync` status, frame rates, and offset telemetry |
| **Camera & Video** | `POST` | `/test/camera/snap` | Capture a synchronized test photo pair from both cameras |
| **Safety & Reset** | `POST` | `/test/cleanup` | Emergency cut on motor torque, lights, and cameras |

---

## 1. Limit Switch Diagnostics

### `GET /test/endstops`
Reads the live electrical status of both physical limit switches.

* **Response (`200 OK`)**:
  ```json
  {
    "bottom_endstop": true,
    "top_endstop": false
  }
  ```
  * `top_endstop`: `true` if carriage is contacting the top limit switch.
  * `bottom_endstop`: `true` if carriage is contacting the bottom limit switch.

* **Example `curl`**:
  ```bash
  curl -X GET http://localhost:8000/test/endstops
  ```

---

### `GET /test/tof`
Reads the VL53L1X time-of-flight sensor (distance to the subject).

* **Response (`200 OK`)**: `{"status": "success", "distance_mm": 412}`
* **Error (`500`)**: `ToF Sensor Error: ...` when the sensor cannot be read.

---

## 2. Illumination System

### `POST /test/lights/enable`
Powers the LED panels ON at the requested brightness (defaults to full brightness when no body is sent).

* **Request Body** (optional): `{"brightness": 0.5}` (`brightness` float, `0.0` to `1.0`).
* **Response (`200 OK`)**:
  ```json
  {"status": "success", "state": "enabled", "brightness": 0.5, "message": "Lights enabled."}
  ```
* **Error (`409 Conflict`)**: Lights are locked (scan in progress).

### `POST /test/lights/disable`
Turns the LED panels OFF (relay cut, PWM to 0).

* **Response (`200 OK`)**: `{"status": "success", "state": "disabled", "message": "Lights disabled."}`
* **Error (`409 Conflict`)**: Lights are locked (scan in progress).

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/lights/enable \
       -H "Content-Type: application/json" \
       -d '{"brightness": 0.25}'
  ```

---

## 3. Motor State, Homing & Limit Seeking

### `GET /test/motor/status`
Returns the electrical holding state of both steppers.

* **Response (`200 OK`)**: `{"turntable": "enabled", "z_axis": "disabled"}`
  (`enabled` = coils energized and holding torque; `disabled` = free-wheeling). The state is invalidated after an
  Arduino reconnect, because the board resets with every driver disabled.

### `POST /test/motor/enable` / `POST /test/motor/disable`
Energizes / releases the coils manually, to verify wiring and check if a motor locks up.

* **Query Parameters**: `target` = `turntable` | `z_axis` | `all` (default `all`).
* **Response (`200 OK`)**: `{"status": "success", "target": "all", "state": "enabled"}`
* **Error (`409 Conflict`)**: A prepare/scan owns the rig (see the busy guard above).

---

### `POST /test/motor/home-bottom`
Moves the carriage downward (a single move limited to the Z travel plus a 15 mm margin, 165 mm, with an acceleration ramp)
until the bottom limit switch is active, then releases the torque after the 0.5s settling pause.

* **Query Parameters** (all optional):
  * `delay` (float, default `0.0015`, range `0.0001`-`0.05`): Cruise step delay in seconds.
  * `start_delay` (float, default `0.004`, range `0.0001`-`0.05`): Starting delay of the ramp.
  * `ramp_percent` (float, default `0.1`, range `0.0`-`1.0`): Fraction of the move used for acceleration.
  * `acceleration` (bool, default `true`): `false` runs the whole move at the constant `delay` (no ramp, like `z-move`); `start_delay` and `ramp_percent` are then ignored.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "message": "Carriage successfully homed to bottom endstop.",
    "bottom_endstop": true
  }
  ```
* **Error (`500`)**: `Homing failed: ...` when the switch was not reached (aborted move, serial error, travel limit).
* **Error (`422`)**: A query parameter is outside the ranges above.
* **Error (`409 Conflict`)**: Scanner busy (`preparing` / `prepared` / `running`), or another motion is in progress.

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/motor/home-bottom
  ```

---

### `POST /test/motor/move-to-top`
Moves the carriage upward until the top limit switch is active. Same query parameters, errors and settling behaviour as
`home-bottom`.

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
  curl -X POST "http://localhost:8000/test/motor/move-to-top?delay=0.0015"
  ```

---

## 4. Manual Incremental Movement

### `POST /test/motor/z-move`
Moves the Z-axis carriage by a specified distance in millimeters. Automatically halted if an endstop is reached.

> [!NOTE]
> **Coil Settling Protection**: Upon completion (or abort), a mandatory **0.5-second settling pause** (`time.sleep(0.5)`) is enforced in the `finally:` block before calling `disable()`. The coils keep holding the lead screw / rotor while it settles; cutting the current immediately would let it drift past the target. (A protective effect against supply dips that reset the Arduino is an unverified hypothesis.)

* **Request Body**:
  ```json
  {
    "distance_mm": 15.0,
    "direction": "up",
    "delay": 0.0010,
    "acceleration": false
  }
  ```
  * `distance_mm` (float, default `100.0`, `> 0` and `<= 150.0`, whole steps: multiples of `0.01` mm): Travel distance. The Z travel is 150 mm.
  * `direction` (`"up"` | `"down"`, default `"up"`).
  * `delay` (float, default `0.0010`, range `0.0001`–`0.05`): Step pulse delay.
  * `acceleration` (bool, default `false`): Speed ramp (constant acceleration).
  * `start_delay` (float, optional, default `null`, range `0.0001`–`0.05`): Starting delay of the ramp (only used with `acceleration`).
  * `ramp_percent` (float, default `0.2`, range `0.0`–`1.0`), `decel_percent` (float, optional, default `null` = `ramp_percent`).
  * `end_delay` (float, optional, default `null`, range `0.005`–`0.030`): Step delay in seconds at the end of the deceleration for a slow stop. `null` disables it; ignored when `acceleration` is `false`.

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

* **Error Response (`409 Conflict`)**: A prepare/scan owns the rig, or another motion is already in progress.
* **Error Response (`422`)**: `distance_mm` above `150` or not a whole number of steps; `delay` / `start_delay` out of range.

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/motor/z-move \
       -H "Content-Type: application/json" \
       -d '{"distance_mm": 20.0, "direction": "up"}'
  ```

---

### `POST /test/turntable/rotate`
Rotates the turntable platter by a specific angle.

> [!NOTE]
> **Coil Settling Protection**: Like the Z-axis, rotation enforces a **0.5-second settling pause** (`time.sleep(0.5)`) in the `finally:` block before calling `disable()`. While the coils keep holding, the platter stops before the torque is released, which avoids inertia-induced angular overshoot.

* **Request Body**:
  ```json
  {
    "angle": 18.0,
    "clockwise": true,
    "delay": 0.0010,
    "acceleration": true,
    "ramp_percent": 0.2,
    "end_delay": 0.015
  }
  ```
  * `angle` (float, default `18.0`, `> 0` and `<= 360.0`, whole steps: multiples of `0.1125°`). Unlike the scan configuration (`turntable.angle` of `POST /scanner/start`: at most `180` and must divide `360°` in whole steps), the manual test accepts a full turn.
  * `clockwise` (bool, default `true`).
  * `delay` (float, default `0.0010`, range `0.0001`–`0.05`).
  * `acceleration` (bool, default `true`).
  * `start_delay` (float, optional, default `null`, range `0.0001`–`0.05`).
  * `ramp_percent` (float, default `0.2`, range `0.0`–`1.0`).
  * `end_delay` (float, default `0.015`, range `0.005`–`0.030`, `null` = off): Step delay in seconds at the very end of the deceleration. The turntable decelerates down to this slow step rate, so the platter (on a bearing) does not keep rotating after the last step, even with a heavy object. Values outside the range are rejected with `422`. Ignored when `acceleration` is `false`.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "angle": 18.0,
    "clockwise": true
  }
  ```
* **Error Response (`409 Conflict`)**: A prepare/scan owns the rig, or another motion is already in progress.
* **Error Response (`422`)**: `angle` above `360` or not a whole number of steps; `delay` / `start_delay` out of range.

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/turntable/rotate \
       -H "Content-Type: application/json" \
       -d '{"angle": 90.0, "clockwise": true}'
  ```

---

## 5. Camera & Livestream Testing

### `POST /test/stream/start`
Initializes the dual camera array and the lights and starts the MJPEG live preview without moving any motors. It needs no request body (there is no bitrate) and, unlike `POST /scanner/prepare`, it does **not** touch Cloudflare R2, the ToF sensor or the disk-space check, so it works without internet.

* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "stream_urls": [
      "/test/stream/mjpeg?camera=1",
      "/test/stream/mjpeg?camera=2"
    ],
    "is_streaming": true
  }
  ```

* **Example `curl`**:
  ```bash
  curl -X POST http://localhost:8000/test/stream/start
  ```

---

### `POST /test/stream/stop`
Stops the live preview and releases the cameras.

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
Returns whether the stream is active and its preview URLs.

* **Response (`200 OK`)**:
  ```json
  {
    "is_streaming": true,
    "stream_urls": [
      "/test/stream/mjpeg?camera=1",
      "/test/stream/mjpeg?camera=2"
    ]
  }
  ```

---

### `GET /test/stream/mjpeg`
Direct multipart/x-mixed-replace MJPEG video stream directly viewable in browser `<img>` elements.

* **Query Parameters**:
  * `camera` (optional `int`, range `1..2`): Selects which camera feed to activate. Defaults to active preview camera.
* **On-Demand Encoding**: Tracks connected spectators with zero CPU encoding overhead when no clients are viewing.
* **Response (`200 OK`)**:
  `Content-Type: multipart/x-mixed-replace; boundary=FRAME`
* **Example `curl`**:
  ```bash
  curl -N http://localhost:8000/test/stream/mjpeg?camera=1 > /dev/null
  ```

---

### `POST /test/stream/active`
Switches the active preview camera (1 or 2) with zero latency.

* **Query Parameters or Body**:
  * `camera_id` (required `int`, `1` or `2`): Target camera ID.
* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "active_camera": 2,
    "message": "Switched preview to camera 2."
  }
  ```
* **Example `curl`**:
  ```bash
  curl -X POST "http://localhost:8000/test/stream/active?camera_id=2"
  ```

---

### `POST /test/stream/quality`
Dynamically adjusts the live preview JPEG compression quality without affecting still capture resolution or quality.

* **Query Parameters or Body**:
  * `quality` (required `int`, `1..100`): Live stream compression quality (default 80).
* **Response (`200 OK`)**:
  ```json
  {
    "status": "success",
    "quality": 80,
    "message": "Updated stream quality to 80."
  }
  ```
* **Example `curl`**:
  ```bash
  curl -X POST "http://localhost:8000/test/stream/quality?quality=80"
  ```

---

### `GET /test/stream/sync-status`
Returns real-time synchronization diagnostics from the active camera driver (`DualCameraRPISync`, `DualCameraXVS`, or `DualCamera`).

* **Response (`200 OK`)**:
  ```json
  {
    "ready": true,
    "status": "SYNC_LOCKED",
    "camera_1": {
      "fps": 24.2,
      "frames_grabbed": 840,
      "frames_dropped": 0
    },
    "camera_2": {
      "fps": 24.2,
      "frames_grabbed": 840,
      "frames_dropped": 0
    },
    "offset_modulo_ms": 0.028,
    "active_camera": 1
  }
  ```
* **Example `curl`**:
  ```bash
  curl -X GET http://localhost:8000/test/stream/sync-status
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
- Terminates the live preview and closes camera handles.
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

* **Error (`409 Conflict`)**: The scanner is `preparing`, `prepared` or `running`. The `detail` says
  `Scanner is busy (state: ...). Cancel it with POST /scanner/cancel first.` Use `POST /scanner/cancel` as the quick stop
  while a prepare/scan is active; `/test/cleanup` is for idle / finished states only.

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
   - Call `POST /test/lights/enable` -> chamber LEDs should turn on.
3. **Verify Turntable**:
   - Call `POST /test/turntable/rotate` with `angle: 18` -> turntable should rotate smoothly 18 degrees and release holding current.
4. **Verify Z-Axis Homing**:
   - Call `POST /test/motor/home-bottom` -> carriage should move downward until clicking the bottom switch, then stop immediately.
5. **Verify Camera Stream**:
   - Call `POST /test/stream/start` -> open `http://<pi-ip>:8000/test/stream/mjpeg?camera=1` in a browser to verify the video feed and focus; `GET /test/stream/sync-status` shows the `cam1_controls` / `cam2_controls` (exposure time and gain each sensor is really using: a black picture shows a tiny exposure there).
6. **Verify Clean Shutdown**:
   - Call `POST /test/cleanup` -> lights turn off, video stream terminates, motors release.
