from uuid import UUID, uuid4
from typing import Any
from app.schemas.cv_analysis import CVAnalysisDetail, CVAnalysisStatus


jobs: dict[UUID, CVAnalysisDetail] = {}


def create_job(candidate_id: int) -> CVAnalysisDetail:
    job = CVAnalysisDetail(
        job_id=uuid4(),
        candidate_id=candidate_id,
        status=CVAnalysisStatus.queued,
    )

    jobs[job.job_id] = job

    return job


def get_job(job_id: UUID) -> CVAnalysisDetail | None:
    return jobs.get(job_id)

def mark_job_processing(job_id: UUID) -> None:
    job = get_job(job_id)

    if job is not None:
        job.status = CVAnalysisStatus.processing

def mark_job_completed(job_id: UUID, result: dict[str, Any]) -> None:
    job = get_job(job_id)

    if job is not None:
        job.status = CVAnalysisStatus.completed
        job.result = result

def mark_job_failed(job_id: UUID, error: str) -> None:
    job = get_job(job_id)

    if job is not None:
        job.status = CVAnalysisStatus.failed
        job.error = error