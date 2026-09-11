import asyncio
from typing import Any

from fastapi import WebSocket
from pydantic import ValidationError

from app.schemas.analisis import EstadoTarea, JobSnapshot


class ConnectionManager:
    """Une el estado en memoria de cada job con sus clientes conectados por websocket.

    No es una persistence layer: es un demo/desarrollo store. Si el proceso
    se reinicia, los jobs desaparecen (la fuente de verdad es Django).
    """

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    # ---------- websockets ----------

    async def connect(self, job_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.setdefault(job_id, set()).add(websocket)
        print(f"[FLUJO] WS conectado: cliente del job {job_id}")

    async def disconnect(self, job_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            conexiones = self._connections.get(job_id)
            if conexiones:
                conexiones.discard(websocket)
                if not conexiones:
                    self._connections.pop(job_id, None)

    async def broadcast(self, job_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            conexiones = list(self._connections.get(job_id, set()))
        muertas: list[WebSocket] = []
        for websocket in conexiones:
            try:
                await websocket.send_json(payload)
            except (RuntimeError, ConnectionError):
                muertas.append(websocket)
        for websocket in muertas:
            await self.disconnect(job_id, websocket)

    # ---------- jobs (estado en memoria) ----------

    def crear_job(self, job_id: str, filename: str) -> JobSnapshot:
        job: dict[str, Any] = {
            "job_id": job_id,
            "filename": filename,
            "estado": EstadoTarea.pendiente.value,
            "progreso": 0,
            "resultado": None,
            "error": None,
        }
        self._jobs[job_id] = job
        print(f"[FLUJO] manager.crear_job: {job_id} creado en memoria (estado PENDIENTE)")
        return JobSnapshot.model_validate(job)

    def get_job(self, job_id: str) -> JobSnapshot | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        try:
            return JobSnapshot.model_validate(job)
        except ValidationError:
            return None

    def actualizar_job(self, job_id: str, **updates: Any) -> None:
        job = self._jobs.get(job_id)
        if job:
            job.update(updates)

    async def publicar(self, job_id: str, mensaje: dict[str, Any]) -> None:
        """Persiste los campos de estado y lo difunde a los clientes conectados."""
        job = self._jobs.get(job_id)
        if job:
            for campo in ("estado", "progreso", "resultado", "error"):
                if campo in mensaje:
                    job[campo] = mensaje[campo]
        print(
            f"[FLUJO] manager.publicar: {job_id} · {mensaje.get('tipo')} · "
            f"estado={mensaje.get('estado')} · {mensaje.get('progreso')}%"
        )
        await self.broadcast(job_id, mensaje)


manager = ConnectionManager()