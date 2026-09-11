from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app.core.connection_manager import manager
from app.schemas.analisis import WebSocketMessage

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/{job_id}")
async def websocket_endpoint(websocket: WebSocket, job_id: str) -> None:
    job = manager.get_job(job_id)
    if job is None:
        await websocket.close(code=4404)
        return

    await manager.connect(job_id, websocket)
    try:
        snapshot = manager.get_job(job_id)
        if snapshot is not None:
            await websocket.send_json(
                WebSocketMessage(
                    tipo="snapshot",
                    job_id=job_id,
                    estado=snapshot.estado,
                    progreso=snapshot.progreso,
                    resultado=snapshot.resultado,
                    error=snapshot.error,
                ).model_dump(mode="json")
            )

        while True:
            mensaje = await websocket.receive_text()
            if mensaje.strip().lower() != "ping":
                continue
            actual = manager.get_job(job_id)
            if actual is None:
                await websocket.close(code=4404)
                return
            await websocket.send_json(
                WebSocketMessage(
                    tipo="snapshot",
                    job_id=job_id,
                    estado=actual.estado,
                    progreso=actual.progreso,
                    resultado=actual.resultado,
                    error=actual.error,
                ).model_dump(mode="json")
            )
    except WebSocketDisconnect:
        await manager.disconnect(job_id, websocket)
    except RuntimeError:
        await manager.disconnect(job_id, websocket)


@router.get("/ws-test", response_class=HTMLResponse, include_in_schema=False)
async def websocket_test() -> str:
    """Página suelta para probar el websocket desde el navegador."""
    return """<html><body><h3>WS /ws/{job_id}</h3>
    <input id="jid" placeholder="job_id" value=""><button onclick="conn()">Conectar</button>
    <pre id="log"></pre>
    <script>
    const log = document.getElementById('log');
    function conn(){
      const ws = new WebSocket(`ws://${location.host}/ws/${document.getElementById('jid').value}`);
      ws.onmessage = e => { log.textContent += e.data + '\\n'; };
    }
    </script></body></html>"""