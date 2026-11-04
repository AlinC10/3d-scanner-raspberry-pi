"""
router/jobs.py - Unified Jobs API for background scan & reconstruction tasks.

Endpoints:
  GET  /jobs               - List all jobs, or if `?id=<job_id>` is supplied, return that specific job
  GET  /jobs/{job_id}      - Status of a specific job
  POST /jobs/{job_id}/cancel - Cancel a running job and its cloud pod
"""
import logging
from typing import Optional, List, Dict, Any, Union
from fastapi import APIRouter, HTTPException, Query
from cloud.jobs_manager import jobs_manager

log = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get("", response_model=Union[Dict[str, Any], List[Dict[str, Any]]])
def get_jobs(
    id: Optional[str] = Query(None, description="Optional job ID to fetch status for")
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Returns all jobs when no ID is provided, or the status of the specific job if `id` is given.

    :param id: Optional job ID query parameter.
    :return: List of all jobs or a single job dictionary if id is specified.
    :raises HTTPException: 404 if the requested job ID is not found.
    """
    if id is not None:
        rec = jobs_manager.get_job(id)
        if not rec:
            raise HTTPException(status_code=404, detail=f"Job '{id}' not found")
        return rec.to_dict()
    return [j.to_dict() for j in jobs_manager.list_jobs()]


@router.get("/{job_id}", response_model=Dict[str, Any])
def get_job_by_id(job_id: str) -> Dict[str, Any]:
    """
    Returns status and details for a specific job ID.

    :param job_id: Path parameter with the unique job ID.
    :return: Job details dictionary.
    :raises HTTPException: 404 if the job ID is not registered.
    """
    rec = jobs_manager.get_job(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return rec.to_dict()


@router.post("/{job_id}/cancel", response_model=Dict[str, Any])
def cancel_job(job_id: str) -> Dict[str, Any]:
    """
    Cancels a specific job, stops hardware if it is active on the rig, and terminates any associated cloud pod.

    :param job_id: Path parameter with the unique job ID.
    :return: Confirmation dictionary with status and job ID.
    :raises HTTPException: 404 if the job is not found.
    """
    rec = jobs_manager.get_job(job_id)
    if not rec:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    try:
        from router.scanner import scanner
        active_rig_states = {"preparing", "prepared", "running", "awaiting_flip"}
        is_active_on_rig = (
            getattr(scanner, "job_id", None) == job_id
            and (
                getattr(scanner, "state", None) in active_rig_states
                or getattr(scanner, "is_moving", False)
            )
        )
        if is_active_on_rig:
            scanner.emergency_stop()
    except Exception as e:
        log.warning("Could not stop active scanner for cancelled job %s: %s", job_id, e)

    jobs_manager.cancel_job(job_id)
    return {"status": "cancelled", "job_id": job_id}
