# Architectural & Implementation Suggestions

This document compiles recommendations and improvements discussed for the Arducam IMX477 camera pipeline, previewing, and 3D photogrammetry integration on the Raspberry Pi 5.

---

## 1. 4:3 Aspect Ratio for Live Preview (`lores` Stream)

### Context & Problem
* The IMX477 sensor natively has a **4:3 aspect ratio** (4056 × 3040 pixels).
* Standard 1080p (`1920 × 1080`) and 720p (`1280 × 720`) are **16:9**.
* When `picamera2` scales a 4:3 sensor frame into a 16:9 `lores` stream, the ISP crops pixels off the top and bottom.
* As a result, the live preview does not show the full vertical field-of-view that will actually be captured in the 12MP still photo.

### Recommendation
Configure the `lores` stream with a 4:3 resolution so the preview framing matches the captured photo with **zero cropping**:

* **`1440 × 1080`**: Full 1080p vertical detail matching 4:3 framing.
* **`1600 × 1200`**: Ultra-sharp preview matching 4:3 framing.

### Implementation Example in `camera.py`
Make `_configure_still` dynamically accept a `preview_resolution` instead of hardcoding `self.SD_RES`:

```python
def _configure_still(self, resolution=None, preview_resolution=None):
    preview_res = preview_resolution or self._preview_size  # e.g., (1440, 1080)
    cfg = self.picam2.create_still_configuration(
        main={"size": self.FULL_RES},                      # 12MP for photos
        lores={"size": preview_res, "format": "YUV420"},   # 4:3 HD for preview/streaming
        raw={},
        display="lores"
    )
    self.picam2.configure(cfg)
```

---

## 2. Client-Side Web Overlays vs. Server-Side OpenCV Overlays

### Context & Problem
* Burning text overlays (focus position, camera info, status) into video frames using OpenCV (`cv2.putText`) consumes CPU cycles on every single frame and produces blurry, pixelated text when scaled on mobile displays.

### Recommendation
* Send clean, untouched video through the H.264 stream.
* Expose a lightweight JSON endpoint (e.g., `GET /api/status`) returning `{ "focus_position": ..., "rotation": ... }`.
* Use HTML and CSS (`position: absolute; z-index: 10`) on the client webpage to overlay text and interactive controls directly over the video player.
* **Benefits:** Zero Pi CPU overhead for rendering graphics, crisp text on high-DPI/mobile screens, and interactive controls (clickable buttons over the video).

---

## 3. Dual-Stream Strategy for Photogrammetry Web Streaming

### Context & Problem
* The Raspberry Pi 5 lacks a dedicated hardware H.264 encoder, relying on software encoding. Software-encoding a 12MP stream at video framerates will saturate all CPU cores and drop frames.
* Conversely, lowering the main camera resolution to 1080p sacrifices the 12MP detail required by photogrammetry software (Meshroom).

### Recommendation
Leverage `picamera2`'s dual-stream architecture:
1. Keep the **`main` stream** at full sensor resolution (`4056 × 3040`) for stills.
2. Route the **`lores` stream** (`1440 × 1080` or `1280 × 720`, YUV420) to an H.264 encoder pushing to **MediaMTX** via RTSP.
3. Serve WebRTC to browsers for sub-second preview latency.
4. When capturing photos via `/api/photo`, grab directly from the `main` stream without interrupting or pausing the `lores` video stream.
