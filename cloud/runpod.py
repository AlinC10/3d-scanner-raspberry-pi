import os
import json
import time
import requests
from cloud.ram_heuristic import calculate_required_ram
import csv
import cloud.cloudflare_r2 as r2

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY")
GRAPHQL_URL = "https://api.runpod.io/graphql"

# Update this to your actual Docker image name on Docker Hub
IMAGE_NAME = "yourdockerhubuser/meshroom-runner:latest"

# ── Limits of launch_job (each one can be overridden by an environment variable) ─────────────────────────────────
# A pod that was created and did not deliver output.zip costs money: at most this many are tried with the RAM the job
# needs. Then ONE last pod is tried on the host with the most RAM seen so far (see launch_job).
MAX_POD_ATTEMPTS = int(os.environ.get("RUNPOD_MAX_POD_ATTEMPTS", "3"))
# create_pod() failures cost nothing (no capacity for that GPU): the whole GPU list is walked at most this many times.
MAX_CREATE_PASSES = int(os.environ.get("RUNPOD_MAX_CREATE_PASSES", "3"))
# A pod that runs longer than this is terminated and the job is given up (a retry would most likely run as long).
POD_MAX_RUNTIME_S = float(os.environ.get("RUNPOD_POD_MAX_RUNTIME_S", str(60 * 60)))
# Variables copied into the pod (see pod_env in launch_job)
POD_FORWARDED_VARIABLES = ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "MESHROOM_URL",
                           "MESHROOM_FALLBACK_URL", "HF_TOKEN")
POLL_S = 15.0                     # pause between two status queries
STATUS_MAX_ERRORS = 5             # consecutive failed status queries after which the pod is given up (and terminated)
TERMINATE_ATTEMPTS = 5            # terminate_pod() is retried this many times
TERMINATE_RETRY_S = 5.0
MIN_SUCCESS_UPTIME_S = 120        # a pod that exits sooner than this failed its host check (RAM) at start
CREATE_RETRY_S = 2.0
PASS_COOLDOWN_S = 60.0            # between two walks over the GPU list
FAILED_POD_COOLDOWN_S = 10.0


# determine GPU dynamically
# GPU_TYPE_ID = "NVIDIA GeForce RTX 5090"

def gql(query: str, variables: dict | None = None) -> dict:
    """
    Execute a GraphQL query against the RunPod API.

    :param query: The GraphQL query string.
    :type query: str
    :param variables: The variables for the GraphQL query.
    :type variables: dict | None
    :return: The data payload from the GraphQL response.
    :rtype: dict
    """
    if not RUNPOD_API_KEY:
        raise ValueError("RUNPOD_API_KEY is not set.")

    r = requests.post(
        GRAPHQL_URL,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
        },
        json={"query": query, "variables": variables or {}},
        timeout=60,
    )
    r.raise_for_status()
    payload = r.json()
    if "errors" in payload:
        raise RuntimeError(payload["errors"])
    return payload["data"]


def create_pod(env_dict: dict, gpu_type_id: str) -> str:
    """
    Create a new pod on RunPod using the specified environment and GPU.

    :param env_dict: The environment variables to set in the pod.
    :type env_dict: dict
    :param gpu_type_id: The ID of the GPU type to request.
    :type gpu_type_id: str
    :return: The ID of the created pod.
    :rtype: str
    """
    mutation = """
    mutation CreatePod($input: PodFindAndDeployOnDemandInput!) {
      podFindAndDeployOnDemand(input: $input) {
        id
        desiredStatus
      }
    }
    """
    env_list = [{"key": k, "value": str(v)} for k, v in env_dict.items()]

    pod_input = {
        "cloudType": "SECURE",
        "gpuCount": 1,
        "gpuTypeId": gpu_type_id,
        "name": f"meshroom-dynamic-job",
        "imageName": IMAGE_NAME,
        # Give enough disk space to extract the 22.5 GB Meshroom + cache + output (tarball itself is 13.3 GB!)
        "containerDiskInGb": 80,
        "volumeInGb": 0,
        "env": env_list,
        "ports": "",
        "dockerArgs": "",
    }

    data = gql(mutation, {"input": pod_input})
    return data["podFindAndDeployOnDemand"]["id"]


def get_pod_status(pod_id: str) -> dict:
    """
    Retrieve the status of a specific pod.

    :param pod_id: The ID of the pod to check.
    :type pod_id: str
    :return: The pod status data.
    :rtype: dict
    """
    query = """
    query GetPod($podId: String!) {
      pod(input: {podId: $podId}) {
        id
        desiredStatus
        runtime {
          uptimeInSeconds
        }
      }
    }
    """
    data = gql(query, {"podId": pod_id})
    return data["pod"]


def get_pod_memory_gb(pod_id: str) -> float | None:
    """
    RAM of the host a pod runs on, in GB (None when it cannot be read). Best effort on purpose: it feeds only the choice
    of the last-resort host in launch_job(), so a failing query must never fail the job.
    """
    query = """
    query PodMemory($podId: String!) {
      pod(input: {podId: $podId}) {
        memoryInGb
      }
    }
    """
    try:
        value = gql(query, {"podId": pod_id})["pod"]["memoryInGb"]
        return float(value) if value is not None else None
    except Exception as e:
        print(f"Could not read the RAM of pod {pod_id}: {e}")
        return None


def get_pod_logs(pod_id: str) -> str:
    """
    Fetches the logs of a specific pod.
    :param pod_id: The ID of the pod
    :type pod_id: str
    :return: The log string
    :rtype: str
    """
    query = """
    query PodLog($podId: String!) {
      podLog(input: {podId: $podId}) {
        stepLog
      }
    }
    """
    response = gql(query, {"podId": pod_id})
    return response.get("podLog", {}).get("stepLog", "")


def terminate_pod(pod_id: str) -> str:
    """
    Terminate a running pod on RunPod. This completely deletes the pod and disk to stop all billing.
    :param pod_id: The ID of the pod to terminate.
    :type pod_id: str
    :return: The status of the pod after termination.
    :rtype: str
    """
    mutation = """
    mutation TerminatePod($podId: String!) {
      podTerminate(input: {podId: $podId})
    }
    """
    data = gql(mutation, {"podId": pod_id})
    # podTerminate returns a boolean or null usually, we can just return success string
    return "TERMINATED"


def terminate_pod_reliably(pod_id: str) -> bool:
    """
    Terminates a pod and makes sure it is gone: terminate_pod() is retried a few times (a network blip must not leave a
    billed pod running). Returns False, after saying loudly which pod is still alive, when every attempt failed.
    """
    for attempt in range(1, TERMINATE_ATTEMPTS + 1):
        try:
            terminate_pod(pod_id)
            return True
        except Exception as e:
            print(f"Terminating pod {pod_id} failed (attempt {attempt} of {TERMINATE_ATTEMPTS}): {e}")
            if attempt < TERMINATE_ATTEMPTS:
                time.sleep(TERMINATE_RETRY_S)
    print(f"!!! POD {pod_id} COULD NOT BE TERMINATED AND MAY STILL BE BILLED. Terminate it in the RunPod console. !!!")
    return False


class PodRun:
    """What happened to one pod: how it ended, how long it ran, the RAM of its host and whether it is gone."""

    def __init__(self, pod_id: str, started_at: float):
        self.pod_id = pod_id
        self.started_at = started_at
        self.outcome = "failed"      # exited | cancelled | timeout | status_error | failed
        self.uptime = 0
        self.memory_gb: float | None = None
        self.terminated = False


def run_pod(env: dict, gpu_type_id: str, cancel_event=None) -> PodRun:
    """
    Creates one pod, waits until it exits (or is cancelled, runs too long, or cannot be queried) and ALWAYS terminates it
    afterwards, whatever happens in between (the `finally`). Raises only when the pod could not be created: nothing is
    left running in that case.
    """
    started_at = time.time()
    pod_id = create_pod(env, gpu_type_id)
    print(f"Created pod: {pod_id}")
    run = PodRun(pod_id, started_at)
    try:
        run.memory_gb = get_pod_memory_gb(pod_id)
        print(f"Waiting for pod {pod_id} to start and complete (host RAM: {run.memory_gb} GB)...")
        errors = 0
        while True:
            if cancel_event is not None:
                if cancel_event.wait(timeout=POLL_S):
                    print("Job cancelled by user. Terminating pod immediately.")
                    run.outcome = "cancelled"
                    break
            else:
                time.sleep(POLL_S)

            if time.time() - started_at > POD_MAX_RUNTIME_S:
                print(f"Pod exceeded the maximum runtime of {POD_MAX_RUNTIME_S / 60:g} minutes. Terminating it to prevent runaway billing.")
                run.outcome = "timeout"
                break

            try:
                pod_data = get_pod_status(pod_id)
            except Exception as e:
                errors += 1
                print(f"Could not read the status of pod {pod_id} ({errors} of {STATUS_MAX_ERRORS}): {e}")
                if errors >= STATUS_MAX_ERRORS:
                    run.outcome = "status_error"
                    break
                continue
            errors = 0

            runtime = pod_data.get("runtime")
            if runtime:
                run.uptime = runtime.get("uptimeInSeconds", 0)
            status = pod_data["desiredStatus"]
            print(f"Pod status: {status} (Uptime: {run.uptime}s)")
            if status in ("EXITED", "TERMINATED"):
                print("Pod has exited.")
                run.outcome = "exited"
                break
    finally:
        print(f"Stopping/cleaning up pod {pod_id}...")
        run.terminated = terminate_pod_reliably(pod_id)
    return run


def output_is_ready(started_at: float) -> bool:
    """True when output.zip is in the bucket and was written after `started_at` (False when it cannot be checked)."""
    try:
        return bool(r2.file_exists_and_is_new("output.zip", after_timestamp=started_at))
    except Exception as e:
        print(f"Could not check output.zip in the bucket: {e}")
        return False


def launch_job(job: dict, cancel_event=None):
    """
    Launch a processing job on a dynamically provisioned pod based on RAM requirements.

    The number of attempts is bounded (a pod is billed from the moment it is created):

    * pods that could not be created (no capacity) cost nothing: the GPU list is walked at most MAX_CREATE_PASSES times;
    * pods that were created and did not deliver output.zip (the host failed the RAM check at start, or the job was
      killed) count as failed attempts: after MAX_POD_ATTEMPTS of them ONE last pod is started on the GPU whose host had
      the most RAM, with the RAM requirement lowered to what that host has (best effort: the job may still run out of
      memory). If no host RAM could be read, the job is given up instead;
    * a pod that exceeds POD_MAX_RUNTIME_S is terminated and the job is given up at once.

    Every pod is terminated in a `finally` (run_pod), also on cancel, timeout, API errors and unexpected exceptions; when
    a pod cannot be terminated no other pod is started.

    :param job: The job configuration and details.
    :type job: dict
    :param cancel_event: Optional threading.Event to instantly cancel execution.
    :return: True if the job completed successfully.
    :rtype: bool
    """
    photo_count = job.get("photo_count", 50)
    resolution_mp = job.get("resolution_mp", 12.0)
    mode = job.get("mode", "single")
    two_sides = (mode == "two-sides")

    # Store these key parameters in the job dict so they flow into the final stats
    job["depthmap_downscale"] = job.get("depthmap_downscale", 2)
    job["max_input_points"] = job.get("max_input_points", 10000000)

    # Calculate required RAM
    ram_min_gb = calculate_required_ram(
        num_images=photo_count,
        resolution_mp=resolution_mp,
        depthmap_downscale=job["depthmap_downscale"],
        max_input_points=job["max_input_points"],
        two_sides_mode=two_sides
    )
    print(f"Calculated required RAM: {ram_min_gb} GB for {photo_count} photos ({mode} mode)")

    # Define GPU fallback strategy based on required RAM and Secure Cloud availability
    if ram_min_gb > 64:
        # High RAM needs:
        gpu_fallback_list = [
            "NVIDIA GeForce RTX 5090",
            "NVIDIA L40",
            "NVIDIA RTX 6000 Ada Generation",
            "NVIDIA GeForce RTX 3090",
            "NVIDIA RTX A6000"
        ]
        complementary_gpus_list = [
            "NVIDIA GeForce RTX 4090",
            "NVIDIA A40"
        ]
    else:
        # Low/Medium RAM needs: Stick to fast/cheap consumer GPUs.
        gpu_fallback_list = [
            "NVIDIA GeForce RTX 5090",
            "NVIDIA GeForce RTX 4090",
            "NVIDIA GeForce RTX 3090",
            "NVIDIA A40"
        ]
        complementary_gpus_list = [
            "NVIDIA L40",
            "NVIDIA RTX 6000 Ada Generation",
            "NVIDIA RTX A6000"
        ]
    candidates = gpu_fallback_list + complementary_gpus_list

    def pod_env(gpu: str, ram_gb: int) -> dict:
        # Inject the target GPU into the job JSON so main.py can save it in stats
        job["target_gpu"] = gpu
        env = {"JOB_JSON": json.dumps(job), "RAM_MIN_GB": str(ram_gb)}
        # Forward the Cloudflare R2 access, the Meshroom URLs and the Hugging Face token. The pod runs on a third party's
        # host, so POD_<NAME> (for example POD_R2_ACCESS_KEY_ID, a token limited to the scanner bucket) is preferred over
        # <NAME> (the Pi's own key) when it is set.
        for key in POD_FORWARDED_VARIABLES:
            value = os.getenv("POD_" + key) or os.getenv(key)
            if value:
                env[key] = value
        return env

    def wait(seconds: float) -> bool:
        """Pause; True when the job was cancelled meanwhile."""
        if cancel_event is not None:
            return cancel_event.wait(timeout=seconds)
        time.sleep(seconds)
        return False

    failed: list[tuple[float | None, str]] = []   # (host RAM in GB, GPU) of the pods that failed
    create_failures = 0
    max_create_failures = max(1, MAX_CREATE_PASSES) * len(candidates)
    index = 0
    attempt = 0

    while len(failed) < MAX_POD_ATTEMPTS:
        if cancel_event is not None and cancel_event.is_set():
            print("Job cancelled by user before pod creation.")
            return False
        if create_failures >= max_create_failures:
            print(f"[!] No pod could be created in {MAX_CREATE_PASSES} walks over the GPU list. Giving up.")
            return False

        target_gpu = candidates[index % len(candidates)]
        index += 1
        attempt += 1
        print(f"\n--- Attempt {attempt} (Targeting: {target_gpu}) ---")

        try:
            run = run_pod(pod_env(target_gpu, ram_min_gb), target_gpu, cancel_event)
        except Exception as e:
            print(f"Failed to create pod for {target_gpu}: {e}")
            create_failures += 1
            # after a whole walk over the list without a pod, wait before the next one (spares the API)
            if index % len(candidates) == 0:
                print(f"Walked the whole GPU list; waiting {PASS_COOLDOWN_S:g} s before the next walk...")
                if wait(PASS_COOLDOWN_S):
                    return False
            elif wait(CREATE_RETRY_S):
                return False
            continue

        if not run.terminated:
            return False          # a pod may still be running: do not start another one
        if run.outcome == "cancelled":
            return False
        if run.outcome == "timeout":
            return False
        if run.outcome == "exited" and run.uptime > MIN_SUCCESS_UPTIME_S:
            print("Pod ran for a significant amount of time. Verifying R2 for output.zip...")
            if output_is_ready(run.started_at):
                print("Success! output.zip is present and fresh.")
                return True
            print("Failure! output.zip is missing or stale. The job likely failed via OOM kill.")
        else:
            print("Pod exited very quickly or could not be followed. RAM check likely failed on this host.")

        failed.append((run.memory_gb, target_gpu))
        if len(failed) < MAX_POD_ATTEMPTS and wait(FAILED_POD_COOLDOWN_S):
            return False

    # Last resort: the host with the most RAM seen, with the requirement lowered to what it has
    known = [item for item in failed if item[0] is not None]
    if not known:
        print(f"[!] {len(failed)} pods failed and none reported its RAM, so there is no better host to pick. Giving up.")
        return False
    best_ram, best_gpu = max(known, key=lambda item: item[0])
    lowered = min(ram_min_gb, int(best_ram))
    print(f"[!] {len(failed)} pods failed. Last attempt on {best_gpu} (host RAM {best_ram:g} GB, "
          f"requirement lowered from {ram_min_gb} GB to {lowered} GB); the job may run out of memory.")
    if cancel_event is not None and cancel_event.is_set():
        return False
    try:
        run = run_pod(pod_env(best_gpu, lowered), best_gpu, cancel_event)
    except Exception as e:
        print(f"Failed to create the last-resort pod for {best_gpu}: {e}")
        return False
    if run.outcome == "exited" and run.terminated and run.uptime > MIN_SUCCESS_UPTIME_S and output_is_ready(run.started_at):
        print("Success! output.zip is present and fresh.")
        return True
    print("The last-resort pod did not deliver output.zip. Giving up.")
    return False


def update_statistics(output_dir="./output", csv_file="stats.csv"):
    stats_path = os.path.join(output_dir, "stats.json")
    if not os.path.exists(stats_path):
        print(f"No stats.json found in {output_dir}. Skipping statistics update.")
        return

    try:
        with open(stats_path, "r", encoding="utf-8") as f:
            stats = json.load(f)

        file_exists = os.path.exists(csv_file)

        with open(csv_file, "a", newline="", encoding="utf-8") as csvfile:
            fieldnames = [
                "timestamp", "job_id", "status",
                "run_time_seconds", "max_ram_mb", "min_ram_calculated", "cpu_cores",
                "photo_count", "resolution_mp", "mode", "depthmap_downscale", "max_input_points",
                "output_size_mb", "target_gpu"
            ]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            if not file_exists:
                writer.writeheader()

            writer.writerow(stats)

        print(f"Successfully appended run statistics to {csv_file}!")
    except Exception as e:
        print(f"Error updating statistics: {e}")


if __name__ == "__main__":
    print("Starting the simulation...")

    # send_images()
    # send_rig_images()

    # Run a Runpod command
    print("Run Runpod...")

    test_job = {
        "job_id": "job-12345",
        "photo_count": 80,
        "mode": "single"
    }
    launch_job(test_job)

    print("Runpod Finishes its job!")

    r2.download_result()
    update_statistics()
    r2.delete_all_files()