# Cloud Module Documentation

The `cloud` module is responsible for orchestrating remote processing and storage for the 3D scanning pipeline. It contains the integration logic for **Cloudflare R2 (S3-compatible object storage)** and **RunPod (dynamic GPU provisioning)**.

---

## 🏗️ Cloudflare R2 (`cloudflare_r2.py`)

This file manages all upload, download, and cleanup operations for images and generated 3D assets.

### 1. Robust File Paths
To ensure the module can be executed safely from anywhere (like a FastAPI router at the project root), all paths are defined dynamically using absolute positioning:
```python
BASE_DIR = str(Path(__file__).resolve().parent.parent)
INPUT_IMAGES = os.path.join(BASE_DIR, "input_images")
```
This guarantees it will reliably find `input_images` and `output` folders regardless of the current working directory.

### 2. Strict Socket Timeouts
Because the background upload queue (`scanner.py`) relies on network stability, `boto3` is explicitly configured to prevent Python threads from hanging infinitely in kernel I/O wait during Wi-Fi drops:
- `connect_timeout=5`: Fails fast if the TCP socket cannot open.
- `read_timeout=15`: Fails if an ongoing data transfer stalls.
- `retries={'max_attempts': 1}`: Internal blind retries are disabled so our custom Resilient Upload Worker can actively manage the backoff and cancellation logic.

### 3. Pipeline Validation (`file_exists_and_is_new`)
To determine if Meshroom succeeded or failed on RunPod, the system uses `file_exists_and_is_new()`. This relies on `boto3.head_object()` to check the `LastModified` UNIX timestamp against the time the job was started. This guarantees we don't accidentally download a stale `output.zip` from a previous scan.

---

## 🚀 RunPod Orchestration (`runpod.py`)

This file dynamically provisions secure GPU containers on-demand to process photogrammetry.

### 1. Dynamic GPU Fallbacks
The orchestrator checks the required RAM (via `ram_heuristic.py`). If a specific GPU type fails to deploy (due to datacenter unavailability), it gracefully loops through a fallback list of suitable NVIDIA GPUs (e.g., RTX 5090 -> L40 -> RTX 6000 Ada -> RTX 3090).

### 2. Cost-Effective Termination (`terminate_pod`)
Because RunPod bills for **both compute and disk volume storage**, simply stopping a pod (`podStop`) halts compute billing but continues bleeding credits for the allocated container disk. 

The orchestrator explicitly uses the GraphQL `podTerminate` mutation. As soon as the Meshroom job finishes and the results are downloaded, the pod is entirely destroyed to eliminate all lingering costs.

### 3. Proper Python Import Routing
Since `runpod.py` is invoked from the parent FastAPI application, internal module dependencies strictly use absolute imports:
```python
import cloud.cloudflare_r2 as r2
from cloud.ram_heuristic import calculate_required_ram
```

### 4. Cancellation & Max Timeout Protection
The orchestrator natively supports a `cancel_event` threading event in its `launch_job` polling loop. If a user triggers an emergency stop via the FastAPI endpoint, the RunPod orchestrator instantly wakes up (bypassing the 15-second polling sleep) and terminates the active pod.
Additionally, an automated 60-minute hard loop timeout acts as a failsafe to protect against infinite billing in the event Meshroom freezes silently without exiting.

### 5. Upgraded RAM Heuristic (OOM Detection)
RunPod does not cleanly broadcast Out-Of-Memory (OOM) kills through its GraphQL API; the pod simply transitions to `EXITED`. 
The system relies on a two-part heuristic:
1. **Uptime Check**: If the pod exits in under 120 seconds, it almost certainly hit an OOM during the initial mesh depthmap calculation. The script catches this and automatically provisions a larger GPU.
2. **Payload Validation**: Even if the pod runs for >120 seconds, the orchestrator queries R2 (`r2.file_exists_and_is_new`) to verify `output.zip` is present and was generated *after* the job started. If the payload is missing, it assumes a late-stage OOM kill and restarts on a bigger GPU.
