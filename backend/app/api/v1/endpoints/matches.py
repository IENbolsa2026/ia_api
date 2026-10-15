from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from ien_contracts.models import MatchRequest

from app.core.celery_app import celery_app
from app.core.security import bearer_token, is_valid_service_token, require_service_token
from app.services.match_job_store import MatchJobStore
from app.tasks.match_tasks import evaluate_match_task


router = APIRouter()


def start(request: MatchRequest) -> dict:
    store = MatchJobStore()
    job, created = store.create_or_get(request.model_dump(mode="json"))
    if created or job['status'] == 'accepted':
        celery_app.send_task(evaluate_match_task.name, args=[str(request.request_id)], task_id=f"match-{request.request_id}")
    return {"request_id": str(request.request_id), "status": job["status"], "created": created}


@router.post('/matches', status_code=202)
def create_match(request: MatchRequest, _: None = Depends(require_service_token)) -> dict:
    """Encola una comparación de perfiles en el mismo worker que analiza CVs."""
    return start(request)


@router.get("/matches/{request_id}")
async def get_match(request_id: str, _: None = Depends(require_service_token)) -> dict:
    job = MatchJobStore().get(request_id)
    if not job:
        raise HTTPException(status_code=404, detail="Match request not found.")
    return {key: value for key, value in job.items() if key not in {"request_hash"}}


@router.websocket("/ws/v1/matches")
async def match_socket(websocket: WebSocket) -> None:
    if not is_valid_service_token(bearer_token(websocket.headers.get("authorization"))):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    request_id: str | None = None
    last_status: str | None = None
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
                    if job["status"] in {"completed", "failed"}:
                        request_id = None
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
                if not MatchJobStore().get(request_id):
                    await websocket.send_json({"type": "match.failed", "error": "Match request not found."})
                    request_id = None
                continue
            if event_type != "match.start":
                await websocket.send_json({"type": "match.failed", "error": "Unsupported event type."})
                continue
            request = MatchRequest.model_validate(message.get("payload"))
            started = start(request)
            request_id = started["request_id"]
            last_status = None
            await websocket.send_json({"type": "match.accepted", **started})
    except WebSocketDisconnect:
        return
