# Implementation Plan: Resilient Upload Queue (Retry + Watchdog)

## 🎯 Goals
1. **Survive temporary network drops (e.g. 5–15 seconds):** Automatically retry the failed photo with backoff instead of dropping it.
2. **Prevent premature aborts on large/slow scans:** Remove the fixed 120s queue cap; as long as photos are uploading, keep going.
3. **Fail fast on permanent disconnections:** If no photo succeeds for 45 continuous seconds, abort and report an error.
4. **Instant cancellation:** If the user triggers `emergency_stop()` or cancels, immediately break out of retry delays without waiting out sleep timers.

---

## 📋 Step-by-Step Implementation

### Step 1: Enforce Low-Level Socket Timeouts in `cloud/cloudflare_r2.py`
*(Prerequisite: Without this, boto3 can freeze at the OS kernel level on a broken TCP socket for 15+ minutes).*

* **Change:** Configure `botocore.config.Config` on the S3 client:
  ```python
  from botocore.config import Config

  R2_CONFIG = Config(
      connect_timeout=5,          # Fail fast (5s) if unable to open socket
      read_timeout=15,            # Fail if transfer stalls for 15s
      retries={'max_attempts': 1} # Disable internal blind retries so our worker controls the backoff
  )
  s3 = boto3.client("s3", config=R2_CONFIG, ...)
  ```

---

### Step 2: Add Heartbeat & Failure Tracking to `Scanner.__init__` in `hardware/scanner.py`
Track both upload activity and overall upload health.

* **Add instance variables:**
  * `self._last_upload_time = time.time()`: Heartbeat timestamp updated on every successful upload.
  * `self._upload_failed = False`: Flag set to `True` if any image permanently exhausts all retries.

---

### Step 3: Implement Retry with Backoff in `_upload_worker()`
Replace the naive single-try block with an active retry loop.

* **Logic Flow per Photo:**
  1. Pull `file_path` from queue. If `None`, exit.
  2. Set `max_retries = 3` with delays `[3, 7, 15]` seconds.
  3. For each attempt:
     * Check: `if self._cancel_event.is_set(): break` (abort immediately if user cancelled).
     * Try `r2.upload_file(...)`.
     * **On Success:**
       * Update `self._last_upload_time = time.time()`
       * Log success and `break` (exit retry loop).
     * **On Exception (Network failure):**
       * If retries remain: Log warning, then pause using **`self._cancel_event.wait(timeout=delay)`** (wakes up instantly if cancelled instead of freezing inside `time.sleep()`).
       * If all retries exhausted: Log error and set `self._upload_failed = True`.
  4. Call `self.upload_queue.task_done()`.

---

### Step 4: Implement Stalled Progress Watchdog in `stop_upload_worker()`
Replace the global static 120s timer with an activity watchdog.

* **Signature:**
  ```python
  def stop_upload_worker(self, wait_for_completion: bool = True, stall_timeout: float = 45.0)
  ```
* **Logic Flow:**
  1. Put `None` sentinel in queue.
  2. If `wait_for_completion`:
     * Loop while `self.upload_queue.unfinished_tasks > 0`:
       * **Check 1 (Cancellation):** If `self._cancel_event.is_set()`, break immediately.
       * **Check 2 (Stalled Connection):** If `time.time() - self._last_upload_time > stall_timeout`:
         ```python
         log.error("[UploadWorker] Uploads stalled for >%ds (network offline). Aborting.", stall_timeout)
         self.state = ScannerState.ERROR
         break
         ```
       * Sleep `1.0` second.
     * Join the thread with a short grace timeout (e.g. 5.0s).

---

## 🛡️ Edge Cases Handled

| Scenario | Behavior |
| :--- | :--- |
| **10s Wi-Fi flicker during scan** | Attempt 1 fails $\rightarrow$ waits 3s $\rightarrow$ Attempt 2 succeeds. **Zero photos lost.** |
| **500-photo scan on slow 1 Mbps link** | Upload takes 8 minutes, but a photo finishes every 2–3s. Watchdog continually resets. **Scan completes normally.** |
| **Wi-Fi router completely dies** | Retries fail $\rightarrow$ no photos complete for 45s $\rightarrow$ Watchdog trips $\rightarrow$ Scanner enters `ScannerState.ERROR` cleanly. |
| **User hits Emergency Stop mid-upload** | `_cancel_event.set()` instantly wakes up `_cancel_event.wait()`. Worker and cleanup abort within milliseconds. |
