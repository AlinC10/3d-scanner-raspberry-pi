"""
job.py — The whole cloud flow for one scan, run from the Pi or the laptop:

  preflight R2 -> upload photos to jobs/<id>/input/ -> verify every photo is there
  -> RAM / disk estimate -> pod (fresh read-only / read-write R2 credentials limited to this job,
     expiring with the pod's runtime cap) -> wait for status.json -> terminate the pod
  -> download + extract output.zip into results/<id>/ -> delete jobs/<id>/ from R2.

The R2 job folder is deleted only after output.zip was downloaded AND extracted; after a failure the photos
stay in R2, so the job can be retried with skip_upload=True.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError

from cloud import cloudflare_r2 as r2
from cloud import runpod as rp
from cloud.resources import estimate_ram_gb

logger = logging.getLogger("TurntableStudio.CloudJob")

DEFAULT_IMAGE_ENV = "TURNTABLE_IMAGE"
RAM_MARGIN = 1.3          # on the estimate until more runs refit the model (tools/fit_ram_model.py)
DEFAULT_MIN_VCPU = 8


def _image_size(path: str) -> tuple[int, int]:
    """(width, height) of one photo, read from its header only."""
    from PIL import Image
    with Image.open(path) as im:
        return im.size


def _merge_cloud_stats(stats_path: Path, report: Dict[str, Any], runs) -> None:
    """
    Adds what only the client knows to the job's stats.json (training data for RAM / time / cost models):
    the pod actually rented, its price, the time from pod creation to the pipeline start (image pull + boot),
    upload / download times and the requested RAM.
    """
    if not stats_path.is_file() or not runs:
        return
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        last = runs[-1]
        pod_started = (report.get("status") or {}).get("started_at")
        stats["cloud"] = {
            "job_id": report["job_id"],
            "plan": report.get("plan"),
            "pods": report.get("pods"),
            "pod_attempts": len(runs),
            "cost_estimate": report.get("cost_estimate"),
            "pull_and_boot_s": round(float(pod_started) - last.started_at, 1) if pod_started else None,
            "client_timings_s": report.get("timings_s"),
            "pod_timings_s": report.get("pod_timings_s"),
        }
        stats_path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    except (OSError, ValueError, KeyError) as e:
        logger.warning(f"Could not add the cloud data to {stats_path}: {e}")


def plan_job(input_dir: str, resolution_level: int = 1, min_vcpu: int = DEFAULT_MIN_VCPU,
             image_prefix: str = "") -> Dict[str, Any]:
    """
    Photo count, sizes and the pod requirements derived from them (§2 of the performance plan).
    image_prefix: only the photos whose path relative to input_dir starts with it (the scanner passes "rig/").
    """
    files = {rel: path for rel, path in r2.local_image_files(input_dir).items() if rel.startswith(image_prefix)}
    if not files:
        raise r2.R2Error(f"No images found under {input_dir}")
    first = next(iter(files.values()))
    width, height = _image_size(first)
    input_gb = sum(os.path.getsize(p) for p in files.values()) / 1024 ** 3
    estimate = estimate_ram_gb(len(files), width, height, resolution_level)
    return {
        "n_images": len(files),
        "width": width,
        "height": height,
        "input_gb": round(input_gb, 3),
        "resolution_level": resolution_level,
        "ram_estimate_gb": round(estimate, 2),
        "min_ram_gb": int(math.ceil(estimate * RAM_MARGIN)),
        "min_vcpu": int(min_vcpu),
        "disk_gb": rp.disk_gb_for(len(files), input_gb, resolution_level),
        "files": files,
    }


# Pipeline steps of Turntable Studio (the name it logs and writes in jobs/<id>/output/progress.json as "step", e.g.
# "3. Bridge SFM & Image Undistortion") -> the milestone shown to the user when that step is finished. Matched by the
# number the step name starts with; a step that is not listed is shown by its own name.
POD_STEP_LABELS = {
    "1": "cameras found",
    "2": "turntable camera poses computed",
    "2.5": "object masks computed",
    "2.6": "visual hull computed",
    "3": "structure from motion (SfM) done",
    "4.1": "dense reconstruction prepared",
    "4.2": "dense point cloud computed",
    "4.3": "3D mesh built",
    "4.4": "texturing done",
    "5": "base sealed (watertight mesh)",
    "5.5": "texturing images prepared",
    "6": "models exported",
}


def pod_step_label(progress: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    The milestone message for a progress.json of the pod (``{"step": "<last finished step>", ...}``), or None when
    there is none yet. Only the finished step is reported, so the message changes once per phase.
    """
    step = (progress or {}).get("step")
    if not isinstance(step, str) or not step.strip():
        return None
    step = step.strip()
    number = step.split(" ", 1)[0].rstrip(".")
    number = number[:-1] if number[-1:].isalpha() else number   # "4.2b" -> "4.2"
    label = POD_STEP_LABELS.get(number)
    return f"3D reconstruction: {label or step}"


def run_cloud_job(
    input_dir: str,
    results_dir: str = "results",
    job_id: Optional[str] = None,
    pipeline_options: Optional[Dict[str, Any]] = None,
    image: Optional[str] = None,
    dry_run: bool = False,
    skip_upload: bool = False,
    keep_remote: bool = False,
    min_vcpu: int = DEFAULT_MIN_VCPU,
    delete_stale_after_s: Optional[float] = None,
    cancel_event=None,
    on_progress: Optional[Callable[[str], None]] = None,
    image_prefix: str = "",
    terminate_old_pods: bool = True,
    on_phase: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Runs one scan in the cloud. pipeline_options go to run_pipeline() in the pod (e.g. {"rig_mode": True,
    "estimate_lift": True}). Returns a report with timings, the pod used, the cost estimate and the result folder.
    delete_stale_after_s: first delete the jobs/<id>/ folders left in R2 by older jobs untouched for that long
    (r2.delete_stale_jobs; 0 = every older job, None = keep them). This job and anything outside jobs/ (e.g. the
    scanner's own folder) are never touched.
    on_progress gets every message (technical detail: pod polling, uploads...); on_phase only the milestones (photos
    checked, cloud server created, reconstruction started, each finished pipeline step the pod reports in
    progress.json, model downloaded), each one once.
    image_prefix: see plan_job. terminate_old_pods: first terminate every "turntable-*" pod of the account (an
    earlier job's pod left alive by a crash), so a new scan never runs next to a forgotten, billed pod.
    """
    say = on_progress or logger.info

    def phase(message: str) -> None:
        if on_phase is not None:
            on_phase(message)

    options = dict(pipeline_options or {})
    image = image or os.environ.get(DEFAULT_IMAGE_ENV)
    bucket = r2.bucket_from_env()
    job_id = job_id or r2.new_job_id()
    report: Dict[str, Any] = {"job_id": job_id, "bucket": bucket, "timings_s": {}, "dry_run": dry_run}
    t = report["timings_s"]

    plan = plan_job(input_dir, int(options.get("resolution_level", 1)), min_vcpu, image_prefix)
    files = plan.pop("files")
    report["plan"] = plan
    say(f"Job {job_id}: {plan['n_images']} photos ({plan['input_gb']:.2f} GB, {plan['width']}x{plan['height']}), "
        f"RAM estimate {plan['ram_estimate_gb']} GB -> min {plan['min_ram_gb']} GB, "
        f"{plan['min_vcpu']} vCPU, disk {plan['disk_gb']} GB")

    client = r2.client_from_env()
    t0 = time.time()
    r2.preflight(bucket, client=client, prefix=r2.job_prefix(job_id) + "_preflight/")
    t["preflight"] = round(time.time() - t0, 2)
    say(f"R2 preflight ok ({t['preflight']} s)")

    if delete_stale_after_s is not None and not dry_run:
        try:
            stale = r2.delete_stale_jobs(delete_stale_after_s, bucket, client=client, keep=[job_id])
            report["deleted_stale_jobs"] = stale
            if stale:
                say(f"Deleted {len(stale)} old job folder(s) from R2: {', '.join(stale)}")
        except r2.R2Error as e:
            say(f"Could not clean old jobs from R2 (continuing): {e}")

    expected = [r2.input_key(job_id, rel) for rel in files]
    if not skip_upload and not dry_run:
        t0 = time.time()
        res = r2.upload_job_images(job_id, input_dir, bucket, client=client)
        t["upload"] = round(time.time() - t0, 2)
        mb = plan["input_gb"] * 1024
        say(f"Uploaded {len(res['uploaded'])} photos ({mb:.0f} MB) in {t['upload']} s "
            f"({mb / max(t['upload'], 1e-3):.1f} MB/s), failed: {len(res['failed'])}")
    if not dry_run:
        missing = r2.verify_keys(expected, bucket, prefix=r2.input_prefix(job_id), client=client)
        if missing:
            raise r2.R2Error(f"{len(missing)} photo(s) missing in R2, not starting a pod: {missing[:5]}")
        say("All photos are in R2.")
        phase("Photos checked in the cloud storage: starting a cloud server")

    ttl = int(rp.POD_MAX_RUNTIME_S)
    job_spec = dict(options, job_id=job_id)

    def make_env() -> Dict[str, str]:
        env = r2.pod_credentials_env(job_id, ttl) if not dry_run else {
            "R2_ENDPOINT_URL": os.environ.get("R2_ENDPOINT_URL", ""), "R2_BUCKET": bucket,
            "R2_IN_ACCESS_KEY_ID": "<temp>", "R2_IN_SECRET_ACCESS_KEY": "<temp>", "R2_IN_SESSION_TOKEN": "<temp>",
            "R2_OUT_ACCESS_KEY_ID": "<temp>", "R2_OUT_SECRET_ACCESS_KEY": "<temp>", "R2_OUT_SESSION_TOKEN": "<temp>",
        }
        env.update({
            "JOB_ID": job_id,
            "JOB_JSON": json.dumps(job_spec),
            "TURNTABLE_IMAGE": image or "",
            "POD_MAX_RUNTIME_S": str(ttl),
            "RAM_MARGIN": "1.0",
        })
        return env

    def make_body(min_ram_gb: int) -> Dict[str, Any]:
        if not image:
            raise rp.RunPodError(f"No Docker image: set {DEFAULT_IMAGE_ENV} (e.g. ghcr.io/<user>/turntable-studio:v1).")
        return rp.build_pod_request(
            image=image, env=make_env(), min_ram_gb=min_ram_gb, min_vcpu=plan["min_vcpu"],
            disk_gb=plan["disk_gb"], gpu_types=rp.gpu_types_from_env(), name=f"turntable-{job_id}",
            cloud_type=os.environ.get("RUNPOD_CLOUD_TYPE", "SECURE"),
            registry_auth_id=os.environ.get("RUNPOD_REGISTRY_AUTH_ID") or None,
        )

    if dry_run:
        report["pod_request"] = rp.redact(make_body(plan["min_ram_gb"]))
        say("Dry run: pod request that would be sent:\n" + json.dumps(report["pod_request"], indent=2))
        return report

    status_key = r2.output_key(job_id, "status.json")
    started_key = r2.output_key(job_id, "started.json")
    progress_key = r2.output_key(job_id, "progress.json")

    def read_status():
        return r2.get_json(status_key, bucket, client=client)

    def read_started() -> bool:
        return r2.get_json(started_key, bucket, client=client) is not None

    def read_progress() -> Optional[str]:
        return pod_step_label(r2.get_json(progress_key, bucket, client=client))

    def clear_status():
        for key in (status_key, started_key, progress_key):
            try:
                client.delete_object(Bucket=bucket, Key=key)
            except (ClientError, BotoCoreError) as e:
                say(f"Could not remove the previous {key.rsplit('/', 1)[-1]}: {e}")

    rp_client = rp.RunPodClient()
    if terminate_old_pods:
        old_pods = rp.terminate_stale_pods(rp_client)
        if old_pods:
            say(f"Terminated {len(old_pods)} pod(s) left by older jobs: {', '.join(old_pods)}")

    clear_status()  # a status.json left by an earlier attempt of this job must not end the new pod at once
    t0 = time.time()
    runs = rp.run_job_pods(rp_client, make_body, plan["min_ram_gb"], read_status,
                           cancel_event=cancel_event, on_progress=say, before_retry=clear_status,
                           read_started=read_started, on_phase=on_phase, read_progress=read_progress)
    t["pods"] = round(time.time() - t0, 2)
    report["pods"] = [{
        "pod_id": r.pod_id, "outcome": r.outcome, "terminated": r.terminated, "elapsed_s": round(r.elapsed_s, 1),
        "cost_estimate": r.cost_estimate, **r.pod_info,
    } for r in runs]
    report["cost_estimate"] = round(sum(r.cost_estimate or 0.0 for r in runs), 4)
    last = runs[-1] if runs else None
    status = (last.status if last else None) or {}
    report["status"] = status
    if status.get("timings_s"):
        report["pod_timings_s"] = status["timings_s"]

    if status.get("state") != "ok":
        report["result"] = "failed"
        say(f"Job failed: {status.get('state') or (last.outcome if last else 'no pod')} {status.get('error', '')}")
        # Keep the photos in R2 for a retry; fetch the log if the pod managed to upload it
        r2.download_file(r2.output_key(job_id, "run_log.txt"),
                         str(Path(results_dir) / job_id / "run_log.txt"), bucket, client=client)
        return report

    phase("3D reconstruction finished: downloading the model")
    t0 = time.time()
    dest = r2.download_job_output(job_id, str(Path(results_dir) / job_id), bucket, client=client)
    t["download"] = round(time.time() - t0, 2)
    report["result"] = "ok"
    report["results_dir"] = str(dest)
    say(f"Result extracted to {dest} ({t['download']} s)")

    _merge_cloud_stats(dest / "stats.json", report, runs)

    if not keep_remote:
        t0 = time.time()
        report["deleted_objects"] = r2.delete_prefix(r2.job_prefix(job_id), bucket, client=client)
        t["cleanup"] = round(time.time() - t0, 2)
        say(f"Deleted {report['deleted_objects']} objects of job {job_id} from R2.")
    (dest / "cloud_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
