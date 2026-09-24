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


def launch_job(job: dict, cancel_event=None):
    """
    Launch a processing job on a dynamically provisioned pod based on RAM requirements.

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

    # Loop continuously until we successfully run a pod
    attempt = 1
    original_len = len(gpu_fallback_list)
    
    while True:
        if cancel_event and cancel_event.is_set():
            print("Job cancelled by user before pod creation.")
            return False
            
        for target_gpu in gpu_fallback_list:
            print(f"\n--- Attempt {attempt} (Targeting: {target_gpu}) ---")

            # Inject the target GPU into the job JSON so main.py can save it in stats
            job["target_gpu"] = target_gpu

            # Prepare environment variables for the pod
            env = {
                "JOB_JSON": json.dumps(job),
                "RAM_MIN_GB": str(ram_min_gb),
            }

            # Forward Cloudflare R2 secrets, Meshroom URLs, or Hugging Face Tokens if they exist locally
            for key in ["R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "MESHROOM_URL",
                        "MESHROOM_FALLBACK_URL", "HF_TOKEN"]:
                if os.getenv(key):
                    env[key] = os.getenv(key)

            job_start_time = time.time()
            try:
                pod_id = create_pod(env, target_gpu)
                print(f"Created pod: {pod_id}")
            except Exception as e:
                print(f"Failed to create pod for {target_gpu}: {e}")
                print("Trying next GPU in fallback list...")

                if attempt == 4 * original_len:
                    gpu_fallback_list.extend(complementary_gpus_list)

                attempt += 1
                if cancel_event and cancel_event.wait(timeout=2):
                    return False
                elif not cancel_event:
                    time.sleep(2)
                continue

            print("Waiting for pod to start and complete...")
            uptime = 0
            max_loop_timeout = 60 * 60  # 60 minutes
            
            while True:
                if cancel_event:
                    if cancel_event.wait(timeout=15):
                        print("Job cancelled by user. Terminating pod immediately.")
                        terminate_pod(pod_id)
                        return False
                else:
                    time.sleep(15)
                    
                if time.time() - job_start_time > max_loop_timeout:
                    print("Pod exceeded maximum runtime of 60 minutes. Terminating to prevent runaway billing.")
                    terminate_pod(pod_id)
                    return False

                pod_data = get_pod_status(pod_id)
                status = pod_data["desiredStatus"]

                runtime = pod_data.get("runtime")
                if runtime:
                    uptime = runtime.get("uptimeInSeconds", 0)

                print(f"Pod status: {status} (Uptime: {uptime}s)")

                if status in ("EXITED", "TERMINATED"):
                    print("Pod has exited.")
                    break

            print(f"Stopping/cleaning up pod {pod_id}...")
            terminate_pod(pod_id)

            # Check if pod ran successfully using the uptime RAM heuristic and R2 payload validation
            if uptime > 120:
                print("Pod ran for a significant amount of time. Verifying R2 for output.zip...")
                if r2.file_exists_and_is_new("output.zip", after_timestamp=job_start_time):
                    print("Success! output.zip is present and fresh.")
                    return True
                else:
                    print("Failure! output.zip is missing or stale. The job likely failed via OOM kill. Retrying on next GPU...")
            else:
                print("Pod exited very quickly (under 120s). RAM check likely failed on this host. Retrying...")

                if attempt == 4 * original_len:
                    gpu_fallback_list.extend(complementary_gpus_list)

                attempt += 1
                time.sleep(10)  # Cooldown before trying a new pod

        print("\n[!] Cycled through the entire GPU list without success.")
        print("Waiting 60 seconds before restarting the cycle to avoid spamming the API...")
        time.sleep(60)


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