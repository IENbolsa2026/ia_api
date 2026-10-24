from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from ien_contracts.models import MatchRequest

from app.core.celery_app import celery_app
from app.core.security import bearer_token, is_valid_service_token, require_service_token
from app.services.match_job_store import MatchJobStore
from app.tasks.match_tasks import evaluate_match_task


router = APIRouter()
MATCH_WEBSOCKET_HEARTBEAT_SECONDS = 15


def start(request: MatchRequest) -> dict:
    store = MatchJobStore()
    try:
        job, created = store.create_or_get(request.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail="El identificador de solicitud ya se usó con otros datos.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="El servicio de compatibilidad no está disponible.") from exc
    if created or job['status'] == 'accepted':
        try:
            celery_app.send_task(evaluate_match_task.name, args=[str(request.request_id)], task_id=f"match-{request.request_id}")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="No se pudo encolar la compatibilidad. Volvé a intentarlo con la misma solicitud.") from exc
    return {"request_id": str(request.request_id), "status": job["status"], "created": created}


@router.post('/matches', status_code=202)
def create_match(request: MatchRequest, _: None = Depends(require_service_token)) -> dict:
    """Encola una comparación de perfiles en el mismo worker que analiza CVs."""
    return start(request)


@router.get("/matches/{request_id}")
async def get_match(request_id: str, _: None = Depends(require_service_token)) -> dict:
    job = MatchJobStore().get(request_id)
    if not job:
        raise HTTPException(status_code=404, detail="No se encontró la solicitud de compatibilidad.")
    return {
        "request_id": job.get("request_id", request_id),
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


@router.websocket("/ws/v1/matches")
async def match_socket(websocket: WebSocket) -> None:
    if not is_valid_service_token(bearer_token(websocket.headers.get("authorization"))):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    request_id: str | None = None
    last_status: str | None = None
    last_message_at = asyncio.get_running_loop().time()
    try:
        while True:
            if request_id:
                job = MatchJobStore().get(request_id)
                if job and job["status"] != last_status:
                    last_status = job["status"]
                    message = {"type": f"match.{job['status']}", "request_id": request_id, "status": job["status"]}
                    if job.get("result"):
                        message["result"] = job["result"]
                    if job.get("error"):
                        message["error"] = job["error"]
                    await websocket.send_json(message)
                    last_message_at = asyncio.get_running_loop().time()
                    if job["status"] in {"completed", "failed"}:
                        request_id = None
                elif job and asyncio.get_running_loop().time() - last_message_at >= MATCH_WEBSOCKET_HEARTBEAT_SECONDS:
                    # El dispatcher mantiene una conexión por trabajo; sin
                    # actividad, su recv vence a los 60 s y posterga la entrega.
                    await websocket.send_json({"type": "match.heartbeat", "request_id": request_id})
                    last_message_at = asyncio.get_running_loop().time()
            try:
                message = await asyncio.wait_for(websocket.receive_json(), timeout=0.5)
            except TimeoutError:
                continue
            event_type = message.get("type")
            if event_type == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if event_type == "match.resume":
                request_id = str(message.get("request_id", ""))
                last_status = None
                if not MatchJobStore().get(request_id):
                    await websocket.send_json({"type": "match.failed", "error": "No se encontró la solicitud de compatibilidad."})
                    request_id = None
                continue
            if event_type != "match.start":
                await websocket.send_json({"type": "match.failed", "error": "El tipo de evento no está admitido."})
                continue
            try:
                request = MatchRequest.model_validate(message.get("payload"))
            except Exception:
                await websocket.send_json({"type": "match.failed", "error": "La solicitud de compatibilidad no es válida."})
                continue
            started = start(request)
            request_id = started["request_id"]
            last_status = None
            await websocket.send_json({"type": "match.accepted", **started})
    except WebSocketDisconnect:
        return
