from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, status

from app.schemas.cv_analysis import (
    CVAnalysisAccepted,
    CVAnalysisDetail,
    CVAnalysisRequest,
)
from app.services.cv_analysis_runner import run_cv_analysis
from app.services.job_store import create_job, get_job


router = APIRouter()


@router.post(
    "/cv-analyses",
    response_model=CVAnalysisAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_cv_analysis(
    payload: CVAnalysisRequest,
    background_tasks: BackgroundTasks,
) -> CVAnalysisAccepted:
    job = create_job(candidate_id=payload.candidate_id)

    background_tasks.add_task(
        run_cv_analysis,
        job.job_id,
        payload.cv_text,
    )

    return CVAnalysisAccepted(
        job_id=job.job_id,
        status=job.status,
    )


@router.get(
    "/cv-analyses/{job_id}",
    response_model=CVAnalysisDetail,
)
async def retrieve_cv_analysis(
    job_id: UUID,
) -> CVAnalysisDetail:
    job = get_job(job_id)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="CV analysis not found",
        )

    return job
