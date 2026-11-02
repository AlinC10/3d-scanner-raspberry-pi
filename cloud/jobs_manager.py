"""
In-memory manager for scan and reconstruction jobs.

Tracks object and face jobs, their status, pod IDs and cancellation events. Allows
concurrent cloud jobs while preserving active pods during stale cleanup.
"""
from dataclasses import dataclass, field
import logging
import os
import threading
import time
from typing import Optional, Dict, List, Any

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {"queued", "running", "uploading", "processing"}
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

MAX_PARALLEL_CLOUD_JOBS = int(os.environ.get("MAX_PARALLEL_CLOUD_JOBS", "2"))


@dataclass
class JobRecord:
    job_id: str
    kind: str = "object"   # "object" or "face"
    status: str = "queued" # "queued", "running", "uploading", "processing", "completed", "failed", "cancelled"
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    pod_id: Optional[str] = None
    pod_status: Optional[str] = None
    phase: Optional[str] = None
    phase_message: Optional[str] = None
    cloud_message: Optional[str] = None
    error: Optional[str] = None
    result_id: Optional[str] = None
    cost_estimate: Optional[float] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """
        Serializes the record into a JSON-compatible dictionary.

        :return: Dictionary representation of the job record.
        """
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "pod_id": self.pod_id,
            "pod_status": self.pod_status,
            "phase": self.phase,
            "phase_message": self.phase_message,
            "cloud_message": self.cloud_message,
            "error": self.error,
            "result_id": self.result_id,
            "cost_estimate": self.cost_estimate,
            "meta": dict(self.meta),
        }


class JobManager:
    def __init__(self) -> None:
        """Initializes the thread-safe job repository."""
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def register_job(
        self,
        job_id: str,
        kind: str = "object",
        status: str = "queued",
        phase: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
        created_at: Optional[float] = None,
        **kwargs: Any,
    ) -> JobRecord:
        """
        Registers a new scan or reconstruction job in the repository.

        :param job_id: Unique identifier for the scan job.
        :param kind: 'object' or 'face'.
        :param status: Initial job status.
        :param phase: Current execution phase.
        :param meta: Optional dictionary of metadata.
        :param created_at: Optional creation timestamp in seconds.
        :param kwargs: Additional fields to initialize on JobRecord.
        :return: The registered JobRecord instance.
        """
        with self._lock:
            if job_id in self._jobs:
                rec = self._jobs[job_id]
                rec.status = status
                rec.kind = kind
                if phase:
                    rec.phase = phase
                if meta:
                    rec.meta.update(meta)
                if created_at is not None:
                    rec.created_at = created_at
                for k, v in kwargs.items():
                    if hasattr(rec, k) and k != "cancel_event":
                        setattr(rec, k, v)
                return rec
            rec = JobRecord(
                job_id=job_id,
                kind=kind,
                status=status,
                phase=phase,
                meta=dict(meta or {}),
            )
            if created_at is not None:
                rec.created_at = created_at
            for k, v in kwargs.items():
                if hasattr(rec, k) and k != "cancel_event":
                    setattr(rec, k, v)
            self._jobs[job_id] = rec
            return rec

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        """
        Retrieves a job record by its identifier.

        :param job_id: Unique job ID.
        :return: JobRecord or None if not registered.
        """
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> List[JobRecord]:
        """
        Returns all registered jobs ordered newest first.

        :return: List of all JobRecord instances.
        """
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def update_job(self, job_id: str, **kwargs: Any) -> Optional[JobRecord]:
        """
        Updates specific fields on a registered job record.

        :param job_id: Unique job ID.
        :param kwargs: Fields to update on the JobRecord.
        :return: Updated JobRecord or None if not found.
        """
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                return None
            for k, v in kwargs.items():
                if hasattr(rec, k) and k != "cancel_event":
                    setattr(rec, k, v)
            if "status" in kwargs and kwargs["status"] in TERMINAL_STATUSES and rec.completed_at is None:
                rec.completed_at = time.time()
            return rec

    def get_active_pod_ids(self) -> List[str]:
        """
        Returns all pod IDs of non-terminal jobs.

        :return: List of active pod IDs.
        """
        with self._lock:
            return [
                rec.pod_id
                for rec in self._jobs.values()
                if rec.pod_id and rec.status not in TERMINAL_STATUSES
            ]

    def get_active_job_ids(self) -> List[str]:
        """
        Returns all job IDs of active (non-terminal) jobs.

        :return: List of active job IDs.
        """
        with self._lock:
            return [
                rec.job_id
                for rec in self._jobs.values()
                if rec.status not in TERMINAL_STATUSES
            ]

    def count_active_cloud_jobs(self) -> int:
        """
        Returns the number of active cloud reconstruction jobs.

        :return: Count of active cloud jobs.
        """
        with self._lock:
            return sum(
                1 for rec in self._jobs.values()
                if rec.kind == "object" and rec.status in ("running", "processing")
            )

    def cancel_job(self, job_id: str) -> bool:
        """
        Signals cancellation to the job and its cloud pod, if active.

        :param job_id: Unique identifier of the job to cancel.
        :return: True if the job existed and was cancelled, False otherwise.
        """
        with self._lock:
            rec = self._jobs.get(job_id)
            if not rec:
                return False
            rec.cancel_event.set()
            if rec.status not in TERMINAL_STATUSES:
                rec.status = "cancelled"
                rec.completed_at = time.time()
            pod_id = rec.pod_id

        if pod_id:
            try:
                from cloud.runpod import RunPodClient, terminate_pod_reliably
                client = RunPodClient()
                terminate_pod_reliably(client, pod_id)
                log.info("[JobManager] Terminated pod %s for cancelled job %s", pod_id, job_id)
            except Exception as e:
                log.warning("[JobManager] Could not terminate pod %s on cancel: %s", pod_id, e)

        return True

    def clear(self) -> None:
        """
        Clears all tracked jobs (used in testing).
        """
        with self._lock:
            self._jobs.clear()


jobs_manager = JobManager()
