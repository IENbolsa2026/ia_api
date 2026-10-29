"""Endpoints idempotentes para extracciones de CV y ofertas."""

from __future__ import annotations

import hashlib
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.security import require_service_token
from app.schemas.cv_analysis import CVAnalysisAccepted, CVAnalysisStatus, CVAnalysisTaskStatus
from app.services.job_store import ExtractionJobStore
from app.services.cv_artifacts import artifact_path, delete_artifact
from app.services.document_text import DocumentRejected, detect_format, enabled_formats, legacy_converter_available
from ien_contracts.utils import content_hash
from app.tasks.cv_analysis_tasks import run_extraction_task

# Alias transitorio para integraciones que importaban el nombre anterior.
analyze_cv_task = run_extraction_task


router = APIRouter()


class OfferAnalysisRequest(BaseModel):
    offer_id: int = Field(gt=0)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    offer_snapshot: dict


class ProfileNormalizationRequest(BaseModel):
    candidate_id: int = Field(gt=0)
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_snapshot: dict


def _validate_snapshot_source_hash(source_hash: str, snapshot: dict) -> None:
    if content_hash(snapshot) != source_hash:
        raise HTTPException(status_code=422, detail="El hash de origen no coincide con el contenido enviado.")


def _submit(key: str, payload_hash: str, payload: dict) -> CVAnalysisAccepted:
    job_id = str(uuid4())
    try:
        job, created = ExtractionJobStore().create_or_get(key, payload_hash, payload, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="La clave de solicitud ya se usó con otros datos.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="El servicio de tareas no está disponible.") from exc
    if created or job['status'] == 'accepted':
        queued_job_id = job["job_id"]
        try:
            celery_app.send_task(run_extraction_task.name, args=[queued_job_id], task_id=f"extract-{queued_job_id}")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="No se pudo encolar el análisis. Volvé a intentarlo con la misma clave de solicitud.") from exc
    raw_status = job["status"]
    if raw_status in {"delivering", "delivered", "delivery_failed"}:
        raw_status = "completed" if job.get("result") is not None else "failed"
    elif raw_status not in CVAnalysisStatus._value2member_map_:
        raw_status = "queued"
    return CVAnalysisAccepted(job_id=job["job_id"], status=CVAnalysisStatus(raw_status), request_id=key)


@router.post("/cv-analyses", response_model=CVAnalysisAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_cv_analysis(
    analysis_id: int = Form(gt=0),
    candidate_id: int = Form(gt=0),
    cv_version_id: int | None = Form(default=None, gt=0),
    user_id: int | None = Form(default=None, gt=0),
    cv: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _: None = Depends(require_service_token),
) -> CVAnalysisAccepted:
    try:
        cv_bytes = await cv.read(settings.CV_MAX_PDF_BYTES + 1)
        if len(cv_bytes) > settings.CV_MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail=f"El CV supera el máximo permitido de {settings.CV_MAX_PDF_BYTES // (1024 * 1024)} MiB.")
        if not cv_bytes:
            raise HTTPException(status_code=422, detail="El archivo está vacío.")
        try:
            document_format = detect_format(cv.filename or "", cv_bytes)
        except DocumentRejected as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        source_hash = hashlib.sha256(cv_bytes).hexdigest()
        key = idempotency_key or f"candidate-cv:{analysis_id}:{source_hash}"
        payload = {
            "kind": "candidate_cv", "analysis_id": analysis_id, "candidate_id": candidate_id,
            "user_id": user_id, "source_hash": source_hash,
        }
        if cv_version_id is not None:
            payload["cv_version_id"] = cv_version_id
        semantic_hash = content_hash(payload)
        return await run_in_threadpool(_persist_and_submit_cv, key, source_hash, semantic_hash, cv_bytes, document_format, payload)
    finally:
        await cv.close()


@router.get("/cv-analyses/capabilities")
async def cv_capabilities(_: None = Depends(require_service_token)) -> dict:
    """Formatos que este despliegue realmente puede extraer."""
    return {
        "max_file_bytes": settings.CV_MAX_PDF_BYTES,
        "max_pdf_pages": settings.CV_MAX_PDF_PAGES,
        "max_extracted_characters": settings.CV_MAX_TEXT_CHARACTERS,
        "enabled_formats": list(enabled_formats()),
        "disabled_formats": {} if legacy_converter_available() else {
            "doc": "Requiere Docker y una imagen de conversión aislada configurada.",
            "rtf": "Requiere Docker y una imagen de conversión aislada configurada.",
        },
    }


def _persist_and_submit_cv(key: str, source_hash: str, semantic_hash: str, cv_bytes: bytes,
                           document_format: str, payload: dict) -> CVAnalysisAccepted:
    artifact_id = hashlib.sha256(f"{key}:{source_hash}".encode("utf-8")).hexdigest()
    path = artifact_path(artifact_id, document_format)
    artifact_created = False
    try:
        try:
            with path.open("xb") as artifact:
                artifact.write(cv_bytes)
            artifact_created = True
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != source_hash:
                raise HTTPException(status_code=409, detail="Ya existe un trabajo para esa clave con otro archivo.")
        stored_payload = {**payload, "artifact_id": artifact_id, "format": document_format}
        response = _submit(key, semantic_hash, stored_payload)
        if artifact_created and response.status == CVAnalysisStatus.completed:
            delete_artifact(artifact_id, document_format)
        return response
    except Exception as exc:
        if artifact_created and (not isinstance(exc, HTTPException) or exc.status_code != 503):
            delete_artifact(artifact_id, document_format)
        raise


@router.post("/offer-analyses", response_model=CVAnalysisAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_offer_analysis(
    request: OfferAnalysisRequest,
    idempotency_key: str = Header(min_length=8, alias="Idempotency-Key"),
    _: None = Depends(require_service_token),
) -> CVAnalysisAccepted:
    _validate_snapshot_source_hash(request.source_hash, request.offer_snapshot)
    payload = {
        "kind": "job_offer",
        "offer_id": request.offer_id,
        "source_hash": request.source_hash,
        "offer_snapshot": request.offer_snapshot,
    }
    return _submit(idempotency_key, content_hash(payload), payload)


@router.post("/profile-normalizations", response_model=CVAnalysisAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_profile_normalization(
    request: ProfileNormalizationRequest,
    idempotency_key: str = Header(min_length=8, alias="Idempotency-Key"),
    _: None = Depends(require_service_token),
) -> CVAnalysisAccepted:
    _validate_snapshot_source_hash(request.source_hash, request.profile_snapshot)
    payload = {
        "kind": "profile_normalization",
        "candidate_id": request.candidate_id,
        "source_hash": request.source_hash,
        "profile_snapshot": request.profile_snapshot,
    }
    return _submit(idempotency_key, content_hash(payload), payload)


@router.get("/cv-analyses/{job_id}", response_model=CVAnalysisTaskStatus)
async def retrieve_cv_analysis(job_id: str, _: None = Depends(require_service_token)) -> CVAnalysisTaskStatus:
    try:
        job = ExtractionJobStore().get(job_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="El servicio de tareas no está disponible.") from exc
    if job is None:
        raise HTTPException(status_code=404, detail="No se encontró el análisis solicitado.")
    raw_status = job["status"]
    if raw_status in {"delivering", "delivered", "delivery_failed"}:
        api_status = "completed" if job.get("result") is not None else "failed"
    else:
        api_status = raw_status if raw_status in CVAnalysisStatus._value2member_map_ else "processing"
    return CVAnalysisTaskStatus(job_id=job_id, status=CVAnalysisStatus(api_status), result=job.get("result"), error=job.get("error"), delivered=raw_status == "delivered")
