import os
import json
import time
import requests
from ram_heuristic import calculate_required_ram
import csv
import cloudflare_r2 as r2

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


def stop_pod(pod_id: str) -> str:
    """
    Stop a running pod on RunPod.
    :param pod_id: The ID of the pod to stop.
    :type pod_id: str
    :return: The desired status of the pod after stopping.
    :rtype: str
    """
    mutation = """
    mutation StopPod($podId: String!) {
      podStop(input: {podId: $podId}) {
        id
        desiredStatus
      }
    }
    """
    data = gql(mutation, {"podId": pod_id})
    return data["podStop"]["desiredStatus"]


def launch_job(job: dict):
    """
    Launch a processing job on a dynamically provisioned pod based on RAM requirements.

    :param job: The job configuration and details.
    :type job: dict
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
        # 1. Try 5090 (sometimes it has 94GB)
        # 2. Try L40 (Ada Lovelace, very fast, huge 250GB RAM at $0.82/hr)
        # 3. Try RTX 6000 Ada Generation (Ada Lovelace, very fast)
        # 4. Try RTX 3090 (Ampere, slower but very cheap at $0.50/hr and often has 125GB RAM)
        # 5. Fallback to RTX A6000 (Ampere, similar speed to 3090)
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

    # Prepare environment variables for the pod
    env = {
        "JOB_JSON": json.dumps(job),
        "RAM_MIN_GB": str(ram_min_gb),
    }

    # Loop continuously until we successfully run a pod
    attempt = 1
    original_len = len(gpu_fallback_list)
    while True:
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

            try:
                pod_id = create_pod(env, target_gpu)
                print(f"Created pod: {pod_id}")
            except Exception as e:
                print(f"Failed to create pod for {target_gpu}: {e}")
                print("Trying next GPU in fallback list...")

                if attempt == 4 * original_len:
                    gpu_fallback_list.extend(complementary_gpus_list)

                attempt += 1
                time.sleep(2)
                continue

            print("Waiting for pod to start and complete...")
            uptime = 0
            while True:
                time.sleep(15)
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
            stop_pod(pod_id)

            # If the pod exited in under 120 seconds, we assume it failed the RAM check.
            if uptime > 120:
                print("Pod ran for a significant amount of time. Assuming job success!")
                return True
            else:
                print("Pod exited very quickly. RAM check likely failed on this host. Retrying...")

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