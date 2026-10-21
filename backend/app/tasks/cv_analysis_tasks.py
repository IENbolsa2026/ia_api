"""Tareas separadas para inferencia y entrega de resultados."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Any
from uuid import uuid4
from celery.exceptions import Retry

from app.core.celery_app import celery_app
from app.core.config import settings
from app.services.cv_pipeline import CV_EXTRACTION_PROMPT_VERSION, normalize_candidate_profile, process_cv_text, process_offer_snapshot
from app.services.cv_packets import PacketsIdentityError, count_completed_packets, make_packet_identity
from app.services.job_store import ExtractionJobStore, LockOwnershipLost
from app.services.document_text import DocumentRejected
from app.services.cv_artifacts import artifact_path, delete_artifact
from app.services.document_extractor_runner import extract_in_subprocess
from app.services.rate_limiter import ProviderOutputLimitExceeded, ProviderResponseTruncated, RateLimitExceeded
from app.services.result_notifier import notify_extraction_result
from app.services.channel_events import publish_cv_analysis_complete


logger = logging.getLogger(__name__)


def _callback_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = job["payload"]
    result = {
        "event_id": f"extraction-{job['job_id']}",
        "job_id": job["job_id"],
        "request_id": job["idempotency_key"],
        "analysis_id": payload.get("analysis_id"),
        "candidate_id": payload.get("candidate_id"),
        "offer_id": payload.get("offer_id"),
        "kind": payload["kind"],
        "source_hash": payload["source_hash"],
        "status": "completed" if job.get("result") is not None else "failed",
        "result": job.get("result"),
        "error": job.get("error"),
        "provider_model": job.get("provider_model"),
    }
    if "cv_version_id" in payload:
        result["cv_version_id"] = payload["cv_version_id"]
    return result


@celery_app.task(bind=True, max_retries=5, name="app.tasks.run_extraction")
def run_extraction_task(self: Any, job_id: str) -> dict[str, Any]:
    store = ExtractionJobStore()
    job = store.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "missing"}
    if job["status"] in {"completed", "failed", "delivery_failed"}:
        # Reintenta publicar la entrega si el worker terminó tras guardar el
        # resultado pero antes de que Celery aceptara la tarea de callback.
        try:
            deliver_extraction_result_task.delay(job_id)
        except Exception as exc:
            raise self.retry(exc=exc, countdown=min(60, 2 ** self.request.retries)) from exc
        return {"job_id": job_id, "status": job["status"]}
    if job["status"] in {"delivering", "delivered"}:
        return {"job_id": job_id, "status": job["status"]}
    if not store.acquire_lock(job_id):
        # La tarea pudo ser reentregada mientras otra ejecución aún conserva
        # el lease, o después de que un worker muriera. Reintentar permite
        # recuperar el trabajo cuando venza el lock sin confirmar la tarea.
        countdown = min(600, 30 * (2 ** max(0, int(self.request.retries))))
        raise self.retry(
            exc=RuntimeError("El trabajo sigue bloqueado por otra ejecución."),
            countdown=countdown,
        )
    try:
        # Refresh after acquiring the lease. Another worker may have completed
        # and released it after our first read, while this task waited in queue.
        job = store.get(job_id)
        if not job:
            return {"job_id": job_id, "status": "missing"}
        if job["status"] in {"completed", "failed", "delivery_failed"}:
            try:
                deliver_extraction_result_task.delay(job_id)
            except Exception as exc:
                raise self.retry(exc=exc, countdown=min(60, 2 ** self.request.retries)) from exc
            return {"job_id": job_id, "status": job["status"]}
        if job["status"] in {"delivering", "delivered"}:
            return {"job_id": job_id, "status": job["status"]}
        payload = job["payload"]
        packet_checkpoint = job.get("extraction_packets") if payload["kind"] == "candidate_cv" else None
        completed_packets_at_start = count_completed_packets(packet_checkpoint)
        rate_limit_stalls = int(job.get("rate_limit_stalls", 0))
        provider_failures = int(job.get("provider_failures", 0))
        store.update_owned(job_id, status="processing", error=None)
        if payload["kind"] == "candidate_cv":
            if payload.get("artifact_id"):
                artifact_id = payload["artifact_id"]
                document_format = payload.get("format", "")
                artifact = artifact_path(artifact_id, document_format)
                try:
                    data = artifact.read_bytes()
                except OSError as exc:
                    raise DocumentRejected("El archivo temporal del CV ya no está disponible. Volvé a cargarlo.") from exc
                if hashlib.sha256(data).hexdigest() != payload.get("source_hash"):
                    raise DocumentRejected("El archivo temporal del CV no coincide con la carga original.")
            else:
                # Compatibilidad temporal con trabajos PDF encolados antes del cambio.
                document_format = "pdf"
                artifact_id = uuid4().hex
                artifact = artifact_path(artifact_id, document_format)
                artifact.write_bytes(base64.b64decode(payload["cv_pdf_base64"], validate=True))
            try:
                text = extract_in_subprocess(artifact, document_format)
            finally:
                if not payload.get("artifact_id"):
                    artifact.unlink(missing_ok=True)
            def extraction_heartbeat() -> None:
                if not store.renew_lock(job_id):
                    raise LockOwnershipLost("Se perdió el lease durante la extracción del CV.")

            provider = settings.LLM_PROVIDER.strip().lower()
            configured_model = settings.OPENAI_MODEL if provider == "openai" else settings.CV_MODEL
            packet_identity = make_packet_identity(
                job_id=job_id,
                analysis_id=int(payload["analysis_id"]),
                candidate_id=int(payload["candidate_id"]),
                cv_version_id=payload.get("cv_version_id"),
                source_hash=payload["source_hash"],
                text=text,
                prompt_version=CV_EXTRACTION_PROMPT_VERSION,
                provider=provider,
                model=configured_model,
                packet_chars=settings.CV_PACKET_CHARACTERS,
                block_chars=settings.CV_BLOQUE_CARACTERES,
                budget=settings.CV_EXTRACTION_MAX_TOKENS,
            )

            def save_packet_checkpoint(value: dict[str, Any]) -> None:
                store.update_owned(job_id, extraction_packets=value)

            result = asyncio.run(process_cv_text(
                text,
                int(payload["candidate_id"]),
                identity=packet_identity,
                checkpoint=packet_checkpoint,
                on_checkpoint=save_packet_checkpoint,
                on_heartbeat=extraction_heartbeat,
            ))
        elif payload["kind"] == "job_offer":
            result = asyncio.run(process_offer_snapshot(payload["offer_snapshot"], int(payload["offer_id"])))
        elif payload["kind"] == "profile_normalization":
            result = asyncio.run(normalize_candidate_profile(payload["profile_snapshot"], int(payload["candidate_id"])))
        else:
            raise ValueError("Unsupported extraction kind.")
        provider_model = getattr(result, "provider_model", None) or (
            settings.OPENAI_MODEL if settings.LLM_PROVIDER.strip().lower() == "openai" else settings.CV_MODEL
        )
        store.update_owned(job_id, status="completed", result=result, error=None, provider_model=provider_model)
        if payload.get("artifact_id"):
            delete_artifact(payload["artifact_id"], payload.get("format", ""))
        try:
            deliver_extraction_result_task.delay(job_id)
        except Exception as exc:
            raise self.retry(exc=exc, countdown=min(60, 2 ** self.request.retries)) from exc
        return {"job_id": job_id, "status": "completed"}
    except Retry:
        raise
    except LockOwnershipLost as exc:
        # No se publica un resultado de un worker que ya perdió el lease.
        raise self.retry(
            exc=exc,
            countdown=30,
            max_retries=max(10, self.request.retries + 1),
        ) from exc
    except RateLimitExceeded as exc:
        if exc.is_local_wait:
            try:
                store.update_owned(job_id, status="rate_limited", error=None)
            except LockOwnershipLost as lock_exc:
                raise self.retry(exc=lock_exc, countdown=30, max_retries=max(1000, self.request.retries + 1)) from lock_exc
            raise self.retry(
                exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1),
                max_retries=max(1000, self.request.retries + 1),
            ) from exc
        is_packet_cv = bool(payload and payload.get("kind") == "candidate_cv")
        latest = store.get(job_id) if is_packet_cv else None
        completed_now = count_completed_packets(latest.get("extraction_packets") if latest else None)
        if is_packet_cv:
            rate_limit_stalls = 0 if completed_now > completed_packets_at_start else rate_limit_stalls + 1
        exhausted_no_progress = is_packet_cv and rate_limit_stalls >= 5
        if not is_packet_cv:
            provider_failures += 1
        exhausted_legacy = not is_packet_cv and provider_failures >= self.max_retries
        if exhausted_no_progress or exhausted_legacy:
            error = "Se agotó el límite de solicitudes del proveedor. Intentá analizar el CV más tarde."
            try:
                store.update_owned(job_id, status="failed", error=error, rate_limit_stalls=rate_limit_stalls, provider_failures=provider_failures)
            except LockOwnershipLost as lock_exc:
                raise self.retry(exc=lock_exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from lock_exc
            if payload.get("artifact_id"):
                delete_artifact(payload["artifact_id"], payload.get("format", ""))
            try:
                deliver_extraction_result_task.delay(job_id)
            except Exception as delivery_exc:
                raise self.retry(exc=delivery_exc, countdown=min(60, int(exc.retry_after_seconds) + 1)) from delivery_exc
            return {"job_id": job_id, "status": "failed"}
        try:
            store.update_owned(job_id, status="rate_limited", error=None, rate_limit_stalls=rate_limit_stalls, provider_failures=provider_failures)
        except LockOwnershipLost as lock_exc:
            raise self.retry(exc=lock_exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from lock_exc
        if is_packet_cv:
            raise self.retry(
                exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1),
                max_retries=max(1000, self.request.retries + 1),
            ) from exc
        raise self.retry(exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1), max_retries=max(1000, self.request.retries + 1)) from exc
    except Exception as exc:
        latest_state = store.get(job_id)
        if latest_state and latest_state.get("status") in {"completed", "failed", "delivering", "delivered", "delivery_failed"}:
            # A callback enqueue failure must not overwrite a result already
            # committed as terminal, even when Celery retries are exhausted.
            logger.error("Could not queue delivery for terminal extraction job %s (%s)", job_id, type(exc).__name__)
            raise
        logger.error("Extraction failed for job %s (%s)", job_id, type(exc).__name__)
        permanent_cv_truncation = (
            payload.get("kind") == "candidate_cv" and isinstance(exc, ProviderResponseTruncated)
        )
        if isinstance(exc, (DocumentRejected, ProviderOutputLimitExceeded, PacketsIdentityError)) or permanent_cv_truncation:
            public_error = (
                "La respuesta del análisis del CV quedó incompleta por el límite de salida. "
                "Volvé a intentarlo más tarde."
                if permanent_cv_truncation
                else str(exc)
            )
            try:
                store.update_owned(job_id, status="failed", error=public_error)
            except LockOwnershipLost as lock_exc:
                raise self.retry(exc=lock_exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from lock_exc
            if payload.get("artifact_id"):
                delete_artifact(payload["artifact_id"], payload.get("format", ""))
            try:
                deliver_extraction_result_task.delay(job_id)
            except Exception as delivery_exc:
                raise self.retry(exc=delivery_exc, countdown=min(60, 2 ** self.request.retries)) from delivery_exc
            return {"job_id": job_id, "status": "failed"}
        provider_failures += 1
        if provider_failures < self.max_retries:
            store.update_owned(job_id, provider_failures=provider_failures)
            raise self.retry(exc=exc, countdown=min(60, 2 ** provider_failures), max_retries=max(1000, self.request.retries + 1)) from exc
        try:
            store.update_owned(job_id, status="failed", provider_failures=provider_failures, error="No se pudo procesar el archivo. Revisá el CV y volvé a intentarlo.")
        except LockOwnershipLost as lock_exc:
            raise self.retry(exc=lock_exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from lock_exc
        if payload.get("artifact_id"):
            delete_artifact(payload["artifact_id"], payload.get("format", ""))
        deliver_extraction_result_task.delay(job_id)
        raise
    finally:
        store.release_lock(job_id)


@celery_app.task(bind=True, max_retries=8, name="app.tasks.deliver_extraction_result")
def deliver_extraction_result_task(self: Any, job_id: str) -> dict[str, Any]:
    store = ExtractionJobStore()
    job = store.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "missing"}
    if job["status"] == "delivered":
        return {"job_id": job_id, "status": "delivered"}
    if job["status"] not in {"completed", "failed", "delivering", "delivery_failed"}:
        # A stale or premature delivery must never consume an active extraction.
        return {"job_id": job_id, "status": job["status"]}
    try:
        store.update(job_id, status="delivering")
        notify_extraction_result(_callback_payload(job))
        payload = job['payload']
        if payload.get('user_id') is not None and payload['kind'] == 'candidate_cv':
            publish_cv_analysis_complete(
                payload['user_id'], payload['analysis_id'],
                'completado' if job.get('result') is not None else 'error',
            )
        store.update(job_id, status="delivered", delivery_error=None)
        return {"job_id": job_id, "status": "delivered"}
    except Exception as exc:
        logger.error("Could not deliver extraction result for job %s (%s)", job_id, type(exc).__name__)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=min(300, 2 ** self.request.retries)) from exc
        store.update(job_id, status="delivery_failed", delivery_error="No se pudo entregar el resultado a Bolsa.")
        raise
