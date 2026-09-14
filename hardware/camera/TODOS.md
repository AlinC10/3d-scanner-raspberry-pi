## Optics & Camera Configuration

<a id="sw-ae-awb"></a>

- [x] **Lock AE, AWB, and Focus before scanning**
- **Details:** Meshroom fails if lighting or focus changes between photos. Turn on your LEDs, wait 2-3 seconds for auto-exposure to adjust, then lock the settings via `picamera2` before starting the motor loop.

```python
# After letting the camera run for 2 seconds to calculate the room light:
cam.set_controls({
 "AeEnable": False,       # Turn off Auto Exposure
 "AwbEnable": False,      # Turn off Auto White Balance
 "ExposureValue": 0.0     # Lock the exposure to the current calculated value
})
```

<a id="sw-zoom"></a>

- [x] **Enforce strict "No Digital Zoom" policy**
- **Details:** The Arducam B0272 has a fixed focal length. Never apply digital zoom or Region of Interest (ROI) cropping via `picamera2`. Digital cropping destroys the Principal Point of the lens and throws away 12MP resolution, causing Meshroom to miscalculate the optical distortion model.

<a id="sw-jpeg"></a>

- [x] **Set output format to High-Quality JPEG**
- **Details:** Use maximum-quality JPEGs (Quality 95-100) instead of PNG or RAW formats. High-quality JPEG compression is invisible to Meshroom's algorithms, but shrinks an 80-image scan from ~2GB down to ~400MB.

<a id="sw-downscale"></a>

- [x] **Optional: Downscale resolution to 6MP for faster processing**
- **Details:** If you need Meshroom to process the final 3D model 2-3x faster, lower the capture resolution from 12MP to roughly 6MP (approx. 2800 × 2100). You preserve the necessary overlapping geometry while feeding the GPU 50% less pixel data.

## Web Streaming & API (H.264 + WebRTC)

To stream video to mobile devices with ultra-low latency and low CPU usage, we use **MediaMTX** for WebRTC streaming alongside a **FastAPI** backend for camera control. This architecture allows us to offload HTML/CSS overlays to the client browser instead of burning text into the video using OpenCV.

### Prerequisites

1. **Install FastAPI and Uvicorn:**
   ```bash
   pip install fastapi uvicorn
   ```

2. **Download and run MediaMTX** (in a separate terminal window):
   ```bash
   wget https://github.com/bluenviron/mediamtx/releases/download/v1.6.0/mediamtx_v1.6.0_linux_arm64v8.tar.gz
   tar -xvzf mediamtx_v1.6.0_linux_arm64v8.tar.gz
   ./mediamtx
   ```

### FastAPI Server Integration

Create a `web_api.py` file in the same directory as `camera.py`. This script configures the camera to push an H.264 stream to MediaMTX and serves a web dashboard to view the stream and control the camera.

```python
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse
import uvicorn

from camera import ArducamIMX477
from picamera2.encoders import H264Encoder
from picamera2.outputs import FfmpegOutput

app = FastAPI()
cam = ArducamIMX477(camera_id=0)

@app.on_event("startup")
def startup():
    """Start camera, lock lighting, and keep it running"""
    cam.prepare_scan(keep_running=True)

    # Setup H.264 Encoder (2 Mbps for 720p)
    encoder = H264Encoder(bitrate=2000000)

    # Push the video to MediaMTX via RTSP using FFmpeg
    output = FfmpegOutput(
        "rtsp://localhost:8554/cam",
        options={"-f": "rtsp", "-rtsp_transport": "tcp"}
    )
    cam.picam2.start_recording(encoder, output, stream="lores")
    print("H.264 Stream pushing to MediaMTX...")

@app.on_event("shutdown")
def shutdown():
    """Clean up camera resources."""
    cam.picam2.stop_recording()
    cam.close()

# --- API ENDPOINTS ---

@app.get("/api/status")
def get_status():
    """Returns the current camera status as JSON."""
    return {
        "focus_position": cam.focuser.position,
        "rotation": cam.rotation
    }

@app.post("/api/focus")
def step_focus(delta: int):
    """Moves the focus motor by delta."""
    cam.focus_step(delta)
    return {"status": "success", "new_position": cam.focuser.position}

@app.post("/api/photo")
def take_photo(background_tasks: BackgroundTasks):
    """Takes a high-res photo without freezing the web API."""
    background_tasks.add_task(cam.capture_photo, output="test_photo.jpg")
    return {"status": "capturing"}


# --- WEB FRONTEND (HTML/CSS/JS) ---

@app.get("/")
def get_homepage():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { background: #111; color: white; font-family: sans-serif; margin: 0; padding: 20px; }
            .video-container { position: relative; width: 100%; max-width: 1280px; margin: auto; }
            iframe { width: 100%; aspect-ratio: 16/9; border: none; background: #000; }
            .overlay {
                position: absolute; top: 20px; left: 20px;
                background: rgba(0, 0, 0, 0.6); padding: 15px; border-radius: 8px;
                pointer-events: none; text-shadow: 1px 1px 2px black;
            }
            .controls { text-align: center; margin-top: 20px; }
            button { padding: 15px 30px; font-size: 18px; margin: 5px; border-radius: 8px; cursor: pointer; }
        </style>
    </head>
    <body>
        <div class="video-container">
            <!-- MediaMTX hosts an ultra-low latency WebRTC player on port 8889 -->
            <!-- IMPORTANT: Change 'raspberrypi.local' to your Pi's actual IP -->
            <iframe src="http://raspberrypi.local:8889/cam"></iframe>
            <div class="overlay">
                <h3 style="margin:0; color: #0f0;">● LIVE (H.264)</h3>
                <h2 style="margin:10px 0 0 0;">Focus: <span id="focus-text">Loading...</span></h2>
            </div>
        </div>
        <div class="controls">
            <button onclick="changeFocus(-50)">- Focus Far</button>
            <button onclick="changeFocus(50)">+ Focus Near</button>
            <button onclick="capturePhoto()" style="background: #007bff; color: white; border: none;">📷 Capture High-Res Photo</button>
        </div>
        <script>
            // Poll for focus updates
            setInterval(async () => {
                try {
                    const res = await fetch('/api/status');
                    const data = await res.json();
                    document.getElementById('focus-text').innerText = data.focus_position;
                } catch (e) { console.error("API Offline"); }
            }, 500);

            async function changeFocus(delta) {
                await fetch(`/api/focus?delta=${delta}`, { method: 'POST' });
            }
            
            async function capturePhoto() {
                await fetch('/api/photo', { method: 'POST' });
                alert("Capture command sent!");
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

if __name__ == '__main__':
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

### Usage

1. Start **MediaMTX** in one terminal.
2. Start the API in a second terminal: `python3 web_api.py`.
3. Open a browser on the same network and navigate to `http://<pi-ip-address>:8000`.

**Architecture Benefits:**
The Pi streams compressed H.264 video efficiently via MediaMTX. The client browser renders the overlay boxes and text natively using HTML and fetches the focus state via a lightweight JSON API. This entirely bypasses the need for CPU-heavy OpenCV graphics rendering on the Pi.

## Not important issues

### RAW (DNG) captures do not get rotated

In capture_photo(), if raw=True, the DNG is saved directly from the capture_request() before \_apply_software_rotation() is called. As a result, the accompanying JPEG will be rotated, but the DNG will remain in its original hardware orientation. While this is somewhat standard for RAW files, it's worth noting as it might cause confusion during post-processing.
