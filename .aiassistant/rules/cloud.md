---
apply: by file patterns
instructions: # SCOPE: Cloudflare R2, Runpod. Apply these rules whenever the prompt mentions Cloud, Cloudflare R2, Runpod, Meshroom
patterns: cloud/**
---

# Cloud Orchestration & Pipeline Agent

## 1. Project Overview
This project is an automated, edge-to-cloud IoT 3D scanner. The physical edge device (Raspberry Pi 5) coordinates hardware components (stepper motors, high-res cameras, LED lighting) to capture a sequential dataset of images for either objects or human faces. 

To overcome local processing limitations, the system utilizes a hybrid cloud architecture. Images are pushed to the cloud in real-time as the scan progresses. Once the capture phase is complete, a serverless GPU pipeline runs a photogrammetry engine (Meshroom) to reconstruct the 3D model, which is then downloaded back to the edge device for local viewing or export.

---

## 2. Cloud Architecture & Responsibilities

The cloud integration module is responsible for bridging the local Raspberry Pi hardware with scalable cloud infrastructure. The workflow is divided between **Cloudflare R2** (Object Storage) and **RunPod Serverless** (GPU Compute).

### Data Flow Pipeline:
1. **Async Image Upload (Edge -> R2):** As the scanner captures images, they are added to a processing queue and uploaded sequentially to a Cloudflare R2 bucket. This runs concurrently with motor movements and camera triggers to minimize total scan time.
2. **Job Submission (Edge -> RunPod):** Once all images are successfully uploaded to R2, the Raspberry Pi triggers the RunPod Serverless API endpoint to initiate the photogrammetry pipeline.
3. **Status Polling (Edge <-> RunPod):** The system polls the RunPod API using the received `Job ID` to check the execution status (In Progress, Completed, Failed).
4. **Processing (RunPod):** The serverless worker downloads the dataset from R2, runs AliceVision (Meshroom), processes the high/low poly branches, and uploads an `output.zip` back to R2.
5. **Asset Retrieval (R2 -> Edge):** Upon RunPod job completion, the Raspberry Pi downloads the final 3D assets from R2.

---

## 3. Cloudflare R2 Integration

Cloudflare R2 acts as the centralized data lake for both raw inputs and processed outputs. 

### Environment Configuration
The cloud classes must authenticate using boto3 (or equivalent S3-compatible REST clients). The following variables must be defined in the `.env` file:

```env
R2_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=your_access_key
R2_SECRET_ACCESS_KEY=your_secret_key
BUCKET_NAME=your_bucket_name
```

### Local Post-Download Hook
When the RunPod job is marked as successful, the system will download the resulting `output.zip` from R2. The cloud module must handle this post-processing locally on the Raspberry Pi:
* **Target Directory:** Create a local directory named exactly after the scanned object's identifier (e.g., `./scans/<object_name>/`).
* **Extraction:** Extract the contents of `output.zip` into this directory.
* **Cleanup:** Delete the downloaded `output.zip` archive immediately after successful extraction to save local SD card space.

---

## 4. RunPod Serverless Pipeline

The RunPod environment operates a containerized headless Python script (`main.py`) that handles the heavy lifting via Meshroom. 

### RunPod Endpoint Interaction
* **Trigger Endpoint:** The edge device sends a `POST` request to the RunPod serverless endpoint containing the specific dataset prefix/ID in R2.
* **Retrieval & Status:** RunPod endpoints are asynchronous. The edge device must implement a retry/polling mechanism (e.g., every 10-15 seconds) targeting the `/status/<job_id>` endpoint. 

### Serverless Pipeline Execution Details (RunPod Side)
*The following context is for understanding what happens during the "Processing" phase on the serverless node:*
1. **Download:** Fetches the raw image dataset from Cloudflare R2.
2. **Meshroom Batch:** Prepares a `.mg` JSON template and launches `meshroom_batch` via CLI. The `TMPDIR`/`TEMP` directory is routed inside the output folder to keep the cache portable.
3. **Branching:** Detects output cache by texture extension to separate branches:
   * `PNG` = High-poly branch.
   * `JPG` = Low-poly branch.
4. **Post-Processing (High):** Uses `trimesh` to load the `.obj` and export a watertight `.stl` intended for 3D printing, a `.glb` file intended for Web/AR use. Retains original `.obj` and `.png` textures.
5. **Post-Processing (Low):** Uses `trimesh` to pack the low-poly `.obj`, `.mtl`, and `.jpg` textures into a single binary `.glb` file intended for Web/AR use and a watertight `.stl` intended for 3D printing. Retains original `.obj` and `.jpg` textures.
6. **Upload & Cleanup:** Packages the assets (`Texturing_1/`, `printable_model.stl`, `web_model.glb`) into a single `output.zip`, uploads it back to R2, and purges the worker's temporary cache.

---

## 5. Implementation Guidelines (CLI Phase)

* **Future Phase:** Don't work in this directory.
* **Separation of Concerns:** All cloud and storage interactions (S3/R2 requests, RunPod API HTTP requests) must be encapsulated in their own Python classes (e.g., `CloudflareR2`, `RunpodOrchestrator`).
* **Concurrency:** The image upload queue must utilize threading or async tasks (`asyncio`) so that network latency does not block the main hardware control loop (stepper motors and camera triggers).
