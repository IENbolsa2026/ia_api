"""Endpoints idempotentes para extracciones de CV y ofertas."""

from __future__ import annotations

import base64
import hashlib
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.security import require_service_token
from app.schemas.cv_analysis import CVAnalysisAccepted, CVAnalysisStatus, CVAnalysisTaskStatus
from app.services.job_store import ExtractionJobStore
from app.tasks.cv_analysis_tasks import run_extraction_task

# Alias transitorio para integraciones que importaban el nombre anterior.
analyze_cv_task = run_extraction_task


router = APIRouter()


class OfferAnalysisRequest(BaseModel):
    offer_id: int = Field(gt=0)
    source_hash: str = Field(min_length=32, max_length=128)
    offer_snapshot: dict


class ProfileNormalizationRequest(BaseModel):
    candidate_id: int = Field(gt=0)
    source_hash: str = Field(min_length=32, max_length=128)
    profile_snapshot: dict


def _submit(key: str, payload_hash: str, payload: dict) -> CVAnalysisAccepted:
    job_id = str(uuid4())
    try:
        job, created = ExtractionJobStore().create_or_get(key, payload_hash, payload, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Extraction job store is unavailable.") from exc
    if created or job['status'] == 'accepted':
        celery_app.send_task(run_extraction_task.name, args=[job_id], task_id=f"extract-{job_id}")
    raw_status = job["status"] if job["status"] in CVAnalysisStatus._value2member_map_ else "queued"
    return CVAnalysisAccepted(job_id=job["job_id"], status=CVAnalysisStatus(raw_status), request_id=key)


@router.post("/cv-analyses", response_model=CVAnalysisAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_cv_analysis(
    analysis_id: int = Form(gt=0),
    candidate_id: int = Form(gt=0),
    user_id: int | None = Form(default=None, gt=0),
    cv: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: None = Depends(require_service_token),
) -> CVAnalysisAccepted:
    if cv.content_type not in {"application/pdf", "application/x-pdf"}:
        raise HTTPException(status_code=415, detail="El CV debe enviarse en formato PDF.")
    pdf_bytes = await cv.read()
    if not pdf_bytes:
        raise HTTPException(status_code=422, detail="El PDF está vacío.")
    if len(pdf_bytes) > settings.CV_MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="El PDF supera el máximo permitido.")
    source_hash = hashlib.sha256(pdf_bytes).hexdigest()
    key = idempotency_key or f"candidate-cv:{analysis_id}:{source_hash}"
    return _submit(key, source_hash, {
        "kind": "candidate_cv",
        "analysis_id": analysis_id,
        "candidate_id": candidate_id,
        "user_id": user_id,
        "source_hash": source_hash,
        "cv_pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
    })


@router.post("/offer-analyses", response_model=CVAnalysisAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_offer_analysis(
    request: OfferAnalysisRequest,
    idempotency_key: str = Header(min_length=8, alias="Idempotency-Key"),
    _: None = Depends(require_service_token),
) -> CVAnalysisAccepted:
    return _submit(idempotency_key, request.source_hash, {
        "kind": "job_offer",
        "offer_id": request.offer_id,
        "source_hash": request.source_hash,
        "offer_snapshot": request.offer_snapshot,
    })


@router.post("/profile-normalizations", response_model=CVAnalysisAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_profile_normalization(
    request: ProfileNormalizationRequest,
    idempotency_key: str = Header(min_length=8, alias="Idempotency-Key"),
    _: None = Depends(require_service_token),
) -> CVAnalysisAccepted:
    return _submit(idempotency_key, request.source_hash, {
        "kind": "profile_normalization",
        "candidate_id": request.candidate_id,
        "source_hash": request.source_hash,
        "profile_snapshot": request.profile_snapshot,
    })


@router.get("/cv-analyses/{job_id}", response_model=CVAnalysisTaskStatus)
async def retrieve_cv_analysis(job_id: str, _: None = Depends(require_service_token)) -> CVAnalysisTaskStatus:
    try:
        job = ExtractionJobStore().get(job_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Extraction job store is unavailable.") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="Extraction job not found.")
    raw_status = job["status"]
    api_status = raw_status if raw_status in CVAnalysisStatus._value2member_map_ else "failed" if raw_status in {"failed", "delivery_failed"} else "processing"
    return CVAnalysisTaskStatus(job_id=job_id, status=CVAnalysisStatus(api_status), result=job.get("result"), error=job.get("error"), delivered=raw_status == "delivered")
